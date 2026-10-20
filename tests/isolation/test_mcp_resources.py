# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Tenant isolation tests for MCP resources and prompts.

A resource or a prompt may only ever reach the caller's own library:
reading another tenant's item or tag must fail, listing must not leak,
and no prompt may quote another tenant's items.
"""

from __future__ import annotations

import json

import pytest

from tests.isolation.conftest import IsolationHarness, snapshot


async def _call(harness: IsolationHarness, method: str, params: dict):
    resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 500,
            "method": method,
            "params": params,
        },
        headers=harness.tenant_b.auth_headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_mcp_resource_read_item_isolation(harness: IsolationHarness):
    """Reading Tenant A's item as Tenant B fails and leaks nothing."""
    a = harness.tenant_a
    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    data = await _call(
        harness, "resources/read", {"uri": f"keepfor://item/{a.html_item_id}"}
    )

    assert "error" in data, data
    assert data["error"]["data"]["type"] == "not_found"
    assert a.unique_keyword not in str(data)
    assert a.html_item_id not in str(data)

    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before


@pytest.mark.asyncio
async def test_mcp_resource_read_tag_isolation(harness: IsolationHarness):
    """Reading a tag only Tenant A owns fails for Tenant B."""
    a = harness.tenant_a
    a_only_tag = f"tag_{a.user['id'][:6]}"
    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    data = await _call(
        harness, "resources/read", {"uri": f"keepfor://tag/{a_only_tag}"}
    )

    assert "error" in data, data
    assert data["error"]["data"]["type"] == "not_found"
    assert a_only_tag not in str(data)

    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before


@pytest.mark.asyncio
async def test_mcp_resource_read_shared_tag_isolation(harness: IsolationHarness):
    """A tag name both tenants own lists only the caller's own items."""
    a = harness.tenant_a
    b = harness.tenant_b
    shared_tag = a.tag_names[0]
    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    data = await _call(
        harness, "resources/read", {"uri": f"keepfor://tag/{shared_tag}"}
    )

    assert "result" in data, data
    payload = json.loads(data["result"]["contents"][0]["text"])
    assert payload["name"] == shared_tag
    listed = {i["id"] for i in payload["items"]}
    # Tenant B sees its own items with the shared tag, never Tenant A's.
    assert listed
    assert listed <= set(b.item_ids)
    assert listed.isdisjoint(a.item_ids)
    assert a.unique_keyword not in str(data)

    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before


@pytest.mark.asyncio
async def test_mcp_resources_list_isolation(harness: IsolationHarness):
    """resources/list as Tenant B never surfaces Tenant A's data."""
    a = harness.tenant_a
    b = harness.tenant_b
    a_only_tag = f"tag_{a.user['id'][:6]}"

    data = await _call(harness, "resources/list", {})
    assert "result" in data, data
    uris = [r["uri"] for r in data["result"]["resources"]]

    for a_id in a.item_ids:
        assert f"keepfor://item/{a_id}" not in uris
    assert f"keepfor://tag/{a_only_tag}" not in uris
    assert a.unique_keyword not in str(data)

    # Tenant B still sees its own library.
    for b_id in b.item_ids:
        assert f"keepfor://item/{b_id}" in uris


@pytest.mark.asyncio
async def test_mcp_prompt_research_brief_isolation(harness: IsolationHarness):
    """A research brief for Tenant B never quotes Tenant A's items."""
    a = harness.tenant_a
    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    data = await _call(
        harness,
        "prompts/get",
        {"name": "research_brief", "arguments": {"topic": a.unique_keyword}},
    )

    assert "result" in data, data
    text = str(data)
    # The topic is echoed back (the caller asked for it), but no item of
    # Tenant A may show up.
    assert f"Title {a.unique_keyword}" not in text
    for a_id in a.item_ids:
        assert a_id not in text

    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before, "research_brief modified Tenant A"


@pytest.mark.asyncio
async def test_mcp_prompt_weekly_review_isolation(harness: IsolationHarness):
    """A weekly review for Tenant B never quotes Tenant A's items."""
    a = harness.tenant_a
    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    data = await _call(
        harness, "prompts/get", {"name": "weekly_review", "arguments": {}}
    )

    assert "result" in data, data
    text = str(data)
    assert a.unique_keyword not in text
    for a_id in a.item_ids:
        assert a_id not in text

    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before
