# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for the reader highlight routes (GitHub issue #45)."""

import pytest
from fastapi.testclient import TestClient

from keepfor.app import app
from keepfor.auth.service import login_user, register_user
from keepfor.models.highlights import (
    MAX_HIGHLIGHT_NOTE,
    MAX_HIGHLIGHT_TEXT,
    create_highlight,
    list_highlights,
)
from keepfor.models.items import save_item


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
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
async def auth(db):
    await register_user(
        db, "highlights@test.local", "password123", allow_public_signups=False
    )
    user, session_id = await login_user(db, "highlights@test.local", "password123")
    return user, session_id


@pytest.fixture
async def item_with_content(db, auth):
    """Save an item with content_text for highlight testing."""
    user, _ = auth
    item, _ = await save_item(
        db,
        MockEnv(),
        user["id"],
        "https://example.com/highlight-article",
        [],
    )
    # Add content_text
    content = (
        "Hello world, this is a test article for highlighting. "
        "It has multiple sentences."
    )
    await db.execute(
        "UPDATE items SET content_text = ?, status = 'ok'"
        " WHERE id = ? AND user_id = ?;",
        (content, item["id"], user["id"]),
    )
    return user["id"], item["id"], content


# --- Model layer tests (via routes) ---


@pytest.mark.asyncio
async def test_create_highlight_route_creates_highlight(
    db, client, auth, item_with_content
):
    """POST /items/{id}/highlights creates a highlight with quote and offsets."""
    user_id, item_id, content = item_with_content
    client.cookies["kfm_session"] = auth[1]

    # "test article" starts at index 23, ends at 35
    response = client.post(
        f"/items/{item_id}/highlights",
        data={
            "text": "test article",
            "note": "important bit",
            "start_offset": 23,
            "end_offset": 35,
        },
        headers={"hx-request": "true"},
    )
    assert response.status_code == 200
    assert "test article" in response.text
    assert "important bit" in response.text

    # Verify in database
    highlights = await list_highlights(db, user_id, item_id)
    assert len(highlights) == 1
    assert highlights[0]["text"] == "test article"
    assert highlights[0]["note"] == "important bit"
    assert highlights[0]["start_offset"] == 23
    assert highlights[0]["end_offset"] == 35


@pytest.mark.asyncio
async def test_create_highlight_route_requires_auth(db, item_with_content, client):
    """Creating a highlight requires authentication."""
    _, item_id, _ = item_with_content
    response = client.post(
        f"/items/{item_id}/highlights", data={"text": "test"}, follow_redirects=False
    )
    assert response.status_code in (303, 307, 401)


