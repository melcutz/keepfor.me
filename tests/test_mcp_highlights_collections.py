# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for the MCP highlights and collections tools (GitHub issue #24)."""

import json

import pytest

from keepfor.auth.service import register_user
from keepfor.mcp.server import process_mcp_request
from keepfor.models.items import save_item


class FakeQueue:
    """In-test queue: records sends without delivery."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, message: dict) -> None:
        self.sent.append(message)


class MockEnv:
    """Mock Cloudflare environment for testing."""

    def __init__(self):
        self.DB = None
        self.QUEUE = FakeQueue()
        self.AI = None
        self.VECTORIZE = None
        self.BUCKET = None


async def _call(db, env, user, name, arguments):
    """Calls one MCP tool. Returns the parsed result or the JSON-RPC error."""
    res = await process_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        db,
        env,
        user,
    )
    if "error" in res:
        return res["error"]
    return json.loads(res["result"]["content"][0]["text"])


def _error_type(result):
    """The structured error kind of a failed tool call."""
    return result.get("data", {}).get("type")


@pytest.fixture
async def owner(db):
    """The first user: owns the library the tools operate on."""
    user = await register_user(db, "owner@example.com", "password123")
    return user, MockEnv()


@pytest.fixture
async def stranger(db):
    """A second user: must never see or touch the owner's data."""
    user = await register_user(
        db, "stranger@example.com", "password123", allow_public_signups=True
    )
    return user, MockEnv()


async def _make_item(db, env, user_id, url="https://example.com/alpha", content=""):
    """Saves one item and gives it a body."""
    item, _ = await save_item(db, env, user_id, url)
    if content:
        await db.execute(
            "UPDATE items SET title = ?, content_text = ?, excerpt = ?, status = 'ok'"
            " WHERE id = ? AND user_id = ?;",
            ("Alpha Guide", content, "Alpha excerpt", item["id"], user_id),
        )
    return item["id"]


# ==========================================
# Highlights
# ==========================================


@pytest.mark.asyncio
async def test_create_highlight_stores_text_and_offsets(db, owner):
    """A highlight keeps the quoted text and the offsets it covers."""
    user, env = owner
    item_id = await _make_item(db, env, user["id"], content="Hello world, hello again.")

    result = await _call(
        db,
        env,
        user,
        "create_highlight",
        {
            "item_id": item_id,
            "text": "hello again",
            "start_offset": 13,
            "end_offset": 24,
            "note": "the good bit",
            "color": "#ffd60a",
        },
    )
    highlight = result["highlight"]
    assert highlight["item_id"] == item_id
    assert highlight["text"] == "hello again"
    assert highlight["start_offset"] == 13
    assert highlight["end_offset"] == 24
    assert highlight["note"] == "the good bit"
    assert highlight["color"] == "#ffd60a"

    listed = await _call(db, env, user, "list_highlights", {"item_id": item_id})
    assert listed["count"] == 1
    assert listed["highlights"][0]["id"] == highlight["id"]
    assert listed["highlights"][0]["text"] == "hello again"


@pytest.mark.asyncio
async def test_create_highlight_offsets_default_to_the_quoted_text(db, owner):
    """Without offsets the passage is anchored at 0..len(text)."""
    user, env = owner
    item_id = await _make_item(db, env, user["id"], content="Hello world.")

    result = await _call(
        db,
        env,
        user,
        "create_highlight",
        {"item_id": item_id, "text": "Hello"},
    )
    assert result["highlight"]["start_offset"] == 0
    assert result["highlight"]["end_offset"] == 5


@pytest.mark.asyncio
async def test_create_highlight_end_offset_defaults_to_the_start_plus_the_quote(
    db, owner
):
    """A start offset without an end offset anchors the quote at the start."""
    user, env = owner
    item_id = await _make_item(db, env, user["id"], content="Hello world, hello again.")

    result = await _call(
        db,
        env,
        user,
        "create_highlight",
        {"item_id": item_id, "text": "hello again", "start_offset": 13},
    )
    assert result["highlight"]["start_offset"] == 13
    assert result["highlight"]["end_offset"] == 24


