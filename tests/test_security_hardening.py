# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for same-origin CSRF protection and atomic first-user registration."""

import pytest
from fastapi.testclient import TestClient

from keepfor.app import app
from keepfor.auth.service import RegistrationClosedError, login_user, register_user
from keepfor.models.items import save_item
from tests.test_endpoints import MockEnv


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: MockEnv())
    return TestClient(app)


@pytest.mark.asyncio
async def test_cross_origin_post_blocked_across_ui_and_settings(client, db):
    """Cross-origin or cross-site cookie mutations return 403."""
    user = await register_user(db, "owner@test.local", "password123")
    _, session_id = await login_user(db, "owner@test.local", "password123")
    item, _ = await save_item(
        db, MockEnv(), user["id"], "https://example.com/item", ["tag1"]
    )
    item_id = item["id"]
    client.cookies["kfm_session"] = session_id

    cross_headers_list = [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ]
    targets = [
        (f"/items/{item_id}/delete-forever", {}),
        ("/settings/tokens", {"name": "evil-token"}),
        ("/settings/tokens/fake-id/delete", {}),
        ("/settings/cleanup-failed", {}),
        ("/tags/delete", {"name": "tag1"}),
        ("/save", {"url": "https://evil.example/phish"}),
    ]
    for headers in cross_headers_list:
        for path, data in targets:
            resp = client.post(path, data=data, headers=headers, follow_redirects=False)
            assert resp.status_code == 403, f"Expected 403 for {path} with {headers}"


@pytest.mark.asyncio
async def test_cross_site_logout_blocked_and_same_origin_allowed(client, db):
    """Cross-site GET/POST /auth/logout is blocked; same-origin logout works."""
    await register_user(db, "owner@test.local", "password123")
    _, session_id = await login_user(db, "owner@test.local", "password123")
    client.cookies["kfm_session"] = session_id

    # Cross-site <img src="/auth/logout"> sends Sec-Fetch-Site: cross-site
    r_cross = client.get(
        "/auth/logout",
        headers={"Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )
    assert r_cross.status_code == 403

    r_evil_origin = client.post(
        "/auth/logout",
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert r_evil_origin.status_code == 403

    # Session must still be valid
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 200

    # Same-origin POST /auth/logout succeeds and destroys the session
    r_ok = client.post(
        "/auth/logout",
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r_ok.status_code == 303


@pytest.mark.asyncio
async def test_bookmarklet_popup_flow_allowed(client, db):
    """Bookmarklet cross-site GET /save-popup and same-origin POST /save-popup work."""
    await register_user(db, "owner@test.local", "password123")
    _, session_id = await login_user(db, "owner@test.local", "password123")
    client.cookies["kfm_session"] = session_id

    # Opening popup from any third-party site is a top-level GET with cross-site header
    popup_get = client.get(
        "/save-popup?url=https://news.ycombinator.com&title=HN",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert popup_get.status_code == 200

    # Submitting the popup form is same-origin to the Keepfor.me server
    popup_post = client.post(
        "/save-popup",
        data={"url": "https://news.ycombinator.com", "tags": "reading"},
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    assert popup_post.status_code == 200
    assert "Saved" in popup_post.text


@pytest.mark.asyncio
async def test_register_user_atomic_first_admin_guard(db):
    """Concurrent first-user signup cannot create a second admin when closed."""
    orig_query_first = db.query_first
    interleaved = False

    async def racing_query_first(sql: str, params=()):
        nonlocal interleaved
        res = await orig_query_first(sql, params)
        if not interleaved and "SELECT COUNT(*)" in sql:
            interleaved = True
            # Simulate a concurrent request winning the race before our INSERT runs
            await db.execute(
                "INSERT INTO users (id, email, password_hash, role) "
                "VALUES (?, ?, ?, 'admin');",
                ("winner-id", "winner@test.local", "salt$hash"),
            )
        return res

    db.query_first = racing_query_first  # type: ignore[method-assign]
    with pytest.raises(RegistrationClosedError):
        await register_user(
            db, "loser@test.local", "password123", allow_public_signups=False
        )

    admins = await orig_query_first(
        "SELECT COUNT(*) as c FROM users WHERE role = 'admin';"
    )
    assert admins is not None and admins["c"] == 1
