# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""MCP list_trash + restore_item (GitHub issue #90).

An agent undoes its own deletes over MCP: it lists the Trash and restores
the items it trashed, without the user opening the web Trash view. The
trash results carry the item ids, so the ids go straight from the delete
into the restore.
"""

from __future__ import annotations

import json

import pytest

from keepfor.auth.service import register_user
from keepfor.mcp.server import MCP_TOOLS, process_mcp_request
from keepfor.models.items import delete_item, save_item, trash_item


@pytest.fixture
async def user(db):
    return await register_user(db, "trash-mcp@example.com", "password123")


@pytest.fixture
async def other(db):
    """A second user: the tenant boundary."""
    return await register_user(db, "trash-mcp-other@example.com", "password123", True)


async def _seed(
    db, env, user_id, url="https://example.com/trash-me", title="Trash Guide"
):
    """A saved item with a title."""
    item, _ = await save_item(db, env, user_id, url, ["work"])
    await db.execute(
        "UPDATE items SET title = ?, status = 'ok' WHERE id = ? AND user_id = ?;",
        (title, item["id"], user_id),
    )
    return item


async def _call(db, env, user, tool, arguments):
    """Calls one MCP tool and returns the parsed payload."""
    res = await process_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        db,
        env,
        user,
    )
    assert "error" not in res, res
    return json.loads(res["result"]["content"][0]["text"])


def _tool(name):
    return next(t for t in MCP_TOOLS if t["name"] == name)


# ==========================================
# list_trash
# ==========================================


@pytest.mark.asyncio
async def test_list_trash_lists_the_trashed_items(db, fake_env, user):
    """The tool returns the id, the title, the url and the dates."""
    item = await _seed(db, fake_env, user["id"])
    await trash_item(db, user["id"], item["id"])

    result = await _call(db, fake_env, user, "list_trash", {})

    assert result["count"] == 1
    row = result["items"][0]
    assert row["id"] == item["id"]
    assert row["title"] == "Trash Guide"
    assert row["url"] == "https://example.com/trash-me"
    assert row["deleted_at"]
    assert row["purge_at"] > row["deleted_at"]


@pytest.mark.asyncio
async def test_list_trash_leaves_the_live_items_out(db, fake_env, user):
    """Only the trashed items are listed."""
    live = await _seed(db, fake_env, user["id"], "https://example.com/live")
    gone = await _seed(db, fake_env, user["id"], "https://example.com/gone")
    await trash_item(db, user["id"], gone["id"])

    result = await _call(db, fake_env, user, "list_trash", {})

    assert [row["id"] for row in result["items"]] == [gone["id"]]
    assert live["id"] not in [row["id"] for row in result["items"]]


@pytest.mark.asyncio
async def test_list_trash_filters_by_query(db, fake_env, user):
    """The query keeps the items whose title or url holds it."""
    keep = await _seed(db, fake_env, user["id"], "https://example.com/keep", "Keep Me")
    drop = await _seed(db, fake_env, user["id"], "https://example.com/drop", "Drop Me")
    await trash_item(db, user["id"], keep["id"])
    await trash_item(db, user["id"], drop["id"])

    result = await _call(db, fake_env, user, "list_trash", {"query": "keep"})

    assert [row["title"] for row in result["items"]] == ["Keep Me"]


@pytest.mark.asyncio
async def test_list_trash_pages(db, fake_env, user):
    """The limit and the offset page through the Trash."""
    ids = []
    for i in range(3):
        item = await _seed(
            db, fake_env, user["id"], f"https://example.com/page-{i}", f"Page {i}"
        )
        await trash_item(db, user["id"], item["id"])
        ids.append(item["id"])

    first = await _call(db, fake_env, user, "list_trash", {"limit": 2})
    second = await _call(db, fake_env, user, "list_trash", {"limit": 2, "offset": 2})

    assert first["count"] == 2
    assert second["count"] == 1
    assert len({row["id"] for row in first["items"] + second["items"]}) == 3


@pytest.mark.asyncio
async def test_list_trash_is_read_only(db, fake_env, user):
    """The tool is annotated read-only and changes nothing."""
    item = await _seed(db, fake_env, user["id"])
    await trash_item(db, user["id"], item["id"])

    tool = _tool("list_trash")
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["annotations"]["destructiveHint"] is False

    before = await db.query_first(
        "SELECT deleted_at FROM items WHERE id = ? AND user_id = ?;",
        (item["id"], user["id"]),
    )
    await _call(db, fake_env, user, "list_trash", {})
    after = await db.query_first(
        "SELECT deleted_at FROM items WHERE id = ? AND user_id = ?;",
        (item["id"], user["id"]),
    )
    assert before == after


@pytest.mark.asyncio
async def test_list_trash_never_shows_another_users_items(db, fake_env, user, other):
    """The Trash of another user is not readable."""
    theirs = await _seed(db, fake_env, other["id"], "https://example.com/theirs")
    await trash_item(db, other["id"], theirs["id"])

    result = await _call(db, fake_env, user, "list_trash", {})

    assert result["count"] == 0
    assert theirs["id"] not in json.dumps(result)


# ==========================================
# restore_item
# ==========================================


@pytest.mark.asyncio
async def test_restore_item_brings_the_item_back(db, fake_env, user):
    """A trashed item is live again after the restore."""
    item = await _seed(db, fake_env, user["id"])
    await trash_item(db, user["id"], item["id"])

    result = await _call(db, fake_env, user, "restore_item", {"item_ids": [item["id"]]})

    assert result["restored"] == [item["id"]]
    assert result["not_found"] == []
    assert result["already_active"] == []
    row = await db.query_first(
        "SELECT deleted_at FROM items WHERE id = ? AND user_id = ?;",
        (item["id"], user["id"]),
    )
    assert row["deleted_at"] is None


@pytest.mark.asyncio
async def test_restored_item_is_in_the_library_again(db, fake_env, user):
    """The restored item shows up in the library listing."""
    item = await _seed(db, fake_env, user["id"])
    await trash_item(db, user["id"], item["id"])

    await _call(db, fake_env, user, "restore_item", {"item_ids": [item["id"]]})

    listed = await _call(db, fake_env, user, "list_items", {})
    assert [row["id"] for row in listed["items"]] == [item["id"]]


@pytest.mark.asyncio
async def test_restore_item_reports_an_item_that_is_not_trashed(db, fake_env, user):
    """A live item is reported as already_active, not restored."""
    item = await _seed(db, fake_env, user["id"])

    result = await _call(db, fake_env, user, "restore_item", {"item_ids": [item["id"]]})

    assert result["already_active"] == [item["id"]]
    assert result["restored"] == []


@pytest.mark.asyncio
async def test_restore_item_reports_an_item_that_was_purged(db, fake_env, user):
    """An id that no longer exists is reported as not_found."""
    item = await _seed(db, fake_env, user["id"])
    await trash_item(db, user["id"], item["id"])
    # The permanent delete: the row is gone for good.
    assert await delete_item(db, fake_env, user["id"], item["id"]) is True

    result = await _call(db, fake_env, user, "restore_item", {"item_ids": [item["id"]]})

    assert result["not_found"] == [item["id"]]
    assert result["restored"] == []


@pytest.mark.asyncio
async def test_restore_item_never_restores_another_users_item(
    db, fake_env, user, other
):
    """Another user's trashed item is not restored: it is not found."""
    theirs = await _seed(db, fake_env, other["id"], "https://example.com/theirs")
    await trash_item(db, other["id"], theirs["id"])

    result = await _call(
        db, fake_env, user, "restore_item", {"item_ids": [theirs["id"]]}
    )

    assert result["not_found"] == [theirs["id"]]
    assert result["restored"] == []
    row = await db.query_first(
        "SELECT deleted_at FROM items WHERE id = ? AND user_id = ?;",
        (theirs["id"], other["id"]),
    )
    assert row["deleted_at"], "the other user's item was restored"


