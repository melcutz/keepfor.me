# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for MCP server functionality."""

import asyncio
import json
import time

import pytest

from keepfor.auth.service import register_user
from keepfor.mcp.server import MCP_TOOLS, MCP_TOOLS_BY_NAME, process_mcp_request


class FakeQueue:
    """In-test queue: records sends without delivery."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, message: dict) -> None:
        self.sent.append(message)


class MockEnv:
    """Mock Cloudflare environment for testing."""

    def __init__(self):
        self.DB = None
        self.QUEUE = FakeQueue()
        self.AI = None
        self.VECTORIZE = None
        self.BUCKET = None


@pytest.fixture
async def mcp_user(db):
    """Create user for MCP testing."""
    user = await register_user(db, "mcpuser@example.com", "password123")
    return user, MockEnv(), db


@pytest.mark.asyncio
async def test_initialize(mcp_user):
    """Test MCP server initialization."""
    user, env, db = mcp_user
    req = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    res = await process_mcp_request(req, db, env, user)
    assert res["id"] == 1
    assert res["result"]["serverInfo"]["name"] == "keepfor-me-mcp"


@pytest.mark.asyncio
async def test_tools_list(mcp_user):
    """Test MCP tools listing."""
    user, env, db = mcp_user
    req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    res = await process_mcp_request(req, db, env, user)
    tools = res["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert "save_url" in tool_names
    assert "search_library" in tool_names
    assert "get_item" in tool_names
    assert "list_items" in tool_names
    assert "tag_item" in tool_names
    assert "delete_item" in tool_names


@pytest.mark.asyncio
async def test_save_and_get_tool(mcp_user):
    """Test MCP save_url and get_item tools."""
    user, env, db = mcp_user
    # 1. save_url tool
    save_req = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "save_url",
            "arguments": {"url": "https://python.org", "tags": ["python", "code"]},
        },
    }
    res = await process_mcp_request(save_req, db, env, user)
    save_data = json.loads(res["result"]["content"][0]["text"])
    item_id = save_data["id"]
    assert save_data["is_new"]

    # 2. get_item tool
    get_req = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "get_item", "arguments": {"item_id": item_id}},
    }
    res_get = await process_mcp_request(get_req, db, env, user)
    get_data = json.loads(res_get["result"]["content"][0]["text"])
    assert get_data["id"] == item_id
    assert "python" in get_data["tags"]


# ==========================================
# Search latency / resilience
# ==========================================


class _SlowAI:
    """Stands in for Workers AI embedding latency."""

    def __init__(self, delay=0.30):
        self.delay = delay

    async def run(self, model, payload):
        await asyncio.sleep(self.delay)
        return {"data": [[0.1] * 768]}


class _HangingAI:
    async def run(self, model, payload):
        await asyncio.sleep(3600)


class _FakeVectorize:
    async def query(self, vector, opts):
        return {"matches": [{"metadata": {"item_id": "i1"}, "score": 0.9}]}


class _SearchEnv:
    def __init__(self, ai):
        self.AI = ai
        self.VECTORIZE = _FakeVectorize()


class _StubDB:
    """DB stub whose row queries take a measurable amount of time."""

    def __init__(self, delay=0.0):
        self.delay = delay

    async def query_all(self, sql, params=()):
        await asyncio.sleep(self.delay)
        if "FROM item_tags" in sql:
            return []
        return [
            {
                "id": "i1",
                "url": "https://example.com/a",
                "canonical_url": "https://example.com/a",
                "title": "A",
                "byline": None,
                "site_name": "example.com",
                "published_date": None,
                "excerpt": None,
                "status": "ok",
                "fail_reason": None,
                "is_fallback": 0,
                "word_count": 10,
                "created_at": "2026-01-01 00:00:00",
            }
        ]


@pytest.mark.asyncio
async def test_hybrid_search_runs_fts_and_vector_concurrently(monkeypatch):
    """Hybrid search overlaps the FTS query with the AI embedding call.

    Sequential awaits made hybrid ~0.6s (FTS round-trip + ~0.39s embedding
    paid back to back). Concurrently, total time tracks the slower branch.
    """
    from keepfor.search import engine

    async def slow_fts(*args, **kwargs):
        await asyncio.sleep(0.20)
        return [{"item_id": "i1", "rank": 0.1, "snippet": "x"}]

    monkeypatch.setattr(engine, "search_fts", slow_fts)

    start = time.perf_counter()
    results, _ = await engine.hybrid_search(
        _StubDB(), _SearchEnv(_SlowAI(0.30)), "u1", "query", mode="hybrid"
    )
    elapsed = time.perf_counter() - start

    assert len(results) == 1
    # Concurrent ~= max(0.20, 0.30); sequential would be ~=0.50.
    assert elapsed < 0.45, f"hybrid search took {elapsed:.2f}s (not concurrent?)"


@pytest.mark.asyncio
async def test_vector_search_timeout_degrades_to_keyword(monkeypatch):
    """A hanging AI binding must not hang search; it falls back to FTS."""
    from keepfor.search import engine

    async def fast_fts(*args, **kwargs):
        return [{"item_id": "i1", "rank": 0.1, "snippet": "x"}]

    monkeypatch.setattr(engine, "search_fts", fast_fts)
    monkeypatch.setattr(engine, "VECTOR_SEARCH_TIMEOUT", 0.05)

    results, _ = await engine.hybrid_search(
        _StubDB(), _SearchEnv(_HangingAI()), "u1", "query", mode="hybrid"
    )
    assert len(results) == 1
    assert results[0]["id"] == "i1"


# ==========================================
# Streamable HTTP transport (endpoint level)
# ==========================================


@pytest.fixture
def mcp_client(db, monkeypatch):
    """TestClient with DB/env pinned, mirroring test_endpoints.client."""
    from fastapi.testclient import TestClient

    from keepfor import app as app_module

    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: MockEnv())

    return TestClient(app_module.app)


@pytest.fixture
async def pat_headers(db):
    """Bearer headers for a fresh PAT."""
    from keepfor.auth.service import create_pat, register_user

    user = await register_user(db, "mcphttp@example.com", "password123")
    pat = await create_pat(db, user["id"], "Test MCP PAT")
    return {"Authorization": f"Bearer {pat['token']}"}


async def test_mcp_initialize_negotiates_version(mcp_client, pat_headers):
    """Server echoes a supported client protocol version + header."""
    res = mcp_client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        headers=pat_headers,
    )
    assert res.status_code == 200
    assert res.json()["result"]["protocolVersion"] == "2025-06-18"
    assert res.headers["mcp-protocol-version"] == "2025-06-18"


async def test_mcp_initialize_defaults_to_latest(mcp_client, pat_headers):
    """Missing version is answered with the latest, not rejected."""
    res = mcp_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers=pat_headers,
    )
    assert res.status_code == 200
    assert res.json()["result"]["protocolVersion"] == "2025-11-25"


async def test_mcp_initialize_rejects_unknown_version(mcp_client, pat_headers):
    """Explicitly unknown versions fail with 400, not a 200 error payload."""
    res = mcp_client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "1999-01-01"},
        },
        headers=pat_headers,
    )
    assert res.status_code == 400


async def test_mcp_notification_returns_202_empty(mcp_client, pat_headers):
    """notifications/initialized (no id) is accepted with 202 + empty body."""
    res = mcp_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=pat_headers,
    )
    assert res.status_code == 202
    assert res.content == b""


async def test_mcp_tools_list_over_http(mcp_client, pat_headers):
    """tools/list works over plain-JSON POST (what the old proxy sent)."""
    res = mcp_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers=pat_headers,
    )
    assert res.status_code == 200
    names = [t["name"] for t in res.json()["result"]["tools"]]
    assert len(names) == 41
    assert "save_url" in names
    assert "save_note" in names
    assert "pin_item" in names
    # New v2 tools
    assert "search_passages" in names
    assert "read_item" in names
    assert "reading_history" in names
    assert "related_items" in names
    # New v3 tools: direct lookup + tag management
    assert "find_item" in names
    assert "list_tags" in names
    assert "create_tag" in names
    assert "delete_tag" in names
    assert "tag_cleanup" in names
    # Bulk updates
    assert "bulk_update" in names
    # Library recap
    assert "library_digest" in names
    # New highlights and collections tools
    assert "create_highlight" in names
    assert "list_highlights" in names
    assert "delete_highlight" in names
    assert "create_collection" in names
    assert "list_collections" in names
    assert "add_to_collection" in names
    assert "remove_from_collection" in names
    assert "rename_collection" in names
    assert "delete_collection" in names
    # Every tool declares annotations
    for tool in res.json()["result"]["tools"]:
        assert set(tool["annotations"]) == {
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
        }
        assert tool["description"]


async def test_mcp_tools_call_save_url_over_http(mcp_client, pat_headers):
    """Full save_url roundtrip over HTTP with a PAT."""
    res = mcp_client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "save_url",
                "arguments": {"url": "https://example.com/mcp-http"},
            },
        },
        headers=pat_headers,
    )
    assert res.status_code == 200
    data = json.loads(res.json()["result"]["content"][0]["text"])
    assert data["url"] == "https://example.com/mcp-http"


async def test_mcp_get_returns_405(mcp_client, pat_headers):
    """Stateless server: no SSE stream, GET is 405."""
    res = mcp_client.get("/api/mcp", headers=pat_headers)
    assert res.status_code == 405
    assert "POST" in res.headers["allow"]


async def test_mcp_delete_returns_405(mcp_client, pat_headers):
    """Stateless server: no sessions, DELETE is 405."""
    res = mcp_client.delete("/api/mcp", headers=pat_headers)
    assert res.status_code == 405


async def test_mcp_requires_auth(mcp_client):
    """No PAT and no session cookie: 401 on POST, GET, and DELETE."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert mcp_client.post("/api/mcp", json=body).status_code == 401
    assert mcp_client.get("/api/mcp").status_code == 401
    assert mcp_client.delete("/api/mcp").status_code == 401


