# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for tag management model funcs (create/rename-merge/delete)."""

import uuid

import pytest

from keepfor.models import items as items_model


async def _seed_item_with_tags(db, user_id, url, tags):
    item_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status)"
        " VALUES (?, ?, ?, ?, 'ok');",
        (item_id, user_id, url, url),
    )
    if tags:
        await items_model.add_tags_to_item(db, user_id, item_id, tags)
    return item_id


@pytest.mark.asyncio
async def test_create_tag_with_zero_items(db):
    user_id = str(uuid.uuid4())
    tag = await items_model.create_tag(db, user_id, "  Research ")
    assert tag == "research"
    row = await db.query_first(
        "SELECT id FROM tags WHERE user_id = ? AND name = ?;", (user_id, "research")
    )
    assert row is not None


@pytest.mark.asyncio
async def test_create_tag_invalid_returns_none(db):
    user_id = str(uuid.uuid4())
    assert await items_model.create_tag(db, user_id, "  ") is None
    assert await items_model.create_tag(db, user_id, "has/slash") is None


@pytest.mark.asyncio
async def test_rename_tag_moves_item_tags(db):
    user_id = str(uuid.uuid4())
    item_id = await _seed_item_with_tags(db, user_id, "https://a.example/", ["ml"])
    assert await items_model.rename_tag(db, user_id, "ml", "ai") == "merged"
    assert await items_model.get_item_tags(db, user_id, item_id) == ["ai"]
    assert (
        await db.query_first(
            "SELECT id FROM tags WHERE user_id = ? AND name = ?;", (user_id, "ml")
        )
        is None
    )


@pytest.mark.asyncio
async def test_rename_tag_into_existing_merges(db):
    user_id = str(uuid.uuid4())
    item_a = await _seed_item_with_tags(db, user_id, "https://a.example/", ["ml"])
    item_b = await _seed_item_with_tags(db, user_id, "https://b.example/", ["ai"])
    assert await items_model.rename_tag(db, user_id, "ml", "ai") == "merged"
    assert await items_model.get_item_tags(db, user_id, item_a) == ["ai"]
    assert await items_model.get_item_tags(db, user_id, item_b) == ["ai"]


@pytest.mark.asyncio
async def test_rename_tag_missing_or_invalid(db):
    user_id = str(uuid.uuid4())
    assert await items_model.rename_tag(db, user_id, "nope", "ai") == "not_found"
    assert await items_model.rename_tag(db, user_id, "ml", "bad/name") == "invalid"


@pytest.mark.asyncio
async def test_delete_tag_keeps_items_but_untags(db):
    user_id = str(uuid.uuid4())
    item_id = await _seed_item_with_tags(
        db, user_id, "https://a.example/", ["obsolete", "keep"]
    )
    assert await items_model.delete_tag(db, user_id, "obsolete") is True
    assert await items_model.get_item_tags(db, user_id, item_id) == ["keep"]
    # Item itself survives.
    assert await db.query_first("SELECT id FROM items WHERE id = ?;", (item_id,))


@pytest.mark.asyncio
async def test_delete_tag_missing_returns_false(db):
    assert await items_model.delete_tag(db, str(uuid.uuid4()), "nope") is False


@pytest.mark.asyncio
async def test_delete_tag_dismisses_matching_suggestions(db):
    user_id = str(uuid.uuid4())
    item_id = await _seed_item_with_tags(
        db, user_id, "https://a.example/", ["obsolete"]
    )
    await db.execute(
        "INSERT INTO suggested_tags (id, item_id, user_id, phrase, score)"
        " VALUES (?, ?, ?, ?, ?);",
        (str(uuid.uuid4()), item_id, user_id, "obsolete", 3.0),
    )
    await items_model.delete_tag(db, user_id, "obsolete")
    row = await db.query_first(
        "SELECT status FROM suggested_tags WHERE item_id = ?;", (item_id,)
    )
    assert row["status"] == "dismissed"


@pytest.mark.asyncio
async def test_unicode_tags_create_rename_merge_and_nfc(db):
    user_id = str(uuid.uuid4())
    assert (
        await items_model.create_tag(db, user_id, "Konsumgüterbranche")
        == "konsumgüterbranche"
    )
    assert await items_model.create_tag(db, user_id, "Straße") == "straße"
    assert await items_model.create_tag(db, user_id, "Café") == "café"

    # Decomposed u + combining diaeresis resolves to the same NFC row.
    assert (
        await items_model.create_tag(db, user_id, "konsumgu\u0308terbranche")
        == "konsumgüterbranche"
    )
    rows = await db.query_all(
        "SELECT name FROM tags WHERE user_id = ? AND name LIKE 'konsum%';",
        (user_id,),
    )
    assert [r["name"] for r in rows] == ["konsumgüterbranche"]

    # Save paths also NFC-normalise tags so composed and decomposed share one row.
    class _FakeQueue:
        async def send(self, msg):
            pass

    class _FakeEnv:
        QUEUE = _FakeQueue()

    saved, _ = await items_model.save_item(
        db,
        _FakeEnv(),
        user_id,
        "https://a.example/unicode",
        tags=["u\u0308bung", "übung", "straße"],
    )
    item_id = saved["id"]
    await items_model.add_tags_to_item(db, user_id, item_id, ["cafe\u0301", "café"])
    assert await items_model.get_item_tags(db, user_id, item_id) == [
        "café",
        "straße",
        "übung",
    ]

    assert (
        await items_model.rename_tag(db, user_id, "u\u0308bung", "prüfung") == "merged"
    )
    assert (
        await items_model.merge_tags(
            db, user_id, ["pru\u0308fung", "straße"], "konsumgu\u0308terbranche"
        )
        == "merged"
    )
    assert await items_model.get_item_tags(db, user_id, item_id) == [
        "café",
        "konsumgüterbranche",
    ]