@pytest.mark.asyncio
async def test_create_highlight_rejects_offsets_past_the_content(db, owner):
    """Offsets past the end of the item's content are an argument error."""
    user, env = owner
    item_id = await _make_item(db, env, user["id"], content="Hello world.")

    result = await _call(
        db,
        env,
        user,
        "create_highlight",
        {"item_id": item_id, "text": "Hello", "start_offset": 0, "end_offset": 99},
    )
    assert _error_type(result) == "invalid_arguments"


@pytest.mark.asyncio
async def test_create_highlight_caps_the_quoted_text(db, owner):
    """Long quotes are refused instead of stored."""
    user, env = owner
    item_id = await _make_item(db, env, user["id"], content="Hello world.")

    result = await _call(
        db,
        env,
        user,
        "create_highlight",
        {"item_id": item_id, "text": "x" * 2001},
    )
    assert _error_type(result) == "invalid_arguments"


@pytest.mark.asyncio
async def test_create_highlight_caps_the_note(db, owner):
    """An over-long note is refused instead of stored."""
    user, env = owner
    item_id = await _make_item(db, env, user["id"], content="Hello world.")

    result = await _call(
        db,
        env,
        user,
        "create_highlight",
        {"item_id": item_id, "text": "Hello", "note": "n" * 501},
    )
    assert _error_type(result) == "invalid_arguments"

    listed = await _call(db, env, user, "list_highlights", {"item_id": item_id})
    assert listed["count"] == 0


@pytest.mark.asyncio
async def test_create_highlight_on_a_foreign_item_is_not_found(db, owner, stranger):
    """A highlight cannot be attached to another user's item."""
    user, env = owner
    other, other_env = stranger
    item_id = await _make_item(db, env, user["id"], content="Hello world.")

    result = await _call(
        db,
        other_env,
        other,
        "create_highlight",
        {"item_id": item_id, "text": "Hello"},
    )
    assert _error_type(result) == "not_found"

    stored = await db.query_all(
        "SELECT id FROM highlights WHERE item_id = ? AND user_id = ?;",
        (item_id, user["id"]),
    )
    assert stored == []


@pytest.mark.asyncio
async def test_list_highlights_never_leaks_another_users_highlights(
    db, owner, stranger
):
    """Reading the highlights of a foreign item returns nothing."""
    user, env = owner
    other, other_env = stranger
    item_id = await _make_item(db, env, user["id"], content="Hello world.")
    await _call(
        db, env, user, "create_highlight", {"item_id": item_id, "text": "Hello"}
    )

    result = await _call(db, other_env, other, "list_highlights", {"item_id": item_id})
    assert result["highlights"] == []
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_delete_highlight_only_deletes_owned_highlights(db, owner, stranger):
    """A foreign highlight survives the attempt to delete it."""
    user, env = owner
    other, other_env = stranger
    item_id = await _make_item(db, env, user["id"], content="Hello world.")
    created = await _call(
        db, env, user, "create_highlight", {"item_id": item_id, "text": "Hello"}
    )
    highlight_id = created["highlight"]["id"]

    result = await _call(
        db, other_env, other, "delete_highlight", {"highlight_id": highlight_id}
    )
    assert _error_type(result) == "not_found"

    still_there = await db.query_first(
        "SELECT id FROM highlights WHERE id = ? AND user_id = ?;",
        (highlight_id, user["id"]),
    )
    assert still_there is not None

    deleted = await _call(
        db, env, user, "delete_highlight", {"highlight_id": highlight_id}
    )
    assert deleted["deleted"] is True


@pytest.mark.asyncio
async def test_list_highlights_caps_the_limit(db, owner):
    """The limit is capped, not merely trusted."""
    user, env = owner
    item_id = await _make_item(db, env, user["id"], content="Hello world.")

    result = await _call(
        db, env, user, "list_highlights", {"item_id": item_id, "limit": 100000}
    )
    assert result["limit"] == 100


# ==========================================
# Collections
# ==========================================