async def test_mcp_rejects_foreign_origin(mcp_client, pat_headers):
    """DNS-rebinding guard: forged Origin is 403 even with a valid PAT."""
    res = mcp_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={**pat_headers, "Origin": "https://evil.test"},
    )
    assert res.status_code == 403


def test_heavy_extraction_libs_are_not_imported_at_module_load():
    """bs4/lxml/trafilatura must stay out of the request import path.

    They are only needed by the extraction consumer, but keepfor.worker imports
    that chain at module scope -- eager imports made every request (even
    /auth/login) pay to load the article-parsing stack.
    """
    import subprocess
    import sys

    code = (
        "import sys, keepfor.worker;"
        "print(','.join(m for m in ('trafilatura','bs4','lxml') if m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "", (
        f"heavy modules loaded at import: {out.stdout.strip()}"
    )


# ==========================================
# Scope independence (keepfor-cloud#61)
# ==========================================

# Minimal valid arguments per tool. "ITEM" is replaced by a real item id.
TOOL_ARGUMENTS = {
    "save_url": {"url": "https://example.com/probe-item"},
    "save_note": {"title": "note", "content": "body"},
    "search_library": {"query": "alpha"},
    "search_passages": {"query": "alpha"},
    "read_item": {"item_id": "ITEM"},
    "get_item": {"item_id": "ITEM"},
    "find_item": {"title": "Alpha"},
    "list_items": {},
    "reading_history": {},
    "related_items": {"item_id": "ITEM"},
    "list_tags": {},
    "create_tag": {"name": "probe-tag"},
    "describe_tag": {"name": "work"},
    "update_tag": {"name": "probe-tag", "description": "d"},
    "rename_tag": {"old": "probe-tag", "new": "probe-tag2"},
    "merge_tags": {"sources": ["probe-tag2"], "target": "probe-tag3"},
    "delete_tag": {"name": "probe-tag3"},
    "tag_item": {"item_id": "ITEM", "add_tags": ["t"]},
    "tag_items": {"item_ids": ["ITEM"], "add": ["t"]},
    "retag_by_query": {"query": "alpha", "add": ["t"]},
    "bulk_update": {"tags_all": ["work"], "add_tags": ["probe-tag"]},
    "set_item_tags": {"item_id": "ITEM", "tags": ["t"]},
    "suggest_tags": {"item_id": "ITEM"},
    "search_tags": {"query": "a"},
    "tag_stats": {},
    "related_tags": {"tag": "work"},
    "tag_cleanup": {},
    "find_cleanup": {},
    "delete_item": {"item_id": "ITEM"},
    "pin_item": {"item_id": "ITEM"},
    "library_digest": {},
    "create_highlight": {"item_id": "ITEM", "text": "quoted", "end_offset": 6},
    "list_highlights": {"item_id": "ITEM"},
    "delete_highlight": {"highlight_id": "ITEM"},
    "update_highlight": {"highlight_id": "ITEM", "note": "new note"},
    "create_collection": {"name": "probe-collection"},
    "list_collections": {},
    "add_to_collection": {"collection": "probe-collection", "item_ids": ["ITEM"]},
    "remove_from_collection": {"collection": "probe-collection", "item_ids": ["ITEM"]},
    "rename_collection": {
        "collection": "probe-collection",
        "new_name": "probe-collection-2",
    },
    "delete_collection": {"collection": "probe-collection-2"},
}


