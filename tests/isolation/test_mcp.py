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
                    if "trashed" in parsed:
                        assert parsed["trashed"] is False, (
                            f"Tool {tool_name} claimed to trash A's item!"
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
    """MCP delete_item as Tenant B targeting Tenant A's item does not trash
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
                "arguments": {"item_id": a.html_item_id},
            },
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert (
        "error" in data
        or json.loads(data["result"]["content"][0]["text"])["trashed"] is False
    )
    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before


@pytest.mark.asyncio
async def test_mcp_delete_item_trashes_the_item(harness: IsolationHarness):
    """MCP delete_item on one's own item moves it to the Trash.

    The row, the R2 snapshot and the vector stay: the item can be restored.
    """
    a = harness.tenant_a

    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 210,
            "method": "tools/call",
            "params": {
                "name": "delete_item",
                "arguments": {"item_id": a.html_item_id},
            },
        },
        headers=a.auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "error" not in data, data
    assert json.loads(data["result"]["content"][0]["text"])["trashed"] is True

    row = await a.db.query_first(
        "SELECT deleted_at FROM items WHERE id = ? AND user_id = ?;",
        (a.html_item_id, a.user["id"]),
    )
    assert row is not None, "the item row must survive a trash"
    assert row["deleted_at"], "the item must be marked as trashed"

    # The snapshot and the vector are untouched, so the item is restorable.
    snap = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert "_r2" in snap
    assert "_vectorize" in snap


@pytest.mark.asyncio
async def test_trashed_item_is_invisible_to_mcp(harness: IsolationHarness):
    """A trashed item leaves the library: it is not listed or searchable."""
    a = harness.tenant_a

    await a.db.execute(
        "UPDATE items SET deleted_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?;",
        (a.html_item_id, a.user["id"]),
    )

    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 211,
            "method": "tools/call",
            "params": {"name": "list_items", "arguments": {}},
        },
        headers=a.auth_headers,
    )
    assert resp.status_code == 200
    text = resp.json()["result"]["content"][0]["text"]
    assert a.html_item_id not in text
    assert a.unique_keyword not in text


@pytest.mark.asyncio
async def test_mcp_list_trash_isolation(harness: IsolationHarness):
    """MCP list_trash as Tenant B never returns Tenant A's trashed items."""
    a = harness.tenant_a
    b = harness.tenant_b

    await a.db.execute(
        "UPDATE items SET deleted_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?;",
        (a.html_item_id, a.user["id"]),
    )

    resp_b = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 212,
            "method": "tools/call",
            "params": {"name": "list_trash", "arguments": {}},
        },
        headers=b.auth_headers,
    )
    assert resp_b.status_code == 200
    text_b = resp_b.json()["result"]["content"][0]["text"]
    assert a.html_item_id not in text_b
    assert a.unique_keyword not in text_b

    resp_a = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 213,
            "method": "tools/call",
            "params": {"name": "list_trash", "arguments": {}},
        },
        headers=a.auth_headers,
    )
    assert resp_a.status_code == 200
    data_a = json.loads(resp_a.json()["result"]["content"][0]["text"])
    trashed_ids = [item["id"] for item in data_a["items"]]
    assert a.html_item_id in trashed_ids


@pytest.mark.asyncio
async def test_mcp_restore_item_isolation(harness: IsolationHarness):
    """MCP restore_item as Tenant B targeting Tenant A's trashed item returns not_found
    and does not restore Tenant A's item.
    """
    a = harness.tenant_a
    b = harness.tenant_b

    await a.db.execute(
        "UPDATE items SET deleted_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?;",
        (a.html_item_id, a.user["id"]),
    )

    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 214,
            "method": "tools/call",
            "params": {
                "name": "restore_item",
                "arguments": {"item_ids": [a.html_item_id]},
            },
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200
    res_data = json.loads(resp.json()["result"]["content"][0]["text"])
    assert a.html_item_id in res_data["not_found"]
    assert res_data["restored"] == []

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
    ("find_cleanup", {}),
    ("library_digest", {"tag": "tag1"}),
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
async def test_mcp_bulk_update_isolation(harness: IsolationHarness):
    """MCP bulk_update as Tenant B never touches Tenant A's items.

    Both tenants own items tagged 'tag1', so the filter matches for B too:
    the update must land on B's items only.
    """
    a = harness.tenant_a
    b = harness.tenant_b

    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 403,
            "method": "tools/call",
            "params": {
                "name": "bulk_update",
                "arguments": {
                    "tags_all": ["tag1"],
                    "add_tags": ["hijack"],
                    "pin": True,
                    "set_status": "failed",
                    "dry_run": False,
                },
            },
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert "error" not in resp.json(), resp.json()["error"]
    assert a.unique_keyword not in resp.text
    assert a.html_item_id not in resp.text

    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before, "bulk_update modified Tenant A"

    # Tenant A's items are untouched down to their status and tags.
    a_statuses = await a.db.query_all(
        "SELECT status FROM items WHERE user_id = ?;", (a.user["id"],)
    )
    assert {r["status"] for r in a_statuses} == {"queued"}
    a_hijack = await a.db.query_all(
        "SELECT id FROM tags WHERE user_id = ? AND name = 'hijack';", (a.user["id"],)
    )
    assert a_hijack == []

    # Tenant B's own items were updated: the call was not a silent no-op.
    b_rows = await b.db.query_all(
        "SELECT id, status, is_pinned FROM items"
        " WHERE user_id = ? AND status = 'failed';",
        (b.user["id"],),
    )
    assert len(b_rows) == 2
    assert all(r["is_pinned"] == 1 for r in b_rows)
    b_hijack = await b.db.query_all(
        "SELECT it.item_id FROM item_tags it JOIN tags t ON t.id = it.tag_id"
        " WHERE t.user_id = ? AND t.name = 'hijack';",
        (b.user["id"],),
    )
    assert len(b_hijack) == 2


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


