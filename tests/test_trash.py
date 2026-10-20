# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Trash: a soft delete that keeps an item restorable for 30 days.

Covers the MCP `delete_item` semantics (trash, not destroy), the restore
path, the exclusion of trashed items from every read path, and the purge.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from keepfor.app import app
from keepfor.auth.service import login_user, register_user
from keepfor.mcp.server import MCP_TOOLS, process_mcp_request
from keepfor.models.items import (
    add_tags_to_item,
    delete_item,
    get_item,
    get_item_tags,
    list_trash,
    purge_trash,
    restore_item,
    save_item,
    toggle_pin_item,
    trash_item,
    update_user_notes,
)
from keepfor.models.tags import (
    find_untagged_items,
    find_unused_tags,
    list_tags,
    related_tags,
    set_item_tags,
    tag_stats,
)
from keepfor.search.engine import (
    count_recent_items,
    find_items,
    get_recent_items,
    get_status_counts,
    hybrid_search,
    list_items_compact,
    match_items,
    read_item_chunks,
    related_items,
    search_fts,
)
from keepfor.utils.importer import export_library_json


@pytest.fixture
async def user(db):
    return await register_user(db, "trash@example.com", "password123")


class _ClientEnv:
    """Env for the HTTP tests: sqlite through the scope, fake R2/Vectorize."""

    def __init__(self, conn, bucket, vectorize):
        self.DB = None
        self.keepfor_me_db = None
        self.D1 = None
        self.sqlite_conn = conn
        self.BUCKET = bucket
        self.VECTORIZE = vectorize
        self.AI = None
        self.QUEUE = None


@pytest.fixture
def client(db, sqlite_conn, fake_bucket, fake_vectorize, monkeypatch):
    env = _ClientEnv(sqlite_conn, fake_bucket, fake_vectorize)
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    return TestClient(app)


@pytest.fixture
async def session(db, user):
    _, session_id = await login_user(db, "trash@example.com", "password123")
    return session_id


async def _seed_item(db, env, user_id, url="https://example.com/trash-me"):
    """A saved, extracted-looking item with an FTS row."""
    item, _ = await save_item(db, env, user_id, url, ["work"])
    await db.execute(
        "UPDATE items SET title = ?, content_text = ?, excerpt = ?, status = 'ok'"
        " WHERE id = ? AND user_id = ?;",
        ("Trash Guide", "Trash content " * 20, "Trash excerpt", item["id"], user_id),
    )
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?, ?, ?, ?);",
        (item["id"], user_id, "Trash Guide", "Trash content " * 20),
    )
    return item


async def _seed_snapshots(db, env, user_id, item_id):
    """R2 clean.html snapshot + one vector chunk for an item."""
    await env.BUCKET.put(f"items/{item_id}/clean.html", "<html><body>hi</body></html>")
    chunk_id = f"item_{item_id}_chunk_0"
    await db.execute(
        "INSERT INTO chunks (id, item_id, user_id, chunk_index, token_count)"
        " VALUES (?, ?, ?, 0, 100);",
        (chunk_id, item_id, user_id),
    )
    await env.VECTORIZE.upsert(
        [
            {
                "id": chunk_id,
                "values": [0.42] * 768,
                "metadata": {"item_id": item_id, "user_id": user_id},
            }
        ]
    )
    return chunk_id


async def _call_mcp(db, env, user, tool, arguments):
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
    return res["result"]["content"][0]["text"]


# ==========================================
# MCP delete_item -> Trash
# ==========================================


@pytest.mark.asyncio
async def test_mcp_delete_item_moves_to_trash_and_keeps_snapshots(db, fake_env, user):
    """MCP delete_item trashes: the row, the R2 snapshot and the vector stay."""
    item = await _seed_item(db, fake_env, user["id"])
    chunk_id = await _seed_snapshots(db, fake_env, user["id"], item["id"])

    text = await _call_mcp(db, fake_env, user, "delete_item", {"item_id": item["id"]})
    assert "trash" in text.lower()

    row = await db.query_first(
        "SELECT deleted_at FROM items WHERE id = ? AND user_id = ?;",
        (item["id"], user["id"]),
    )
    assert row is not None, "the item row must survive a trash"
    assert row["deleted_at"], "the item must be marked as trashed"

    # The snapshot and the vector are untouched: the item can be restored.
    assert await fake_env.BUCKET.get(f"items/{item['id']}/clean.html") is not None
    assert (None, chunk_id) in fake_env.VECTORIZE.store


