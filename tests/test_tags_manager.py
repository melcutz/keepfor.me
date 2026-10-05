"""Model tests for tag merge, prune, and the untagged sentinel."""

import pytest

from src.auth.service import register_user
from src.models.items import (
    add_tags_to_item,
    create_tag,
    list_user_tags,
    merge_tags,
    prune_unused_tags,
)


async def _seed(db, user_id, item_id, url, tags):
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status)"
        " VALUES (?, ?, ?, ?, 'ok');",
        (item_id, user_id, url, url),
    )
    if tags:
        await add_tags_to_item(db, user_id, item_id, tags)


@pytest.mark.asyncio
async def test_merge_two_tags_moves_item_links(db):
    user = await register_user(db, "merge@keepfor.me", "password123")
    await _seed(db, user["id"], "m1", "https://example.com/1", ["ai"])
    await _seed(db, user["id"], "m2", "https://example.com/2", ["ml"])
    result = await merge_tags(db, user["id"], ["ai", "ml"], "tech")
    assert result == "merged"
    names = {t["name"]: t["count"] for t in await list_user_tags(db, user["id"])}
    assert names == {"tech": 2}


@pytest.mark.asyncio
async def test_merge_invalid_new_name_rejected(db):
    user = await register_user(db, "merge2@keepfor.me", "password123")
    await _seed(db, user["id"], "m1", "https://example.com/1", ["ai"])
    assert await merge_tags(db, user["id"], ["ai"], "bad/name") == "invalid"
    names = [t["name"] for t in await list_user_tags(db, user["id"])]
    assert names == ["ai"]


@pytest.mark.asyncio
async def test_prune_unused_deletes_only_zero_count(db):
    user = await register_user(db, "prune@keepfor.me", "password123")
    await create_tag(db, user["id"], "empty")
    await _seed(db, user["id"], "m1", "https://example.com/1", ["used"])
    assert await prune_unused_tags(db, user["id"]) == 1
    names = [t["name"] for t in await list_user_tags(db, user["id"])]
    assert names == ["used"]


@pytest.mark.asyncio
async def test_untagged_sentinel_cannot_be_created(db):
    user = await register_user(db, "sent@keepfor.me", "password123")
    assert await create_tag(db, user["id"], "__untagged__") is None
    assert await list_user_tags(db, user["id"]) == []