@pytest.mark.asyncio
async def test_mcp_find_cleanup_isolation(harness: IsolationHarness):
    """MCP find_cleanup as Tenant B never returns Tenant A's items or tags."""
    a = harness.tenant_a
    b = harness.tenant_b

    # Give A one candidate per category: a duplicate pair, a dead link, an
    # untagged item and a near-duplicate tag pair.
    for item_id, url in (
        ("aaa-dupe-1", "https://dup-a.test/page"),
        ("aaa-dupe-2", "https://www.dup-a.test/page"),
        ("aaa-dead-1", "https://dead-a.test/page"),
        ("aaa-untagged-1", "https://untagged-a.test/page"),
    ):
        await a.db.execute(
            "INSERT INTO items (id, user_id, url, canonical_url, status, title)"
            " VALUES (?, ?, ?, ?, 'queued', ?);",
            (item_id, a.user["id"], url, url, "Title A"),
        )
    await a.db.execute(
        "UPDATE items SET status = 'failed', fail_reason = 'HTTP 404' WHERE id = ?;",
        ("aaa-dead-1",),
    )
    for tag in ("machine-learning", "machine_learning"):
        await a.db.execute(
            "INSERT INTO tags (id, user_id, name) VALUES (?, ?, ?);",
            (f"aaa-tag-{tag}", a.user["id"], tag),
        )

    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 403,
            "method": "tools/call",
            "params": {"name": "find_cleanup", "arguments": {}},
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200, resp.text
    text = resp.text
    for leaked in (
        "aaa-dupe-1",
        "aaa-dupe-2",
        "aaa-dead-1",
        "aaa-untagged-1",
        "machine-learning",
        "machine_learning",
        "dup-a.test",
        "dead-a.test",
        "untagged-a.test",
    ):
        assert leaked not in text, f"find_cleanup leaked {leaked}"

    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before, "find_cleanup modified Tenant A"


@pytest.mark.asyncio
async def test_mcp_library_digest_isolation(harness: IsolationHarness):
    """MCP library_digest as Tenant B never reports Tenant A's library."""
    a = harness.tenant_a
    b = harness.tenant_b
    a_only_tag_name = f"tag_{a.user['id'][:6]}"

    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 405,
            "method": "tools/call",
            "params": {
                "name": "library_digest",
                # A tag Tenant A owns: B's digest must stay on B's items.
                "arguments": {"tag": "tag1"},
            },
        },
        headers=b.auth_headers,
    )
    assert resp.status_code == 200, resp.text
    text = resp.text
    assert a.unique_keyword not in text, "library_digest leaked A's keyword"
    assert a.html_item_id not in text, "library_digest leaked A's item id"

    payload = json.loads(resp.json()["result"]["content"][0]["text"])
    assert payload["saves"] == 2, "library_digest counted another tenant's saves"
    for highlight in payload["unread_highlights"]:
        assert highlight["id"] not in a.item_ids
        assert highlight["id"] in b.item_ids
    assert a_only_tag_name not in {t["name"] for t in payload["top_tags"]}

    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before, "library_digest modified Tenant A"


@pytest.mark.asyncio
async def test_mcp_library_digest_tag_filter_isolated(harness: IsolationHarness):
    """A tag that only exists for Tenant A resolves to nothing for Tenant B.

    Each seeded tenant owns a private tag (tag_<user id prefix>), so this
    catches a tag filter that leaks across users.
    """
    a = harness.tenant_a
    b = harness.tenant_b
    a_only_tag = f"tag_{a.user['id'][:6]}"
    b_only_tag = f"tag_{b.user['id'][:6]}"

    def _digest(tag: str) -> dict[str, Any]:
        resp = harness.client.post(
            "/api/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 406,
                "method": "tools/call",
                "params": {
                    "name": "library_digest",
                    "arguments": {"tag": tag},
                },
            },
            headers=b.auth_headers,
        )
        assert resp.status_code == 200, resp.text
        return json.loads(resp.json()["result"]["content"][0]["text"])

    # B's own private tag: exactly one of B's items carries it.
    own = _digest(b_only_tag)
    assert own["saves"] == 1
    assert [h["id"] for h in own["unread_highlights"]] == [b.html_item_id]

    # A's private tag must resolve to nothing at all for B.
    other = _digest(a_only_tag)
    assert other["saves"] == 0
    assert other["unread"] == 0
    assert other["opens"] == 0
    assert other["unread_highlights"] == []
    assert other["top_tags"] == []


