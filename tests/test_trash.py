# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Trash: a soft delete that keeps an item restorable for 30 days.

Covers the MCP `delete_item` semantics (trash, not destroy), the restore
path, the exclusion of trashed items from every read path, and the purge.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from keepfor.app import app
from keepfor.auth.service import login_user, register_user
from keepfor.mcp.server import MCP_TOOLS, process_mcp_request
from keepfor.models.cleanup import find_cleanup
from keepfor.models.collections import add_items_to_collection, list_collections
from keepfor.models.highlights import create_highlight, list_highlights
from keepfor.models.items import (
    add_tags_to_item,
    delete_item,
    get_item,
    get_item_tags,
    list_purgeable_items,
    list_trash,
    purge_trash,
    record_open,
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
    match_items_by_url,
    read_item_chunks,
    related_items,
    search_fts,
)
from keepfor.spi import TenantScope
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


async def _seed_tenant_snapshots(db, env, scope, user_id, item_id):
    """The same, but under the tenant prefix and in the tenant namespace."""
    blobs = scope.blobs(env)
    await blobs.put_html(
        blobs.key("items", item_id, "clean.html"), "<html><body>hi</body></html>"
    )
    chunk_id = f"item_{item_id}_chunk_0"
    await db.execute(
        "INSERT INTO chunks (id, item_id, user_id, chunk_index, token_count)"
        " VALUES (?, ?, ?, 0, 100);",
        (chunk_id, item_id, user_id),
    )
    await scope.vectors(env).upsert(
        [
            {
                "id": chunk_id,
                "values": [0.42] * 768,
                "metadata": {"item_id": item_id, "user_id": user_id},
            }
        ]
    )
    return chunk_id


def _tenant_scope(db, env, user_id, prefix="t/user_a/", namespace="user_a"):
    """A hosted-style scope: a tenant prefix and a tenant namespace."""
    return TenantScope(
        tenant_id=user_id,
        user_id=user_id,
        db=db,
        bucket=env.BUCKET,
        index=env.VECTORIZE,
        blob_prefix=prefix,
        vector_namespace=namespace,
    )


async def _backdate_trash(db, user_id, item_id, days=40):
    """Trashes an item and moves it out of the 30-day window."""
    await trash_item(db, user_id, item_id)
    await db.execute(
        "UPDATE items SET deleted_at = datetime('now', ?)"
        " WHERE id = ? AND user_id = ?;",
        (f"-{days} days", item_id, user_id),
    )


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
# Exclusion from the cleanup report (find_cleanup)
# ==========================================


@pytest.mark.asyncio
async def test_trashed_item_is_not_a_cleanup_duplicate(db, fake_env, user):
    """find_cleanup never groups a trashed item with its live duplicates."""
    ids = []
    for i in range(3):
        item_id = f"dup-{i}"
        ids.append(item_id)
        # Same dedupe key (the tracking param is dropped), distinct rows.
        await db.execute(
            "INSERT INTO items (id, user_id, url, canonical_url, status)"
            " VALUES (?, ?, ?, ?, 'ok');",
            (
                item_id,
                user["id"],
                f"https://example.com/same?utm_source={i}",
                f"https://example.com/same?utm_source={i}",
            ),
        )

    report = await find_cleanup(db, user["id"])
    assert report["duplicates"]["items"][0]["group_size"] == 3

    await trash_item(db, user["id"], ids[0])

    report = await find_cleanup(db, user["id"])
    group = report["duplicates"]["items"][0]
    assert group["group_size"] == 2
    assert ids[0] not in group["ids"]


@pytest.mark.asyncio
async def test_trashed_item_is_not_a_cleanup_dead_link(db, fake_env, user):
    """A failed extraction in the Trash is not reported as a dead link."""
    item = await _seed_item(db, fake_env, user["id"], "https://example.com/dead")
    await db.execute(
        "UPDATE items SET status = 'failed', fail_reason = 'HTTP 404' WHERE id = ?;",
        (item["id"],),
    )
    await trash_item(db, user["id"], item["id"])

    report = await find_cleanup(db, user["id"])
    assert report["dead_links"]["items"] == []
    assert report["dead_links"]["count"] == 0


