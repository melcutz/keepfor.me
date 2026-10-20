# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Tenant isolation tests for MCP (Model Context Protocol) endpoints and tools.

Enforces:
- Dynamic enumeration of all MCP tools accepting an id parameter.
- Dedicated test cases for every MCP tool.
- Invocation as Tenant B with Tenant A's IDs results in error or empty result.
- Snapshot of Tenant A is completely unchanged.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.isolation.conftest import IsolationHarness, snapshot


@pytest.mark.asyncio
async def test_mcp_tool_isolation(harness: IsolationHarness):
    """Dynamically enumerate all MCP tools accepting an id parameter and verify
    isolation.
    """
    a = harness.tenant_a
    b = harness.tenant_b

    # 1. Enumerate tools via tools/list
    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "result" in data and "tools" in data["result"]
    tools = data["result"]["tools"]
    assert len(tools) > 0

    # 2. Find all tools with an '*id*' parameter in inputSchema
    tools_with_id: list[tuple[str, str, dict[str, Any]]] = []
    for tool in tools:
        tname = tool["name"]
        schema = tool.get("inputSchema", {})
        properties = schema.get("properties", {})
        for prop_name in properties:
            if "id" in prop_name.lower():
                tools_with_id.append((tname, prop_name, schema))

    # Must find at least get_item, tag_item, delete_item, pin_item
    tool_names_found = {t[0] for t in tools_with_id}
    assert {"get_item", "tag_item", "delete_item", "pin_item"}.issubset(
        tool_names_found
    ), f"Expected tools with id not found: {tool_names_found}"

    # 3. For each tool, invoke as B with A's item ID
    for tool_name, id_param, schema in tools_with_id:
        snap_a_before = await snapshot(
            a.db, a.user["id"], env=harness.env, scope=a.scope
        )

        # Build dummy arguments fulfilling required schema
        arguments: dict[str, Any] = {}
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        for req_field in required:
            if req_field == id_param:
                arguments[req_field] = a.html_item_id
            else:
                field_type = properties.get(req_field, {}).get("type")
                if field_type == "string":
                    arguments[req_field] = "dummy_val"
                elif field_type == "boolean":
                    arguments[req_field] = True
                elif field_type == "integer":
                    arguments[req_field] = 1
                elif field_type == "array":
                    arguments[req_field] = ["dummy_tag"]
                else:
                    arguments[req_field] = "dummy"

        # Explicitly ensure id_param is set to A's ID
        arguments[id_param] = a.html_item_id

        # Call tool as tenant B
        call_resp = harness.client.post(
            "/api/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 100,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments,
                },
            },
            headers=b.auth_headers,
        )
        assert call_resp.status_code == 200, (
            f"Tool {tool_name} failed with HTTP {call_resp.status_code}"
        )
        call_data = call_resp.json()

        # The result must be an error OR an empty/noop result
        if "error" in call_data:
            assert call_data["error"]["code"] != 0
        elif "result" in call_data:
            content = call_data["result"].get("content", [])
            for block in content:
                text = block.get("text", "")
                assert a.unique_keyword not in text, (
                    f"Tool {tool_name} leaked keyword from tenant A!"
                )
                assert f"Title {a.unique_keyword}" not in text, (
                    f"Tool {tool_name} leaked title from tenant A!"
                )
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    if "deleted" in parsed:
                        assert parsed["deleted"] is False, (
                            f"Tool {tool_name} claimed to delete A's item!"
                        )
                    if "tags" in parsed:
                        assert parsed["tags"] == []
        else:
            pytest.fail(
                f"Tool {tool_name} returned neither error nor result: {call_data}"
            )

        # Assert snapshot(A) is completely unchanged
        snap_a_after = await snapshot(
            a.db, a.user["id"], env=harness.env, scope=a.scope
        )
        assert snap_a_after == snap_a_before, (
            f"Tool {tool_name} modified tenant A state!"
        )


@pytest.mark.asyncio
async def test_mcp_get_item_isolation(harness: IsolationHarness):
    """MCP get_item as Tenant B targeting Tenant A's item fails or returns error."""
    a = harness.tenant_a
    b = harness.tenant_b

    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 201,
            "method": "tools/call",
            "params": {
                "name": "get_item",
                "arguments": {"id": a.html_item_id},
            },
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data or a.unique_keyword not in str(data)
    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before


@pytest.mark.asyncio
async def test_mcp_tag_item_isolation(harness: IsolationHarness):
    """MCP tag_item as Tenant B targeting Tenant A's item does not tag A's item."""
    a = harness.tenant_a
    b = harness.tenant_b

    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 202,
            "method": "tools/call",
            "params": {
                "name": "tag_item",
                "arguments": {"id": a.html_item_id, "tags": ["malicious_tag"]},
            },
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200
    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before