async def _library_for_scopes(db, email):
    """A user for the scope-agnostic tool sweep."""
    user = await register_user(db, email, "password123")
    return user, MockEnv()


async def _make_item(db, env, user_id):
    """Creates one owned item and returns its id."""
    from keepfor.models.items import save_item

    item, _ = await save_item(
        db, env, user_id, "https://example.com/alpha", tags=["work"]
    )
    await db.execute(
        "UPDATE items SET title = ?, content_text = ?, excerpt = ?, status = 'ok'"
        " WHERE id = ?;",
        ("Alpha Guide", "Alpha content " * 100, "Alpha excerpt", item["id"]),
    )
    return item["id"]


async def _call_tool(db, env, user, name, item_id):
    """Calls one tool and returns (outcome, text)."""
    args = {
        key: (item_id if value == "ITEM" else value)
        for key, value in TOOL_ARGUMENTS[name].items()
    }
    res = await process_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
        db,
        env,
        user,
    )
    if "error" in res:
        kind = res["error"].get("data", {}).get("type", "")
        return kind, json.dumps(res["error"])
    return "ok", res["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_every_tool_works_without_scope_information(db):
    """No tool fails closed on missing scope information.

    Core does not enforce scopes: the MCP annotations are advisory and a
    token is never rejected for missing scope info. Every tool must behave
    the same for scopes={"*"} and for a user dict with no scopes attribute.
    """
    from keepfor.mcp.server import MCP_TOOLS

    user, env = await _library_for_scopes(db, "scopes@example.com")
    outcomes: dict[str, dict[str, tuple[str, str]]] = {}
    callers = {
        "wildcard": {**user, "scopes": {"*"}},
        "missing": {k: v for k, v in user.items() if k != "scopes"},
        "null": {**user, "scopes": None},
    }
    for label, caller in callers.items():
        # A fresh item per variant: some tools delete what they are given.
        item_id = await _make_item(db, env, user["id"])

        for tool in MCP_TOOLS:
            name = tool["name"]
            assert name in TOOL_ARGUMENTS, f"no arguments defined for {name}"
            outcome, text = await _call_tool(db, env, caller, name, item_id)
            outcomes.setdefault(name, {})[label] = (outcome, text)

    for name, variants in outcomes.items():
        for label, variant in variants.items():
            assert variant[0] == variants["wildcard"][0], (
                f"{name} behaves differently with {label} scope info: "
                f"{variants['wildcard'][0]} vs {variant[0]}"
            )
        for label, (_, text) in variants.items():
            lowered = text.lower()
            assert "scope" not in lowered, f"{name} ({label}) mentions scope: {text}"
            assert "forbidden" not in lowered, f"{name} ({label}) is forbidden"

    # The sweep must actually exercise the tools, not just fail everywhere.
    # (pin_item legitimately fails: delete_item already removed the item.)
    succeeded = sum(1 for v in outcomes.values() if v["wildcard"][0] == "ok")
    assert succeeded >= 25, f"only {succeeded} tools succeeded: {outcomes}"


# ==========================================
# bulk_update (GitHub issue #20)
# ==========================================


@pytest.fixture
async def bulk_user(db):
    """A user with three items: two tagged 'tech', one tagged 'science'."""
    from keepfor.models.items import save_item

    user = await register_user(db, "bulk@example.com", "password123")
    env = MockEnv()
    ids: list[str] = []
    for url, tags in (
        ("https://example.com/tech-1", ["tech"]),
        ("https://example.com/tech-2", ["tech"]),
        ("https://example.com/science-1", ["science"]),
    ):
        item, _ = await save_item(db, env, user["id"], url, tags=tags)
        ids.append(item["id"])
    return user, env, db, ids


async def _bulk_update(db, env, user, arguments):
    """Calls the bulk_update tool and returns the JSON-RPC response."""
    return await process_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "bulk_update", "arguments": arguments},
        },
        db,
        env,
        user,
    )