@pytest.mark.asyncio
async def test_create_collection_is_idempotent(db, owner):
    """Creating an existing collection returns it instead of failing."""
    user, env = owner

    first = await _call(db, env, user, "create_collection", {"name": "Reading"})
    assert first["name"] == "Reading"
    assert first["created"] is True

    second = await _call(
        db, env, user, "create_collection", {"name": "Reading", "description": "x"}
    )
    assert second["id"] == first["id"]
    assert second["created"] is False


@pytest.mark.asyncio
async def test_create_collection_caps_the_name(db, owner):
    """Over-long names are refused."""
    user, env = owner

    result = await _call(db, env, user, "create_collection", {"name": "n" * 65})
    assert _error_type(result) == "invalid_arguments"


@pytest.mark.asyncio
async def test_add_to_collection_adds_owned_items_only(db, owner, stranger):
    """Items that are not the caller's are skipped, not linked."""
    user, env = owner
    other, other_env = stranger
    mine = await _make_item(db, env, user["id"], url="https://example.com/mine")
    theirs = await _make_item(
        db, other_env, other["id"], url="https://example.com/theirs"
    )
    await _call(db, env, user, "create_collection", {"name": "Reading"})

    result = await _call(
        db,
        env,
        user,
        "add_to_collection",
        {"collection": "Reading", "item_ids": [mine, theirs]},
    )
    assert result["added"] == 1
    assert result["skipped"] == 1
    assert result["item_count"] == 1

    listed = await _call(db, env, user, "list_collections", {})
    collection = listed["collections"][0]
    assert collection["item_ids"] == [mine]
    assert collection["items_truncated"] is False


@pytest.mark.asyncio
async def test_add_to_collection_reports_an_unknown_collection(db, owner):
    """Adding to a collection that does not exist is a not-found error."""
    user, env = owner
    item_id = await _make_item(db, env, user["id"])

    result = await _call(
        db,
        env,
        user,
        "add_to_collection",
        {"collection": "Nope", "item_ids": [item_id]},
    )
    assert _error_type(result) == "not_found"


@pytest.mark.asyncio
async def test_a_collection_can_be_addressed_by_id(db, owner):
    """The collection argument accepts the id as well as the name."""
    user, env = owner
    created = await _call(db, env, user, "create_collection", {"name": "Reading"})
    item_id = await _make_item(db, env, user["id"])

    result = await _call(
        db,
        env,
        user,
        "add_to_collection",
        {"collection": created["id"], "item_ids": [item_id]},
    )
    assert result["added"] == 1


@pytest.mark.asyncio
async def test_collection_tools_never_touch_another_users_collection(
    db, owner, stranger
):
    """A foreign collection is invisible: not found, never renamed or deleted."""
    user, env = owner
    other, other_env = stranger
    item_id = await _make_item(db, env, user["id"])
    await _call(db, env, user, "create_collection", {"name": "Reading"})

    for tool, arguments in (
        ("add_to_collection", {"collection": "Reading", "item_ids": [item_id]}),
        ("remove_from_collection", {"collection": "Reading", "item_ids": [item_id]}),
        ("rename_collection", {"collection": "Reading", "new_name": "Hijacked"}),
        ("delete_collection", {"collection": "Reading"}),
    ):
        result = await _call(db, other_env, other, tool, arguments)
        assert _error_type(result) == "not_found", tool

    listed = await _call(db, env, user, "list_collections", {})
    assert listed["collections"][0]["name"] == "Reading"
    assert listed["collections"][0]["item_count"] == 0


@pytest.mark.asyncio
async def test_rename_collection_rejects_a_taken_name(db, owner):
    """Renaming onto an existing collection is refused, never merged."""
    user, env = owner
    await _call(db, env, user, "create_collection", {"name": "Reading"})
    await _call(db, env, user, "create_collection", {"name": "Archive"})

    result = await _call(
        db,
        env,
        user,
        "rename_collection",
        {"collection": "Reading", "new_name": "Archive"},
    )
    assert _error_type(result) == "invalid_arguments"

    listed = await _call(db, env, user, "list_collections", {})
    assert sorted(c["name"] for c in listed["collections"]) == ["Archive", "Reading"]


