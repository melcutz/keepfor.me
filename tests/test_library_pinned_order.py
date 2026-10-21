# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Pinned items lead the browse feed, in every view and every branch (#39, #40).

The feed is one list ordered by is_pinned DESC, created_at DESC: a save
pinned last year still opens page 1, in Cards, List and Compact, and under
every SQL branch of get_recent_items (plain, tag-filtered, untagged).
Search keeps its relevance order.
"""

import re

import pytest
from fastapi.testclient import TestClient

from keepfor import app as app_module
from keepfor.auth.service import login_user, register_user
from keepfor.models.items import save_item, save_note, toggle_pin_item
from keepfor.search.engine import get_recent_items, hybrid_search, search_fts

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


async def _seed(db, email, items):
    """Saves the items of a feed and returns their ids.

    Each item is a (slug, created_at, pinned, tags) tuple.
    """
    user = await register_user(db, email, "password123")
    env = MockEnv()
    ids: dict[str, str] = {}

    async def add(slug, created_at, *, pinned=False, tags=None):
        item, _ = await save_item(
            db, env, user["id"], f"https://example.com/{slug}", tags or []
        )
        if pinned:
            await toggle_pin_item(db, user["id"], item["id"])
        await db.execute(
            "UPDATE items SET created_at = ?, title = ?, status = 'saved'"
            " WHERE id = ?;",
            (created_at, slug, item["id"]),
        )
        ids[slug] = item["id"]
        return item["id"]

    for slug, created_at, pinned, tags in items:
        await add(slug, created_at, pinned=pinned, tags=tags)

    _, session_id = await login_user(db, email, "password123")
    ids["user_id"] = user["id"]
    ids["session"] = session_id
    return ids


@pytest.fixture
async def feed(db):
    """A user whose oldest save is pinned; the eleven newer ones are not."""
    items = [
        ("pinned-old", "2024-01-01 00:00:00", True, None),
        ("new-tagged", "2024-06-01 00:00:00", False, ["keepme"]),
    ]
    items += [
        (f"new-{n}", f"2024-06-{n + 2:02d} 00:00:00", False, None) for n in range(9)
    ]
    ids = await _seed(db, "pinned@keepfor.me", items)
    # A note, so the type filter has something to keep.
    note = await save_note(db, MockEnv(), ids["user_id"], "note-0", "A note body.")
    await db.execute(
        "UPDATE items SET created_at = ? WHERE id = ?;",
        ("2024-06-11 00:00:00", note["id"]),
    )
    ids["note-0"] = note["id"]
    # The unpinned saves, newest first.
    ids["unpinned_order"] = [
        ids["note-0"],
        *[ids[f"new-{n}"] for n in reversed(range(9))],
        ids["new-tagged"],
    ]
    return ids


@pytest.fixture
async def tagged_feed(db):
    """A user whose oldest save is pinned *and* tagged, so the tag branch keeps it."""
    return await _seed(
        db,
        "pinned-tagged@keepfor.me",
        [
            ("pinned-old", "2024-01-01 00:00:00", True, ["keepme"]),
            ("new-tagged", "2024-06-01 00:00:00", False, ["keepme"]),
            ("new-0", "2024-06-02 00:00:00", False, None),
        ],
    )


def _card_ids(html: str) -> list[str]:
    return re.findall(r'id="item-card-([^"]+)"', html)


def _assert_pinned_lead(items: list[dict]):
    """No unpinned item may sort above a pinned one."""
    assert all(
        items[i]["is_pinned"] >= items[i + 1]["is_pinned"]
        for i in range(len(items) - 1)
    ), "a pinned item sorts below an unpinned one"


@pytest.mark.parametrize("view", VIEWS)
async def test_pinned_item_leads_page_one_in_every_view(client, db, feed, view):
    """The pinned save opens the feed, in Cards, List and Compact alike."""
    client.cookies["kfm_session"] = feed["session"]
    res = client.get("/", params={"view": view})
    assert res.status_code == 200
    ids = _card_ids(res.text)
    # Pinned first, then the unpinned saves newest first.
    assert ids == [
        feed["pinned-old"],
        *feed["unpinned_order"],
    ]


async def test_plain_branch_puts_the_pinned_item_first(db, feed):
    """The unfiltered branch of get_recent_items leads with the pinned save."""
    items = await get_recent_items(db, feed["user_id"])
    assert items[0]["id"] == feed["pinned-old"]
    _assert_pinned_lead(items)


async def test_untagged_branch_puts_the_pinned_item_first(db, feed):
    """The untagged branch leads with the pinned save too."""
    items = await get_recent_items(db, feed["user_id"], untagged=True)
    assert items[0]["id"] == feed["pinned-old"]
    _assert_pinned_lead(items)


async def test_tag_branch_puts_the_pinned_item_first(db, tagged_feed):
    """The tag-filtered branch leads with the pinned save too."""
    items = await get_recent_items(db, tagged_feed["user_id"], tags=["keepme"])
    assert items[0]["id"] == tagged_feed["pinned-old"]
    _assert_pinned_lead(items)


async def test_a_filter_that_excludes_the_pinned_item_hides_it(db, feed):
    """The feed is one query: a filter that excludes it drops it entirely."""
    # The tag filter keeps only the tagged saves, and the pinned one is not
    # among them.
    tagged = await get_recent_items(db, feed["user_id"], tags=["keepme"])
    assert [i["id"] for i in tagged] == [feed["new-tagged"]]

    # The untagged filter keeps the untagged pinned save and drops the other.
    untagged = await get_recent_items(db, feed["user_id"], untagged=True)
    assert feed["new-tagged"] not in {i["id"] for i in untagged}
    assert untagged[0]["id"] == feed["pinned-old"]

    # A type filter (notes only) excludes the pinned save.
    notes = await get_recent_items(db, feed["user_id"], category="notes")
    assert [i["id"] for i in notes] == [feed["note-0"]]


async def test_the_pinned_item_appears_once_across_the_pages(client, db, feed):
    """One list, one copy: the pinned save is not repeated by a pinned shelf."""
    client.cookies["kfm_session"] = feed["session"]
    seen: list[str] = []
    for page in (1, 2):
        res = client.get("/", params={"page": page, "per_page": 10})
        assert res.status_code == 200
        seen.extend(_card_ids(res.text))
    assert len(seen) == 12
    assert seen.count(feed["pinned-old"]) == 1
    assert len(seen) == len(set(seen)), "an item is rendered twice"


async def test_search_stays_relevance_ordered(db):
    """A query is ranked by relevance: the pinned save is not hoisted."""
    user = await register_user(db, "pinned-search@keepfor.me", "password123")
    env = MockEnv()

    pinned, _ = await save_item(db, env, user["id"], "https://example.com/rust-old")
    await toggle_pin_item(db, user["id"], pinned["id"])
    await db.execute(
        "UPDATE items SET created_at = ?, title = 'Rust', status = 'saved'"
        " WHERE id = ?;",
        ("2024-01-01 00:00:00", pinned["id"]),
    )
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?, ?, 'Rust', ?);",
        (pinned["id"], user["id"], "rust " + "filler " * 60),
    )

    best, _ = await save_item(db, env, user["id"], "https://example.com/rust-new")
    await db.execute(
        "UPDATE items SET created_at = ?, title = 'Rust', status = 'saved'"
        " WHERE id = ?;",
        ("2024-06-01 00:00:00", best["id"]),
    )
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?, ?, 'Rust', 'rust rust rust rust rust');",
        (best["id"], user["id"]),
    )

    hits = await search_fts(db, user["id"], "rust", limit=10)
    expected = [h["item_id"] for h in hits]
    assert expected == [best["id"], pinned["id"]]

    results, _ = await hybrid_search(db, None, user["id"], "rust", mode="keyword")
    assert [r["id"] for r in results] == expected
    assert results[0]["id"] == best["id"]