@pytest.mark.asyncio
async def test_mcp_delete_item_description_mentions_trash():
    """The tool description tells the agent the item is recoverable."""
    tool = next(t for t in MCP_TOOLS if t["name"] == "delete_item")
    lowered = tool["description"].lower()
    assert "trash" in lowered
    assert "30 days" in lowered


@pytest.mark.asyncio
async def test_mcp_delete_item_of_a_foreign_item_does_nothing(db, fake_env, user):
    """Another user's item is never trashed."""
    other = await register_user(db, "other@example.com", "password123", True)
    item = await _seed_item(db, fake_env, other["id"], "https://example.com/other")

    assert await trash_item(db, user["id"], item["id"]) is False
    row = await db.query_first(
        "SELECT deleted_at FROM items WHERE id = ? AND user_id = ?;",
        (item["id"], other["id"]),
    )
    assert row["deleted_at"] is None


# ==========================================
# Restore
# ==========================================


@pytest.mark.asyncio
async def test_restore_item_brings_the_item_back(db, fake_env, user):
    """A restored item is live again and shows up in the library."""
    item = await _seed_item(db, fake_env, user["id"])
    assert await trash_item(db, user["id"], item["id"]) is True

    assert await restore_item(db, user["id"], item["id"]) is True
    row = await db.query_first(
        "SELECT deleted_at FROM items WHERE id = ? AND user_id = ?;",
        (item["id"], user["id"]),
    )
    assert row["deleted_at"] is None
    assert await get_item(db, user["id"], item["id"]) is not None

    items = await get_recent_items(db, user["id"])
    assert [i["id"] for i in items] == [item["id"]]


@pytest.mark.asyncio
async def test_restore_item_of_a_live_or_foreign_item_returns_false(db, fake_env, user):
    """Restore only works on an owned, trashed item."""
    other = await register_user(db, "other2@example.com", "password123", True)
    item = await _seed_item(db, fake_env, user["id"], "https://example.com/live")
    assert await restore_item(db, user["id"], item["id"]) is False
    assert await restore_item(db, other["id"], item["id"]) is False


# ==========================================
# Exclusion from every read path
# ==========================================


@pytest.mark.asyncio
async def test_trashed_item_is_hidden_from_library_and_counts(db, fake_env, user):
    """The library feed and the status pills skip trashed items."""
    item = await _seed_item(db, fake_env, user["id"])
    await trash_item(db, user["id"], item["id"])

    assert await get_recent_items(db, user["id"]) == []
    assert await count_recent_items(db, user["id"]) == 0
    counts = await get_status_counts(db, user["id"])
    assert counts["all"] == 0


@pytest.mark.asyncio
async def test_trashed_item_is_hidden_from_search(db, fake_env, user):
    """FTS and hybrid search skip trashed items."""
    item = await _seed_item(db, fake_env, user["id"])
    await trash_item(db, user["id"], item["id"])

    assert await search_fts(db, user["id"], "Trash") == []
    results, total = await hybrid_search(
        db, fake_env, user["id"], query="Trash", mode="keyword"
    )
    assert results == []
    assert total == 0


@pytest.mark.asyncio
async def test_trashed_item_is_hidden_from_vector_results(db, fake_env, user):
    """A trashed item keeps its vector but must not come back from a search."""
    item = await _seed_item(db, fake_env, user["id"])
    await _seed_snapshots(db, fake_env, user["id"], item["id"])
    await trash_item(db, user["id"], item["id"])

    results, total = await hybrid_search(
        db, fake_env, user["id"], query="Trash", mode="semantic"
    )
    assert results == []
    assert total == 0


