# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Every library view lists the items the counts promise (issue #39).

Cards, List and Compact used to hide pinned items (and the pinned shelf
was cards-only), so the feed showed less than the category and status
counts: the Archive category looked empty in List/Compact.
"""

import re

import pytest
from fastapi.testclient import TestClient

from keepfor import app as app_module
from keepfor.auth.service import login_user, register_user
from keepfor.models.items import archive_item, save_item, toggle_pin_item

VIEWS = ("cards", "list", "compact")

# The filters under test: the default feed, the Archive category and a tag
# filter that only the pinned item carries.
SCENARIOS = {
    "all": ({}, ("plain", "archived", "pinned", "pinned_archived")),
    "archive": ({"category": "archive"}, ("archived", "pinned_archived")),
    "pinned": ({"tag": "keepme"}, ("pinned",)),
}


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
    """A user with a plain, an archived, a pinned and a pinned+archived item."""
    user = await register_user(db, "views@keepfor.me", "password123")
    env = MockEnv()
    plain, _ = await save_item(db, env, user["id"], "https://example.com/plain")
    archived, _ = await save_item(db, env, user["id"], "https://example.com/archived")
    await archive_item(db, user["id"], archived["id"])
    pinned, _ = await save_item(
        db, env, user["id"], "https://example.com/pinned", ["keepme"]
    )
    await toggle_pin_item(db, user["id"], pinned["id"])
    both, _ = await save_item(db, env, user["id"], "https://example.com/both")
    await toggle_pin_item(db, user["id"], both["id"])
    await archive_item(db, user["id"], both["id"])
    _, session_id = await login_user(db, "views@keepfor.me", "password123")
    return {
        "session": session_id,
        "plain": plain["id"],
        "archived": archived["id"],
        "pinned": pinned["id"],
        "pinned_archived": both["id"],
    }


def _card_ids(html: str) -> list[str]:
    return re.findall(r'id="item-card-([^"]+)"', html)


def _library_total(html: str) -> int:
    match = re.search(r'id="library-total">(\d+)<', html)
    assert match, "the library total count is not rendered"
    return int(match.group(1))


@pytest.mark.parametrize("view", VIEWS)
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_view_lists_the_items_the_counts_promise(
    client, db, library, view, scenario
):
    """Each view renders the matching items exactly once, count included."""
    params, expected_keys = SCENARIOS[scenario]
    client.cookies["kfm_session"] = library["session"]
    res = client.get("/", params={"view": view, **params})
    assert res.status_code == 200
    expected = {library[key] for key in expected_keys}
    rendered = _card_ids(res.text)
    assert set(rendered) == expected
    assert len(rendered) == len(expected), "an item is rendered twice"
    assert _library_total(res.text) == len(expected)


async def test_all_views_render_the_same_items(client, db, library):
    """Cards, List and Compact list the same items in the same order."""
    client.cookies["kfm_session"] = library["session"]
    per_view = {}
    for view in VIEWS:
        res = client.get("/", params={"view": view})
        assert res.status_code == 200
        per_view[view] = _card_ids(res.text)
    assert per_view["cards"] == per_view["list"] == per_view["compact"]


@pytest.mark.parametrize("view", VIEWS)
async def test_view_parameter_selects_the_layout(client, db, library, view):
    """The server renders the requested layout."""
    client.cookies["kfm_session"] = library["session"]
    res = client.get("/", params={"view": view})
    assert res.status_code == 200
    assert f'data-view="{view}"' in res.text
    assert f"view-{view}" in res.text


async def test_unknown_view_falls_back_to_the_default(client, db, library):
    client.cookies["kfm_session"] = library["session"]
    res = client.get("/", params={"view": "nope"})
    assert res.status_code == 200
    assert 'data-view="list"' in res.text


async def test_no_view_hides_pinned_items_with_css(client, db, library):
    """The list/compact views used to hide pinned items with a CSS rule.

    The feed is the only list of items now, so no rule may hide a card:
    a hidden card is an item the counts promise but the view drops.
    """
    client.cookies["kfm_session"] = library["session"]
    res = client.get("/")
    css = "".join(re.findall(r"<style>(.*?)</style>", res.text, re.S))
    hiding = [
        rule
        for rule in css.split("}")
        if "is-pinned" in rule and "display" in rule and "none" in rule
    ]
    assert not hiding, f"a CSS rule hides pinned items: {hiding}"