@pytest.mark.asyncio
async def test_trashed_item_is_not_a_cleanup_never_opened(db, fake_env, user):
    """An old, never-opened item in the Trash is not a cleanup candidate."""
    item = await _seed_item(db, fake_env, user["id"], "https://example.com/old")
    await db.execute(
        "UPDATE items SET created_at = '2020-01-02 03:04:05' WHERE id = ?;",
        (item["id"],),
    )
    await trash_item(db, user["id"], item["id"])

    report = await find_cleanup(db, user["id"], older_than_days=90)
    assert report["never_opened"]["items"] == []
    assert report["never_opened"]["count"] == 0


@pytest.mark.asyncio
async def test_trashed_item_is_not_a_cleanup_untagged(db, fake_env, user):
    """An untagged item in the Trash is not a cleanup candidate."""
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status)"
        " VALUES ('untagged-1', ?, 'https://example.com/untagged',"
        " 'https://example.com/untagged', 'ok');",
        (user["id"],),
    )
    await trash_item(db, user["id"], "untagged-1")

    report = await find_cleanup(db, user["id"])
    assert report["untagged"]["items"] == []
    assert report["untagged"]["count"] == 0


@pytest.mark.asyncio
async def test_trashed_item_is_not_matched_by_url(db, fake_env, user):
    """A url lookup never returns a trashed item."""
    item = await _seed_item(db, fake_env, user["id"], "https://example.com/lookup")
    assert await match_items_by_url(db, user["id"], "https://example.com/lookup") == [
        item["id"]
    ]

    await trash_item(db, user["id"], item["id"])

    assert await match_items_by_url(db, user["id"], "https://example.com/lookup") == []


# ==========================================
# Exclusion from the MCP resources and prompts
# ==========================================


async def _call_mcp_method(db, env, user, method, params=None):
    """Calls one MCP JSON-RPC method and returns the raw response."""
    body: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return await process_mcp_request(body, db, env, user)


@pytest.mark.asyncio
async def test_trashed_item_is_hidden_from_mcp_resources(db, fake_env, user):
    """The MCP resources read the live library only."""
    item = await _seed_item(db, fake_env, user["id"])
    await trash_item(db, user["id"], item["id"])

    listed = await _call_mcp_method(db, fake_env, user, "resources/list", {})
    uris = [r["uri"] for r in listed["result"]["resources"]]
    assert f"keepfor://item/{item['id']}" not in uris

    read = await _call_mcp_method(
        db, fake_env, user, "resources/read", {"uri": f"keepfor://item/{item['id']}"}
    )
    assert "error" in read

    tagged = await _call_mcp_method(
        db, fake_env, user, "resources/read", {"uri": "keepfor://tag/work"}
    )
    payload = json.loads(tagged["result"]["contents"][0]["text"])
    assert payload["items"] == []


