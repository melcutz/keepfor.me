# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""?view= survives every navigation the library page offers.

The layout was a URL parameter the server honoured only on the first load:
the pager, the category tabs, the status chips, the tag filters and the
search all dropped it, so the next request rendered the default layout and
the client fell back to the saved preference (localStorage) instead.
"""

import html as html_lib
import re

import pytest
from fastapi.testclient import TestClient

from keepfor import app as app_module
from keepfor.auth.service import login_user, register_user
from keepfor.models.items import save_item

VIEWS = ("cards", "list", "compact")


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
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: MockEnv())
    return TestClient(app_module.app)


@pytest.fixture
async def library(db):
    """A user with a tagged item and enough saves for a second page."""
    user = await register_user(db, "viewparam@keepfor.me", "password123")
    env = MockEnv()
    await save_item(db, env, user["id"], "https://example.com/one", ["keepme"])
    for n in range(11):
        await save_item(db, env, user["id"], f"https://example.com/pg{n}")
    _, session_id = await login_user(db, "viewparam@keepfor.me", "password123")
    return {"session": session_id}


def _region(html: str) -> str:
    """The library page's own markup, without the header or the mobile nav."""
    return html[html.index('id="library-page"') : html.index("</main>")]


def _nav_urls(html: str) -> list[str]:
    """The library's own navigation URLs: the pager, the tabs, the filters."""
    raw = re.findall(r'(?:href|hx-push-url)="([^"]*)"', _region(html))
    urls = [html_lib.unescape(u) for u in raw]
    return [u for u in urls if u == "/" or u.startswith("/?")]


def _push_urls(html: str) -> list[str]:
    """The URLs the pager pushes into the address bar."""
    return [html_lib.unescape(u) for u in re.findall(r'hx-push-url="([^"]*)"', html)]


def _status_pills(html: str) -> str:
    match = re.search(r'<div id="status-filters".*?</div>', html, re.S)
    assert match, "the status filters are not rendered"
    return match.group(0)


def _switcher(html: str) -> dict[str, str]:
    """The layout switcher: button name -> its rendered aria-pressed."""
    buttons = re.findall(
        r'<button[^>]*data-view-btn="([^"]+)"[^>]*aria-pressed="([^"]*)"', html
    )
    assert buttons, "the layout switcher is not rendered"
    return dict(buttons)


@pytest.mark.parametrize("view", VIEWS)
async def test_view_is_carried_into_every_library_url(client, db, library, view):
    """Every link keeps the requested layout, and the switcher matches it."""
    client.cookies["kfm_session"] = library["session"]
    res = client.get("/", params={"view": view, "per_page": 10})
    assert res.status_code == 200

    # Rendered layout matches ?view=, and the matching switcher button is pressed.
    assert f'data-view="{view}"' in res.text
    switcher = _switcher(res.text)
    assert set(switcher) == set(VIEWS)
    for name, pressed in switcher.items():
        assert pressed == ("true" if name == view else "false")

    # Every navigation link in the library carries view=<view>.
    urls = _nav_urls(res.text)
    assert urls, "the library page offers no navigation URLs"
    for url in urls:
        assert f"view={view}" in url, f"{url} drops the layout"

    # Pager keeps both the layout and the requested per_page.
    pushes = _push_urls(res.text)
    assert pushes, "the pager offers no page links"
    for url in pushes:
        assert f"view={view}" in url
        assert "per_page=10" in url

    # Search form and status chips carry the layout.
    assert f'name="view" value="{view}"' in res.text
    assert "#active-view-input" in _status_pills(res.text)
    search = re.search(r'<input[^>]*id="search-input"[^>]*>', res.text, re.S)
    assert search and "#active-view-input" in search.group(0)


async def test_no_view_parameter_keeps_the_links_view_free(client, db, library):
    """Without an explicit layout the page stays neutral: the client decides."""
    client.cookies["kfm_session"] = library["session"]
    res = client.get("/")
    assert res.status_code == 200
    for url in _nav_urls(res.text):
        assert "view=" not in url, f"{url} pins a layout"
    assert 'name="view" value=""' in res.text
    assert 'data-view="list"' in res.text


async def test_search_endpoint_keeps_the_layout(client, db, library):
    """The htmx search re-renders the pager: it keeps the layout too."""
    client.cookies["kfm_session"] = library["session"]
    res = client.post(
        "/search",
        data={
            "query": "",
            "tag": "",
            "status": "",
            "page": 2,
            "per_page": 10,
            "view": "compact",
        },
    )
    assert res.status_code == 200
    pushes = _push_urls(res.text)
    assert pushes
    for url in pushes:
        assert "view=compact" in url
        assert "per_page=10" in url


async def test_client_script_keeps_the_url_view_and_syncs_the_links(
    client, db, library
):
    """?view= wins over the saved preference, and the links follow it."""
    client.cookies["kfm_session"] = library["session"]
    res = client.get("/", params={"view": "cards"})
    assert res.status_code == 200
    scripts = "\n".join(
        re.findall(r"<script\b[^>]*>(.*?)</script\s*>", res.text, re.S | re.I)
    )
    assert "kfm_view" in scripts
    # The URL is read before the saved preference, so a shared link wins.
    assert scripts.index("urlView") < scripts.index("localStorage.getItem('kfm_view')")
    # The chosen layout is written into the links the page builds.
    assert "withView" in scripts
    assert "syncViewLinks" in scripts