# ==========================================
# Highlights and collections (GitHub issue #24)
# ==========================================


async def _call_as_tenant(
    harness: IsolationHarness, tenant, tool, arguments, req_id=500
):
    """Calls one MCP tool as the given tenant and returns the response."""
    return harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        headers=tenant.auth_headers,
    )


@pytest.mark.asyncio
async def test_mcp_highlights_isolation(harness: IsolationHarness):
    """Tenant B can neither read nor change Tenant A's highlights."""
    a = harness.tenant_a
    b = harness.tenant_b

    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    # B cannot highlight A's item.
    resp = await _call_as_tenant(
        harness,
        b,
        "create_highlight",
        {"item_id": a.html_item_id, "text": "hijack"},
    )
    assert resp.status_code == 200
    assert "error" in resp.json()
    assert a.html_item_id not in resp.text

    # B cannot read A's highlights.
    resp = await _call_as_tenant(
        harness, b, "list_highlights", {"item_id": a.html_item_id}
    )
    assert resp.status_code == 200
    assert a.unique_keyword not in resp.text
    assert a.html_item_id not in resp.text

    # B cannot delete A's highlight.
    row = await a.db.query_first(
        "SELECT id FROM highlights WHERE user_id = ? AND item_id = ?;",
        (a.user["id"], a.html_item_id),
    )
    assert row is not None
    resp = await _call_as_tenant(
        harness, b, "delete_highlight", {"highlight_id": row["id"]}
    )
    assert resp.status_code == 200
    assert "error" in resp.json()
    assert row["id"] not in resp.text

    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before, "highlights of Tenant A were modified"


@pytest.mark.asyncio
async def test_mcp_collections_isolation(harness: IsolationHarness):
    """Tenant B can neither see nor change Tenant A's collections."""
    a = harness.tenant_a
    b = harness.tenant_b

    row = await a.db.query_first(
        "SELECT name FROM collections WHERE user_id = ?;", (a.user["id"],)
    )
    assert row is not None
    collection_name = row["name"]

    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    # B does not see A's collections.
    resp = await _call_as_tenant(harness, b, "list_collections", {})
    assert resp.status_code == 200
    assert collection_name not in resp.text
    assert a.html_item_id not in resp.text

    # B cannot touch A's collection in any way.
    for tool, arguments in (
        (
            "add_to_collection",
            {"collection": collection_name, "item_ids": [b.item_ids[0]]},
        ),
        (
            "remove_from_collection",
            {"collection": collection_name, "item_ids": [a.item_ids[0]]},
        ),
        ("rename_collection", {"collection": collection_name, "new_name": "hijacked"}),
        ("delete_collection", {"collection": collection_name}),
    ):
        resp = await _call_as_tenant(harness, b, tool, arguments)
        assert resp.status_code == 200
        assert "error" in resp.json(), f"{tool} did not refuse a foreign collection"
        assert collection_name not in resp.text, f"{tool} leaked A's collection"
        assert a.html_item_id not in resp.text, f"{tool} leaked A's item id"

    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before, "collections of Tenant A were modified"


@pytest.mark.asyncio
async def test_mcp_collection_items_stay_tenant_scoped(harness: IsolationHarness):
    """A collection never holds an item that belongs to somebody else."""
    a = harness.tenant_a
    b = harness.tenant_b

    resp = await _call_as_tenant(
        harness, b, "create_collection", {"name": "B collection"}
    )
    assert resp.status_code == 200
    assert "error" not in resp.json()

    resp = await _call_as_tenant(
        harness,
        b,
        "add_to_collection",
        {"collection": "B collection", "item_ids": [a.html_item_id]},
    )
    assert resp.status_code == 200
    data = resp.json()
    if "result" in data:
        payload = json.loads(data["result"]["content"][0]["text"])
        assert payload["added"] == 0
        assert payload["item_count"] == 0
    assert a.html_item_id not in resp.text

    links = await b.db.query_all(
        "SELECT ci.item_id FROM collection_items ci JOIN collections c"
        " ON c.id = ci.collection_id WHERE c.user_id = ? AND ci.item_id = ?;",
        (b.user["id"], a.html_item_id),
    )
    assert links == []
