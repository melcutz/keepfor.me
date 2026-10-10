# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Ranked tag suggestions and bulk updates."""

import pytest

from keepfor.auth.service import register_user
from keepfor.models.items import add_tags_to_item, suggest_tags


async def _seed(db, user_id, item_id, url, tags):
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status)"
        " VALUES (?, ?, ?, ?, 'ok');",
        (item_id, user_id, url, url),
    )
    if tags:
        await add_tags_to_item(db, user_id, item_id, tags)


@pytest.mark.asyncio
async def test_suggest_ranks_frequent_first_then_prefix(db):
    user = await register_user(db, "sg@keepfor.me", "password123")
    await _seed(db, user["id"], "s1", "https://example.com/1", ["tech", "ai"])
    await _seed(db, user["id"], "s2", "https://example.com/2", ["tech"])
    await _seed(db, user["id"], "s3", "https://example.com/3", ["cooking"])
    # Distinct timestamps: CURRENT_TIMESTAMP has 1s precision, so ties would
    # make the recency tiebreak nondeterministic.
    await db.execute("UPDATE items SET created_at = '2026-01-01 00:00:01';")
    await db.execute(
        "UPDATE items SET created_at = '2026-01-01 00:00:03' WHERE id = 's3';"
    )
    assert [r["name"] for r in await suggest_tags(db, user["id"], "")] == [
        "tech",
        "cooking",
        "ai",
    ]
    assert [r["name"] for r in await suggest_tags(db, user["id"], "a")] == ["ai"]
    assert await suggest_tags(db, user["id"], "", exclude=["tech"]) == [
        {"name": "cooking", "count": 1},
        {"name": "ai", "count": 1},
    ]


@pytest.mark.asyncio
async def test_suggest_escapes_like_wildcards(db):
    user = await register_user(db, "sg2@keepfor.me", "password123")
    await _seed(db, user["id"], "w1", "https://example.com/1", ["tech"])
    assert await suggest_tags(db, user["id"], "%") == []
    assert await suggest_tags(db, user["id"], "_") == []


@pytest.mark.asyncio
async def test_suggest_caps_fetch_on_huge_exclude(db):
    user = await register_user(db, "sg3@keepfor.me", "password123")
    await _seed(db, user["id"], "w1", "https://example.com/1", ["tech"])
    huge = [f"nope{i}" for i in range(5000)]
    got = await suggest_tags(db, user["id"], "", exclude=huge)
    assert [r["name"] for r in got] == ["tech"]


@pytest.mark.asyncio
async def test_bulk_add_and_remove_roundtrip(db):
    from keepfor.models.items import bulk_update_tags, get_item

    user = await register_user(db, "blk@keepfor.me", "password123")
    await _seed(db, user["id"], "b1", "https://example.com/1", ["old"])
    await _seed(db, user["id"], "b2", "https://example.com/2", ["old"])
    done = await bulk_update_tags(db, user["id"], ["b1", "b2"], ["new"], ["old"])
    assert done == 2
    assert (await get_item(db, user["id"], "b1"))["tags"] == ["new"]
    assert (await get_item(db, user["id"], "b2"))["tags"] == ["new"]


@pytest.mark.asyncio
async def test_bulk_ignores_foreign_items_and_caps_ids(db):
    from keepfor.models.items import bulk_update_tags, get_item_tags

    user = await register_user(db, "blk2@keepfor.me", "password123")
    other = await register_user(
        db, "blk3@keepfor.me", "password123", allow_public_signups=True
    )
    await _seed(db, other["id"], "bx", "https://example.com/x", [])
    ids = ["bx"] + ["missing-%d" % i for i in range(150)]
    assert await bulk_update_tags(db, user["id"], ids, ["hi"], []) == 0
    assert await get_item_tags(db, "bx") == []
