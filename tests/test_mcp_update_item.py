# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for the MCP update_item tool and the read_state filters (#91)."""

import json

import pytest

from keepfor.auth.service import register_user
from keepfor.mcp.server import MCP_TOOLS_BY_NAME, process_mcp_request
from tests.fakes import FakeAI


async def _call(db, env, user, name, arguments):
    """Calls one MCP tool and returns the JSON-RPC response."""
    return await process_mcp_request(
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


def _payload(res):
    """The payload of a successful call."""
    assert "error" not in res, res["error"]
    return json.loads(res["result"]["content"][0]["text"])


def _error(res):
    """The error payload of a refused call."""
    assert "error" in res, res
    return res["error"]


@pytest.fixture
async def user(db):
    return await register_user(db, "update@example.com", "password123")


async def _make_item(db, env, user_id, *, title, url, content="alpha body"):
    """Creates an indexed item (title, text and an FTS row)."""
    from keepfor.models.items import save_item

    item, _ = await save_item(db, env, user_id, url, tags=["work"])
    await db.execute(
        "UPDATE items SET title = ?, content_text = ?, excerpt = ?, status = 'ok'"
        " WHERE id = ? AND user_id = ?;",
        (title, content, content[:80], item["id"], user_id),
    )
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?, ?, ?, ?);",
        (item["id"], user_id, title, content),
    )
    return item["id"]


async def _read_state(db, user_id, item_id):
    row = await db.query_first(
        "SELECT read_state FROM items WHERE id = ? AND user_id = ?;",
        (item_id, user_id),
    )
    return row["read_state"] if row else None


# ==========================================
# The tool itself
# ==========================================


def test_update_item_is_a_write_tool():
    """It changes the library, so it needs the write scope."""
    tool = MCP_TOOLS_BY_NAME["update_item"]
    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["annotations"]["destructiveHint"] is False
    properties = tool["inputSchema"]["properties"]
    assert set(properties) == {
        "item_id",
        "title",
        "user_notes",
        "notes_mode",
        "read_state",
        "is_pinned",
    }
    assert tool["inputSchema"]["required"] == ["item_id"]


@pytest.mark.asyncio
async def test_update_item_changes_the_title_and_reindexes_the_search(
    db, fake_env, user
):
    """The new title is searchable, the old one is gone."""
    item_id = await _make_item(
        db, fake_env, user["id"], title="Quokka Manual", url="https://example.com/q"
    )

    res = await _call(
        db,
        fake_env,
        user,
        "update_item",
        {"item_id": item_id, "title": "Wombat Manual"},
    )
    payload = _payload(res)
    assert payload["id"] == item_id
    assert payload["title"] == "Wombat Manual"

    row = await db.query_first(
        "SELECT title FROM items WHERE id = ? AND user_id = ?;",
        (item_id, user["id"]),
    )
    assert row["title"] == "Wombat Manual"

    # The FTS row was rebuilt: the new title is found, the old one is not.
    res = await _call(db, fake_env, user, "search_library", {"query": "Wombat"})
    assert item_id in [it["id"] for it in _payload(res)["items"]]
    res = await _call(db, fake_env, user, "search_library", {"query": "Quokka"})
    assert item_id not in [it["id"] for it in _payload(res)["items"]]


@pytest.mark.asyncio
async def test_update_item_replaces_or_appends_the_notes(db, fake_env, user):
    """notes_mode decides whether the notes are replaced or appended."""
    item_id = await _make_item(
        db, fake_env, user["id"], title="Quokka Manual", url="https://example.com/q"
    )

    res = await _call(
        db, fake_env, user, "update_item", {"item_id": item_id, "user_notes": "first"}
    )
    assert _payload(res)["user_notes"] == "first"

    res = await _call(
        db,
        fake_env,
        user,
        "update_item",
        {"item_id": item_id, "user_notes": "second", "notes_mode": "append"},
    )
    assert _payload(res)["user_notes"] == "first\n\nsecond"

    res = await _call(
        db, fake_env, user, "update_item", {"item_id": item_id, "user_notes": "third"}
    )
    assert _payload(res)["user_notes"] == "third"

    # The notes are indexed too: a word of them finds the item.
    res = await _call(db, fake_env, user, "search_library", {"query": "third"})
    assert item_id in [it["id"] for it in _payload(res)["items"]]


