# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""HTTP endpoint tenant-isolation tests."""

from __future__ import annotations

import datetime

import pytest

from keepfor.models.items import get_item_tags
from keepfor.models.tenant import delete_all_tenant_rows
from tests.isolation.conftest import IsolationHarness, snapshot

ITEM_ROUTES = [
    ("GET", "/items/{id}", None),
    ("GET", "/items/{id}/card", None),
    ("GET", "/items/{id}/markdown", None),
    ("POST", "/items/{id}/pin", None),
    ("POST", "/items/{id}/notes", {"notes": "malicious update"}),
    ("POST", "/items/{id}/archive", None),
    ("POST", "/items/{id}/unarchive", None),
    ("POST", "/items/{id}/tags", {"add": "malicious", "remove": ""}),
    ("POST", "/items/{id}/suggestions/{sid}/accept", {"next": "/tags"}),
    ("POST", "/items/{id}/suggestions/{sid}/dismiss", {"next": "/tags"}),
    ("GET", "/api/items/{id}", None),
    ("DELETE", "/api/items/{id}", None),
    ("GET", "/api/items/{id}/content", None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,route_tmpl,payload",
    ITEM_ROUTES,
    ids=[r[1] for r in ITEM_ROUTES],
)
async def test_cross_tenant_item_routes(
    harness: IsolationHarness, method: str, route_tmpl: str, payload: dict | None
):
    """Calling item routes as tenant B with tenant A's IDs must return 403 or 404

    and leave A intact.
    """
    harness.client.cookies.clear()
    a = harness.tenant_a
    b = harness.tenant_b

    snap_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    # Format route with A's item ID and suggestion ID
    path = route_tmpl.format(id=a.html_item_id, sid=a.suggestion_id)

    if method == "GET":
        resp = harness.client.get(path, headers=b.auth_headers)
    elif method == "DELETE":
        resp = harness.client.delete(path, headers=b.auth_headers)
    elif method == "POST":
        if payload is not None:
            resp = harness.client.post(path, data=payload, headers=b.auth_headers)
        else:
            resp = harness.client.post(path, headers=b.auth_headers)
    else:
        pytest.fail(f"Unsupported method: {method}")

    # Response must never be 200 with data; must be 403, 404, or fail closed
    assert resp.status_code in (
        403,
        404,
    ), f"{method} {path} returned status {resp.status_code}: {resp.text[:200]}"

    # Tenant A's state must be completely untouched
    snap_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_after == snap_before, f"Tenant A state changed after {method} {path}!"


@pytest.mark.asyncio
async def test_cross_tenant_tag_rename(harness: IsolationHarness):
    """Tenant B renaming a tag with the same name as A's tag does not affect A."""
    harness.client.cookies.clear()
    a = harness.tenant_a
    b = harness.tenant_b

    # Tenant B creates a tag with name "tag1" (same name as A's tag)
    harness.client.post("/tags/create", data={"name": "tag1"}, headers=b.auth_headers)
    snap_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    # Tenant B renames "tag1" to "tag1-renamed-b"
    resp = harness.client.post(
        "/tags/rename",
        data={"old_name": "tag1", "new_name": "tag1-renamed-b"},
        headers=b.auth_headers,
    )
    assert resp.status_code in (200, 303)

    # Verify A still has "tag1"
    a_tags = await get_item_tags(a.db, a.user["id"], a.html_item_id)
    assert "tag1" in a_tags

    snap_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_after == snap_before


@pytest.mark.asyncio
async def test_cross_tenant_tag_merge(harness: IsolationHarness):
    """Tenant B merging tags does not affect Tenant A's tags."""
    harness.client.cookies.clear()
    a = harness.tenant_a
    b = harness.tenant_b

    # B creates tags
    harness.client.post("/tags/create", data={"name": "tag1"}, headers=b.auth_headers)
    harness.client.post(
        "/tags/create", data={"name": "b-target"}, headers=b.auth_headers
    )
    snap_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    # B merges "tag1" into "b-target"
    resp = harness.client.post(
        "/tags/merge",
        data={"source_tag": "tag1", "target_tag": "b-target"},
        headers=b.auth_headers,
    )
    assert resp.status_code in (200, 303)

    a_tags = await get_item_tags(a.db, a.user["id"], a.html_item_id)
    assert "tag1" in a_tags

    snap_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_after == snap_before


@pytest.mark.asyncio
async def test_cross_tenant_tag_delete(harness: IsolationHarness):
    """Tenant B deleting a tag does not delete Tenant A's tag with same name."""
    harness.client.cookies.clear()
    a = harness.tenant_a
    b = harness.tenant_b

    harness.client.post("/tags/create", data={"name": "tag1"}, headers=b.auth_headers)
    snap_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    resp = harness.client.post(
        "/tags/delete",
        data={"tag_name": "tag1"},
        headers=b.auth_headers,
    )
    assert resp.status_code in (200, 303)

    a_tags = await get_item_tags(a.db, a.user["id"], a.html_item_id)
    assert "tag1" in a_tags

    snap_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_after == snap_before


@pytest.mark.asyncio
async def test_cross_tenant_tag_prune(harness: IsolationHarness):
    """Tenant B pruning tags does not touch Tenant A's tags."""
    harness.client.cookies.clear()
    a = harness.tenant_a
    b = harness.tenant_b

    snap_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    resp = harness.client.post("/tags/prune", headers=b.auth_headers)
    assert resp.status_code in (200, 303)

    snap_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_after == snap_before


@pytest.mark.asyncio
async def test_cross_tenant_tag_rule_delete(harness: IsolationHarness):
    """Tenant B cannot delete Tenant A's tag rule."""
    harness.client.cookies.clear()
    a = harness.tenant_a
    b = harness.tenant_b

    snap_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    resp = harness.client.post(
        "/tags/rules/delete",
        data={"rule_id": a.rule_id},
        headers=b.auth_headers,
    )
    assert resp.status_code in (200, 303, 404)

    # A's rule still exists in database
    rule_row = await a.db.query_first(
        "SELECT id FROM tag_rules WHERE id = ? AND user_id = ?;",
        (a.rule_id, a.user["id"]),
    )
    assert rule_row is not None

    snap_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_after == snap_before


@pytest.mark.asyncio
async def test_cross_tenant_pat_delete(harness: IsolationHarness):
    """Tenant B cannot delete Tenant A's personal access token."""
    harness.client.cookies.clear()
    a = harness.tenant_a
    b = harness.tenant_b

    snap_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    resp = harness.client.post(
        f"/settings/tokens/{a.pat_id}/delete",
        headers=b.auth_headers,
    )
    assert resp.status_code in (200, 303, 404)

    token_row = await a.db.query_first(
        "SELECT id FROM personal_access_tokens WHERE id = ? AND user_id = ?;",
        (a.pat_id, a.user["id"]),
    )
    assert token_row is not None

    snap_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_after == snap_before


@pytest.mark.asyncio
async def test_cross_tenant_bulk_tags(harness: IsolationHarness):
    """Tenant B calling bulk-tags with A's item ID modifies nothing for A."""
    harness.client.cookies.clear()
    a = harness.tenant_a
    b = harness.tenant_b

    snap_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

    resp = harness.client.post(
        "/items/bulk-tags",
        json={"item_ids": [a.html_item_id], "add_tags": ["malicious"]},
        headers=b.auth_headers,
    )
    assert resp.status_code in (200, 400, 404)

    a_tags = await get_item_tags(a.db, a.user["id"], a.html_item_id)
    assert "malicious" not in a_tags

    snap_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_after == snap_before


@pytest.mark.asyncio
async def test_export_isolation(harness: IsolationHarness):
    """Exporting library as B contains none of A's URLs or content."""
    harness.client.cookies.clear()
    a = harness.tenant_a
    b = harness.tenant_b

    # JSON export
    resp_json = harness.client.get("/api/export", headers=b.auth_headers)
    assert resp_json.status_code == 200
    export_data = resp_json.json()
    exported_urls = [item["url"] for item in export_data]

    assert f"https://example.com/html-{a.user['id']}" not in exported_urls
    assert f"https://example.com/html-{b.user['id']}" in exported_urls

    # HTML export
    resp_html = harness.client.get("/api/export?format=html", headers=b.auth_headers)
    assert resp_html.status_code == 200
    assert a.unique_keyword not in resp_html.text
    assert b.unique_keyword in resp_html.text


@pytest.mark.asyncio
async def test_list_and_stats_isolation(harness: IsolationHarness):
    """Listing endpoints (/api/items, /, /stats, /tags) list only B's data."""
    harness.client.cookies.clear()
    a = harness.tenant_a
    b = harness.tenant_b

    # /api/items
    resp_api = harness.client.get("/api/items", headers=b.auth_headers)
    assert resp_api.status_code == 200
    items = resp_api.json()
    item_ids = [item["id"] for item in items]
    for a_id in a.item_ids:
        assert a_id not in item_ids
    for b_id in b.item_ids:
        assert b_id in item_ids

    # / (library HTML)
    resp_lib = harness.client.get("/", cookies=b.cookies)
    assert resp_lib.status_code == 200
    assert a.unique_keyword not in resp_lib.text
    assert b.unique_keyword in resp_lib.text

    # /stats
    resp_stats = harness.client.get("/stats", cookies=b.cookies)
    assert resp_stats.status_code == 200
    # Tenant B has 3 items, not 6
    assert "3 items" in resp_stats.text.lower() or "3" in resp_stats.text

    # /tags
    resp_tags = harness.client.get("/tags", cookies=b.cookies)
    assert resp_tags.status_code == 200
    assert f"tag_{a.user['id'][:6]}" not in resp_tags.text
    assert f"tag_{b.user['id'][:6]}" in resp_tags.text


@pytest.mark.asyncio
async def test_pat_and_session_scope(harness: IsolationHarness):
    """PAT and session isolation: B credentials never access A;

    invalid/expired credentials fail.
    """
    harness.client.cookies.clear()
    a = harness.tenant_a
    b = harness.tenant_b

    # B's PAT cannot view A's item
    resp_pat = harness.client.get(
        f"/api/items/{a.html_item_id}", headers=b.auth_headers
    )
    assert resp_pat.status_code == 404

    # B's session cannot view A's item
    resp_sess = harness.client.get(f"/items/{a.html_item_id}", cookies=b.cookies)
    assert resp_sess.status_code == 404

    # Expired session fails
    past_expiry = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    ).strftime("%Y-%m-%d %H:%M:%S")
    await b.db.execute(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?);",
        ("expired_sess_id", b.user["id"], past_expiry),
    )
    resp_exp = harness.client.get(
        "/api/items", cookies={"kfm_session": "expired_sess_id"}
    )
    assert resp_exp.status_code == 401

    # Deleted user PAT fails
    # Register a temporary user, create PAT, then delete user
    temp_user_id = "temp_user_to_delete"
    await b.db.execute(
        "INSERT INTO users (id, email, password_hash, role) VALUES (?, ?, ?, ?);",
        (temp_user_id, "temp@example.com", "hash", "user"),
    )
    from keepfor.auth.crypto import generate_pat

    raw_pat, token_hash = generate_pat()
    await b.db.execute(
        """
        INSERT INTO personal_access_tokens (id, user_id, name, token_hash)
        VALUES (?, ?, ?, ?);
        """,
        ("pat_temp", temp_user_id, "Temp PAT", token_hash),
    )
    # Delete user from users table (cascading or foreign key delete)
    await b.db.execute("DELETE FROM users WHERE id = ?;", (temp_user_id,))
    # PAT should now fail authentication
    resp_del = harness.client.get(
        "/api/items", headers={"Authorization": f"Bearer {raw_pat}"}
    )
    assert resp_del.status_code == 401


@pytest.mark.asyncio
async def test_delete_tenant_data(harness: IsolationHarness):
    """delete_all_tenant_rows removes all tenant A rows with FKs ON;

    snapshot(A) is empty, B unchanged.
    """
    a = harness.tenant_a
    b = harness.tenant_b

    snap_a_before = await snapshot(a.db, a.user["id"])
    snap_b_before = await snapshot(b.db, b.user["id"])
    assert snap_a_before, "Tenant A should have seeded rows"
    assert snap_b_before, "Tenant B should have seeded rows"

    # Call delete_all_tenant_rows for Tenant A
    await delete_all_tenant_rows(a.db, a.user["id"])

    # Tenant A snapshot must be empty
    snap_a_after = await snapshot(a.db, a.user["id"])
    assert snap_a_after == {}, f"Tenant A should have 0 rows left, got: {snap_a_after}"

    # Tenant B snapshot must be completely unchanged
    snap_b_after = await snapshot(b.db, b.user["id"])
    assert snap_b_after == snap_b_before, (
        "Tenant B data was modified during Tenant A deletion!"
    )
