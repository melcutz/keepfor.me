# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for the MCP update_highlight tool (GitHub issue #93).

A highlight could be created, listed and deleted, but its note and its
color could not be edited: an agent that annotates a passage had to
delete the highlight and create it again.
"""

import json

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


async def _make_item(db, env, user_id, url="https://example.com/alpha"):
    """Saves one item and gives it a body."""
    from keepfor.models.items import save_item

    item, _ = await save_item(db, env, user_id, url)
    await db.execute(
        "UPDATE items SET title = ?, content_text = ?, excerpt = ?, status = 'ok'"
        " WHERE id = ? AND user_id = ?;",
        ("Alpha Guide", "Hello world.", "Alpha excerpt", item["id"], user_id),
    )
    return item["id"]


async def _make_highlight(db, env, user, **extra):
    """Creates a highlight and returns its id."""
    item_id = await _make_item(db, env, user["id"])
    created = await _call(
        db,
        env,
        user,
        "create_highlight",
        {"item_id": item_id, "text": "Hello", **extra},
    )
    return created["highlight"]["id"], item_id


@pytest.mark.asyncio
async def test_update_highlight_changes_the_note_and_the_color(db, owner):
    """The note and the color of a highlight can be edited."""
    user, env = owner
    highlight_id, item_id = await _make_highlight(
        db, env, user, note="old note", color="#ff0000"
    )

    result = await _call(
        db,
        env,
        user,
        "update_highlight",
        {"highlight_id": highlight_id, "note": "new note", "color": "#00ff00"},
    )
    assert result["updated"] is True
    assert result["highlight"]["note"] == "new note"
    assert result["highlight"]["color"] == "#00ff00"
    assert result["highlight"]["id"] == highlight_id

    listed = await _call(db, env, user, "list_highlights", {"item_id": item_id})
    assert listed["highlights"][0]["note"] == "new note"
    assert listed["highlights"][0]["color"] == "#00ff00"


@pytest.mark.asyncio
async def test_update_highlight_leaves_the_other_field_alone(db, owner):
    """Editing the note does not touch the color, and the other way round."""
    user, env = owner
    highlight_id, item_id = await _make_highlight(
        db, env, user, note="old note", color="#ff0000"
    )

    await _call(
        db, env, user, "update_highlight", {"highlight_id": highlight_id, "note": "new"}
    )
    listed = await _call(db, env, user, "list_highlights", {"item_id": item_id})
    assert listed["highlights"][0]["note"] == "new"
    assert listed["highlights"][0]["color"] == "#ff0000"

    await _call(
        db,
        env,
        user,
        "update_highlight",
        {"highlight_id": highlight_id, "color": "#0000ff"},
    )
    listed = await _call(db, env, user, "list_highlights", {"item_id": item_id})
    assert listed["highlights"][0]["note"] == "new"
    assert listed["highlights"][0]["color"] == "#0000ff"


@pytest.mark.asyncio
async def test_update_highlight_clears_the_note_with_an_empty_string(db, owner):
    """An empty note removes it, it does not store an empty string."""
    user, env = owner
    highlight_id, item_id = await _make_highlight(db, env, user, note="old note")

    result = await _call(
        db, env, user, "update_highlight", {"highlight_id": highlight_id, "note": ""}
    )
    assert result["highlight"]["note"] is None

    listed = await _call(db, env, user, "list_highlights", {"item_id": item_id})
    assert listed["highlights"][0]["note"] is None


@pytest.mark.asyncio
async def test_update_highlight_keeps_the_quoted_text(db, owner):
    """Only the note and the color are editable."""
    user, env = owner
    highlight_id, item_id = await _make_highlight(db, env, user, note="old note")

    result = await _call(
        db, env, user, "update_highlight", {"highlight_id": highlight_id, "note": "new"}
    )
    assert result["highlight"]["text"] == "Hello"
    assert result["highlight"]["item_id"] == item_id


@pytest.mark.asyncio
async def test_update_highlight_on_a_foreign_highlight_is_not_found(
    db, owner, stranger
):
    """A highlight of another user cannot be edited."""
    user, env = owner
    other, other_env = stranger
    highlight_id, _ = await _make_highlight(db, env, user, note="secret")

    result = await _call(
        db,
        other_env,
        other,
        "update_highlight",
        {"highlight_id": highlight_id, "note": "hijacked"},
    )
    assert _error_type(result) == "not_found"
    assert "Highlight not found" in result["message"]

    row = await db.query_first(
        "SELECT note FROM highlights WHERE id = ? AND user_id = ?;",
        (highlight_id, user["id"]),
    )
    assert row["note"] == "secret"


@pytest.mark.asyncio
async def test_update_highlight_of_an_unknown_highlight_is_not_found(db, owner):
    """An unknown highlight id is a not-found error, not a silent success."""
    user, env = owner

    result = await _call(
        db, env, user, "update_highlight", {"highlight_id": "nope", "note": "x"}
    )
    assert _error_type(result) == "not_found"
    assert "Highlight not found" in result["message"]


@pytest.mark.asyncio
async def test_update_highlight_requires_a_highlight_id(db, owner):
    """The highlight id is mandatory."""
    user, env = owner

    result = await _call(db, env, user, "update_highlight", {"note": "x"})
    assert _error_type(result) == "invalid_arguments"


@pytest.mark.asyncio
async def test_update_highlight_caps_the_note(db, owner):
    """An over-long note is refused instead of stored."""
    user, env = owner
    highlight_id, _ = await _make_highlight(db, env, user)

    result = await _call(
        db,
        env,
        user,
        "update_highlight",
        {"highlight_id": highlight_id, "note": "n" * 501},
    )
    assert _error_type(result) == "invalid_arguments"

    row = await db.query_first(
        "SELECT note FROM highlights WHERE id = ? AND user_id = ?;",
        (highlight_id, user["id"]),
    )
    assert row["note"] is None


@pytest.mark.asyncio
async def test_update_highlight_caps_the_color(db, owner):
    """An over-long color is refused instead of stored."""
    user, env = owner
    highlight_id, _ = await _make_highlight(db, env, user)

    result = await _call(
        db,
        env,
        user,
        "update_highlight",
        {"highlight_id": highlight_id, "color": "c" * 33},
    )
    assert _error_type(result) == "invalid_arguments"


@pytest.mark.asyncio
async def test_update_highlight_is_a_write_tool(db, owner):
    """The tool is annotated as a write and needs the write scope."""
    from keepfor.mcp.server import MCP_TOOLS

    user, env = owner
    highlight_id, _ = await _make_highlight(db, env, user)

    tool = {t["name"]: t for t in MCP_TOOLS}["update_highlight"]
    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["annotations"]["destructiveHint"] is False

    res = await process_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "update_highlight",
                "arguments": {"highlight_id": highlight_id, "note": "nope"},
            },
        },
        db,
        env,
        {**user, "scopes": ["read"]},
    )
    assert res["error"]["data"]["type"] == "insufficient_scope"

    row = await db.query_first(
        "SELECT note FROM highlights WHERE id = ? AND user_id = ?;",
        (highlight_id, user["id"]),
    )
    assert row["note"] is None
