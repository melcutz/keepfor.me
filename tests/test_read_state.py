# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Issue #42 regression: Mark as Read is a real read state, not a silent archive.

Covers the read/unread model helpers, the /items/{id}/read + /unread routes,
the read/unread category filters and the archived-item guard.
"""

from http.cookies import SimpleCookie

import pytest
from fastapi.testclient import TestClient

from keepfor.app import app
from keepfor.auth.service import login_user, register_user
from keepfor.models.items import (
    archive_item,
    mark_item_read,
    mark_item_unread,
    save_item,
)
from keepfor.search.engine import get_recent_items


class FakeQueue:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, message: dict) -> None:
        self.sent.append(message)


class MockEnv:
    def __init__(self):
        self.DB = None
        self.QUEUE = FakeQueue()
        self.AI = None
        self.VECTORIZE = None
        self.BUCKET = None


@pytest.fixture
def client(db, monkeypatch):
    def mock_get_db(request):
        return db

    monkeypatch.setattr("keepfor.deps.get_db", mock_get_db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: MockEnv())
    # https: the flash cookie is Secure, so it is only sent back over TLS.
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
async def auth(db):
    await register_user(
        db, "reader@test.local", "password123", allow_public_signups=False
    )
    user, session_id = await login_user(db, "reader@test.local", "password123")
    return user, session_id


@pytest.fixture
async def unread_item(db, auth):
    """One saved, still-unread item. Returns (user_id, item_id)."""
    user, _ = auth
    item, _ = await save_item(
        db,
        MockEnv(),
        user["id"],
        "https://example.com/unread-article",
        [],
    )
    return user["id"], item["id"]


# --- model layer ---


@pytest.mark.asyncio
async def test_mark_read_sets_read_state_only(db, unread_item):
    """mark_item_read flips read_state to 'read', never to 'archived'."""
    user_id, item_id = unread_item
    assert await mark_item_read(db, user_id, item_id) is True
    row = await db.query_first("SELECT read_state FROM items WHERE id = ?;", (item_id,))
    assert row["read_state"] == "read"


@pytest.mark.asyncio
async def test_mark_unread_reverts_read_state(db, unread_item):
    """mark_item_unread flips a read item back to 'unread'."""
    user_id, item_id = unread_item
    await mark_item_read(db, user_id, item_id)
    assert await mark_item_unread(db, user_id, item_id) is True
    row = await db.query_first("SELECT read_state FROM items WHERE id = ?;", (item_id,))
    assert row["read_state"] == "unread"


@pytest.mark.asyncio
async def test_mark_read_unknown_item_is_false(db, unread_item):
    user_id, _ = unread_item
    assert await mark_item_read(db, user_id, "does-not-exist") is False
    assert await mark_item_unread(db, user_id, "does-not-exist") is False


@pytest.mark.asyncio
async def test_read_item_stays_in_library_with_filters(db, auth, unread_item):
    """A read item leaves the unread filter but stays in the library."""
    user_id, item_id = unread_item
    await mark_item_read(db, user_id, item_id)

    all_items = {r["id"] for r in await get_recent_items(db, user_id)}
    assert item_id in all_items

    read_ids = {r["id"] for r in await get_recent_items(db, user_id, category="read")}
    assert item_id in read_ids

    unread_ids = {
        r["id"] for r in await get_recent_items(db, user_id, category="unread")
    }
    assert item_id not in unread_ids


@pytest.mark.asyncio
async def test_filter_distinguishes_read_and_unread(db, auth, unread_item):
    """Two items, one read: each filter keeps exactly the right one."""
    user_id, read_id = unread_item
    other, _ = await save_item(
        db,
        MockEnv(),
        user_id,
        "https://example.com/still-unread",
        [],
    )
    await mark_item_read(db, user_id, read_id)

    assert (await get_recent_items(db, user_id, category="read"))[0]["id"] == read_id
    assert (await get_recent_items(db, user_id, category="unread"))[0]["id"] == other[
        "id"
    ]


@pytest.mark.asyncio
async def test_archive_is_still_a_separate_action(db, unread_item):
    """Archiving still sets 'archived'; reading never does."""
    user_id, item_id = unread_item
    await mark_item_read(db, user_id, item_id)
    assert await archive_item(db, user_id, item_id) is True
    row = await db.query_first("SELECT read_state FROM items WHERE id = ?;", (item_id,))
    assert row["read_state"] == "archived"


# --- routes ---


@pytest.mark.asyncio
async def test_post_read_marks_read_keeps_item(db, client, auth, unread_item):
    """POST /items/{id}/read sets read_state='read' and redirects (no archive)."""
    user_id, item_id = unread_item
    client.cookies["kfm_session"] = auth[1]
    response = client.post(f"/items/{item_id}/read", follow_redirects=False)
    assert response.status_code == 303
    row = await db.query_first("SELECT read_state FROM items WHERE id = ?;", (item_id,))
    assert row["read_state"] == "read"
    # Still in the library, not archived.
    assert item_id in {r["id"] for r in await get_recent_items(db, user_id)}


@pytest.mark.asyncio
async def test_post_unread_reverts(db, client, auth, unread_item):
    """POST /items/{id}/unread is the undo path."""
    _, item_id = unread_item
    client.cookies["kfm_session"] = auth[1]
    await mark_item_read(db, auth[0]["id"], item_id)
    response = client.post(f"/items/{item_id}/unread", follow_redirects=False)
    assert response.status_code == 303
    row = await db.query_first("SELECT read_state FROM items WHERE id = ?;", (item_id,))
    assert row["read_state"] == "unread"


@pytest.mark.asyncio
async def test_post_read_htmx_returns_card_with_confirmation(client, auth, unread_item):
    """The htmx path re-renders the card with inline confirmation + undo."""
    user, item_id = unread_item
    client.cookies["kfm_session"] = auth[1]
    response = client.post(f"/items/{item_id}/read", headers={"hx-request": "true"})
    assert response.status_code == 200
    assert "Marked as read" in response.text
    assert f"/items/{item_id}/unread" in response.text


@pytest.mark.asyncio
async def test_post_read_unknown_item_404(client, auth):
    client.cookies["kfm_session"] = auth[1]
    response = client.post("/items/does-not-exist/read", follow_redirects=False)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_post_read_requires_auth(db, unread_item, client):
    user_id, item_id = unread_item
    response = client.post(f"/items/{item_id}/read", follow_redirects=False)
    assert response.status_code in (303, 307, 401)
    row = await db.query_first("SELECT read_state FROM items WHERE id = ?;", (item_id,))
    assert row["read_state"] != "read"


# --- archived items are left alone ---


@pytest.mark.asyncio
async def test_mark_read_leaves_archived_item_archived(db, unread_item):
    """Marking an archived item read must not pull it out of the archive."""
    user_id, item_id = unread_item
    assert await archive_item(db, user_id, item_id) is True
    assert await mark_item_read(db, user_id, item_id) is False
    row = await db.query_first("SELECT read_state FROM items WHERE id = ?;", (item_id,))
    assert row["read_state"] == "archived"


@pytest.mark.asyncio
async def test_mark_unread_leaves_archived_item_archived(db, unread_item):
    """Marking an archived item unread must not pull it out of the archive."""
    user_id, item_id = unread_item
    await mark_item_read(db, user_id, item_id)
    assert await archive_item(db, user_id, item_id) is True
    assert await mark_item_unread(db, user_id, item_id) is False
    row = await db.query_first("SELECT read_state FROM items WHERE id = ?;", (item_id,))
    assert row["read_state"] == "archived"


@pytest.mark.asyncio
async def test_post_read_on_archived_item_is_refused(db, client, auth, unread_item):
    """POST /items/{id}/read on an archived item: 409 and no state change."""
    user_id, item_id = unread_item
    await archive_item(db, user_id, item_id)
    client.cookies["kfm_session"] = auth[1]
    response = client.post(f"/items/{item_id}/read", follow_redirects=False)
    assert response.status_code == 409
    row = await db.query_first("SELECT read_state FROM items WHERE id = ?;", (item_id,))
    assert row["read_state"] == "archived"


@pytest.mark.asyncio
async def test_post_unread_on_archived_item_is_refused(db, client, auth, unread_item):
    """POST /items/{id}/unread on an archived item: 409 and no state change."""
    user_id, item_id = unread_item
    await mark_item_read(db, user_id, item_id)
    await archive_item(db, user_id, item_id)
    client.cookies["kfm_session"] = auth[1]
    response = client.post(f"/items/{item_id}/unread", follow_redirects=False)
    assert response.status_code == 409
    row = await db.query_first("SELECT read_state FROM items WHERE id = ?;", (item_id,))
    assert row["read_state"] == "archived"


@pytest.mark.asyncio
async def test_read_toggle_not_rendered_for_archived_item(
    db, client, auth, unread_item
):
    """An archived item shows no Mark as Read / Mark as Unread control."""
    user_id, item_id = unread_item
    await archive_item(db, user_id, item_id)
    client.cookies["kfm_session"] = auth[1]

    card = client.get(f"/items/{item_id}/card")
    assert card.status_code == 200
    assert f"/items/{item_id}/read" not in card.text
    assert f"/items/{item_id}/unread" not in card.text

    reader = client.get(f"/items/{item_id}")
    assert reader.status_code == 200
    assert f"/items/{item_id}/read" not in reader.text
    assert f"/items/{item_id}/unread" not in reader.text


@pytest.mark.asyncio
async def test_read_flash_cookie_has_the_security_flags(client, auth, unread_item):
    """The one-shot flash cookie is Secure, HttpOnly and SameSite=Lax."""
    _, item_id = unread_item
    client.cookies["kfm_session"] = auth[1]
    response = client.post(f"/items/{item_id}/read", follow_redirects=False)
    assert response.status_code == 303
    jar = SimpleCookie()
    jar.load(response.headers["set-cookie"])
    morsel = jar["read_flash"]
    assert morsel["secure"] is True
    assert morsel["httponly"] is True
    assert morsel["samesite"] == "lax"


# --- library page: indicator + filter ---


@pytest.mark.asyncio
async def test_library_category_read_filter(db, client, auth, unread_item):
    """GET /?category=read shows the read item, hides the unread one."""
    user, session_id = auth
    user_id, read_id = unread_item
    await mark_item_read(db, user_id, read_id)
    client.cookies["kfm_session"] = session_id

    response = client.get("/")
    assert response.status_code == 200
    assert "example.com/unread-article" in response.text

    response = client.get("/?category=read")
    assert response.status_code == 200
    assert "example.com/unread-article" in response.text

    response = client.get("/?category=unread")
    assert response.status_code == 200
    assert "example.com/unread-article" not in response.text


@pytest.mark.asyncio
async def test_library_category_unread_filter(db, client, auth, unread_item):
    """GET /?category=unread keeps unread items only."""
    user, session_id = auth
    user_id, read_id = unread_item
    other, _ = await save_item(
        db, MockEnv(), user_id, "https://example.com/still-unread", []
    )
    await mark_item_read(db, user_id, read_id)
    client.cookies["kfm_session"] = session_id

    response = client.get("/?category=unread")
    assert response.status_code == 200
    assert "example.com/still-unread" in response.text
    assert "example.com/unread-article" not in response.text


@pytest.mark.asyncio
async def test_library_shows_read_indicator_on_card(db, client, auth, unread_item):
    """The card carries a Read indicator once marked read."""
    user, session_id = auth
    user_id, item_id = unread_item
    await mark_item_read(db, user_id, item_id)
    client.cookies["kfm_session"] = session_id

    response = client.get("/")
    assert response.status_code == 200
    assert f'data-item-read="{item_id}"' in response.text


@pytest.mark.asyncio
async def test_reader_shows_confirmation_with_undo(client, auth, unread_item):
    """Mark-as-read from the reader returns to the reader with a flash + undo."""
    _, item_id = unread_item
    client.cookies["kfm_session"] = auth[1]
    response = client.post(
        f"/items/{item_id}/read",
        headers={"referer": f"https://testserver/items/{item_id}"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Marked as read" in response.text
    assert f'action="/items/{item_id}/unread"' in response.text