def _bulk_result(res):
    """Unwraps the tool payload; fails loudly on a JSON-RPC error."""
    assert "error" not in res, res["error"]
    return json.loads(res["result"]["content"][0]["text"])


async def _item_state(db, user_id):
    """Returns {item_id: (status, is_pinned, sorted tags)} for the user."""
    rows = await db.query_all(
        "SELECT id, status, is_pinned FROM items WHERE user_id = ? ORDER BY id;",
        (user_id,),
    )
    state = {}
    for r in rows:
        tags = await db.query_all(
            "SELECT t.name FROM item_tags it JOIN tags t ON t.id = it.tag_id"
            " WHERE it.item_id = ? ORDER BY t.name;",
            (r["id"],),
        )
        state[r["id"]] = (r["status"], r["is_pinned"], [t["name"] for t in tags])
    return state


@pytest.mark.asyncio
async def test_bulk_update_dry_run_writes_nothing(bulk_user):
    """The default dry run reports the change and leaves the library alone."""
    user, env, db, ids = bulk_user
    before = await _item_state(db, user["id"])

    res = await _bulk_update(
        db, env, user, {"tags_all": ["tech"], "add_tags": ["bulk"], "pin": True}
    )
    result = _bulk_result(res)
    assert result["dry_run"] is True
    assert result["total_matched"] == 2
    assert result["affected"] == 2
    assert result["truncated"] is False
    assert sorted(result["sample"]) == sorted(ids[:2])

    assert await _item_state(db, user["id"]) == before


@pytest.mark.asyncio
async def test_bulk_update_applies_only_matched_items(bulk_user):
    """A real run touches the matched items and nothing else."""
    user, env, db, ids = bulk_user
    res = await _bulk_update(
        db,
        env,
        user,
        {"tags_all": ["tech"], "add_tags": ["bulk"], "pin": True, "dry_run": False},
    )
    result = _bulk_result(res)
    assert result["dry_run"] is False
    assert result["affected"] == 2

    state = await _item_state(db, user["id"])
    for item_id in ids[:2]:
        _, pinned, tags = state[item_id]
        assert pinned == 1
        assert "bulk" in tags
    # The unmatched item is untouched.
    assert state[ids[2]] == ("queued", 0, ["science"])


@pytest.mark.asyncio
async def test_bulk_update_sets_status(bulk_user):
    """set_status writes the raw status; the status filter uses the groups."""
    user, env, db, ids = bulk_user
    res = await _bulk_update(
        db,
        env,
        user,
        {"domain": "example.com", "set_status": "failed", "dry_run": False},
    )
    assert _bulk_result(res)["affected"] == 3
    state = await _item_state(db, user["id"])
    assert {v[0] for v in state.values()} == {"failed"}

    res = await _bulk_update(
        db, env, user, {"status": "failed", "pin": True, "dry_run": False}
    )
    assert _bulk_result(res)["total_matched"] == 3
    state = await _item_state(db, user["id"])
    assert {v[1] for v in state.values()} == {1}


@pytest.mark.asyncio
async def test_bulk_update_removes_tags(bulk_user):
    """remove_tags drops the tag from the matched items only."""
    user, env, db, ids = bulk_user
    res = await _bulk_update(
        db, env, user, {"tags_all": ["tech"], "remove_tags": ["tech"], "dry_run": False}
    )
    assert _bulk_result(res)["affected"] == 2
    state = await _item_state(db, user["id"])
    assert state[ids[0]][2] == []
    assert state[ids[1]][2] == []
    assert state[ids[2]][2] == ["science"]


@pytest.mark.asyncio
async def test_bulk_update_caps_at_200_items(db):
    """The 200-item cap truncates the update and reports it."""
    user = await register_user(db, "cap@example.com", "password123")
    env = MockEnv()
    for i in range(205):
        url = f"https://example.com/cap-{i}"
        await db.execute(
            "INSERT INTO items (id, user_id, url, canonical_url, title, status)"
            " VALUES (?, ?, ?, ?, ?, 'ok');",
            (f"cap-{i}", user["id"], url, url, f"Cap {i}"),
        )

    res = await _bulk_update(
        db, env, user, {"domain": "example.com", "pin": True, "dry_run": False}
    )
    result = _bulk_result(res)
    assert result["total_matched"] == 205
    assert result["affected"] == 200
    assert result["truncated"] is True

    rows = await db.query_all(
        "SELECT COUNT(*) as c FROM items WHERE user_id = ? AND is_pinned = 1;",
        (user["id"],),
    )
    assert rows[0]["c"] == 200


@pytest.mark.asyncio
async def test_bulk_update_requires_a_filter(bulk_user):
    """An empty filter would match the whole library: it is rejected."""
    user, env, db, ids = bulk_user
    res = await _bulk_update(db, env, user, {"pin": True})
    assert res["error"]["data"]["type"] == "invalid_arguments"
    assert "filter" in res["error"]["message"]


@pytest.mark.asyncio
async def test_bulk_update_requires_a_change(bulk_user):
    """A filter without any change is a no-op: it is rejected."""
    user, env, db, ids = bulk_user
    res = await _bulk_update(db, env, user, {"tags_all": ["tech"]})
    assert res["error"]["data"]["type"] == "invalid_arguments"
    assert "change" in res["error"]["message"]