@pytest.mark.asyncio
async def test_restore_item_sorts_every_id_into_a_bucket(db, fake_env, user):
    """One call reports the three outcomes of its ids."""
    live = await _seed(db, fake_env, user["id"], "https://example.com/live")
    gone = await _seed(db, fake_env, user["id"], "https://example.com/gone")
    await trash_item(db, user["id"], gone["id"])

    result = await _call(
        db,
        fake_env,
        user,
        "restore_item",
        {"item_ids": [gone["id"], live["id"], "no-such-item"]},
    )

    assert result["restored"] == [gone["id"]]
    assert result["already_active"] == [live["id"]]
    assert result["not_found"] == ["no-such-item"]


@pytest.mark.asyncio
async def test_restore_item_takes_at_most_50_ids(db, fake_env, user):
    """The call is capped at 50 ids per call."""
    assert _tool("restore_item")["inputSchema"]["properties"]["item_ids"]

    ids = [f"item-{i}" for i in range(60)]
    result = await _call(db, fake_env, user, "restore_item", {"item_ids": ids})

    assert result["restored"] == []
    assert len(result["not_found"]) == 50
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_restore_item_needs_the_write_scope(db, fake_env, user):
    """A read-only token may not restore anything."""
    item = await _seed(db, fake_env, user["id"])
    await trash_item(db, user["id"], item["id"])

    res = await process_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "restore_item",
                "arguments": {"item_ids": [item["id"]]},
            },
        },
        db,
        fake_env,
        {**user, "scopes": ["read"]},
    )

    assert "error" in res, res
    assert res["error"]["data"]["type"] == "insufficient_scope"
    assert res["error"]["data"]["required_scope"] == "write"
    row = await db.query_first(
        "SELECT deleted_at FROM items WHERE id = ? AND user_id = ?;",
        (item["id"], user["id"]),
    )
    assert row["deleted_at"], "a read-only token restored an item"


