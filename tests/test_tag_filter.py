# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Multi-tag AND filtering and the untagged sentinel."""

import pytest

from src.auth.service import register_user
from src.models.items import add_tags_to_item
from src.search.engine import (
    count_recent_items,
    get_recent_items,
    hybrid_search,
    parse_tag_filter,
)


async def _seed(db, user_id, item_id, url, tags):
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status)"
        " VALUES (?, ?, ?, ?, 'ok');",
        (item_id, user_id, url, url),
    )
    if tags:
        await add_tags_to_item(db, user_id, item_id, tags)


def test_parse_tag_filter_splits_and_caps():
    tags, untagged = parse_tag_filter(" tech, AI ,,tech")
    assert tags == ["tech", "ai"]
    assert untagged is False
    tags, untagged = parse_tag_filter("__untagged__")
    assert (tags, untagged) == ([], True)
    tags, untagged = parse_tag_filter("tech, __untagged__")
    assert (tags, untagged) == ([], True)
    assert parse_tag_filter("") == ([], False)
    assert parse_tag_filter(None) == ([], False)
    many, _ = parse_tag_filter(",".join(f"t{i}" for i in range(12)))
    assert len(many) == 10


@pytest.mark.asyncio
async def test_browse_multi_tag_returns_intersection(db):
    user = await register_user(db, "flt@keepfor.me", "password123")
    await _seed(db, user["id"], "f1", "https://example.com/1", ["tech", "ai"])
    await _seed(db, user["id"], "f2", "https://example.com/2", ["tech"])
    items = await get_recent_items(db, user["id"], tags=["tech", "ai"])
    assert [i["id"] for i in items] == ["f1"]
    assert await count_recent_items(db, user["id"], tags=["tech", "ai"]) == 1


@pytest.mark.asyncio
async def test_browse_untagged_returns_only_tagless(db):
    user = await register_user(db, "flt2@keepfor.me", "password123")
    await _seed(db, user["id"], "f1", "https://example.com/1", ["tech"])
    await _seed(db, user["id"], "f2", "https://example.com/2", [])
    items = await get_recent_items(db, user["id"], untagged=True)
    assert [i["id"] for i in items] == ["f2"]
    assert await count_recent_items(db, user["id"], untagged=True) == 1


@pytest.mark.asyncio
async def test_legacy_single_tag_still_works(db):
    user = await register_user(db, "flt3@keepfor.me", "password123")
    await _seed(db, user["id"], "f1", "https://example.com/1", ["Tech"])
    items = await get_recent_items(db, user["id"], tag="tech")
    assert [i["id"] for i in items] == ["f1"]
    items, total = await hybrid_search(db, None, user["id"], "", tag="tech")
    assert total == 1


@pytest.mark.asyncio
async def test_mixed_case_tag_matches_text_query_path(db):
    user = await register_user(db, "flt4@keepfor.me", "password123")
    await _seed(db, user["id"], "f1", "https://example.com/1", ["Tech"])
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?, ?, ?, ?);",
        ("f1", user["id"], "Example article", "Example content about example things"),
    )
    items, total = await hybrid_search(
        db, None, user["id"], "example", mode="keyword", tag="Tech"
    )
    assert total == 1
    assert [i["id"] for i in items] == ["f1"]


@pytest.mark.asyncio
async def test_untagged_filter_applies_to_text_query_path(db):
    user = await register_user(db, "flt5@keepfor.me", "password123")
    await _seed(db, user["id"], "f1", "https://example.com/1", ["tech"])
    await _seed(db, user["id"], "f2", "https://example.com/2", [])
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?, ?, ?, ?);",
        ("f1", user["id"], "Example article", "Example content about example things"),
    )
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?, ?, ?, ?);",
        ("f2", user["id"], "Example other", "More example content here"),
    )
    items, total = await hybrid_search(
        db, None, user["id"], "example", mode="keyword", untagged=True
    )
    assert total == 1
    assert [i["id"] for i in items] == ["f2"]