@pytest.mark.asyncio
async def test_remove_from_collection_keeps_the_items(db, owner):
    """Removing an item from a collection never deletes the item."""
    user, env = owner
    item_id = await _make_item(db, env, user["id"])
    await _call(db, env, user, "create_collection", {"name": "Reading"})
    await _call(
        db,
        env,
        user,
        "add_to_collection",
        {"collection": "Reading", "item_ids": [item_id]},
    )

    result = await _call(
        db,
        env,
        user,
        "remove_from_collection",
        {"collection": "Reading", "item_ids": [item_id]},
    )
    assert result["removed"] == 1
    assert result["item_count"] == 0

    item = await db.query_first(
        "SELECT id FROM items WHERE id = ? AND user_id = ?;", (item_id, user["id"])
    )
    assert item is not None


@pytest.mark.asyncio
async def test_delete_collection_keeps_the_items(db, owner):
    """Deleting a collection drops the links only."""
    user, env = owner
    item_id = await _make_item(db, env, user["id"])
    await _call(db, env, user, "create_collection", {"name": "Reading"})
    await _call(
        db,
        env,
        user,
        "add_to_collection",
        {"collection": "Reading", "item_ids": [item_id]},
    )

    result = await _call(db, env, user, "delete_collection", {"collection": "Reading"})
    assert result["deleted"] is True
    assert result["items_untouched"] == 1

    listed = await _call(db, env, user, "list_collections", {})
    assert listed["collections"] == []

    item = await db.query_first(
        "SELECT id FROM items WHERE id = ? AND user_id = ?;", (item_id, user["id"])
    )
    assert item is not None
    links = await db.query_all(
        "SELECT collection_id FROM collection_items WHERE item_id = ?;", (item_id,)
    )
    assert links == []


@pytest.mark.asyncio
async def test_list_collections_caps_the_listed_item_ids(db, owner):
    """A big collection lists a capped number of item ids."""
    user, env = owner
    item_ids = []
    for i in range(25):
        item_ids.append(
            await _make_item(
                db, env, user["id"], url=f"https://example.com/item-{i}", content="c"
            )
        )
    await _call(db, env, user, "create_collection", {"name": "Big"})
    await _call(
        db, env, user, "add_to_collection", {"collection": "Big", "item_ids": item_ids}
    )

    listed = await _call(db, env, user, "list_collections", {})
    collection = listed["collections"][0]
    assert collection["item_count"] == 25
    assert len(collection["item_ids"]) == 20
    assert collection["items_truncated"] is True


@pytest.mark.asyncio
async def test_list_collections_caps_the_limit(db, owner):
    """The limit is capped, not merely trusted."""
    user, env = owner
    await _call(db, env, user, "create_collection", {"name": "Reading"})

    result = await _call(db, env, user, "list_collections", {"limit": 100000})
    assert result["limit"] == 50


@pytest.mark.asyncio
async def test_list_collections_never_exposes_a_foreign_item_id(db, owner, stranger):
    """A link to somebody else's item is not listed, even if it exists."""
    user, env = owner
    other, other_env = stranger
    theirs = await _make_item(
        db, other_env, other["id"], url="https://example.com/theirs"
    )
    await _call(db, env, user, "create_collection", {"name": "Reading"})

    collection = await _call(db, env, user, "list_collections", {})
    collection_id = collection["collections"][0]["id"]
    # Plant the link behind the tool's back: the listing must still hide it.
    await db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?);",
        (collection_id, theirs),
    )

    listed = await _call(db, env, user, "list_collections", {})
    assert listed["collections"][0]["item_ids"] == []
    assert theirs not in listed["collections"][0]["item_ids"]


@pytest.mark.asyncio
async def test_add_to_collection_caps_the_item_list(db, owner):
    """More than 100 ids are truncated, and the result says so."""
    user, env = owner
    item_id = await _make_item(db, env, user["id"])
    await _call(db, env, user, "create_collection", {"name": "Reading"})

    result = await _call(
        db,
        env,
        user,
        "add_to_collection",
        {"collection": "Reading", "item_ids": [item_id] * 150},
    )
    assert result["truncated"] is True
    assert result["added"] == 1


