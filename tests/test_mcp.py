"""Tests for MCP server functionality."""

import json

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