@pytest.mark.asyncio
async def test_mcp_delete_item_isolation(harness: IsolationHarness):
    """MCP delete_item as Tenant B targeting Tenant A's item does not delete
    A's item.
    """
    a = harness.tenant_a
    b = harness.tenant_b

    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 203,
            "method": "tools/call",
            "params": {
                "name": "delete_item",
                "arguments": {"id": a.html_item_id},
            },
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200
    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before


@pytest.mark.asyncio
async def test_mcp_pin_item_isolation(harness: IsolationHarness):
    """MCP pin_item as Tenant B targeting Tenant A's item does not pin A's item."""
    a = harness.tenant_a
    b = harness.tenant_b

    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 204,
            "method": "tools/call",
            "params": {
                "name": "pin_item",
                "arguments": {"id": a.html_item_id},
            },
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200
    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before


@pytest.mark.asyncio
async def test_mcp_save_note_isolation(harness: IsolationHarness):
    """MCP save_note as Tenant B targeting Tenant A's item does not modify A's item."""
    a = harness.tenant_a
    b = harness.tenant_b

    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 205,
            "method": "tools/call",
            "params": {
                "name": "save_note",
                "arguments": {"text": "Note from B", "title": "B Note"},
            },
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200
    # Note created should belong to B, snapshot A must be unchanged
    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before


@pytest.mark.asyncio
async def test_mcp_save_url_isolation(harness: IsolationHarness):
    """MCP save_url as Tenant B saves URL to B and does not touch Tenant A."""
    a = harness.tenant_a
    b = harness.tenant_b

    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 206,
            "method": "tools/call",
            "params": {
                "name": "save_url",
                "arguments": {"url": "https://example.com/saved-by-b"},
            },
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200
    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before


@pytest.mark.asyncio
async def test_mcp_search_library_isolation(harness: IsolationHarness):
    """MCP search_library as Tenant B never returns Tenant A's items."""
    a = harness.tenant_a
    b = harness.tenant_b

    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 207,
            "method": "tools/call",
            "params": {
                "name": "search_library",
                "arguments": {"query": a.unique_keyword},
            },
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200
    call_data = resp.json()
    assert "result" in call_data
    content = call_data["result"].get("content", [])
    for block in content:
        text = block.get("text", "")
        assert a.unique_keyword not in text
        assert a.html_item_id not in text


@pytest.mark.asyncio
async def test_mcp_list_items_isolation(harness: IsolationHarness):
    """MCP list_items called as Tenant B returns none of Tenant A's items."""
    a = harness.tenant_a
    b = harness.tenant_b

    call_resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 200,
            "method": "tools/call",
            "params": {
                "name": "list_items",
                "arguments": {"limit": 50},
            },
        },
        headers=b.auth_headers,
    )
    assert call_resp.status_code == 200
    call_data = call_resp.json()
    assert "result" in call_data
    content = call_data["result"].get("content", [])
    assert len(content) > 0
    result = json.loads(content[0]["text"])
    items = result.get("items", [])
    item_ids = [it["id"] for it in items]

    for a_id in a.item_ids:
        assert a_id not in item_ids
    for b_id in b.item_ids:
        assert b_id in item_ids


@pytest.mark.asyncio
async def test_mcp_search_passages_isolation(harness: IsolationHarness):
    """MCP search_passages as Tenant B never returns Tenant A's passages."""
    a = harness.tenant_a
    b = harness.tenant_b

    # Ensure A has an item with searchable content
    # (harness already creates items for A)

    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 301,
            "method": "tools/call",
            "params": {
                "name": "search_passages",
                "arguments": {"query": a.unique_keyword, "limit": 10},
            },
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200
    call_data = resp.json()
    assert "result" in call_data
    content = call_data["result"].get("content", [])
    for block in content:
        text = block.get("text", "")
        assert a.unique_keyword not in text
        assert a.html_item_id not in text


@pytest.mark.asyncio
async def test_mcp_read_item_isolation(harness: IsolationHarness):
    """MCP read_item as Tenant B targeting Tenant A's item fails."""
    a = harness.tenant_a
    b = harness.tenant_b

    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 302,
            "method": "tools/call",
            "params": {
                "name": "read_item",
                "arguments": {
                    "item_id": a.html_item_id,
                    "cursor": 0,
                    "max_tokens": 1000,
                },
            },
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data or a.unique_keyword not in str(data)
    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before


@pytest.mark.asyncio
async def test_mcp_reading_history_isolation(harness: IsolationHarness):
    """MCP reading_history as Tenant B never returns Tenant A's history."""
    a = harness.tenant_a
    b = harness.tenant_b

    # Ensure A has some history (harness creates items, which are 'saved' events)

    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 303,
            "method": "tools/call",
            "params": {
                "name": "reading_history",
                "arguments": {"limit": 10, "event": "all"},
            },
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200
    call_data = resp.json()
    assert "result" in call_data
    content = call_data["result"].get("content", [])
    for block in content:
        text = block.get("text", "")
        assert a.unique_keyword not in text
        assert a.html_item_id not in text


