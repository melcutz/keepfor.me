# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for notes, pins, user notes, archiving, and cover metadata (0006)."""

import pytest

from keepfor.auth.service import register_user
from keepfor.consumer.extractor import extract_article, extract_image_url
from keepfor.models.items import (
    archive_item,
    get_item,
    get_pinned_items,
    save_item,
    save_note,
    toggle_pin_item,
    unarchive_item,
    update_user_notes,
)
from keepfor.search.engine import get_recent_items, hybrid_search


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
async def user_with_env(db):
    user = await register_user(db, "notes@keepfor.me", "password123")
    return user, MockEnv(), db


@pytest.mark.asyncio
async def test_migration_0006_columns(db):
    rows = await db.query_all("PRAGMA table_info(items);")
    names = {r["name"] for r in rows}
    for col in (
        "item_type",
        "is_pinned",
        "user_notes",
        "image_url",
        "summary",
        "read_state",
    ):
        assert col in names


@pytest.mark.asyncio
async def test_toggle_pin_item(user_with_env):
    user, env, db = user_with_env
    item, _ = await save_item(db, env, user["id"], "https://example.com/pin1", [])
    assert await toggle_pin_item(db, user["id"], item["id"]) is True
    fetched = await get_item(db, user["id"], item["id"])
    assert fetched["is_pinned"] == 1
    assert await toggle_pin_item(db, user["id"], item["id"]) is False
    assert await toggle_pin_item(db, user["id"], "missing") is None


@pytest.mark.asyncio
async def test_get_pinned_items(user_with_env):
    user, env, db = user_with_env
    a, _ = await save_item(db, env, user["id"], "https://example.com/a", [])
    b, _ = await save_item(db, env, user["id"], "https://example.com/b", [])
    await toggle_pin_item(db, user["id"], b["id"])
    pinned = await get_pinned_items(db, user["id"])
    assert [p["id"] for p in pinned] == [b["id"]]
    assert a["id"] not in [p["id"] for p in pinned]


@pytest.mark.asyncio
async def test_get_pinned_items_batch_tags(user_with_env):
    user, env, db = user_with_env
    # Item 1 with 2 tags
    i1, _ = await save_item(
        db, env, user["id"], "https://example.com/pin1", ["tag1", "tag2"]
    )
    await toggle_pin_item(db, user["id"], i1["id"])

    # Item 2 with 0 tags
    i2, _ = await save_item(db, env, user["id"], "https://example.com/pin2", [])
    await toggle_pin_item(db, user["id"], i2["id"])

    # Item 3 with 1 tag
    i3, _ = await save_item(db, env, user["id"], "https://example.com/pin3", ["tag3"])
    await toggle_pin_item(db, user["id"], i3["id"])

    pinned = await get_pinned_items(db, user["id"])
    assert len(pinned) == 3
    tags_by_id = {p["id"]: p["tags"] for p in pinned}
    assert set(tags_by_id[i1["id"]]) == {"tag1", "tag2"}
    assert tags_by_id[i2["id"]] == []
    assert set(tags_by_id[i3["id"]]) == {"tag3"}


@pytest.mark.asyncio
async def test_save_note(user_with_env):
    user, env, db = user_with_env
    note = await save_note(
        db, env, user["id"], "My idea", "# Hello\nSome body text", ["ideas"]
    )
    assert note["item_type"] == "note"
    fetched = await get_item(db, user["id"], note["id"])
    assert fetched["item_type"] == "note"
    assert fetched["status"] == "saved"
    # FTS5 presence
    rows = await db.query_all(
        "SELECT item_id FROM items_fts WHERE item_id = ?;", (note["id"],)
    )
    assert rows
    # Unified search finds the note
    items, _ = await hybrid_search(db, env, user["id"], query="Hello")
    assert any(i["id"] == note["id"] for i in items)


@pytest.mark.asyncio
async def test_update_user_notes(user_with_env):
    user, env, db = user_with_env
    item, _ = await save_item(db, env, user["id"], "https://example.com/n", [])
    assert await update_user_notes(db, user["id"], item["id"], "Use for D1 migration")
    fetched = await get_item(db, user["id"], item["id"])
    assert fetched["user_notes"] == "Use for D1 migration"
    assert await update_user_notes(db, user["id"], "missing", "x") is False


@pytest.mark.asyncio
async def test_archive_item(user_with_env):
    user, env, db = user_with_env
    item, _ = await save_item(db, env, user["id"], "https://example.com/r", [])
    assert await archive_item(db, user["id"], item["id"]) is True
    fetched = await get_item(db, user["id"], item["id"])
    assert fetched["read_state"] == "archived"
    assert await unarchive_item(db, user["id"], item["id"]) is True
    fetched = await get_item(db, user["id"], item["id"])
    assert fetched["read_state"] == "unread"


@pytest.mark.asyncio
async def test_category_tabs(user_with_env):
    user, env, db = user_with_env
    url_item, _ = await save_item(db, env, user["id"], "https://example.com/post", [])
    note = await save_note(db, env, user["id"], "N", "body text", [])
    await archive_item(db, user["id"], url_item["id"])
    notes = await get_recent_items(db, user["id"], category="notes")
    assert {i["id"] for i in notes} == {note["id"]}
    archived = await get_recent_items(db, user["id"], category="archive")
    assert {i["id"] for i in archived} == {url_item["id"]}


def test_extractor_cover_image():
    html = (
        "<html><head>"
        '<meta property="og:image" content="https://example.com/cover.jpg">'
        "<title>T</title></head><body><p>text</p></body></html>"
    )
    assert extract_image_url(html) == "https://example.com/cover.jpg"
    extracted = extract_article(html, "https://example.com/t")
    assert extracted["image_url"] == "https://example.com/cover.jpg"


@pytest.mark.asyncio
async def test_copy_markdown_endpoint(user_with_env):
    user, env, db = user_with_env
    from keepfor.app import build_ai_markdown

    item, _ = await save_item(db, env, user["id"], "https://example.com/m", ["ai"])
    await update_user_notes(db, user["id"], item["id"], "Because reasons")
    fetched = await get_item(db, user["id"], item["id"])
    md = build_ai_markdown(fetched)
    assert md.startswith("# ")
    assert "Source:" in md
    assert "#ai" in md
    assert "Because reasons" in md


@pytest.mark.asyncio
async def test_mcp_save_note_and_pin(user_with_env):
    user, env, db = user_with_env
    from keepfor.mcp.server import handle_tool_call

    res = await handle_tool_call(
        "save_note",
        {"title": "T", "content": "hello world"},
        db,
        env,
        {"id": user["id"]},
    )
    assert res["item_type"] == "note"
    pin = await handle_tool_call(
        "pin_item",
        {"item_id": res["id"], "pinned": True},
        db,
        env,
        {"id": user["id"]},
    )
    assert pin["is_pinned"] is True
    got = await handle_tool_call(
        "get_item", {"item_id": res["id"]}, db, env, {"id": user["id"]}
    )
    assert got["item_type"] == "note"
    assert got["is_pinned"] is True
