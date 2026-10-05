"""Ranked tag suggestions and bulk updates."""

import pytest

from src.auth.service import register_user
from src.models.items import add_tags_to_item, suggest_tags


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
