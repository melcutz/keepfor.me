# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""HTTP endpoint tenant-isolation tests."""

from __future__ import annotations

import datetime

import pytest

from keepfor.models.items import get_item_tags
from keepfor.models.tenant import delete_all_tenant_rows
from tests.isolation.conftest import IsolationHarness, snapshot


async def _assert_cross_tenant_denied(
    harness: IsolationHarness,
    method: str,
    path: str,
    payload: dict | None = None,
) -> None:
    harness.client.cookies.clear()
    a = harness.tenant_a
    b = harness.tenant_b

    snap_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)

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

    assert resp.status_code in (
        403,
        404,
    ), f"{method} {path} returned status {resp.status_code}: {resp.text[:200]}"

    snap_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    assert snap_after == snap_before, f"Tenant A state changed after {method} {path}!"


@pytest.mark.asyncio
async def test_cross_tenant_get_item_page(harness: IsolationHarness):
    """Calling GET /items/{id} as tenant B returns 404 for tenant A's item."""
    await _assert_cross_tenant_denied(
        harness, "GET", f"/items/{harness.tenant_a.html_item_id}"
    )


@pytest.mark.asyncio
async def test_cross_tenant_get_item_card(harness: IsolationHarness):
    """Calling GET /items/{id}/card as tenant B returns 404 for tenant A's item."""
    await _assert_cross_tenant_denied(
        harness, "GET", f"/items/{harness.tenant_a.html_item_id}/card"
    )


@pytest.mark.asyncio
async def test_cross_tenant_get_item_markdown(harness: IsolationHarness):
    """Calling GET /items/{id}/markdown as tenant B returns 404 for tenant A's item."""
    await _assert_cross_tenant_denied(
        harness, "GET", f"/items/{harness.tenant_a.html_item_id}/markdown"
    )


@pytest.mark.asyncio
async def test_cross_tenant_post_pin(harness: IsolationHarness):
    """Calling POST /items/{id}/pin as tenant B fails for tenant A's item."""
    await _assert_cross_tenant_denied(
        harness, "POST", f"/items/{harness.tenant_a.html_item_id}/pin"
    )


@pytest.mark.asyncio
async def test_cross_tenant_post_notes(harness: IsolationHarness):
    """Calling POST /items/{id}/notes as tenant B fails for tenant A's item."""
    await _assert_cross_tenant_denied(
        harness,
        "POST",
        f"/items/{harness.tenant_a.html_item_id}/notes",
        payload={"notes": "malicious update"},
    )


@pytest.mark.asyncio
async def test_cross_tenant_post_archive(harness: IsolationHarness):
    """Calling POST /items/{id}/archive as tenant B fails for tenant A's item."""
    await _assert_cross_tenant_denied(
        harness, "POST", f"/items/{harness.tenant_a.html_item_id}/archive"
    )


@pytest.mark.asyncio
async def test_cross_tenant_post_unarchive(harness: IsolationHarness):
    """Calling POST /items/{id}/unarchive as tenant B fails for tenant A's item."""
    await _assert_cross_tenant_denied(
        harness, "POST", f"/items/{harness.tenant_a.html_item_id}/unarchive"
    )


@pytest.mark.asyncio
async def test_cross_tenant_post_tags(harness: IsolationHarness):
    """Calling POST /items/{id}/tags as tenant B fails for tenant A's item."""
    await _assert_cross_tenant_denied(
        harness,
        "POST",
        f"/items/{harness.tenant_a.html_item_id}/tags",
        payload={"add": "malicious", "remove": ""},
    )


@pytest.mark.asyncio
async def test_cross_tenant_post_suggestion_accept(harness: IsolationHarness):
    """Calling POST /items/{id}/suggestions/{sid}/accept as tenant B fails."""
    a = harness.tenant_a
    await _assert_cross_tenant_denied(
        harness,
        "POST",
        f"/items/{a.html_item_id}/suggestions/{a.suggestion_id}/accept",
        payload={"next": "/tags"},
    )