@pytest.mark.asyncio
async def test_create_highlight_route_rejects_empty_text(
    db, client, auth, item_with_content
):
    """Empty quote text is rejected."""
    user_id, item_id, content = item_with_content
    client.cookies["kfm_session"] = auth[1]

    response = client.post(
        f"/items/{item_id}/highlights",
        data={"text": "", "start_offset": 0, "end_offset": 0},
        headers={"hx-request": "true"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_highlight_route_rejects_overlong_text(
    db, client, auth, item_with_content
):
    """Quote text exceeding MAX_HIGHLIGHT_TEXT is rejected."""
    user_id, item_id, content = item_with_content
    client.cookies["kfm_session"] = auth[1]

    response = client.post(
        f"/items/{item_id}/highlights",
        data={
            "text": "x" * (MAX_HIGHLIGHT_TEXT + 1),
            "start_offset": 0,
            "end_offset": MAX_HIGHLIGHT_TEXT + 1,
        },
        headers={"hx-request": "true"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_highlight_route_rejects_overlong_note(
    db, client, auth, item_with_content
):
    """Note exceeding MAX_HIGHLIGHT_NOTE is rejected."""
    user_id, item_id, content = item_with_content
    client.cookies["kfm_session"] = auth[1]

    response = client.post(
        f"/items/{item_id}/highlights",
        data={
            "text": "test",
            "note": "n" * (MAX_HIGHLIGHT_NOTE + 1),
            "start_offset": 0,
            "end_offset": 4,
        },
        headers={"hx-request": "true"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_highlight_route_rejects_invalid_offsets(
    db, client, auth, item_with_content
):
    """Offsets past content length are rejected."""
    user_id, item_id, content = item_with_content
    client.cookies["kfm_session"] = auth[1]

    # Content is ~75 chars, so offset 100 is invalid
    response = client.post(
        f"/items/{item_id}/highlights",
        data={"text": "test", "start_offset": 100, "end_offset": 104},
        headers={"hx-request": "true"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_highlight_route_rejects_mismatched_quote(
    db, client, auth, item_with_content
):
    """Quote text not matching content at offsets is rejected."""
    user_id, item_id, content = item_with_content
    client.cookies["kfm_session"] = auth[1]

    # Content is "Hello world, this is a test article..."
    # Asking for "goodbye" at offset 0-7 will fail
    response = client.post(
        f"/items/{item_id}/highlights",
        data={"text": "goodbye", "start_offset": 0, "end_offset": 7},
        headers={"hx-request": "true"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_highlight_route_tenant_scoped(
    db, client, auth, item_with_content
):
    """Highlights are tenant-scoped: another user cannot create on this item."""
    # Create a second user
    await register_user(
        db, "other@test.local", "password123", allow_public_signups=True
    )
    other_user, other_session = await login_user(db, "other@test.local", "password123")

    _, item_id, _ = item_with_content
    client.cookies["kfm_session"] = other_session

    response = client.post(
        f"/items/{item_id}/highlights",
        data={"text": "test", "start_offset": 0, "end_offset": 4},
        headers={"hx-request": "true"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_create_highlight_route_no_js_fallback(
    db, client, auth, item_with_content
):
    """Without hx-request, route redirects back to reader."""
    user_id, item_id, content = item_with_content
    client.cookies["kfm_session"] = auth[1]

    response = client.post(
        f"/items/{item_id}/highlights",
        data={
            "text": "test article",
            "note": "note",
            "start_offset": 23,
            "end_offset": 35,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/items/{item_id}"


# --- Delete highlight route ---


@pytest.mark.asyncio
async def test_delete_highlight_route_deletes_highlight(
    db, client, auth, item_with_content
):
    """POST /items/{id}/highlights/{hid}/delete deletes the highlight."""
    user_id, item_id, content = item_with_content
    client.cookies["kfm_session"] = auth[1]

    # First create a highlight
    highlight = await create_highlight(
        db, user_id, item_id, "test article", 23, 35, note="test note"
    )
    highlight_id = highlight["id"]

    response = client.post(
        f"/items/{item_id}/highlights/{highlight_id}/delete",
        headers={"hx-request": "true"},
    )
    assert response.status_code == 200
    assert "test article" not in response.text

    # Verify deleted from database
    highlights = await list_highlights(db, user_id, item_id)
    assert len(highlights) == 0


@pytest.mark.asyncio
async def test_delete_highlight_route_requires_ownership(
    db, client, auth, item_with_content
):
    """A user cannot delete another user's highlight."""
    user_id, item_id, content = item_with_content

    # Create a highlight as the owner
    highlight = await create_highlight(db, user_id, item_id, "test article", 23, 35)
    highlight_id = highlight["id"]

    # Create second user
    await register_user(
        db, "other@test.local", "password123", allow_public_signups=True
    )
    other_user, other_session = await login_user(db, "other@test.local", "password123")

    client.cookies["kfm_session"] = other_session

    response = client.post(
        f"/items/{item_id}/highlights/{highlight_id}/delete",
        headers={"hx-request": "true"},
    )
    assert response.status_code == 404

    # Original highlight still exists
    highlights = await list_highlights(db, user_id, item_id)
    assert len(highlights) == 1


@pytest.mark.asyncio
async def test_delete_highlight_route_no_js_fallback(
    db, client, auth, item_with_content
):
    """Without hx-request, delete route redirects back to reader."""
    user_id, item_id, content = item_with_content
    client.cookies["kfm_session"] = auth[1]

    highlight = await create_highlight(db, user_id, item_id, "test article", 23, 35)
    highlight_id = highlight["id"]

    response = client.post(
        f"/items/{item_id}/highlights/{highlight_id}/delete",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/items/{item_id}"


@pytest.mark.asyncio
async def test_delete_highlight_route_404_on_another_item(
    db, client, auth, item_with_content
):
    """A highlight of another item is not deleted through this item's URL."""
    user_id, item_id, content = item_with_content
    client.cookies["kfm_session"] = auth[1]

    other_item, _ = await save_item(
        db,
        MockEnv(),
        user_id,
        "https://example.com/highlight-other-article",
        [],
    )
    highlight = await create_highlight(db, user_id, item_id, "test article", 23, 35)

    response = client.post(
        f"/items/{other_item['id']}/highlights/{highlight['id']}/delete",
        headers={"hx-request": "true"},
    )
    assert response.status_code == 404

    # The highlight survives: the URL did not name its item.
    highlights = await list_highlights(db, user_id, item_id)
    assert len(highlights) == 1


# --- Cross-origin POSTs are refused ---


@pytest.mark.asyncio
async def test_create_highlight_route_rejects_cross_origin(
    db, client, auth, item_with_content
):
    """A cross-origin POST gets 403 and creates nothing."""
    user_id, item_id, content = item_with_content
    client.cookies["kfm_session"] = auth[1]

    response = client.post(
        f"/items/{item_id}/highlights",
        data={"text": "test article", "start_offset": 23, "end_offset": 35},
        headers={"hx-request": "true", "Origin": "https://evil.test"},
    )
    assert response.status_code == 403
    assert await list_highlights(db, user_id, item_id) == []


@pytest.mark.asyncio
async def test_delete_highlight_route_rejects_cross_origin(
    db, client, auth, item_with_content
):
    """A cross-origin delete gets 403 and the highlight survives."""
    user_id, item_id, content = item_with_content
    client.cookies["kfm_session"] = auth[1]

    highlight = await create_highlight(db, user_id, item_id, "test article", 23, 35)

    response = client.post(
        f"/items/{item_id}/highlights/{highlight['id']}/delete",
        headers={"hx-request": "true", "Origin": "https://evil.test"},
    )
    assert response.status_code == 403
    assert len(await list_highlights(db, user_id, item_id)) == 1


@pytest.mark.asyncio
async def test_create_highlight_route_accepts_same_origin(
    db, client, auth, item_with_content
):
    """The same-origin POST still works: the Origin check is not a blanket deny."""
    user_id, item_id, content = item_with_content
    client.cookies["kfm_session"] = auth[1]

    response = client.post(
        f"/items/{item_id}/highlights",
        data={"text": "test article", "start_offset": 23, "end_offset": 35},
        headers={"hx-request": "true", "Origin": "https://testserver"},
    )
    assert response.status_code == 200
    assert len(await list_highlights(db, user_id, item_id)) == 1


# --- Stored XSS ---

XSS_PAYLOAD = "<img src=x onerror=alert(1)>"


@pytest.mark.asyncio
async def test_highlight_text_is_escaped_in_reader_and_fragment(
    db, client, auth, item_with_content
):
    """Highlight text is article text: it is escaped, never rendered as markup."""
    user_id, item_id, content = item_with_content
    client.cookies["kfm_session"] = auth[1]

    # A highlight created outside the route (MCP, import): the quote is markup.
    await create_highlight(db, user_id, item_id, XSS_PAYLOAD, 0, len(XSS_PAYLOAD))

    reader = client.get(f"/items/{item_id}")
    assert reader.status_code == 200
    assert XSS_PAYLOAD not in reader.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in reader.text

    # The htmx fragment carries the same escaped text.
    fragment = client.post(
        f"/items/{item_id}/highlights",
        data={"text": "test article", "start_offset": 23, "end_offset": 35},
        headers={"hx-request": "true"},
    )
    assert fragment.status_code == 200
    assert XSS_PAYLOAD not in fragment.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in fragment.text


# --- Reader page shows highlights ---


@pytest.mark.asyncio
async def test_reader_page_shows_highlights(db, client, auth, item_with_content):
    """Reader page renders highlights from the database."""
    user_id, item_id, content = item_with_content
    client.cookies["kfm_session"] = auth[1]

    # Create highlights via model (simulating MCP creation)
    await create_highlight(
        db, user_id, item_id, "test article", 23, 35, note="from MCP"
    )
    await create_highlight(
        db, user_id, item_id, "Hello world", 0, 11, note="first highlight"
    )

    response = client.get(f"/items/{item_id}")
    assert response.status_code == 200
    assert "test article" in response.text
    assert "from MCP" in response.text
    assert "Hello world" in response.text
    assert "first highlight" in response.text


@pytest.mark.asyncio
async def test_reader_page_shows_no_highlights_message(
    db, client, auth, item_with_content
):
    """Reader page shows empty state when no highlights exist."""
    _, item_id, _ = item_with_content
    client.cookies["kfm_session"] = auth[1]

    response = client.get(f"/items/{item_id}")
    assert response.status_code == 200
    assert "No highlights yet" in response.text


# --- Highlights list partial ---


@pytest.mark.asyncio
async def test_highlights_list_partial_renders(db, client, auth, item_with_content):
    """The highlights list partial renders correctly for htmx swap."""
    user_id, item_id, content = item_with_content
    client.cookies["kfm_session"] = auth[1]

    await create_highlight(
        db, user_id, item_id, "test article", 23, 35, note="test note"
    )

    # "this is a test" is at offset 13-27 in the content
    response = client.post(
        f"/items/{item_id}/highlights",
        data={
            "text": "this is a test",
            "note": "another note",
            "start_offset": 13,
            "end_offset": 27,
        },
        headers={"hx-request": "true"},
    )
    assert response.status_code == 200
    assert "test article" in response.text
    assert "this is a test" in response.text
    assert "test note" in response.text
    assert "another note" in response.text
    assert 'hx-post="/items/' in response.text  # Delete forms have htmx


# --- MCP highlights show in reader ---


@pytest.mark.asyncio
async def test_mcp_created_highlight_shows_in_reader(
    db, client, auth, item_with_content
):
    """A highlight created via MCP create_highlight tool appears in the reader."""
    user_id, item_id, content = item_with_content
    client.cookies["kfm_session"] = auth[1]

    # Simulate MCP create_highlight call
    await create_highlight(
        db,
        user_id,
        item_id,
        "test article",
        23,
        35,
        note="created via MCP",
        color="#ffd60a",
    )

    response = client.get(f"/items/{item_id}")
    assert response.status_code == 200
    assert "test article" in response.text
    assert "created via MCP" in response.text