@pytest.mark.asyncio
async def test_bulk_update_rejects_unknown_status(bulk_user):
    """An unknown status group is rejected instead of matching everything."""
    user, env, db, ids = bulk_user
    res = await _bulk_update(db, env, user, {"status": "bogus", "pin": True})
    assert res["error"]["data"]["type"] == "invalid_arguments"
    assert "status" in res["error"]["message"]


@pytest.mark.asyncio
async def test_bulk_update_dry_run_must_be_a_boolean(bulk_user):
    """dry_run fails closed: only a JSON boolean is accepted.

    A null, 0 or "false" must never be coerced into "apply".
    """
    user, env, db, ids = bulk_user
    for bad in (None, "false", 0, "", 1):
        res = await _bulk_update(
            db, env, user, {"tags_all": ["tech"], "pin": True, "dry_run": bad}
        )
        assert res["error"]["data"]["type"] == "invalid_arguments", bad
        assert "dry_run" in res["error"]["message"], bad
    # Nothing was applied.
    state = await _item_state(db, user["id"])
    assert all(pinned == 0 for _, pinned, _ in state.values())


@pytest.mark.asyncio
async def test_bulk_update_normalizes_and_validates_tag_names(bulk_user):
    """Tag names follow the create_tag rules: normalized, or rejected."""
    user, env, db, ids = bulk_user
    res = await _bulk_update(
        db,
        env,
        user,
        {"tags_all": ["tech"], "add_tags": ["  Bulk  "], "dry_run": False},
    )
    assert _bulk_result(res)["affected"] == 2
    state = await _item_state(db, user["id"])
    assert "bulk" in state[ids[0]][2]

    for bad in ("work/research", "x", "a" * 65, "c++"):
        res = await _bulk_update(
            db, env, user, {"tags_all": ["tech"], "add_tags": [bad], "dry_run": False}
        )
        assert res["error"]["data"]["type"] == "invalid_arguments", bad
        assert "tag name" in res["error"]["message"], bad

    rows = await db.query_all(
        "SELECT name FROM tags WHERE user_id = ? ORDER BY name;", (user["id"],)
    )
    assert [r["name"] for r in rows] == ["bulk", "science", "tech"]


@pytest.mark.asyncio
async def test_bulk_update_apply_is_atomic(bulk_user):
    """The apply is one transaction: a failure changes nothing.

    The status change and the tag change run in the same batch, so a
    statement that fails rolls both back (D1's batch() is transactional).
    """
    import sqlite3

    from keepfor.models.db import Database
    from keepfor.models.items import bulk_update_items

    user, env, db, ids = bulk_user
    before = await _item_state(db, user["id"])

    class _FailingBatch(Database):
        """Fails the batch after its statements ran, like a D1 error."""

        async def execute_batch(self, statements):
            await super().execute_batch(
                [*statements, ("SELECT no_such_function();", ())]
            )

    with pytest.raises(sqlite3.OperationalError):
        await bulk_update_items(
            _FailingBatch(sqlite_conn=db.sqlite),
            user["id"],
            ids[:2],
            set_status="failed",
            pin=True,
            add_tags=["bulk"],
        )

    assert await _item_state(db, user["id"]) == before
    rows = await db.query_all(
        "SELECT name FROM tags WHERE user_id = ? AND name = 'bulk';", (user["id"],)
    )
    assert rows == []


@pytest.mark.asyncio
async def test_bulk_update_never_updates_foreign_items(bulk_user):
    """Another user's item is never touched, even when its id is passed in."""
    from keepfor.models.items import bulk_update_items, save_item

    user, env, db, ids = bulk_user
    other = await register_user(db, "other@example.com", "password123", True)
    other_item, _ = await save_item(db, env, other["id"], "https://other.example.com/x")
    before = await _item_state(db, other["id"])

    await bulk_update_items(
        db,
        user["id"],
        [ids[0], other_item["id"]],
        set_status="failed",
        pin=True,
        add_tags=["bulk"],
    )

    assert await _item_state(db, other["id"]) == before
    state = await _item_state(db, user["id"])
    assert state[ids[0]][0] == "failed"
    assert state[ids[0]][1] == 1
    assert "bulk" in state[ids[0]][2]


def test_bulk_update_is_annotated_destructive():
    """The tool is annotated destructive, like delete_item and delete_tag."""
    from keepfor.mcp.server import MCP_TOOLS

    tool = next(t for t in MCP_TOOLS if t["name"] == "bulk_update")
    assert tool["annotations"]["destructiveHint"] is True
    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["inputSchema"]["properties"]["dry_run"]["default"] is True


# ==========================================
# find_cleanup (read-only cleanup suggestions)
# ==========================================


@pytest.fixture
async def cleanup_user(db):
    """A user with an empty library for the find_cleanup tests."""
    user = await register_user(db, "cleanup@example.com", "password123")
    return user, MockEnv(), db


async def _call_cleanup(db, env, user, **arguments):
    """Calls find_cleanup and returns the raw JSON-RPC response."""
    return await process_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "find_cleanup", "arguments": arguments},
        },
        db,
        env,
        user,
    )


async def _cleanup(db, env, user, **arguments):
    """Calls find_cleanup and returns the parsed payload."""
    res = await _call_cleanup(db, env, user, **arguments)
    assert "result" in res, res
    return json.loads(res["result"]["content"][0]["text"])


async def _save(db, env, user_id, url, tags=None):
    """Saves one item and returns its id."""
    from keepfor.models.items import save_item

    item, _ = await save_item(db, env, user_id, url, tags=tags)
    return item["id"]


async def _fingerprint(db):
    """Every tenant row, so a test can prove a tool wrote nothing."""
    out = {}
    for table in ("items", "tags", "item_tags", "item_opens"):
        rows = await db.query_all(f"SELECT * FROM {table};")
        out[table] = sorted(str(sorted(row.items())) for row in rows)
    return out