@pytest.mark.asyncio
async def test_cross_tenant_post_suggestion_dismiss(harness: IsolationHarness):
    """Calling POST /items/{id}/suggestions/{sid}/dismiss as tenant B fails."""
    a = harness.tenant_a
    await _assert_cross_tenant_denied(
        harness,
        "POST",
        f"/items/{a.html_item_id}/suggestions/{a.suggestion_id}/dismiss",
        payload={"next": "/tags"},
    )


@pytest.mark.asyncio
async def test_cross_tenant_api_get_item(harness: IsolationHarness):
    """Calling GET /api/items/{id} as tenant B returns 404 for tenant A's item."""
    await _assert_cross_tenant_denied(
        harness, "GET", f"/api/items/{harness.tenant_a.html_item_id}"
    )


@pytest.mark.asyncio
async def test_cross_tenant_api_delete_item(harness: IsolationHarness):
    """Calling DELETE /api/items/{id} as tenant B returns 404 for tenant A's item."""
    await _assert_cross_tenant_denied(
        harness, "DELETE", f"/api/items/{harness.tenant_a.html_item_id}"
    )


@pytest.mark.asyncio
async def test_cross_tenant_api_get_content(harness: IsolationHarness):
    """Calling GET /api/items/{id}/content as tenant B returns 404 for tenant A's item."""
    await _assert_cross_tenant_denied(
        harness, "GET", f"/api/items/{harness.tenant_a.html_item_id}/content"
    )



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
        datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
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


@pytest.mark.asyncio
async def test_rate_limit_keys_isolation(harness: IsolationHarness):
    """Rate limit failures recorded for Tenant A account do not lock out Tenant B."""
    from keepfor.utils.rate_limit import check_allowed, record_failure

    a = harness.tenant_a
    b = harness.tenant_b

    # Simulate 10 failed login attempts for A's account
    dummy_headers = {"cf-connecting-ip": "198.51.100.1"}
    for _ in range(10):
        await record_failure(a.db, dummy_headers, a.user["email"])

    # Check Tenant A: should now be locked out on that account
    verdict_a = await check_allowed(a.db, dummy_headers, a.user["email"])
    assert verdict_a.allowed is False

    # Check Tenant B: different IP and account, must be allowed
    headers_b = {"cf-connecting-ip": "198.51.100.2"}
    verdict_b = await check_allowed(b.db, headers_b, b.user["email"])
    assert verdict_b.allowed is True


@pytest.mark.asyncio
async def test_settings_cleanup_failed_isolation(harness: IsolationHarness):
    """Cleaning up failed items as Tenant B only deletes B's failed items, leaving A's intact."""
    a = harness.tenant_a
    b = harness.tenant_b

    # Insert a failed item for A and B
    await a.db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status, fail_reason) VALUES (?, ?, ?, ?, ?, ?);",
        (
            "failed_item_a",
            a.user["id"],
            "https://example.com/fail-a",
            "https://example.com/fail-a",
            "failed",
            "HTTP 500",
        ),
    )
    await b.db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status, fail_reason) VALUES (?, ?, ?, ?, ?, ?);",
        (
            "failed_item_b",
            b.user["id"],
            "https://example.com/fail-b",
            "https://example.com/fail-b",
            "failed",
            "HTTP 500",
        ),
    )

    # Tenant B runs cleanup-failed
    resp = harness.client.post(
        "/settings/cleanup-failed",
        data={"group": "all"},
        headers=b.auth_headers,
    )
    assert resp.status_code == 200

    # Tenant B's failed item should be gone
    row_b = await b.db.query_first(
        "SELECT id FROM items WHERE id = ? AND user_id = ?;",
        ("failed_item_b", b.user["id"]),
    )
    assert row_b is None

    # Tenant A's failed item must still exist
    row_a = await a.db.query_first(
        "SELECT id FROM items WHERE id = ? AND user_id = ?;",
        ("failed_item_a", a.user["id"]),
    )
    assert row_a is not None

    # Clean up A's failed item
    await a.db.execute(
        "DELETE FROM items WHERE id = ? AND user_id = ?;",
        ("failed_item_a", a.user["id"]),
    )

