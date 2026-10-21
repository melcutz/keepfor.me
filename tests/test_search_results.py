# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Search results, counts and empty states must tell the same story."""

import re
import uuid

import pytest
from fastapi.testclient import TestClient

from keepfor import app as app_module
from keepfor.auth.service import login_user, register_user


class FakeQueue:
    def __init__(self):
        self.sent = []

    async def send(self, message):
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
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: MockEnv())
    return TestClient(app_module.app)


@pytest.fixture
async def search_setup(db):
    await register_user(db, "search@keepfor.me", "password123")
    user, session_id = await login_user(db, "search@keepfor.me", "password123")
    for n in range(3):
        item_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO items (id, user_id, url, canonical_url, title, status)"
            " VALUES (?, ?, ?, ?, ?, 'saved');",
            (
                item_id,
                user["id"],
                f"https://example.com/alpha{n}",
                f"https://example.com/alpha{n}",
                f"Alpha guide {n}",
            ),
        )
        await db.execute(
            "INSERT INTO items_fts (item_id, user_id, title, content_text)"
            " VALUES (?, ?, ?, ?);",
            (item_id, user["id"], f"Alpha guide {n}", f"alpha content {n}"),
        )
    for n in range(5):
        await db.execute(
            "INSERT INTO items (id, user_id, url, canonical_url, title, status)"
            " VALUES (?, ?, ?, ?, ?, 'saved');",
            (
                str(uuid.uuid4()),
                user["id"],
                f"https://example.com/other{n}",
                f"https://example.com/other{n}",
                f"Other save {n}",
            ),
        )
    return {"user": user, "session": session_id}


def _login(client, session_id):
    client.cookies["kfm_session"] = session_id


def _rendered_ids(html):
    return re.findall(r'id="item-card-([^"]+)"', html)


def _library_total(html):
    match = re.search(r'<span id="library-total"[^>]*>(\d+)</span>', html)
    assert match, "library-total span missing"
    return int(match.group(1))


async def test_search_count_matches_rendered_items(client, db, search_setup):
    _login(client, search_setup["session"])
    res = client.post("/search", data={"query": "alpha", "tag": ""})
    assert res.status_code == 200
    assert len(_rendered_ids(res.text)) == 3
    assert _library_total(res.text) == 3
    assert "of 3 saves" in res.text


async def test_library_query_count_matches_rendered_items(client, db, search_setup):
    """A direct /?q= load must not show the whole-library count either."""
    _login(client, search_setup["session"])
    res = client.get("/", params={"q": "alpha"})
    assert res.status_code == 200
    assert len(_rendered_ids(res.text)) == 3
    assert _library_total(res.text) == 3
    assert "of 3 saves" in res.text


async def test_search_renders_no_score_label(client, db, search_setup):
    _login(client, search_setup["session"])
    res = client.post("/search", data={"query": "alpha", "tag": ""})
    assert "score:" not in res.text
    assert "rrf_score" not in res.text


async def test_search_zero_results_empty_state(client, db, search_setup):
    _login(client, search_setup["session"])
    res = client.post("/search", data={"query": "nonexistentterm", "tag": ""})
    assert res.status_code == 200
    assert _rendered_ids(res.text) == []
    assert _library_total(res.text) == 0
    assert "No results for" in res.text
    assert "nonexistentterm" in res.text
    assert "Clear search" in res.text


async def test_library_query_zero_results_empty_state(client, db, search_setup):
    _login(client, search_setup["session"])
    res = client.get("/", params={"q": "nonexistentterm"})
    assert res.status_code == 200
    assert _rendered_ids(res.text) == []
    assert _library_total(res.text) == 0
    assert "No results for" in res.text
    assert "Clear search" in res.text
