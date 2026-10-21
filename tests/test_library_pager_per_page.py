# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""The pager links carry the page size only when it is not the default (#62).

The pager used to pin per_page=20 on every prev/next/page link, so the size
the user picked was overridden as soon as the page moved. A link that carries
the chosen size keeps it; a link that carries nothing leaves the default
alone.
"""

import html as html_lib
import re

import pytest
from fastapi.testclient import TestClient

from keepfor import app as app_module
from keepfor.auth.service import login_user, register_user
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
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: MockEnv())
    return TestClient(app_module.app)


@pytest.fixture
async def library(db):
    """A logged-in user with enough saves for a second page."""
    user = await register_user(db, "perpage@keepfor.me", "password123")
    env = MockEnv()
    for n in range(25):
        await save_item(db, env, user["id"], f"https://example.com/pg{n}")
    _, session_id = await login_user(db, "perpage@keepfor.me", "password123")
    return {"session": session_id}


def _push_urls(html: str) -> list[str]:
    """The URLs the pager pushes into the address bar."""
    return [html_lib.unescape(u) for u in re.findall(r'hx-push-url="([^"]*)"', html)]


async def test_pager_links_keep_a_chosen_per_page(client, db, library):
    """A picked page size survives the move to the next page."""
    client.cookies["kfm_session"] = library["session"]
    res = client.get("/", params={"per_page": 10})
    assert res.status_code == 200
    pushes = _push_urls(res.text)
    assert pushes, "the pager offers no page links"
    for url in pushes:
        assert "per_page=10" in url
        assert "page=" in url


async def test_pager_links_omit_the_default_per_page(client, db, library):
    """Without a choice the links stay free of per_page: nothing is pinned."""
    client.cookies["kfm_session"] = library["session"]
    res = client.get("/")
    assert res.status_code == 200
    pushes = _push_urls(res.text)
    assert pushes, "the pager offers no page links"
    for url in pushes:
        assert "per_page" not in url
        assert "page=" in url


async def test_search_pager_links_keep_a_chosen_per_page(client, db, library):
    """The htmx search re-renders the pager: it keeps the chosen size too."""
    client.cookies["kfm_session"] = library["session"]
    res = client.post(
        "/search",
        data={"query": "", "tag": "", "status": "", "page": 2, "per_page": 10},
    )
    assert res.status_code == 200
    pushes = _push_urls(res.text)
    assert pushes
    for url in pushes:
        assert "per_page=10" in url


async def test_search_pager_links_omit_the_default_per_page(client, db, library):
    """The re-rendered pager drops the default size, like the first render."""
    client.cookies["kfm_session"] = library["session"]
    res = client.post(
        "/search",
        data={"query": "", "tag": "", "status": "", "page": 2, "per_page": 20},
    )
    assert res.status_code == 200
    pushes = _push_urls(res.text)
    assert pushes
    for url in pushes:
        assert "per_page" not in url


async def test_pager_links_keep_the_other_params(client, db, library):
    """The layout and the filters still travel with the pager (#60)."""
    client.cookies["kfm_session"] = library["session"]
    res = client.get("/", params={"view": "compact", "status": "saved", "per_page": 50})
    assert res.status_code == 200
    pushes = _push_urls(res.text)
    assert pushes
    for url in pushes:
        assert "view=compact" in url
        assert "status=saved" in url
        assert "per_page=50" in url