@pytest.mark.asyncio
async def test_collection_queries_stay_within_the_d1_parameter_limit(db, owner):
    """No statement binds more than 100 parameters: D1 rejects the rest.

    SQLite has no such limit, so this only fails in production.
    """
    user, env = owner
    item_id = await _make_item(db, env, user["id"])
    await _call(db, env, user, "create_collection", {"name": "Reading"})

    seen: list[str] = []
    original = db.execute

    async def spy(sql, params=()):
        seen.append(sql)
        return await original(sql, params)

    db.execute = spy
    ids = [item_id] * 100
    await _call(
        db, env, user, "add_to_collection", {"collection": "Reading", "item_ids": ids}
    )
    await _call(
        db,
        env,
        user,
        "remove_from_collection",
        {"collection": "Reading", "item_ids": ids},
    )
    assert seen, "no statement was recorded"
    for sql in seen:
        assert sql.count("?") <= 100, sql


@pytest.mark.asyncio
async def test_collections_are_scoped_to_their_owner(db, owner, stranger):
    """Each user sees only their own collections."""
    user, env = owner
    other, other_env = stranger
    await _call(db, env, user, "create_collection", {"name": "Reading"})
    await _call(db, other_env, other, "create_collection", {"name": "Archive"})

    mine = await _call(db, env, user, "list_collections", {})
    assert [c["name"] for c in mine["collections"]] == ["Reading"]

    theirs = await _call(db, other_env, other, "list_collections", {})
    assert [c["name"] for c in theirs["collections"]] == ["Archive"]


# ==========================================
# Deleting an item cleans up its annotations
# ==========================================


@pytest.mark.asyncio
async def test_delete_item_removes_highlights_and_collection_links(db, owner):
    """A deleted item takes its highlights and collection links with it."""
    user, env = owner
    item_id = await _make_item(db, env, user["id"], content="Hello world.")
    await _call(
        db, env, user, "create_highlight", {"item_id": item_id, "text": "Hello"}
    )
    await _call(db, env, user, "create_collection", {"name": "Reading"})
    await _call(
        db,
        env,
        user,
        "add_to_collection",
        {"collection": "Reading", "item_ids": [item_id]},
    )

    deleted = await _call(db, env, user, "delete_item", {"item_id": item_id})
    assert deleted["deleted"] is True

    highlights = await db.query_all(
        "SELECT id FROM highlights WHERE item_id = ?;", (item_id,)
    )
    assert highlights == []
    links = await db.query_all(
        "SELECT collection_id FROM collection_items WHERE item_id = ?;", (item_id,)
    )
    assert links == []


# ==========================================
# Tool metadata
# ==========================================


@pytest.mark.asyncio
async def test_new_tools_are_listed_with_annotations(db, owner):
    """Every new tool is exposed and annotated."""
    from keepfor.mcp.server import MCP_TOOLS

    user, env = owner
    names = {tool["name"] for tool in MCP_TOOLS}
    assert {
        "create_highlight",
        "list_highlights",
        "delete_highlight",
        "create_collection",
        "list_collections",
        "add_to_collection",
        "remove_from_collection",
        "rename_collection",
        "delete_collection",
    } <= names

    res = await process_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, db, env, user
    )
    listed = {tool["name"]: tool for tool in res["result"]["tools"]}
    for name in (
        "create_highlight",
        "list_highlights",
        "delete_highlight",
        "create_collection",
        "list_collections",
        "add_to_collection",
        "remove_from_collection",
        "rename_collection",
        "delete_collection",
    ):
        annotations = listed[name]["annotations"]
        assert "readOnlyHint" in annotations
        assert "destructiveHint" in annotations
        assert "idempotentHint" in annotations
    assert listed["list_highlights"]["annotations"]["readOnlyHint"] is True
    assert listed["list_collections"]["annotations"]["readOnlyHint"] is True
    assert listed["delete_highlight"]["annotations"]["destructiveHint"] is True
    assert listed["delete_collection"]["annotations"]["destructiveHint"] is True
    assert listed["create_collection"]["annotations"]["readOnlyHint"] is False