@pytest.mark.asyncio
async def test_trashed_item_is_hidden_from_mcp_prompts(db, fake_env, user):
    """The MCP prompts brief on the live library only."""
    item = await _seed_item(db, fake_env, user["id"])
    await trash_item(db, user["id"], item["id"])

    brief = await _call_mcp_method(
        db,
        fake_env,
        user,
        "prompts/get",
        {"name": "research_brief", "arguments": {"topic": "Trash"}},
    )
    assert item["id"] not in brief["result"]["messages"][0]["content"]["text"]

    review = await _call_mcp_method(
        db, fake_env, user, "prompts/get", {"name": "weekly_review", "arguments": {}}
    )
    assert item["id"] not in review["result"]["messages"][0]["content"]["text"]


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
async def test_purge_trash_with_a_scope_destroys_the_tenant_files_and_vectors(
    db, fake_env, user
):
    """A scoped purge takes the tenant's snapshots, vectors and annotations.

    Without the scope the R2 prefix is "" and the Vectorize namespace is
    None, so the tenant's files (`t/<tenant_id>/...`) and its vectors are
    orphaned. The scope carries both.
    """
    scope = _tenant_scope(db, fake_env, user["id"])
    old = await _seed_item(db, fake_env, user["id"], "https://example.com/old-scoped")
    recent = await _seed_item(
        db, fake_env, user["id"], "https://example.com/recent-scoped"
    )
    live = await _seed_item(db, fake_env, user["id"], "https://example.com/live-scoped")

    await create_highlight(db, user["id"], old["id"], "Trash content", 0, 13)
    await db.execute(
        "INSERT INTO collections (id, user_id, name)"
        " VALUES ('col-scoped', ?, 'Reading');",
        (user["id"],),
    )
    await add_items_to_collection(db, user["id"], "Reading", [old["id"]])

    await _backdate_trash(db, user["id"], old["id"])
    await trash_item(db, user["id"], recent["id"])
    chunk_id = await _seed_tenant_snapshots(db, fake_env, scope, user["id"], old["id"])

    purged = await purge_trash(db, fake_env, user["id"], scope=scope)

    assert purged == 1
    # The row is gone.
    assert (
        await db.query_first(
            "SELECT id FROM items WHERE id = ? AND user_id = ?;",
            (old["id"], user["id"]),
        )
        is None
    )
    # The snapshot under the tenant prefix is gone.
    assert await fake_env.BUCKET.get(f"t/user_a/items/{old['id']}/clean.html") is None
    # The vector in the tenant namespace is gone.
    assert ("user_a", chunk_id) not in fake_env.VECTORIZE.store
    # The annotations went with it.
    assert (
        await db.query_all("SELECT id FROM highlights WHERE item_id = ?;", (old["id"],))
        == []
    )
    assert (
        await db.query_all(
            "SELECT collection_id FROM collection_items WHERE item_id = ?;",
            (old["id"],),
        )
        == []
    )
    # The item inside the window and the live one stay.
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
async def test_purge_trash_with_a_scope_never_touches_another_user(db, fake_env, user):
    """The scope is the tenant boundary: only its own user is purged."""
    other = await register_user(db, "other5@example.com", "password123", True)
    scope = _tenant_scope(db, fake_env, user["id"])
    mine = await _seed_item(db, fake_env, user["id"], "https://example.com/mine-scoped")
    theirs = await _seed_item(
        db, fake_env, other["id"], "https://example.com/theirs-scoped"
    )
    await _backdate_trash(db, user["id"], mine["id"])
    await _backdate_trash(db, other["id"], theirs["id"])

    purged = await purge_trash(db, fake_env, scope=scope)

    assert purged == 1
    assert (
        await db.query_first(
            "SELECT id FROM items WHERE id = ? AND user_id = ?;",
            (mine["id"], user["id"]),
        )
        is None
    )
    assert (
        await db.query_first(
            "SELECT id FROM items WHERE id = ? AND user_id = ?;",
            (theirs["id"], other["id"]),
        )
        is not None
    )


@pytest.mark.asyncio
async def test_purge_trash_refuses_an_unscoped_call_in_multi_tenant_mode(
    db, fake_env, user
):
    """A multi-tenant host must name the tenant it purges."""
    from keepfor.runtime import set_providers
    from keepfor.spi import Providers

    class _MultiTenantScopeProvider:
        """Any provider but the single-tenant default is multi-tenant."""

        async def scope_for_principal(self, principal, env):
            raise NotImplementedError

        async def scope_for_tenant(self, tenant_id, env):
            raise NotImplementedError

    set_providers(Providers(scope=_MultiTenantScopeProvider()))

    item = await _seed_item(db, fake_env, user["id"], "https://example.com/unscoped")
    await _backdate_trash(db, user["id"], item["id"])

    with pytest.raises(ValueError):
        await purge_trash(db, fake_env, user["id"])

    # Nothing was destroyed.
    assert (
        await db.query_first(
            "SELECT id FROM items WHERE id = ? AND user_id = ?;",
            (item["id"], user["id"]),
        )
        is not None
    )


@pytest.mark.asyncio
async def test_purge_trash_refuses_a_user_that_is_not_the_scope(db, fake_env, user):
    """A user_id that is not the scope's user would orphan its files."""
    other = await register_user(db, "other6@example.com", "password123", True)
    scope = _tenant_scope(db, fake_env, user["id"])

    with pytest.raises(ValueError):
        await purge_trash(db, fake_env, other["id"], scope=scope)


@pytest.mark.asyncio
async def test_purge_trash_respects_the_item_limit(db, fake_env, user):
    """One call destroys at most `limit` items."""
    for i in range(3):
        item = await _seed_item(
            db, fake_env, user["id"], f"https://example.com/limit-{i}"
        )
        await _backdate_trash(db, user["id"], item["id"])

    assert await purge_trash(db, fake_env, user["id"], limit=2) == 2
    assert len(await list_trash(db, user["id"])) == 1