@pytest.mark.asyncio
async def test_update_item_sets_the_read_state_and_the_pin(db, fake_env, user):
    """read_state and is_pinned are written to the item."""
    item_id = await _make_item(
        db, fake_env, user["id"], title="Quokka Manual", url="https://example.com/q"
    )

    res = await _call(
        db, fake_env, user, "update_item", {"item_id": item_id, "read_state": "read"}
    )
    assert _payload(res)["read_state"] == "read"
    assert await _read_state(db, user["id"], item_id) == "read"

    res = await _call(
        db, fake_env, user, "update_item", {"item_id": item_id, "is_pinned": True}
    )
    assert _payload(res)["is_pinned"] is True
    row = await db.query_first(
        "SELECT is_pinned FROM items WHERE id = ? AND user_id = ?;",
        (item_id, user["id"]),
    )
    assert row["is_pinned"] == 1


@pytest.mark.asyncio
async def test_update_item_validates_its_arguments(db, fake_env, user):
    """The read state, the title and the notes are validated."""
    item_id = await _make_item(
        db, fake_env, user["id"], title="Quokka Manual", url="https://example.com/q"
    )

    res = await _call(
        db, fake_env, user, "update_item", {"item_id": item_id, "read_state": "bogus"}
    )
    assert _error(res)["data"]["type"] == "invalid_arguments"

    res = await _call(
        db, fake_env, user, "update_item", {"item_id": item_id, "title": "x" * 501}
    )
    assert _error(res)["data"]["type"] == "invalid_arguments"

    res = await _call(
        db,
        fake_env,
        user,
        "update_item",
        {"item_id": item_id, "user_notes": "x" * 10001},
    )
    assert _error(res)["data"]["type"] == "invalid_arguments"

    # A call without a change is refused: nothing would happen.
    res = await _call(db, fake_env, user, "update_item", {"item_id": item_id})
    assert _error(res)["data"]["type"] == "invalid_arguments"

    # A wrong type is refused too.
    res = await _call(
        db, fake_env, user, "update_item", {"item_id": item_id, "is_pinned": "yes"}
    )
    assert _error(res)["data"]["type"] == "invalid_arguments"

    # Nothing was written by the refused calls.
    row = await db.query_first(
        "SELECT title, user_notes FROM items WHERE id = ? AND user_id = ?;",
        (item_id, user["id"]),
    )
    assert row["title"] == "Quokka Manual"
    assert not row["user_notes"]


@pytest.mark.asyncio
async def test_update_item_reindexes_the_vectors_when_they_apply(db, fake_env, user):
    """The title and the notes are embedded again when Vectorize is there."""
    item_id = await _make_item(
        db, fake_env, user["id"], title="Quokka Manual", url="https://example.com/q"
    )
    embedded: list[str] = []

    def _script(model, inputs):
        embedded.extend(inputs["text"])
        return {"data": [[0.1] * 768]}

    FakeAI.script(_script)
    res = await _call(
        db,
        fake_env,
        user,
        "update_item",
        {"item_id": item_id, "title": "Wombat Manual", "user_notes": "a note"},
    )
    _payload(res)

    assert embedded, "the new title and notes were not embedded"
    assert "Wombat Manual" in embedded[0]
    assert "a note" in embedded[0]
    vector_ids = {vid for (_ns, vid) in fake_env.VECTORIZE.store}
    assert f"item_{item_id}_chunk_meta" in vector_ids


@pytest.mark.asyncio
async def test_update_item_needs_the_write_scope(db, fake_env, user):
    """A read-only token is refused before anything is written."""
    item_id = await _make_item(
        db, fake_env, user["id"], title="Quokka Manual", url="https://example.com/q"
    )

    res = await _call(
        db,
        fake_env,
        {**user, "scopes": ["read"]},
        "update_item",
        {"item_id": item_id, "title": "Wombat Manual"},
    )
    error = _error(res)
    assert error["data"]["type"] == "insufficient_scope"
    assert error["data"]["required_scope"] == "write"

    row = await db.query_first(
        "SELECT title FROM items WHERE id = ? AND user_id = ?;",
        (item_id, user["id"]),
    )
    assert row["title"] == "Quokka Manual"


@pytest.mark.asyncio
async def test_update_item_cannot_touch_another_users_item(db, fake_env, user):
    """A foreign item is not found, and it stays untouched."""
    other = await register_user(
        db, "other@example.com", "password123", allow_public_signups=True
    )
    item_id = await _make_item(
        db, fake_env, other["id"], title="Quokka Manual", url="https://example.com/q"
    )

    res = await _call(
        db, fake_env, user, "update_item", {"item_id": item_id, "title": "Hijacked"}
    )
    assert _error(res)["data"]["type"] == "not_found"

    row = await db.query_first(
        "SELECT title FROM items WHERE id = ? AND user_id = ?;",
        (item_id, other["id"]),
    )
    assert row["title"] == "Quokka Manual"


# ==========================================
# The read_state filters
# ==========================================