async def test_find_cleanup_detects_duplicate_urls(cleanup_user):
    """Two items with the same normalized url are reported as one group."""
    user, env, db = cleanup_user
    first = await _save(db, env, user["id"], "https://example.com/dup")
    second = await _save(db, env, user["id"], "https://www.example.com/dup")

    report = (await _cleanup(db, env, user))["duplicates"]
    assert len(report["items"]) == 1
    group = report["items"][0]
    assert set(group["ids"]) == {first, second}
    assert group["group_size"] == 2
    assert group["truncated"] is False
    assert report["count"] == 1
    assert report["truncated"] is False


async def test_find_cleanup_keeps_large_duplicate_group_small(cleanup_user):
    """300 saves of one url report one capped group, not 300 rows of ids."""
    user, env, db = cleanup_user
    # Distinct canonical urls (same url + tracking param) that normalize to
    # one dedupe key -- the shape a library ends up in after many saves.
    await db.execute_batch(
        [
            (
                "INSERT INTO items (id, user_id, url, canonical_url, status)"
                " VALUES (?, ?, ?, ?, 'queued');",
                (
                    f"dup-{i}",
                    user["id"],
                    f"https://example.com/same?utm_source={i}",
                    f"https://example.com/same?utm_source={i}",
                ),
            )
            for i in range(300)
        ]
    )

    res = await _call_cleanup(db, env, user, categories=["duplicates"], limit=100)
    payload = json.loads(res["result"]["content"][0]["text"])
    report = payload["duplicates"]

    assert len(report["items"]) == 1
    group = report["items"][0]
    assert group["group_size"] == 300
    assert len(group["ids"]) == 10
    assert group["truncated"] is True
    assert report["count"] == 1
    # The whole payload stays a few KB, not the ~1 MB of 300 x 299 ids.
    assert len(json.dumps(payload)) < 5000


async def test_find_cleanup_detects_dead_links(cleanup_user):
    """An item whose recorded failure is an origin HTTP error is a dead link."""
    user, env, db = cleanup_user
    dead = await _save(db, env, user["id"], "https://example.com/dead")
    alive = await _save(db, env, user["id"], "https://example.com/alive")
    await db.execute(
        "UPDATE items SET status = 'failed', fail_reason = 'HTTP 404 Not Found'"
        " WHERE id = ?;",
        (dead,),
    )

    report = (await _cleanup(db, env, user))["dead_links"]
    assert [e["id"] for e in report["items"]] == [dead]
    assert "404" in report["items"][0]["reason"]
    assert report["count"] == 1
    assert alive not in {e["id"] for e in report["items"]}


async def test_find_cleanup_separates_dead_links_from_failed_extractions(
    cleanup_user,
):
    """An origin HTTP error is a dead link; every other failure is not."""
    user, env, db = cleanup_user
    dead = await _save(db, env, user["id"], "https://example.com/origin-dead")
    wall = await _save(db, env, user["id"], "https://example.com/wall")
    await db.execute_batch(
        [
            (
                "UPDATE items SET status = 'failed', fail_reason = ? WHERE id = ?;",
                (
                    "HTTP 404 returned by origin server (https://example.com/origin-dead)",
                    dead,
                ),
            ),
            (
                "UPDATE items SET status = 'failed', fail_reason = ? WHERE id = ?;",
                ("login wall", wall),
            ),
        ]
    )

    report = await _cleanup(db, env, user)
    assert [e["id"] for e in report["dead_links"]["items"]] == [dead]
    assert [e["id"] for e in report["failed_extraction"]["items"]] == [wall]
    assert report["dead_links"]["count"] == 1
    assert report["failed_extraction"]["count"] == 1


async def test_find_cleanup_detects_never_opened_old_items(cleanup_user):
    """Old items that were never opened are reported; opened/new ones are not."""
    from keepfor.models.items import record_open

    user, env, db = cleanup_user
    old = await _save(db, env, user["id"], "https://example.com/old")
    opened = await _save(db, env, user["id"], "https://example.com/opened")
    recent = await _save(db, env, user["id"], "https://example.com/recent")
    await db.execute(
        "UPDATE items SET created_at = '2020-01-02 03:04:05' WHERE id IN (?, ?);",
        (old, opened),
    )
    await record_open(db, user["id"], opened)

    report = (await _cleanup(db, env, user, older_than_days=90))["never_opened"]
    ids = {e["id"] for e in report["items"]}
    assert old in ids
    assert opened not in ids
    assert recent not in ids
    assert report["count"] == 1


async def test_find_cleanup_detects_untagged_items(cleanup_user):
    """Items without any tag are reported."""
    user, env, db = cleanup_user
    untagged = await _save(db, env, user["id"], "https://example.com/untagged")
    tagged = await _save(db, env, user["id"], "https://example.com/tagged", ["work"])

    report = (await _cleanup(db, env, user))["untagged"]
    ids = {e["id"] for e in report["items"]}
    assert untagged in ids
    assert tagged not in ids


async def test_find_cleanup_detects_near_duplicate_tags(cleanup_user):
    """Tags differing only by separator are grouped as near-duplicates."""
    from keepfor.models.items import create_tag

    user, env, db = cleanup_user
    await create_tag(db, user["id"], "machine-learning")
    await create_tag(db, user["id"], "machine_learning")
    await create_tag(db, user["id"], "unique")

    report = (await _cleanup(db, env, user))["near_duplicate_tags"]
    groups = [set(g["tags"]) for g in report["items"]]
    assert {"machine-learning", "machine_learning"} in groups
    assert report["count"] == 1
    assert report["truncated"] is False


