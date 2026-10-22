# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Every library view lists the items the counts promise (issue #39).

Cards, List and Compact used to hide pinned items (and the pinned shelf
was cards-only), so the feed showed less than the category and status
counts: the Archive category looked empty in List/Compact.
"""

import re
from html.parser import HTMLParser
from urllib.parse import urlparse

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


class _ImgCollector(HTMLParser):
    """Collects ``<img>`` attributes and ``<style>`` blocks."""

    def __init__(self) -> None:
        super().__init__()
        self.imgs: list[dict[str, str | None]] = []
        self.styles: list[str] = []
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "img":
            self.imgs.append(dict(attrs))
        elif tag == "style":
            self._in_style = True

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self.styles.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False


def _parse_page(html: str) -> tuple[list[dict[str, str | None]], str]:
    parser = _ImgCollector()
    parser.feed(html)
    parser.close()
    return parser.imgs, "".join(parser.styles)


async def test_list_view_renders_compact_rows_with_thumbnails_and_fallback(
    client, db, library
):
    """List view uses a 48x48 thumbnail (or favicon) and hides hero images (#96)."""
    await db.execute(
        "UPDATE items SET title = ?, image_url = ?, excerpt = ?, summary = ?,"
        " user_notes = ?, created_at = '2026-10-22 10:00:00' WHERE id = ?;",
        (
            "Illustrated Guide",
            "https://example.com/thumb.jpg",
            "Long excerpt text",
            "Short summary",
            "Personal note",
            library["pinned"],
        ),
    )
    await db.execute(
        "UPDATE items SET title = ?, created_at = '2026-10-21 09:00:00' WHERE id = ?;",
        ("Text Only Save", library["plain"]),
    )

    client.cookies["kfm_session"] = library["session"]
    res = client.get("/", params={"view": "list"})
    assert res.status_code == 200
    html = res.text

    # Row content: title, domain, tags and date are present.
    assert "Illustrated Guide" in html
    assert "Text Only Save" in html
    card_link = re.search(
        r'href="(https?://[^"]+)"[^>]*title="Visit original link"', html
    )
    assert card_link
    assert urlparse(card_link.group(1)).hostname == "example.com"
    assert "#keepme" in html
    assert "2026-10-22" in html

    # All card images use loading=lazy and explicit integer width/height.
    imgs, css = _parse_page(html)
    assert imgs
    for img in imgs:
        assert img.get("loading") == "lazy", img
        assert (img.get("width") or "").isdigit() and int(img["width"]) > 0, img
        assert (img.get("height") or "").isdigit() and int(img["height"]) > 0, img

    thumb_imgs = [
        img for img in imgs if "card-thumb-img" in (img.get("class") or "").split()
    ]
    assert len(thumb_imgs) == 1
    assert thumb_imgs[0]["src"] == "https://example.com/thumb.jpg"
    assert thumb_imgs[0]["width"] == "48"
    assert thumb_imgs[0]["height"] == "48"

    # List view CSS hides the full-width cover, excerpt, note and summary details,
    # sizes the thumbnail slot to 3rem (48px) and keeps the row padding compact.
    assert "#items-list.view-list .card-cover" in css
    assert "#items-list.view-list .card-thumb" in css
    assert "width: 3rem; height: 3rem" in css
    assert (
        "#items-list.view-list .card-thumb .card-thumb-img { display: block; }" in css
    )
    assert "#items-list.view-list .item-card { padding: 0.5rem 0.75rem; }" in css

    # Grid view (cards) keeps the cover image and hides the list-only thumbnail.
    cards_res = client.get("/", params={"view": "cards"})
    assert cards_res.status_code == 200
    cards_imgs, _ = _parse_page(cards_res.text)
    cover_imgs = [
        img
        for img in cards_imgs
        if img.get("src") == "https://example.com/thumb.jpg"
        and "card-thumb-img" not in (img.get("class") or "").split()
    ]
    assert len(cover_imgs) == 1
    assert cover_imgs[0]["width"] == "640"
    assert cover_imgs[0]["height"] == "360"
    assert "#items-list.view-cards .card-cover" not in css