@pytest.mark.asyncio
async def test_list_items_filters_by_read_state(db, fake_env, user):
    """list_items returns only the items in the asked read state."""
    unread = await _make_item(
        db, fake_env, user["id"], title="Alpha Manual", url="https://example.com/a"
    )
    read = await _make_item(
        db, fake_env, user["id"], title="Beta Manual", url="https://example.com/b"
    )
    await _call(
        db, fake_env, user, "update_item", {"item_id": read, "read_state": "read"}
    )

    res = await _call(db, fake_env, user, "list_items", {"read_state": "read"})
    assert [it["id"] for it in _payload(res)["items"]] == [read]

    res = await _call(db, fake_env, user, "list_items", {"read_state": "unread"})
    assert [it["id"] for it in _payload(res)["items"]] == [unread]

    # An unknown read state is refused.
    res = await _call(db, fake_env, user, "list_items", {"read_state": "bogus"})
    assert _error(res)["data"]["type"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_search_library_filters_by_read_state(db, fake_env, user):
    """search_library narrows the hits to the asked read state."""
    unread = await _make_item(
        db, fake_env, user["id"], title="Alpha Manual", url="https://example.com/a"
    )
    read = await _make_item(
        db, fake_env, user["id"], title="Beta Manual", url="https://example.com/b"
    )
    await _call(
        db, fake_env, user, "update_item", {"item_id": read, "read_state": "read"}
    )

    res = await _call(
        db, fake_env, user, "search_library", {"query": "Manual", "read_state": "read"}
    )
    assert [it["id"] for it in _payload(res)["items"]] == [read]

    res = await _call(
        db,
        fake_env,
        user,
        "search_library",
        {"query": "Manual", "read_state": "unread"},
    )
    assert [it["id"] for it in _payload(res)["items"]] == [unread]


# ==========================================
# The bulk_update actions
# ==========================================


@pytest.mark.asyncio
async def test_bulk_update_marks_the_matched_items_read(db, fake_env, user):
    """The read state actions preview by default and then apply."""
    first = await _make_item(
        db, fake_env, user["id"], title="Alpha Manual", url="https://example.com/a"
    )
    second = await _make_item(
        db, fake_env, user["id"], title="Beta Manual", url="https://example.com/b"
    )

    res = await _call(
        db,
        fake_env,
        user,
        "bulk_update",
        {"read_state": "unread", "action": "mark_read"},
    )
    payload = _payload(res)
    assert payload["dry_run"] is True
    assert payload["total_matched"] == 2
    assert sorted(payload["sample"]) == sorted([first, second])
    # The preview writes nothing.
    assert await _read_state(db, user["id"], first) == "unread"

    res = await _call(
        db,
        fake_env,
        user,
        "bulk_update",
        {"read_state": "unread", "action": "mark_read", "dry_run": False},
    )
    assert _payload(res)["affected"] == 2
    assert await _read_state(db, user["id"], first) == "read"
    assert await _read_state(db, user["id"], second) == "read"

    # And back again.
    res = await _call(
        db,
        fake_env,
        user,
        "bulk_update",
        {"read_state": "read", "action": "mark_unread", "dry_run": False},
    )
    assert _payload(res)["affected"] == 2
    assert await _read_state(db, user["id"], first) == "unread"


@pytest.mark.asyncio
async def test_bulk_update_archives_the_matched_items(db, fake_env, user):
    """The archive action archives, and it is the stronger state."""
    item_id = await _make_item(
        db, fake_env, user["id"], title="Alpha Manual", url="https://example.com/a"
    )

    res = await _call(
        db,
        fake_env,
        user,
        "bulk_update",
        {"domain": "example.com", "action": "archive", "dry_run": False},
    )
    assert _payload(res)["affected"] == 1
    assert await _read_state(db, user["id"], item_id) == "archived"

    # An archived item stays archived when it is marked read or unread.
    res = await _call(
        db,
        fake_env,
        user,
        "bulk_update",
        {"domain": "example.com", "action": "mark_read", "dry_run": False},
    )
    assert _payload(res)["affected"] == 1
    assert await _read_state(db, user["id"], item_id) == "archived"


@pytest.mark.asyncio
async def test_bulk_update_rejects_an_unknown_action(db, fake_env, user):
    """An unknown action is refused before anything is written."""
    await _make_item(
        db, fake_env, user["id"], title="Alpha Manual", url="https://example.com/a"
    )

    res = await _call(
        db,
        fake_env,
        user,
        "bulk_update",
        {"domain": "example.com", "action": "burn", "dry_run": False},
    )
    assert _error(res)["data"]["type"] == "invalid_arguments"


def test_bulk_update_offers_the_read_state_actions():
    """The actions are part of the tool schema."""
    properties = MCP_TOOLS_BY_NAME["bulk_update"]["inputSchema"]["properties"]
    assert properties["action"]["enum"] == ["mark_read", "mark_unread", "archive"]
    assert "read_state" in properties
