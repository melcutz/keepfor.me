"""Tests for MCP server functionality."""

import asyncio
import json
import time

import pytest

from src.auth.service import register_user
from src.mcp.server import process_mcp_request


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
    from src.search import engine

    async def slow_fts(*args, **kwargs):
        await asyncio.sleep(0.20)
        return [{"item_id": "i1", "rank": 0.1, "snippet": "x"}]

    monkeypatch.setattr(engine, "search_fts", slow_fts)

    start = time.perf_counter()
    results = await engine.hybrid_search(
        _StubDB(), _SearchEnv(_SlowAI(0.30)), "u1", "query", mode="hybrid"
    )
    elapsed = time.perf_counter() - start

    assert len(results) == 1
    # Concurrent ~= max(0.20, 0.30); sequential would be ~=0.50.
    assert elapsed < 0.45, f"hybrid search took {elapsed:.2f}s (not concurrent?)"


@pytest.mark.asyncio
async def test_vector_search_timeout_degrades_to_keyword(monkeypatch):
    """A hanging AI binding must not hang search; it falls back to FTS."""
    from src.search import engine

    async def fast_fts(*args, **kwargs):
        return [{"item_id": "i1", "rank": 0.1, "snippet": "x"}]

    monkeypatch.setattr(engine, "search_fts", fast_fts)
    monkeypatch.setattr(engine, "VECTOR_SEARCH_TIMEOUT", 0.05)

    results = await engine.hybrid_search(
        _StubDB(), _SearchEnv(_HangingAI()), "u1", "query", mode="hybrid"
    )
    assert len(results) == 1
    assert results[0]["id"] == "i1"


def test_heavy_extraction_libs_are_not_imported_at_module_load():
    """bs4/lxml/trafilatura must stay out of the request import path.

    They are only needed by the extraction consumer, but src.worker imports
    that chain at module scope -- eager imports made every request (even
    /auth/login) pay to load the article-parsing stack.
    """
    import subprocess
    import sys

    code = (
        "import sys, src.worker;"
        "print(','.join(m for m in ('trafilatura','bs4','lxml') if m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "", (
        f"heavy modules loaded at import: {out.stdout.strip()}"
    )