@pytest.mark.asyncio
async def test_trashed_item_is_hidden_from_mcp_read_paths(db, fake_env, user):
    """MCP list, search, find, read and related skip trashed items."""
    item = await _seed_item(db, fake_env, user["id"])
    await _seed_snapshots(db, fake_env, user["id"], item["id"])
    await trash_item(db, user["id"], item["id"])

    assert await list_items_compact(db, user["id"]) == []
    assert await find_items(db, user["id"], title="Trash Guide") == []
    assert await match_items(db, user["id"]) == (0, [])
    with pytest.raises(ValueError):
        await read_item_chunks(db, fake_env, user["id"], item["id"])
    with pytest.raises(ValueError):
        await related_items(db, fake_env, user["id"], item_id=item["id"])

    text = await _call_mcp(db, fake_env, user, "list_items", {})
    assert item["id"] not in text


@pytest.mark.asyncio
async def test_trashed_item_is_not_a_related_result(db, fake_env, user):
    """A trashed item keeps its vector but never comes back as a relation."""
    live = await _seed_item(db, fake_env, user["id"], "https://example.com/live-source")
    gone = await _seed_item(db, fake_env, user["id"], "https://example.com/gone-source")
    await _seed_snapshots(db, fake_env, user["id"], live["id"])
    await _seed_snapshots(db, fake_env, user["id"], gone["id"])
    await trash_item(db, user["id"], gone["id"])

    results = await related_items(db, fake_env, user["id"], item_id=live["id"])

    assert gone["id"] not in [r["id"] for r in results]


@pytest.mark.asyncio
async def test_trashed_item_is_hidden_from_export_and_tag_counts(db, fake_env, user):
    """Exports and tag statistics skip trashed items."""
    item = await _seed_item(db, fake_env, user["id"])
    await trash_item(db, user["id"], item["id"])

    assert await export_library_json(db, user["id"]) == []
    stats = await tag_stats(db, user["id"])
    assert stats["total_items"] == 0
    assert stats["tagged_items"] == 0
    assert stats["untagged_items"] == 0
    assert await find_untagged_items(db, user["id"]) == []
    assert await find_unused_tags(db, user["id"])


@pytest.mark.asyncio
async def test_trashed_item_is_hidden_from_tag_counts_and_relations(db, fake_env, user):
    """Tag counts and tag co-occurrence ignore trashed items."""
    item = await _seed_item(db, fake_env, user["id"])
    await add_tags_to_item(db, user["id"], item["id"], ["python"])

    counts = {t["name"]: t["count"] for t in await list_tags(db, user["id"])}
    assert counts["work"] == 1
    assert counts["python"] == 1
    assert [r["name"] for r in await related_tags(db, user["id"], "work")] == ["python"]

    await trash_item(db, user["id"], item["id"])

    counts = {t["name"]: t["count"] for t in await list_tags(db, user["id"])}
    assert counts["work"] == 0
    assert counts["python"] == 0
    assert await related_tags(db, user["id"], "work") == []


@pytest.mark.asyncio
async def test_trashed_item_cannot_be_pinned_or_noted(db, fake_env, user):
    """A trashed item is not in the library: it cannot be pinned or annotated."""
    item = await _seed_item(db, fake_env, user["id"])
    await trash_item(db, user["id"], item["id"])

    assert await toggle_pin_item(db, user["id"], item["id"]) is None
    assert await update_user_notes(db, user["id"], item["id"], "why") is False
    # The tag set is refused too: the removal runs before the re-add, so a
    # trashed item would lose its tags instead of getting new ones.
    assert (await set_item_tags(db, user["id"], item["id"], ["other"]))[
        "found"
    ] is False
    assert await get_item_tags(db, user["id"], item["id"]) == ["work"]


@pytest.mark.asyncio
async def test_trashed_item_is_not_matched_by_bulk_update(db, fake_env, user):
    """bulk_update matching never picks a trashed item."""
    item = await _seed_item(db, fake_env, user["id"])
    await trash_item(db, user["id"], item["id"])

    total, ids = await match_items(db, user["id"], since="2000-01-01")
    assert total == 0
    assert ids == []


# ==========================================
# Trash view + purge
# ==========================================