async def test_find_cleanup_caps_each_category(cleanup_user):
    """A per-category limit caps the list and flags the category truncated."""
    user, env, db = cleanup_user
    for i in range(3):
        await _save(db, env, user["id"], f"https://example.com/untagged-{i}")

    report = (await _cleanup(db, env, user, limit=1))["untagged"]
    assert len(report["items"]) == 1
    assert report["count"] == 3
    assert report["truncated"] is True


async def test_find_cleanup_reports_only_requested_categories(cleanup_user):
    """Only the requested categories come back."""
    user, env, db = cleanup_user

    payload = await _cleanup(db, env, user, categories=["duplicates"])
    assert set(payload) == {"duplicates"}

    payload = await _cleanup(db, env, user, categories=["duplicates", "untagged"])
    assert set(payload) == {"duplicates", "untagged"}


async def test_find_cleanup_rejects_invalid_arguments(cleanup_user):
    """Bad categories and out-of-range numbers are clear tool errors."""
    user, env, db = cleanup_user
    for arguments in (
        {"categories": ["nope"]},
        {"limit": 0},
        {"limit": 101},
        {"limit": "ten"},
        {"older_than_days": 0},
        {"older_than_days": -5},
    ):
        res = await _call_cleanup(db, env, user, **arguments)
        assert "error" in res, arguments
        assert res["error"]["data"]["type"] == "invalid_arguments", arguments


async def test_find_cleanup_writes_nothing(cleanup_user):
    """The tool only reads: the library is byte-identical afterwards."""
    user, env, db = cleanup_user
    await _save(db, env, user["id"], "https://example.com/a")
    await _save(db, env, user["id"], "https://www.example.com/a")
    await db.execute(
        "UPDATE items SET status = 'failed' WHERE user_id = ?;", (user["id"],)
    )

    before = await _fingerprint(db)
    await _cleanup(db, env, user)
    assert await _fingerprint(db) == before


async def test_find_cleanup_tool_is_read_only(mcp_client, pat_headers):
    """The tool advertises itself as read-only and non-destructive."""
    res = mcp_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
        headers=pat_headers,
    )
    tool = next(t for t in res.json()["result"]["tools"] if t["name"] == "find_cleanup")
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["annotations"]["destructiveHint"] is False


# ==========================================
# Token scopes (GitHub issue #29)
# ==========================================

# Every tool that can change something, straight from the annotations.
WRITE_TOOL_NAMES = [
    t["name"] for t in MCP_TOOLS if not t["annotations"]["readOnlyHint"]
]

# Arguments that really change something. The shared defaults above are
# no-ops for these (the tag already exists, the tag name is too short).
WRITE_ARGUMENTS = {
    "create_tag": {"name": "probe-new-tag"},
    "tag_item": {"item_id": "ITEM", "add_tags": ["probe-add"]},
    "tag_items": {"item_ids": ["ITEM"], "add": ["probe-add"]},
    "retag_by_query": {"query": "alpha", "add": ["probe-add"]},
    "bulk_update": {"tags_all": ["work"], "add_tags": ["probe-add"]},
    "set_item_tags": {"item_id": "ITEM", "tags": ["probe-add"]},
    # Highlights and collections (GitHub issue #24): the collection and the
    # highlight must exist for the call to change anything, so the shared
    # defaults above (which name a collection that is not there yet, or one
    # that already holds the item) are replaced here.
    "create_collection": {"name": "probe-collection-new"},
    "add_to_collection": {"collection": "probe-collection-empty", "item_ids": ["ITEM"]},
    "remove_from_collection": {"collection": "probe-collection", "item_ids": ["ITEM"]},
    "rename_collection": {
        "collection": "probe-collection",
        "new_name": "probe-collection-2",
    },
    "delete_collection": {"collection": "probe-collection"},
    "delete_highlight": {"highlight_id": "probe-highlight"},
    "update_highlight": {"highlight_id": "probe-highlight", "note": "new note"},
}


def _fill_item(value, item_id):
    """Replaces the "ITEM" placeholder, inside lists and dicts too."""
    if isinstance(value, str):
        return item_id if value == "ITEM" else value
    if isinstance(value, list):
        return [_fill_item(v, item_id) for v in value]
    if isinstance(value, dict):
        return {k: _fill_item(v, item_id) for k, v in value.items()}
    return value


def _write_arguments(tool_name, item_id):
    """Arguments that would really write if the tool were allowed to run."""
    args = _fill_item(
        WRITE_ARGUMENTS.get(tool_name, TOOL_ARGUMENTS[tool_name]), item_id
    )
    properties = MCP_TOOLS_BY_NAME[tool_name]["inputSchema"].get("properties", {})
    if "dry_run" in properties:
        # The bulk and destructive tools preview by default: apply them, so
        # the refusal is proven against a call that would really write.
        args = {**args, "dry_run": False}
    return args


async def _library_state(db, user_id):
    """Everything a write tool could touch, as one comparable blob."""
    parts = []
    for sql, params in (
        ("SELECT * FROM items WHERE user_id = ? ORDER BY id;", (user_id,)),
        ("SELECT * FROM tags WHERE user_id = ? ORDER BY id;", (user_id,)),
        (
            "SELECT it.item_id, t.name FROM item_tags it"
            " JOIN items i ON i.id = it.item_id"
            " JOIN tags t ON t.id = it.tag_id WHERE i.user_id = ?"
            " ORDER BY it.item_id, t.name;",
            (user_id,),
        ),
        ("SELECT * FROM highlights WHERE user_id = ? ORDER BY id;", (user_id,)),
        ("SELECT * FROM collections WHERE user_id = ? ORDER BY id;", (user_id,)),
        (
            "SELECT ci.collection_id, ci.item_id FROM collection_items ci"
            " JOIN collections c ON c.id = ci.collection_id AND c.user_id = ?"
            " ORDER BY ci.collection_id, ci.item_id;",
            (user_id,),
        ),
    ):
        parts.append(await db.query_all(sql, params))
    return json.dumps(parts, sort_keys=True, default=str)


