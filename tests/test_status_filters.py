# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Status filter pills + failed-cleanup tests (TDD RED)."""

import uuid

import pytest
from fastapi.testclient import TestClient

from keepfor.app import app
from keepfor.auth.service import login_user, register_user
from keepfor.models.items import save_item


class FakeQueue:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, message: dict) -> None:
        self.sent.append(message)


class MockEnv:
    def __init__(self, sqlite_conn=None):
        self.DB = None
        self.QUEUE = FakeQueue()
        self.AI = None
        self.VECTORIZE = None
        self.BUCKET = None
        self.sqlite_conn = sqlite_conn


@pytest.fixture
def client(db, monkeypatch):
    def mock_get_db(request):
        return db

    monkeypatch.setattr("keepfor.app.get_db", mock_get_db)
    monkeypatch.setattr("keepfor.app.get_env_from_request", lambda request: MockEnv())
    return TestClient(app)


@pytest.fixture
async def auth_headers(db):
    await register_user(
        db, "admin@test.local", "password123", allow_public_signups=False
    )
    user, session_id = await login_user(db, "admin@test.local", "password123")
    return {"admin_session": session_id, "admin_user": user}


async def _seed_statuses(db, user_id):
    """One queued, one ok, two failed (403 + timeout). Returns ids."""
    queued, _ = await save_item(
        db, MockEnv(), user_id, "https://example.com/queued", []
    )
    ok_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title, status)"
        " VALUES (?, ?, ?, ?, ?, 'ok');",
        (ok_id, user_id, "https://example.com/ok", "https://example.com/ok", "Ok"),
    )
    fail403_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status, fail_reason)"
        " VALUES (?, ?, ?, ?, 'failed', 'HTTP 403 returned by origin server');",
        (
            fail403_id,
            user_id,
            "https://example.com/blocked",
            "https://example.com/blocked",
        ),
    )
    failtimeout_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status, fail_reason)"
        " VALUES (?, ?, ?, ?, 'failed', 'connection timed out after 20s');",
        (
            failtimeout_id,
            user_id,
            "https://example.com/slow",
            "https://example.com/slow",
        ),
    )
    return {
        "queued": queued["id"],
        "ok": ok_id,
        "fail403": fail403_id,
        "failtimeout": failtimeout_id,
    }


# --- engine: status counts ---


@pytest.mark.asyncio
async def test_status_counts_groups_queued_and_fetching(db, auth_headers):
    """get_status_counts reports extracting/saved/failed per user."""
    from keepfor.search.engine import get_status_counts

    user = auth_headers["admin_user"]
    await _seed_statuses(db, user["id"])
    counts = await get_status_counts(db, user["id"])
    assert counts == {"all": 4, "saved": 1, "extracting": 1, "failed": 2}


@pytest.mark.asyncio
async def test_recent_items_status_filter(db, auth_headers):
    """get_recent_items honors status=failed / extracting."""
    from keepfor.search.engine import get_recent_items

    user = auth_headers["admin_user"]
    await _seed_statuses(db, user["id"])
    failed = await get_recent_items(db, user["id"], status="failed")
    assert len(failed) == 2
    assert all(r["status"] == "failed" for r in failed)
    extracting = await get_recent_items(db, user["id"], status="extracting")
    assert len(extracting) == 1
    saved = await get_recent_items(db, user["id"], status="saved")
    assert len(saved) == 1
    assert saved[0]["status"] == "ok"


# --- library page: pills + filtering, no mode selector / cmdK badge ---


@pytest.mark.asyncio
async def test_library_shows_status_pills_with_counts(client, db, auth_headers):
    """Library renders status filter pills with live counts."""
    user = auth_headers["admin_user"]
    await _seed_statuses(db, user["id"])
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="status-filters"' in response.text
    assert "Failed" in response.text


@pytest.mark.asyncio
async def test_library_hides_search_mode_selector_and_cmdk_badge(
    client, db, auth_headers
):
    """No nerdy mode dropdown, no ⌘K badge in the search bar."""
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="search-mode"' not in response.text
    assert "Hybrid (FTS5 + Vector)" not in response.text
    assert "⌘K" not in response.text


@pytest.mark.asyncio
async def test_library_status_param_filters_feed(client, db, auth_headers):
    """GET /?status=failed shows only failed items."""
    user = auth_headers["admin_user"]
    await _seed_statuses(db, user["id"])
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    response = client.get("/?status=failed")
    assert response.status_code == 200
    assert "example.com/blocked" in response.text
    assert "example.com/slow" in response.text
    assert "example.com/queued" not in response.text


@pytest.mark.asyncio
async def test_search_htmx_status_filter(client, db, auth_headers):
    """POST /search with status=failed returns only failed cards."""
    user = auth_headers["admin_user"]
    await _seed_statuses(db, user["id"])
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    response = client.post("/search", data={"query": "", "status": "failed", "tag": ""})
    assert response.status_code == 200
    assert "example.com/blocked" in response.text
    assert "example.com/queued" not in response.text


@pytest.mark.asyncio
async def test_search_htmx_always_hybrid(client, db, auth_headers):
    """POST /search ignores mode and runs hybrid (FTS finds the item)."""
    user = auth_headers["admin_user"]
    item_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title, status)"
        " VALUES (?, ?, ?, ?, ?, 'ok');",
        (
            item_id,
            user["id"],
            "https://example.com/hybridcheck",
            "https://example.com/hybridcheck",
            "Hybridcheck Guide",
        ),
    )
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?, ?, ?, ?);",
        (item_id, user["id"], "Hybridcheck Guide", "hybridcheck content"),
    )
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    # semantic with no vectorize binding would return nothing; hybrid finds it.
    response = client.post(
        "/search",
        data={"query": "hybridcheck", "mode": "semantic", "tag": ""},
    )
    assert response.status_code == 200
    assert "hybridcheck" in response.text