@pytest.mark.asyncio
async def test_restore_item_is_annotated_as_a_write(db, fake_env, user):
    """The tool carries the annotations of a write tool."""
    tool = _tool("restore_item")
    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["annotations"]["destructiveHint"] is False


# ==========================================
# The trash results carry the item ids
# ==========================================


@pytest.mark.asyncio
async def test_delete_item_result_carries_the_item_id(db, fake_env, user):
    """The id of a trashed item can be passed straight to restore_item."""
    item = await _seed(db, fake_env, user["id"])

    result = await _call(db, fake_env, user, "delete_item", {"item_id": item["id"]})

    assert result["trashed"] is True
    assert result["item_ids"] == [item["id"]]

    restored = await _call(
        db, fake_env, user, "restore_item", {"item_ids": result["item_ids"]}
    )
    assert restored["restored"] == [item["id"]]


@pytest.mark.asyncio
async def test_bulk_update_trash_result_carries_the_item_ids(db, fake_env, user):
    """A bulk trash reports the ids it trashed: they go to restore_item."""
    item = await _seed(db, fake_env, user["id"])

    result = await _call(
        db,
        fake_env,
        user,
        "bulk_update",
        {"tags_all": ["work"], "trash": True, "dry_run": False},
    )

    assert result["item_ids"] == [item["id"]]
    listed = await _call(db, fake_env, user, "list_items", {})
    assert listed["items"] == [], "the item was not trashed"

    restored = await _call(
        db, fake_env, user, "restore_item", {"item_ids": result["item_ids"]}
    )
    assert restored["restored"] == [item["id"]]


@pytest.mark.asyncio
async def test_bulk_update_trash_dry_run_changes_nothing(db, fake_env, user):
    """The preview of a bulk trash trashes nothing."""
    item = await _seed(db, fake_env, user["id"])

    result = await _call(
        db,
        fake_env,
        user,
        "bulk_update",
        {"tags_all": ["work"], "trash": True},
    )

    assert result["dry_run"] is True
    assert result["item_ids"] == [item["id"]]
    row = await db.query_first(
        "SELECT deleted_at FROM items WHERE id = ? AND user_id = ?;",
        (item["id"], user["id"]),
    )
    assert row["deleted_at"] is None
