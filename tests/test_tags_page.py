"""Endpoint tests for the /tags management page + suggestion review."""

import uuid

import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.auth.service import register_user


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

    monkeypatch.setattr("src.app.get_db", mock_get_db)
    monkeypatch.setattr("src.app.get_env_from_request", lambda request: MockEnv())
    return TestClient(app)


@pytest.fixture
async def auth_headers(db):
    from src.auth.service import login_user

    await register_user(db, "tags@keepfor.me", "password123")
    user, session_id = await login_user(db, "tags@keepfor.me", "password123")
    return {"admin_session": session_id, "admin_user": user}


def _login(client, auth_headers):
    client.cookies["kfm_session"] = auth_headers["admin_session"]


async def _seed_item(db, user_id, url, tags):
    from src.models.items import add_tags_to_item

    item_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status)"
        " VALUES (?, ?, ?, ?, 'ok');",
        (item_id, user_id, url, url),
    )
    if tags:
        await add_tags_to_item(db, user_id, item_id, tags)
    return item_id


@pytest.mark.asyncio
async def test_tags_page_requires_login(client):
    response = client.get("/tags", follow_redirects=False)
    assert response.status_code in (303, 307, 308)
    assert "/auth/login" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_tags_page_lists_tags_with_counts(client, db, auth_headers):
    _login(client, auth_headers)
    user_id = auth_headers["admin_user"]["id"]
    await _seed_item(db, user_id, "https://a.example/", ["alpha", "beta"])
    await _seed_item(db, user_id, "https://b.example/", ["alpha"])
    response = client.get("/tags")
    assert response.status_code == 200
    assert "alpha" in response.text
    assert "beta" in response.text


@pytest.mark.asyncio
async def test_create_tag_via_form(client, db, auth_headers):
    _login(client, auth_headers)
    response = client.post("/tags/create", data={"name": "NewTag "})
    assert response.status_code in (200, 303, 307, 308)
    row = await db.query_first(
        "SELECT name FROM tags WHERE user_id = ? AND name = ?;",
        (auth_headers["admin_user"]["id"], "newtag"),
    )
    assert row is not None


@pytest.mark.asyncio
async def test_create_tag_invalid_name_ignored(client, db, auth_headers):
    _login(client, auth_headers)
    response = client.post("/tags/create", data={"name": "bad/name"})
    assert response.status_code in (200, 303, 307, 308)
    rows = await db.query_all(
        "SELECT name FROM tags WHERE user_id = ?;",
        (auth_headers["admin_user"]["id"],),
    )
    assert rows == []


@pytest.mark.asyncio
async def test_rename_tag_via_form(client, db, auth_headers):
    _login(client, auth_headers)
    user_id = auth_headers["admin_user"]["id"]
    await _seed_item(db, user_id, "https://a.example/", ["oldname"])
    response = client.post(
        "/tags/rename", data={"old_name": "oldname", "new_name": "newname"}
    )
    assert response.status_code in (200, 303, 307, 308)
    row = await db.query_first(
        "SELECT name FROM tags WHERE user_id = ? AND name = ?;", (user_id, "newname")
    )
    assert row is not None


@pytest.mark.asyncio
async def test_delete_tag_via_form(client, db, auth_headers):
    _login(client, auth_headers)
    user_id = auth_headers["admin_user"]["id"]
    item_id = await _seed_item(db, user_id, "https://a.example/", ["goner", "stay"])
    response = client.post(
        "/tags/delete", data={"name": "goner"}, follow_redirects=False
    )
    assert response.status_code in (303, 307, 308)
    from src.models.items import get_item_tags

    assert await get_item_tags(db, item_id) == ["stay"]


@pytest.mark.asyncio
async def test_accept_suggestion_applies_tag(client, db, auth_headers):
    _login(client, auth_headers)
    user_id = auth_headers["admin_user"]["id"]
    item_id = await _seed_item(db, user_id, "https://a.example/", [])
    sugg_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO suggested_tags (id, item_id, user_id, phrase, score)"
        " VALUES (?, ?, ?, ?, ?);",
        (sugg_id, item_id, user_id, "edge caching", 4.0),
    )
    response = client.post(
        f"/items/{item_id}/suggestions/{sugg_id}/accept",
        data={"next": "/tags"},
        follow_redirects=False,
    )
    assert response.status_code in (303, 307, 308)
    from src.models.items import get_item_tags

    assert "edge caching" in await get_item_tags(db, item_id)
    row = await db.query_first(
        "SELECT status FROM suggested_tags WHERE id = ?;", (sugg_id,)
    )
    assert row["status"] == "accepted"


@pytest.mark.asyncio
async def test_dismiss_suggestion(client, db, auth_headers):
    _login(client, auth_headers)
    user_id = auth_headers["admin_user"]["id"]
    item_id = await _seed_item(db, user_id, "https://a.example/", [])
    sugg_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO suggested_tags (id, item_id, user_id, phrase, score)"
        " VALUES (?, ?, ?, ?, ?);",
        (sugg_id, item_id, user_id, "edge caching", 4.0),
    )
    response = client.post(
        f"/items/{item_id}/suggestions/{sugg_id}/dismiss",
        data={"next": "/tags"},
        follow_redirects=False,
    )
    assert response.status_code in (303, 307, 308)
    row = await db.query_first(
        "SELECT status FROM suggested_tags WHERE id = ?;", (sugg_id,)
    )
    assert row["status"] == "dismissed"


@pytest.mark.asyncio
async def test_accept_missing_suggestion_404(client, db, auth_headers):
    _login(client, auth_headers)
    user_id = auth_headers["admin_user"]["id"]
    item_id = await _seed_item(db, user_id, "https://a.example/", [])
    response = client.post(
        f"/items/{item_id}/suggestions/does-not-exist/accept", data={"next": "/tags"}
    )
    assert response.status_code == 404