# --- settings cleanup ---


@pytest.mark.asyncio
async def test_cleanup_failed_pattern_deletes_only_matches(client, db, auth_headers):
    """Cleanup with blocked-pattern removes the 403 item, keeps the rest."""
    user = auth_headers["admin_user"]
    await _seed_statuses(db, user["id"])
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    response = client.post("/settings/cleanup-failed", data={"pattern": "blocked"})
    assert response.status_code == 200
    remaining = await db.query_all(
        "SELECT canonical_url FROM items WHERE user_id = ?;", (user["id"],)
    )
    urls = [r["canonical_url"] for r in remaining]
    assert "https://example.com/blocked" not in urls
    assert "https://example.com/slow" in urls
    assert "https://example.com/ok" in urls
    assert "https://example.com/queued" in urls


@pytest.mark.asyncio
async def test_cleanup_failed_all_removes_every_failed(client, db, auth_headers):
    """Cleanup with pattern=all deletes both failed items, keeps ok/queued."""
    user = auth_headers["admin_user"]
    await _seed_statuses(db, user["id"])
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    response = client.post("/settings/cleanup-failed", data={"pattern": "all"})
    assert response.status_code == 200
    remaining = await db.query_all(
        "SELECT status FROM items WHERE user_id = ?;", (user["id"],)
    )
    assert all(r["status"] != "failed" for r in remaining)
    assert len(remaining) == 2


def test_cleanup_failed_requires_auth(client):
    """Unauthenticated cleanup is rejected."""
    response = client.post(
        "/settings/cleanup-failed", data={"pattern": "all"}, follow_redirects=False
    )
    assert response.status_code in (303, 307, 308, 401)


@pytest.mark.asyncio
async def test_cleanup_failed_rejects_unknown_pattern(client, db, auth_headers):
    """Unknown pattern deletes nothing."""
    user = auth_headers["admin_user"]
    await _seed_statuses(db, user["id"])
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    response = client.post("/settings/cleanup-failed", data={"pattern": "drop-table"})
    assert response.status_code == 400
    remaining = await db.query_all(
        "SELECT COUNT(*) as count FROM items WHERE user_id = ?;", (user["id"],)
    )
    assert remaining[0]["count"] == 4


@pytest.mark.asyncio
async def test_settings_shows_cleanup_section(client, db, auth_headers):
    """Settings page offers the failed-cleanup picker."""
    user = auth_headers["admin_user"]
    await _seed_statuses(db, user["id"])
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    response = client.get("/settings")
    assert response.status_code == 200
    assert "cleanup-failed" in response.text


@pytest.mark.asyncio
async def test_settings_cleanup_options_show_per_group_counts(client, db, auth_headers):
    """Each cleanup dropdown option shows its own count, not just the total."""
    user = auth_headers["admin_user"]
    await _seed_statuses(db, user["id"])
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    response = client.get("/settings")
    assert response.status_code == 200
    # 1 blocked (403), 1 connection (timeout), 2 total.
    assert "blocked" in response.text
    assert response.text.count("· 1") >= 2
    assert "· 2" in response.text


@pytest.mark.asyncio
async def test_fail_group_counts_are_exclusive(db, auth_headers):
    """A 403 failure counts as blocked only, not also as client-error."""
    from keepfor.app import get_fail_group_counts

    user = auth_headers["admin_user"]
    await _seed_statuses(db, user["id"])
    counts = await get_fail_group_counts(db, user["id"])
    assert counts == {
        "all": 2,
        "blocked": 1,
        "notfound": 0,
        "client": 0,
        "server": 0,
        "connection": 1,
        "other": 0,
    }


@pytest.mark.asyncio
async def test_cleanup_failed_other_removes_only_unmatched(client, db, auth_headers):
    """Cleanup with pattern=other deletes failures matching no known group."""
    import uuid as uuid_mod

    user = auth_headers["admin_user"]
    await _seed_statuses(db, user["id"])
    odd_id = str(uuid_mod.uuid4())
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status, fail_reason)"
        " VALUES (?, ?, ?, ?, 'failed', 'parser ate my homework');",
        (odd_id, user["id"], "https://example.com/odd", "https://example.com/odd"),
    )
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    response = client.post("/settings/cleanup-failed", data={"pattern": "other"})
    assert response.status_code == 200
    remaining = await db.query_all(
        "SELECT canonical_url FROM items WHERE user_id = ?;", (user["id"],)
    )
    urls = [r["canonical_url"] for r in remaining]
    assert "https://example.com/odd" not in urls
    assert "https://example.com/blocked" in urls
    assert "https://example.com/slow" in urls


@pytest.mark.asyncio
async def test_quick_notes_included_in_saved_status_group(db):
    from keepfor.auth.service import register_user
    from keepfor.models.items import save_note
    from keepfor.search.engine import get_recent_items, get_status_counts

    user = await register_user(db, "note_status@test.local", "password123")
    user_id = user["id"]

    # Save a quick note (hardcodes status='saved')
    note = await save_note(
        db, None, user_id, "Note Title", "Note content text", ["testtag"]
    )
    assert note["status"] == "saved"

    # Status counts must count the note under "saved"
    counts = await get_status_counts(db, user_id)
    assert counts["saved"] == 1
    assert counts["all"] == 1

    # Filter ?status=saved must return the note
    items = await get_recent_items(db, user_id, status="saved")
    assert len(items) == 1
    assert items[0]["id"] == note["id"]