@pytest.mark.asyncio
async def test_list_trash_returns_only_trashed_items(db, fake_env, user):
    """The Trash view lists the trashed items, newest first."""
    live = await _seed_item(db, fake_env, user["id"], "https://example.com/live")
    trashed = await _seed_item(db, fake_env, user["id"], "https://example.com/gone")
    await trash_item(db, user["id"], trashed["id"])

    rows = await list_trash(db, user["id"])
    assert [r["id"] for r in rows] == [trashed["id"]]
    assert live["id"] not in [r["id"] for r in rows]


@pytest.mark.asyncio
async def test_purge_trash_hard_deletes_only_old_trashed_items(db, fake_env, user):
    """The purge removes trashed items older than the window, for good."""
    old = await _seed_item(db, fake_env, user["id"], "https://example.com/old")
    recent = await _seed_item(db, fake_env, user["id"], "https://example.com/recent")
    live = await _seed_item(db, fake_env, user["id"], "https://example.com/live2")

    await trash_item(db, user["id"], old["id"])
    await trash_item(db, user["id"], recent["id"])
    await db.execute(
        "UPDATE items SET deleted_at = datetime('now', '-40 days')"
        " WHERE id = ? AND user_id = ?;",
        (old["id"], user["id"]),
    )
    chunk_id = await _seed_snapshots(db, fake_env, user["id"], old["id"])

    purged = await purge_trash(db, fake_env, user["id"])

    assert purged == 1
    assert (
        await db.query_first(
            "SELECT id FROM items WHERE id = ? AND user_id = ?;",
            (old["id"], user["id"]),
        )
        is None
    )
    # The hard delete took the snapshot and the vector with it.
    assert await fake_env.BUCKET.get(f"items/{old['id']}/clean.html") is None
    assert (None, chunk_id) not in fake_env.VECTORIZE.store
    # The recent trashed item and the live one stay.
    assert (
        await db.query_first(
            "SELECT id FROM items WHERE id = ? AND user_id = ?;",
            (recent["id"], user["id"]),
        )
        is not None
    )
    assert (
        await db.query_first(
            "SELECT id FROM items WHERE id = ? AND user_id = ?;",
            (live["id"], user["id"]),
        )
        is not None
    )


@pytest.mark.asyncio
async def test_purge_trash_never_touches_another_user(db, fake_env, user):
    """The purge is scoped to the user it is called for."""
    other = await register_user(db, "other3@example.com", "password123", True)
    item = await _seed_item(db, fake_env, other["id"], "https://example.com/other2")
    await trash_item(db, other["id"], item["id"])
    await db.execute(
        "UPDATE items SET deleted_at = datetime('now', '-40 days')"
        " WHERE id = ? AND user_id = ?;",
        (item["id"], other["id"]),
    )

    assert await purge_trash(db, fake_env, user["id"]) == 0
    assert (
        await db.query_first(
            "SELECT id FROM items WHERE id = ? AND user_id = ?;",
            (item["id"], other["id"]),
        )
        is not None
    )


@pytest.mark.asyncio
async def test_purge_trash_without_a_user_purges_every_user(db, fake_env, user):
    """The host can schedule one purge for the whole database."""
    other = await register_user(db, "other4@example.com", "password123", True)
    mine = await _seed_item(db, fake_env, user["id"], "https://example.com/mine")
    theirs = await _seed_item(db, fake_env, other["id"], "https://example.com/theirs")
    await trash_item(db, user["id"], mine["id"])
    await trash_item(db, other["id"], theirs["id"])
    await db.execute(
        "UPDATE items SET deleted_at = datetime('now', '-40 days') WHERE user_id = ?;",
        (user["id"],),
    )
    await db.execute(
        "UPDATE items SET deleted_at = datetime('now', '-40 days') WHERE user_id = ?;",
        (other["id"],),
    )

    assert await purge_trash(db, fake_env) == 2


