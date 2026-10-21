# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Saving a link keeps the library layout (issue #64).

The Save Link form posted no layout, so /save redirected to "/" and the
library came back in the saved preference (localStorage) instead of the
layout the page was in.
"""

from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from keepfor import app as app_module
from keepfor.auth.service import login_user, register_user
from tests.htmltools import script_bodies

VIEWS = ("cards", "list", "compact")


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
async def user(db):
    await register_user(db, "saveview@keepfor.me", "password123")
    _, session_id = await login_user(db, "saveview@keepfor.me", "password123")
    return {"session": session_id}


class _FormFields(HTMLParser):
    """The named inputs of one form, picked by the form's id."""

    def __init__(self, form_id: str) -> None:
        super().__init__()
        self.form_id = form_id
        self.fields: dict[str, str] = {}
        self._inside = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self._inside = attrs.get("id") == self.form_id
        elif tag == "input" and self._inside and attrs.get("name"):
            self.fields[attrs["name"]] = attrs.get("value") or ""

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self._inside = False


def _save_form_fields(html: str) -> dict[str, str]:
    """The inputs the Save Link form submits."""
    parser = _FormFields("save-link-form")
    parser.feed(html)
    parser.close()
    return parser.fields


@pytest.mark.parametrize("view", VIEWS)
async def test_save_returns_to_the_same_layout(client, db, user, view):
    """A save comes back to the library in the layout it was made from."""
    client.cookies["kfm_session"] = user["session"]
    res = client.post(
        "/save",
        data={"url": f"https://example.com/{view}", "tags": "", "view": view},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == f"/?view={view}"


@pytest.mark.parametrize("view", ["evil", "cards&x=1", "/../../etc"])
async def test_save_ignores_an_unknown_layout(client, db, user, view):
    """Only the three known layouts reach the redirect."""
    client.cookies["kfm_session"] = user["session"]
    res = client.post(
        "/save",
        data={"url": "https://example.com/evil", "tags": "", "view": view},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/"


async def test_save_without_a_layout_goes_home(client, db, user):
    """No layout in the form: the redirect is the plain library URL."""
    client.cookies["kfm_session"] = user["session"]
    res = client.post(
        "/save",
        data={"url": "https://example.com/plain", "tags": ""},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/"


@pytest.mark.parametrize("view", VIEWS)
async def test_library_page_renders_the_layout_in_the_save_form(client, db, user, view):
    """The form carries the layout of the page it was rendered on."""
    client.cookies["kfm_session"] = user["session"]
    res = client.get("/", params={"view": view})
    assert res.status_code == 200
    assert _save_form_fields(res.text)["view"] == view


async def test_library_page_without_a_layout_leaves_the_form_neutral(client, db, user):
    """No explicit layout: the form carries none, the client decides."""
    client.cookies["kfm_session"] = user["session"]
    res = client.get("/")
    assert res.status_code == 200
    assert _save_form_fields(res.text)["view"] == ""


async def test_the_layout_switch_updates_the_save_form(client, db, user):
    """Switching the layout also updates the form, so the next save keeps it."""
    client.cookies["kfm_session"] = user["session"]
    res = client.get("/", params={"view": "cards"})
    assert res.status_code == 200
    scripts = script_bodies(res.text)
    assert "save-view-input" in scripts
