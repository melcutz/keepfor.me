# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for MCP server functionality."""

import asyncio
import json
import time

import pytest

from keepfor.auth.service import register_user
from keepfor.mcp.server import process_mcp_request


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
    assert len(names) == 28
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
    "set_item_tags": {"item_id": "ITEM", "tags": ["t"]},
    "suggest_tags": {"item_id": "ITEM"},
    "search_tags": {"query": "a"},
    "tag_stats": {},
    "related_tags": {"tag": "work"},
    "tag_cleanup": {},
    "delete_item": {"item_id": "ITEM"},
    "pin_item": {"item_id": "ITEM"},
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
