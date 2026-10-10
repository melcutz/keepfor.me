# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Tenant isolation tests for search and storage subsystems.

Covers:
- test_search_isolation: FTS, hybrid, MCP search, and vector search
- test_blob_isolation: get_item_clean_html and deletion blob key safety
- test_search_with_foreign_ids: vector search returning foreign IDs is filtered out
"""

from __future__ import annotations

import pytest

from keepfor.models.items import delete_item, get_item_clean_html
from keepfor.search.engine import hybrid_search, search_vectorize
from keepfor.spi import TenantScope
from tests.isolation.conftest import IsolationHarness, snapshot


@pytest.mark.asyncio
async def test_search_isolation(harness: IsolationHarness):
    """FTS and hybrid search as B for a term present only in A's items

    returns zero results.

    Tests:
    1. GET /search?q=<A_keyword> as B returns 0 of A's items
    2. POST /api/search with <A_keyword> as B returns 0 items
    3. MCP tool 'search' with <A_keyword> as B returns 0 items
    4. Python hybrid_search as B returns 0 items
    5. Vector search with query vector identical to A's vector returns no A items
       (both with namespace and with namespace=None).
    """
    a = harness.tenant_a
    b = harness.tenant_b

    # 1. UI Search: POST /search as B
    resp_ui = harness.client.post(
        "/search", data={"query": a.unique_keyword}, cookies=b.cookies
    )
    assert resp_ui.status_code == 200
    assert a.html_item_id not in resp_ui.text
    assert f"Title {a.unique_keyword}" not in resp_ui.text
    assert f"https://example.com/html-{a.user['id']}" not in resp_ui.text

    # 2. REST API Search: POST /api/search as B
    resp_api = harness.client.post(
        "/api/search",
        json={"query": a.unique_keyword, "mode": "hybrid"},
        headers=b.auth_headers,
    )
    assert resp_api.status_code == 200
    api_items = resp_api.json()
    assert len(api_items) == 0

    # Also test mode='keyword' explicitly
    resp_fts = harness.client.post(
        "/api/search",
        json={"query": a.unique_keyword, "mode": "keyword"},
        headers=b.auth_headers,
    )
    assert resp_fts.status_code == 200
    assert len(resp_fts.json()) == 0

    # 3. MCP tool 'search' as B
    mcp_resp = harness.client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search",
                "arguments": {"query": a.unique_keyword},
            },
        },
        headers=b.auth_headers,
    )
    assert mcp_resp.status_code == 200
    mcp_data = mcp_resp.json()
    assert "result" in mcp_data
    content = mcp_data["result"].get("content", [])
    for block in content:
        text = block.get("text", "")
        assert a.html_item_id not in text
        assert a.unique_keyword not in text
        import json as _json

        parsed = _json.loads(text)
        assert len(parsed) == 0

    # 4. Direct hybrid_search Python call as B
    items, total = await hybrid_search(
        b.db, harness.env, b.user["id"], a.unique_keyword, scope=b.scope
    )
    assert total == 0
    assert len(items) == 0

    # 5. Vector search isolation:
    # A's item has vectors with value `a.vector_val`
    # (a) With namespace (b.scope has vector_namespace='user_b')
    results_namespaced = await search_vectorize(
        harness.env,
        b.user["id"],
        "dummy vector query",
        limit=10,
        scope=b.scope,
    )
    for res in results_namespaced:
        assert res.get("item_id") != a.vector_item_id

    # (b) Without namespace (vector_namespace=None, relying on metadata user_id filter)
    unscoped_scope = TenantScope(
        tenant_id=b.user["id"],
        user_id=b.user["id"],
        db=b.db,
        bucket=harness.env.BUCKET,
        index=harness.env.VECTORIZE,
        blob_prefix="",
        vector_namespace=None,
    )
    results_unscoped = await search_vectorize(
        harness.env,
        b.user["id"],
        "dummy vector query",
        limit=10,
        scope=unscoped_scope,
    )
    for res in results_unscoped:
        assert res.get("item_id") != a.vector_item_id


@pytest.mark.asyncio
async def test_blob_isolation(harness: IsolationHarness):
    """get_item_clean_html for A's item id as B returns None;

    deleting B's items never deletes A's keys.
    """
    a = harness.tenant_a
    b = harness.tenant_b

    # 1. get_item_clean_html for Tenant A's item as Tenant B returns None
    html_as_b = await get_item_clean_html(
        b.db, harness.env, b.user["id"], a.html_item_id, scope=b.scope
    )
    assert html_as_b is None

    # Verify A can still read their own clean HTML
    html_as_a = await get_item_clean_html(
        a.db, harness.env, a.user["id"], a.html_item_id, scope=a.scope
    )
    assert html_as_a is not None
    assert a.unique_keyword in html_as_a

    # Snapshot A before B's deletion
    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    a_blob_key = a.scope.blobs(harness.env).key("items", a.html_item_id, "clean.html")

    # 2. Delete B's items via API and model
    resp_del = harness.client.delete(
        f"/api/items/{b.html_item_id}", headers=b.auth_headers
    )
    assert resp_del.status_code in (200, 204)

    # Also delete remaining B items via delete_item
    await delete_item(b.db, harness.env, b.user["id"], b.vector_item_id, scope=b.scope)
    await delete_item(b.db, harness.env, b.user["id"], b.pinned_item_id, scope=b.scope)

    # Assert FakeR2Bucket.deletes never contains A's keys
    bucket_deletes = getattr(harness.env.BUCKET, "deletes", [])
    assert a_blob_key not in bucket_deletes

    # Assert A's blob still exists in R2
    a_blob_content = await a.scope.blobs(harness.env).get_text(a_blob_key)
    assert a_blob_content is not None
    assert a.unique_keyword in a_blob_content

    # Assert Snapshot A is completely unchanged
    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_a_after == snap_a_before


@pytest.mark.asyncio
async def test_search_with_foreign_ids(harness: IsolationHarness):
    """Even if vector search returns foreign tenant IDs, hybrid_search

    filters them out via D1 user_id scoping.
    """
    from unittest.mock import patch

    a = harness.tenant_a
    b = harness.tenant_b

    # Mock search_vectorize to simulate Vectorize returning tenant A's item ID
    foreign_vector_results = [
        {"item_id": a.html_item_id, "score": 0.99},
        {"item_id": a.pinned_item_id, "score": 0.95},
    ]

    with patch(
        "keepfor.search.engine.search_vectorize",
        return_value=foreign_vector_results,
    ):
        items, total = await hybrid_search(
            b.db, harness.env, b.user["id"], "anything", scope=b.scope
        )

    # All foreign IDs must be filtered out because they do not belong to tenant B
    assert len(items) == 0
    assert total == 0