@pytest.mark.asyncio
async def test_list_purgeable_items_selects_the_old_trashed_items(db, fake_env, user):
    """The helper is the 30-day selection a host would otherwise copy."""
    old = await _seed_item(db, fake_env, user["id"], "https://example.com/helper-old")
    recent = await _seed_item(
        db, fake_env, user["id"], "https://example.com/helper-recent"
    )
    live = await _seed_item(db, fake_env, user["id"], "https://example.com/helper-live")
    await _backdate_trash(db, user["id"], old["id"], days=40)
    await _backdate_trash(db, user["id"], recent["id"], days=5)

    # The 30-day window keeps the item that is still inside it out.
    assert await list_purgeable_items(db, user["id"], 30, 10) == [old["id"]]
    # A shorter window takes both.
    assert set(await list_purgeable_items(db, user["id"], 3, 10)) == {
        old["id"],
        recent["id"],
    }
    # The limit caps the selection.
    assert await list_purgeable_items(db, user["id"], 30, 0) == []
    # A live item is never purgeable.
    assert live["id"] not in await list_purgeable_items(db, user["id"], 3, 10)


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
async def test_delete_forever_cleans_the_extra_item_tables(
    db, fake_env, user, monkeypatch
):
    """The permanent delete is where the per-item tables are emptied.

    PR #32 (highlights, collections) plugs its tables into
    ITEM_EXTRA_DELETES. The trash and the restore must leave them alone, so
    a restored item keeps them.
    """
    from keepfor.models import items as items_model

    monkeypatch.setattr(
        items_model,
        "ITEM_EXTRA_DELETES",
        ("DELETE FROM item_opens WHERE item_id = ? AND user_id = ?;",),
    )
    item = await _seed_item(db, fake_env, user["id"])
    await record_open(db, user["id"], item["id"])

    # The trash and the restore leave the extra rows alone.
    await trash_item(db, user["id"], item["id"])
    await restore_item(db, user["id"], item["id"])
    assert (
        await db.query_first(
            "SELECT id FROM item_opens WHERE item_id = ? AND user_id = ?;",
            (item["id"], user["id"]),
        )
        is not None
    )

    # The permanent delete takes them with it.
    assert await delete_item(db, fake_env, user["id"], item["id"]) is True
    assert (
        await db.query_first(
            "SELECT id FROM item_opens WHERE item_id = ? AND user_id = ?;",
            (item["id"], user["id"]),
        )
        is None
    )


@pytest.mark.asyncio
async def test_trash_and_restore_keep_highlights_and_collection_membership(
    db, fake_env, user
):
    """The trash keeps the annotations of an item: they come back on restore.

    The soft delete must not drop the highlights or the collection links:
    only the permanent purge does.
    """
    item = await _seed_item(db, fake_env, user["id"])
    await create_highlight(db, user["id"], item["id"], "Trash content", 0, 13)
    await db.execute(
        "INSERT INTO collections (id, user_id, name) VALUES ('col-1', ?, 'Reading');",
        (user["id"],),
    )
    await add_items_to_collection(db, user["id"], "Reading", [item["id"]])

    await trash_item(db, user["id"], item["id"])

    # The annotations are still there, they are just not listed.
    assert await list_highlights(db, user["id"], item["id"]) == []
    assert (await list_collections(db, user["id"]))[0]["item_count"] == 0

    await restore_item(db, user["id"], item["id"])

    assert len(await list_highlights(db, user["id"], item["id"])) == 1
    assert (await list_collections(db, user["id"]))[0]["item_count"] == 1


@pytest.mark.asyncio
async def test_purge_removes_highlights_and_collection_membership(db, fake_env, user):
    """The permanent purge takes the annotations of the item with it."""
    item = await _seed_item(db, fake_env, user["id"])
    await create_highlight(db, user["id"], item["id"], "Trash content", 0, 13)
    await db.execute(
        "INSERT INTO collections (id, user_id, name) VALUES ('col-1', ?, 'Reading');",
        (user["id"],),
    )
    await add_items_to_collection(db, user["id"], "Reading", [item["id"]])
    await trash_item(db, user["id"], item["id"])
    await db.execute(
        "UPDATE items SET deleted_at = datetime('now', '-40 days')"
        " WHERE id = ? AND user_id = ?;",
        (item["id"], user["id"]),
    )

    assert await purge_trash(db, fake_env, user["id"]) == 1

    assert (
        await db.query_all(
            "SELECT id FROM highlights WHERE item_id = ?;", (item["id"],)
        )
        == []
    )
    assert (
        await db.query_all(
            "SELECT collection_id FROM collection_items WHERE item_id = ?;",
            (item["id"],),
        )
        == []
    )


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
            " VALUES (?, ?, ?, ?, ?, datetime('2026-01-01 00:00:00', ?));",
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