@pytest.mark.asyncio
async def test_mcp_related_items_isolation(harness: IsolationHarness):
    """MCP related_items as Tenant B targeting Tenant A's item fails."""
    a = harness.tenant_a
    b = harness.tenant_b

    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 304,
            "method": "tools/call",
            "params": {
                "name": "related_items",
                "arguments": {"item_id": a.html_item_id, "limit": 5},
            },
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data or a.unique_keyword not in str(data)
    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before


@pytest.mark.asyncio
async def test_mcp_save_url_v2_isolation(harness: IsolationHarness):
    """MCP save_url v2 as Tenant B saves to B and does not touch Tenant A."""
    a = harness.tenant_a
    b = harness.tenant_b

    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 305,
            "method": "tools/call",
            "params": {
                "name": "save_url",
                "arguments": {
                    "url": "https://example.com/saved-by-b",
                    "tags": ["test"],
                    "agent_summary": "Summary",
                    "why_saved": "Reason",
                    "source_context": "Context",
                    "saved_by": "test-agent",
                },
            },
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200
    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before


@pytest.mark.asyncio
async def test_mcp_save_note_v2_isolation(harness: IsolationHarness):
    """MCP save_note v2 as Tenant B saves to B and does not touch Tenant A."""
    a = harness.tenant_a
    b = harness.tenant_b

    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 306,
            "method": "tools/call",
            "params": {
                "name": "save_note",
                "arguments": {
                    "title": "B Note",
                    "content": "Note content",
                    "tags": ["test"],
                    "agent_summary": "Summary",
                    "why_saved": "Reason",
                    "source_context": "Context",
                    "saved_by": "test-agent",
                },
            },
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200
    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before


# Every tool added for MCP v3 (direct lookup + tag management). "ITEM" is
# replaced with Tenant A's item id, which Tenant B must never touch.
NEW_MCP_TOOLS = [
    ("find_item", {"title": "Title"}),
    ("list_tags", {}),
    ("create_tag", {"name": "iso-tag"}),
    ("describe_tag", {"name": "tag1"}),
    ("update_tag", {"name": "tag1", "description": "hijack"}),
    ("rename_tag", {"old": "tag1", "new": "iso-renamed"}),
    ("merge_tags", {"sources": ["tag1"], "target": "iso-merged"}),
    ("delete_tag", {"name": "tag1", "dry_run": False}),
    ("tag_items", {"item_ids": "ITEM", "add": ["hijack"], "dry_run": False}),
    ("retag_by_query", {"query": "html", "add": ["hijack"], "dry_run": False}),
    ("set_item_tags", {"item_id": "ITEM", "tags": ["hijack"]}),
    ("suggest_tags", {"item_id": "ITEM"}),
    ("search_tags", {"query": "tag"}),
    ("tag_stats", {}),
    ("related_tags", {"tag": "tag1"}),
    ("tag_cleanup", {}),
]


@pytest.mark.parametrize("tool_name,arguments", NEW_MCP_TOOLS)
@pytest.mark.asyncio
async def test_mcp_new_tools_isolation(
    harness: IsolationHarness, tool_name: str, arguments: dict[str, Any]
):
    """Each new MCP tool, called as Tenant B, never touches Tenant A."""
    a = harness.tenant_a
    b = harness.tenant_b

    args = {
        key: (a.html_item_id if value == "ITEM" else value)
        for key, value in arguments.items()
    }

    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 400,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": args},
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200, resp.text
    text = resp.text
    assert a.unique_keyword not in text, f"{tool_name} leaked A's keyword"
    assert a.html_item_id not in text, f"{tool_name} leaked A's item id"

    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before, f"{tool_name} modified Tenant A"


@pytest.mark.asyncio
async def test_mcp_find_item_isolation(harness: IsolationHarness):
    """MCP find_item as Tenant B never returns Tenant A's items."""
    a = harness.tenant_a
    b = harness.tenant_b

    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 401,
            "method": "tools/call",
            "params": {
                "name": "find_item",
                "arguments": {"title": f"Title {a.unique_keyword}"},
            },
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "result" in data
    content = data["result"].get("content", [])
    for block in content:
        text = block.get("text", "")
        assert a.unique_keyword not in text
        assert a.html_item_id not in text


@pytest.mark.asyncio
async def test_mcp_tag_filters_isolation(harness: IsolationHarness):
    """Tag filters as Tenant B never surface Tenant A's items."""
    a = harness.tenant_a
    b = harness.tenant_b

    for tool, args in (
        ("list_items", {"tags_all": ["tag1"], "limit": 50}),
        ("search_library", {"query": a.unique_keyword, "tags_any": ["tag1"]}),
        ("reading_history", {"tags_all": ["tag1"], "limit": 50}),
    ):
        resp = harness.client.post(
            "/api/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 402,
                "method": "tools/call",
                "params": {"name": tool, "arguments": args},
            },
            headers=b.auth_headers,
        )
        assert resp.status_code == 200
        text = resp.text
        assert a.unique_keyword not in text, f"{tool} leaked A's keyword"
        assert a.html_item_id not in text, f"{tool} leaked A's item id"