@pytest.fixture
async def scoped_user(db):
    """A user with a library and the tags every write tool acts on."""
    from keepfor.models.collections import (
        add_items_to_collection,
        create_collection,
    )
    from keepfor.models.items import create_tag

    user = await register_user(db, "scoped@example.com", "password123")
    env = MockEnv()
    item_id = await _make_item(db, env, user["id"])
    for name in ("probe-tag", "probe-tag2", "probe-tag3"):
        await create_tag(db, user["id"], name)
    # A highlight to delete and two collections: one holding the item (so
    # removing it changes something) and one empty (so adding it does).
    await db.execute(
        "INSERT INTO highlights (id, user_id, item_id, text, start_offset,"
        " end_offset) VALUES ('probe-highlight', ?, ?, 'quoted', 0, 6);",
        (user["id"], item_id),
    )
    await create_collection(db, user["id"], "probe-collection")
    await create_collection(db, user["id"], "probe-collection-empty")
    await add_items_to_collection(db, user["id"], "probe-collection", [item_id])
    return user, env, item_id


async def test_read_only_token_lists_only_read_tools(db, scoped_user):
    """A read-only token sees the read tools in tools/list, and only those."""
    user, env, _ = scoped_user
    res = await process_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        db,
        env,
        {**user, "scopes": ["read"]},
    )
    names = [t["name"] for t in res["result"]["tools"]]
    assert names == [t["name"] for t in MCP_TOOLS if t["annotations"]["readOnlyHint"]]
    assert "search_library" in names
    assert "save_url" not in names
    assert "delete_item" not in names


@pytest.mark.parametrize("tool_name", WRITE_TOOL_NAMES)
async def test_read_only_token_is_refused_on_write_tools(db, scoped_user, tool_name):
    """Every write or destructive tool is refused, and nothing is written.

    The same call with full scope does write, so the refusal is proven
    against a real change instead of a call that was a no-op anyway.
    """
    user, env, item_id = scoped_user
    args = _write_arguments(tool_name, item_id)

    def call(scopes):
        return process_mcp_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": args},
            },
            db,
            env,
            {**user, "scopes": scopes},
        )

    # Full scope: the call really writes.
    before = await _library_state(db, user["id"])
    res = await call(["*"])
    assert "error" not in res, res
    assert await _library_state(db, user["id"]) != before, (
        f"{tool_name} wrote nothing: the refusal test would be vacuous"
    )

    # Read-only: refused before the tool runs, nothing written.
    before = await _library_state(db, user["id"])
    res = await call(["read"])
    assert "error" in res, res
    assert res["error"]["data"]["type"] == "insufficient_scope"
    assert res["error"]["data"]["tool"] == tool_name
    assert res["error"]["data"]["required_scope"] == "write"
    assert await _library_state(db, user["id"]) == before


async def test_full_scope_token_sees_every_tool(db, scoped_user):
    """All scopes (the default) keep the full tool list."""
    user, env, _ = scoped_user
    for caller in (
        {**user, "scopes": ["*"]},
        {k: v for k, v in user.items() if k != "scopes"},
    ):
        res = await process_mcp_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, db, env, caller
        )
        assert len(res["result"]["tools"]) == len(MCP_TOOLS)


async def test_read_only_pat_over_http(mcp_client, db):
    """A read-only PAT gets a filtered tool list and is refused on writes."""
    from keepfor.auth.service import create_pat, register_user

    user = await register_user(db, "ro-http@example.com", "password123")
    pat = await create_pat(db, user["id"], "Read-only PAT", scopes=["read"])
    headers = {"Authorization": f"Bearer {pat['token']}"}

    res = mcp_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=headers,
    )
    assert res.status_code == 200
    names = [t["name"] for t in res.json()["result"]["tools"]]
    assert "search_library" in names
    assert "save_url" not in names

    before = await _library_state(db, user["id"])
    res = mcp_client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "save_url",
                "arguments": {"url": "https://example.com/should-not-be-saved"},
            },
        },
        headers=headers,
    )
    assert res.status_code == 200
    assert res.json()["error"]["data"]["type"] == "insufficient_scope"
    assert await _library_state(db, user["id"]) == before

    # A read tool still works: the token is read-only, not useless.
    res = mcp_client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_items", "arguments": {}},
        },
        headers=headers,
    )
    assert "result" in res.json(), res.json()


async def test_tools_call_fails_closed_on_unknown_tool(mcp_user):
    """A tool with no schema entry is refused before any handler runs.

    Fail closed: a future handler without a schema entry must not skip the
    scope check. This also retires the undocumented "search" alias of
    search_library, which was never in MCP_TOOLS.
    """
    user, env, db = mcp_user
    for name in ("search", "no_such_tool"):
        res = await process_mcp_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": {"query": "x"}},
            },
            db,
            env,
            user,
        )
        assert "error" in res, res
        assert res["error"]["data"]["type"] == "not_found"
        assert res["error"]["data"]["tool"] == name


def test_every_tool_declares_annotations():
    """Every tool carries explicit annotations: they are the scope source of
    truth, and a missing readOnlyHint keeps defaulting to write (fail closed).
    """
    from keepfor.mcp.server import _tool_scope
    from keepfor.spi import SCOPE_WRITE

    for tool in MCP_TOOLS:
        assert "annotations" in tool, tool["name"]
        assert "readOnlyHint" in tool["annotations"], tool["name"]
    assert _tool_scope({"name": "hypothetical"}) == SCOPE_WRITE
