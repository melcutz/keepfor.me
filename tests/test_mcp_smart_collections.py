# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for the MCP smart (query-backed) collections (GitHub issue #93).

A collection used to be a static list of linked items. A smart collection
carries a query and/or a tag filter instead: its members are resolved at
read time through the existing search, so it stays current.
"""

import json
import uuid

import pytest

from keepfor.auth.service import register_user
from keepfor.mcp.server import process_mcp_request


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


async def _insert_item(db, user_id, key, title, text, tags=()):
    """An item of the library, with its FTS row and its tags."""
    item_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title, content_text,"
        " status) VALUES (?, ?, ?, ?, ?, ?, 'saved');",
        (
            item_id,
            user_id,
            f"https://example.com/{key}",
            f"https://example.com/{key}",
            title,
            text,
        ),
    )
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?, ?, ?, ?);",
        (item_id, user_id, title, text),
    )
    for tag in tags:
        tag_id = f"tag-{user_id[:8]}-{tag}"
        await db.execute(
            "INSERT OR IGNORE INTO tags (id, user_id, name) VALUES (?, ?, ?);",
            (tag_id, user_id, tag),
        )
        await db.execute(
            "INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?);",
            (item_id, tag_id),
        )
    return item_id


@pytest.mark.asyncio
async def test_create_collection_with_a_query_makes_a_smart_collection(db, owner):
    """A collection with a query is a smart collection."""
    user, env = owner

    result = await _call(
        db,
        env,
        user,
        "create_collection",
        {"name": "Alpha reading", "query": "alpha", "tag": "work"},
    )
    assert result["kind"] == "smart"
    assert result["query"] == "alpha"
    assert result["tag"] == "work"


@pytest.mark.asyncio
async def test_create_collection_without_a_query_stays_manual(db, owner):
    """A collection with no filter keeps the manual behaviour."""
    user, env = owner

    result = await _call(db, env, user, "create_collection", {"name": "Reading"})
    assert result["kind"] == "manual"
    assert result["query"] is None
    assert result["tag"] is None


@pytest.mark.asyncio
async def test_list_collections_reports_the_kind_of_every_collection(db, owner):
    """Every collection says whether it is manual or smart."""
    user, env = owner
    await _call(db, env, user, "create_collection", {"name": "Reading"})
    await _call(db, env, user, "create_collection", {"name": "Alpha", "query": "alpha"})

    listed = await _call(db, env, user, "list_collections", {})
    kinds = {c["name"]: c["kind"] for c in listed["collections"]}
    assert kinds == {"Reading": "manual", "Alpha": "smart"}


@pytest.mark.asyncio
async def test_a_smart_collection_resolves_its_members_through_the_search(db, owner):
    """The members of a smart collection are the items its query finds."""
    user, env = owner
    alpha = await _insert_item(db, user["id"], "a", "Alpha guide", "an alpha guide")
    await _insert_item(db, user["id"], "b", "Beta guide", "a beta guide")

    await _call(db, env, user, "create_collection", {"name": "Alpha", "query": "alpha"})

    listed = await _call(db, env, user, "list_collections", {})
    collection = listed["collections"][0]
    assert collection["kind"] == "smart"
    assert collection["item_count"] == 1
    assert collection["item_ids"] == [alpha]


@pytest.mark.asyncio
async def test_a_smart_collection_follows_the_library(db, owner):
    """A new item that matches the query joins the collection."""
    user, env = owner
    await _insert_item(db, user["id"], "a", "Alpha guide", "an alpha guide")
    await _call(db, env, user, "create_collection", {"name": "Alpha", "query": "alpha"})

    listed = await _call(db, env, user, "list_collections", {})
    assert listed["collections"][0]["item_count"] == 1

    await _insert_item(db, user["id"], "b", "More alpha", "another alpha thing")

    listed = await _call(db, env, user, "list_collections", {})
    assert listed["collections"][0]["item_count"] == 2


@pytest.mark.asyncio
async def test_a_smart_collection_narrows_its_members_by_tag(db, owner):
    """The tag filter narrows the items the query finds."""
    user, env = owner
    tagged = await _insert_item(
        db, user["id"], "a", "Alpha guide", "an alpha guide", tags=("work",)
    )
    await _insert_item(
        db, user["id"], "b", "Alpha notes", "an alpha guide", tags=("home",)
    )

    await _call(
        db,
        env,
        user,
        "create_collection",
        {"name": "Alpha", "query": "alpha", "tag": "work"},
    )

    listed = await _call(db, env, user, "list_collections", {})
    collection = listed["collections"][0]
    assert collection["item_count"] == 1
    assert collection["item_ids"] == [tagged]


@pytest.mark.asyncio
async def test_a_smart_collection_with_only_a_tag_holds_the_tagged_items(db, owner):
    """A collection with a tag and no query holds every item with that tag."""
    user, env = owner
    tagged = await _insert_item(
        db, user["id"], "a", "Alpha guide", "an alpha guide", tags=("work",)
    )
    await _insert_item(db, user["id"], "b", "Beta guide", "a beta guide")

    await _call(db, env, user, "create_collection", {"name": "Work", "tag": "work"})

    listed = await _call(db, env, user, "list_collections", {})
    collection = listed["collections"][0]
    assert collection["kind"] == "smart"
    assert collection["item_count"] == 1
    assert collection["item_ids"] == [tagged]


@pytest.mark.asyncio
async def test_add_to_collection_refuses_a_smart_collection(db, owner):
    """A smart collection is resolved by its query: it takes no items."""
    user, env = owner
    item_id = await _insert_item(db, user["id"], "a", "Alpha guide", "an alpha guide")
    await _call(db, env, user, "create_collection", {"name": "Alpha", "query": "alpha"})

    result = await _call(
        db,
        env,
        user,
        "add_to_collection",
        {"collection": "Alpha", "item_ids": [item_id]},
    )
    assert _error_type(result) == "invalid_arguments"
    assert "smart" in result["message"].lower()

    links = await db.query_all("SELECT collection_id FROM collection_items;")
    assert links == []


@pytest.mark.asyncio
async def test_remove_from_collection_refuses_a_smart_collection(db, owner):
    """A smart collection is resolved by its query: it holds no links."""
    user, env = owner
    await _call(db, env, user, "create_collection", {"name": "Alpha", "query": "alpha"})

    result = await _call(
        db,
        env,
        user,
        "remove_from_collection",
        {"collection": "Alpha", "item_ids": ["whatever"]},
    )
    assert _error_type(result) == "invalid_arguments"
    assert "smart" in result["message"].lower()


@pytest.mark.asyncio
async def test_rename_collection_gives_a_collection_a_query(db, owner):
    """A manual collection becomes a smart one when it gets a query."""
    user, env = owner
    alpha = await _insert_item(db, user["id"], "a", "Alpha guide", "an alpha guide")
    await _call(db, env, user, "create_collection", {"name": "Reading"})

    result = await _call(
        db,
        env,
        user,
        "rename_collection",
        {"collection": "Reading", "new_name": "Reading", "query": "alpha"},
    )
    assert result["kind"] == "smart"
    assert result["query"] == "alpha"

    listed = await _call(db, env, user, "list_collections", {})
    collection = listed["collections"][0]
    assert collection["kind"] == "smart"
    assert collection["item_ids"] == [alpha]


@pytest.mark.asyncio
async def test_rename_collection_clears_the_query(db, owner):
    """An empty query turns a smart collection back into a manual one."""
    user, env = owner
    await _call(db, env, user, "create_collection", {"name": "Alpha", "query": "alpha"})

    result = await _call(
        db,
        env,
        user,
        "rename_collection",
        {"collection": "Alpha", "new_name": "Alpha", "query": ""},
    )
    assert result["kind"] == "manual"
    assert result["query"] is None

    listed = await _call(db, env, user, "list_collections", {})
    assert listed["collections"][0]["kind"] == "manual"


@pytest.mark.asyncio
async def test_rename_collection_keeps_the_query_when_none_is_given(db, owner):
    """A rename that says nothing about the filter keeps it."""
    user, env = owner
    await _call(db, env, user, "create_collection", {"name": "Alpha", "query": "alpha"})

    result = await _call(
        db,
        env,
        user,
        "rename_collection",
        {"collection": "Alpha", "new_name": "Alphas"},
    )
    assert result["kind"] == "smart"
    assert result["query"] == "alpha"


@pytest.mark.asyncio
async def test_a_smart_collection_never_resolves_another_users_items(
    db, owner, stranger
):
    """The members of a smart collection belong to its owner only."""
    user, env = owner
    other, other_env = stranger
    theirs = await _insert_item(db, other["id"], "a", "Alpha guide", "an alpha guide")
    await _insert_item(db, user["id"], "b", "Beta guide", "a beta guide")

    await _call(
        db, other_env, other, "create_collection", {"name": "Alpha", "query": "alpha"}
    )

    listed = await _call(db, other_env, other, "list_collections", {})
    collection = listed["collections"][0]
    assert collection["item_count"] == 1
    assert collection["item_ids"] == [theirs]

    # The other user's item is not in the collection of the first user.
    listed = await _call(db, env, user, "list_collections", {})
    assert listed["collections"] == []


@pytest.mark.asyncio
async def test_a_foreign_smart_collection_is_not_found(db, owner, stranger):
    """A smart collection of another user is invisible: not found."""
    user, env = owner
    other, other_env = stranger
    await _call(db, env, user, "create_collection", {"name": "Alpha", "query": "alpha"})

    for tool, arguments in (
        ("rename_collection", {"collection": "Alpha", "new_name": "Hijacked"}),
        ("delete_collection", {"collection": "Alpha"}),
        ("add_to_collection", {"collection": "Alpha", "item_ids": ["x"]}),
        ("remove_from_collection", {"collection": "Alpha", "item_ids": ["x"]}),
    ):
        result = await _call(db, other_env, other, tool, arguments)
        assert _error_type(result) == "not_found", tool

    listed = await _call(db, env, user, "list_collections", {})
    assert listed["collections"][0]["name"] == "Alpha"
    assert listed["collections"][0]["query"] == "alpha"


@pytest.mark.asyncio
async def test_list_collections_never_exposes_a_foreign_item_id(db, owner, stranger):
    """The item ids of a smart collection belong to its owner only."""
    user, env = owner
    other, other_env = stranger
    theirs = await _insert_item(db, other["id"], "a", "Alpha guide", "an alpha guide")
    mine = await _insert_item(db, user["id"], "b", "Alpha guide", "an alpha guide")
    await _call(
        db, other_env, other, "create_collection", {"name": "Alpha", "query": "alpha"}
    )
    await _call(db, env, user, "create_collection", {"name": "Alpha", "query": "alpha"})

    listed = await _call(db, env, user, "list_collections", {})
    assert listed["collections"][0]["item_ids"] == [mine]
    assert theirs not in listed["collections"][0]["item_ids"]


@pytest.mark.asyncio
async def test_the_smart_collection_tools_take_a_query_and_a_tag(db, owner):
    """The tools that carry a smart filter advertise it."""
    from keepfor.mcp.server import MCP_TOOLS_BY_NAME

    user, env = owner
    for name in ("create_collection", "rename_collection"):
        properties = MCP_TOOLS_BY_NAME[name]["inputSchema"]["properties"]
        assert "query" in properties
        assert "tag" in properties
