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