@pytest.mark.asyncio
async def test_delete_forever_removes_a_trashed_item(db, fake_env, user):
    """The hard delete still works on a trashed item (Trash > Delete forever)."""
    item = await _seed_item(db, fake_env, user["id"])
    await trash_item(db, user["id"], item["id"])

    assert await delete_item(db, fake_env, user["id"], item["id"]) is True
    assert (
        await db.query_first(
            "SELECT id FROM items WHERE id = ? AND user_id = ?;",
            (item["id"], user["id"]),
        )
        is None
    )
    assert await list_trash(db, user["id"]) == []


@pytest.mark.asyncio
async def test_saving_a_trashed_url_again_brings_it_back(db, fake_env, user):
    """Re-saving a trashed url restores it (the canonical url is unique)."""
    item = await _seed_item(db, fake_env, user["id"])
    await trash_item(db, user["id"], item["id"])

    again, is_new = await save_item(
        db, fake_env, user["id"], "https://example.com/trash-me", ["work"]
    )

    assert not is_new
    assert again["id"] == item["id"]
    assert await get_item(db, user["id"], item["id"]) is not None


# ==========================================
# App: Trash view, restore, delete forever
# ==========================================


@pytest.mark.asyncio
async def test_trash_page_lists_trashed_items(client, db, fake_env, user, session):
    """The Trash view shows the trashed item with both actions."""
    item = await _seed_item(db, fake_env, user["id"])
    await trash_item(db, user["id"], item["id"])

    client.cookies["kfm_session"] = session
    response = client.get("/trash")

    assert response.status_code == 200
    assert "Trash Guide" in response.text
    assert f"/items/{item['id']}/restore" in response.text
    assert f"/items/{item['id']}/delete-forever" in response.text


@pytest.mark.asyncio
async def test_trash_page_paginates(client, db, fake_env, user, session):
    """The Trash view pages, so nothing is hidden behind the first page."""
    for i in range(51):
        await db.execute(
            "INSERT INTO items (id, user_id, url, canonical_url, title, deleted_at)"
            " VALUES (?, ?, ?, ?, ?, datetime('now', ?));",
            (
                f"item-{i}",
                user["id"],
                f"https://example.com/{i}",
                f"https://example.com/{i}",
                f"Trashed {i}",
                f"-{i} seconds",
            ),
        )

    client.cookies["kfm_session"] = session
    first = client.get("/trash")
    assert first.status_code == 200
    assert "Trashed 0" in first.text
    assert "Trashed 50" not in first.text

    second = client.get("/trash?page=2")
    assert "Trashed 50" in second.text
    assert "Trashed 0" not in second.text


@pytest.mark.asyncio
async def test_restore_route_brings_the_item_back(client, db, fake_env, user, session):
    """Restore puts the item back into the library."""
    item = await _seed_item(db, fake_env, user["id"])
    await trash_item(db, user["id"], item["id"])

    client.cookies["kfm_session"] = session
    response = client.post(f"/items/{item['id']}/restore", follow_redirects=False)

    assert response.status_code == 303
    assert await get_item(db, user["id"], item["id"]) is not None


@pytest.mark.asyncio
async def test_delete_forever_route_removes_the_item(
    client, db, fake_env, user, session
):
    """Delete forever is the permanent path."""
    item = await _seed_item(db, fake_env, user["id"])
    await trash_item(db, user["id"], item["id"])

    client.cookies["kfm_session"] = session
    response = client.post(
        f"/items/{item['id']}/delete-forever", follow_redirects=False
    )

    assert response.status_code == 303
    assert (
        await db.query_first(
            "SELECT id FROM items WHERE id = ? AND user_id = ?;",
            (item["id"], user["id"]),
        )
        is None
    )


@pytest.mark.asyncio
async def test_api_delete_moves_the_item_to_the_trash(
    client, db, fake_env, user, session
):
    """The agent-reachable delete endpoint trashes instead of destroying."""
    item = await _seed_item(db, fake_env, user["id"])

    client.cookies["kfm_session"] = session
    response = client.delete(f"/api/items/{item['id']}")

    assert response.status_code == 200
    row = await db.query_first(
        "SELECT deleted_at FROM items WHERE id = ? AND user_id = ?;",
        (item["id"], user["id"]),
    )
    assert row["deleted_at"], "the item must be trashed, not deleted"
