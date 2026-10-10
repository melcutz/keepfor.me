# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Endpoint tests for library pagination and card tag mutation."""

import pytest
from fastapi.testclient import TestClient

from keepfor import app as app_module
from keepfor.auth.service import create_pat, register_user
from keepfor.models.items import save_item


class FakeQueue:
    def __init__(self):
        self.sent = []

    async def send(self, message):
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
    monkeypatch.setattr(app_module, "get_db", lambda request: db)
    monkeypatch.setattr(app_module, "get_env_from_request", lambda request: MockEnv())
    return TestClient(app_module.app)


@pytest.fixture
async def paging_setup(db):
    user = await register_user(db, "paging@keepfor.me", "password123")
    env = MockEnv()
    for n in range(25):
        await save_item(db, env, user["id"], f"https://example.com/pg{n}")
    pat = await create_pat(db, user["id"], "Paging PAT")
    return {"user": user, "headers": {"Authorization": f"Bearer {pat['token']}"}}


async def _login(client, db, email="paging@keepfor.me"):
    from keepfor.auth.service import login_user

    _, session_id = await login_user(db, email, "password123")
    client.cookies["kfm_session"] = session_id


async def test_library_page_two_per_page(client, db, paging_setup):
    await _login(client, db)
    res = client.get("/", params={"per_page": 10})
    assert res.status_code == 200
    assert res.text.count('id="item-card-') == 10
    assert "Showing 1–10 of 25 saves" in res.text


async def test_library_clamps_bad_per_page(client, db, paging_setup):
    await _login(client, db)
    res = client.get("/", params={"per_page": 999})
    assert res.status_code == 200
    assert 'value="20"' in res.text


async def test_library_out_of_range_page_shows_last(client, db, paging_setup):
    await _login(client, db)
    res = client.get("/", params={"per_page": 10, "page": 99})
    assert res.status_code == 200
    assert res.text.count('id="item-card-') == 5
    assert "Showing 21–25 of 25 saves" in res.text


async def test_search_returns_oob_pager(client, db, paging_setup):
    await _login(client, db)
    res = client.post(
        "/search",
        data={"query": "", "tag": "", "status": "", "page": 2, "per_page": 10},
    )
    assert res.status_code == 200
    assert 'hx-swap-oob="true"' in res.text
    assert "Showing 11–20 of 25 saves" in res.text


async def test_tag_add_and_remove_roundtrip(client, db, paging_setup):
    await _login(client, db)
    from keepfor.models.items import get_item

    page = client.get("/", params={"per_page": 10})
    item_id = page.text.split('id="item-card-')[1].split('"')[0]

    added = client.post(f"/items/{item_id}/tags", data={"add": "triage"})
    assert added.status_code == 200
    assert "#triage" in added.text
    assert (await get_item(db, paging_setup["user"]["id"], item_id))["tags"] == [
        "triage"
    ]

    removed = client.post(f"/items/{item_id}/tags", data={"remove": "triage"})
    assert removed.status_code == 200
    assert "#triage" not in removed.text


async def test_tag_mutation_requires_auth(client, db, paging_setup):
    res = client.post("/items/whatever/tags", data={"add": "x"})
    assert res.status_code == 401


async def test_tag_mutation_404_unknown_item(client, db, paging_setup):
    await _login(client, db)
    res = client.post("/items/does-not-exist/tags", data={"add": "x"})
    assert res.status_code == 404


async def test_page_two_shows_different_items(client, db, paging_setup):
    """Regression: an in-range page>1 must return different cards, not page 1."""
    import re

    await _login(client, db)
    p1 = client.get("/", params={"per_page": 10})
    ids1 = re.findall(r'id="item-card-([^"]+)"', p1.text)
    assert len(ids1) == 10
    p2 = client.get("/", params={"per_page": 10, "page": 2})
    ids2 = re.findall(r'id="item-card-([^"]+)"', p2.text)
    assert len(ids2) == 10
    assert set(ids1).isdisjoint(set(ids2))

    r = client.post(
        "/search",
        data={"query": "", "tag": "", "status": "", "page": 2, "per_page": 10},
    )
    ids3 = re.findall(r'id="item-card-([^"]+)"', r.text)
    assert set(ids3) == set(ids2)
