# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""EXPLAIN QUERY PLAN tests for migration 0018 performance indexes."""

import sqlite3


def _explain(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[str]:
    rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    return [row["detail"] for row in rows]


def test_migration_0018_indexes_present_and_duplicate_dropped(
    sqlite_conn: sqlite3.Connection,
) -> None:
    rows = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index';"
    ).fetchall()
    names = {row["name"] for row in rows}

    expected = {
        "idx_item_opens_item_user_time",
        "idx_sugg_user_item_status_score",
        "idx_items_live_pinned_created_desc",
        "idx_items_live_pinned_created_asc",
        "idx_items_live_state_pinned_created",
        "idx_import_items_user_item",
        "idx_chunks_user_item_idx",
    }
    assert expected.issubset(names)
    assert "idx_opens_user_time" not in names


def test_item_opens_max_opened_subquery_uses_index(
    sqlite_conn: sqlite3.Connection,
) -> None:
    plan = _explain(
        sqlite_conn,
        "SELECT MAX(io.opened_at) FROM item_opens io "
        "WHERE io.item_id = ? AND io.user_id = ?;",
        ("item-1", "user-1"),
    )
    joined = "\n".join(plan)
    assert "idx_item_opens_item_user_time" in joined
    assert "SCAN item_opens" not in joined
    assert "TEMP B-TREE" not in joined


def test_list_pending_suggestions_per_item_uses_index(
    sqlite_conn: sqlite3.Connection,
) -> None:
    plan = _explain(
        sqlite_conn,
        "SELECT s.id, s.item_id, s.phrase, s.score, i.title "
        "FROM suggested_tags s LEFT JOIN items i ON i.id = s.item_id "
        "WHERE s.user_id = ? AND s.item_id = ? AND s.status = 'pending' "
        "AND i.deleted_at IS NULL "
        "ORDER BY s.score DESC LIMIT ?;",
        ("user-1", "item-1", 50),
    )
    joined = "\n".join(plan)
    assert "idx_sugg_user_item_status_score" in joined
    assert "SCAN s" not in joined
    assert "SCAN suggested_tags" not in joined
    assert "TEMP B-TREE" not in joined


def test_live_items_pinned_created_desc_uses_partial_index(
    sqlite_conn: sqlite3.Connection,
) -> None:
    plan = _explain(
        sqlite_conn,
        "SELECT id, title FROM items "
        "WHERE user_id = ? AND deleted_at IS NULL "
        "ORDER BY is_pinned DESC, created_at DESC LIMIT ? OFFSET ?;",
        ("user-1", 20, 0),
    )
    joined = "\n".join(plan)
    assert "idx_items_live_pinned_created_desc" in joined
    assert "SCAN items" not in joined
    assert "TEMP B-TREE" not in joined


def test_live_items_pinned_created_asc_uses_partial_index(
    sqlite_conn: sqlite3.Connection,
) -> None:
    plan = _explain(
        sqlite_conn,
        "SELECT id, title FROM items "
        "WHERE user_id = ? AND deleted_at IS NULL "
        "ORDER BY is_pinned DESC, created_at ASC LIMIT ? OFFSET ?;",
        ("user-1", 20, 0),
    )
    joined = "\n".join(plan)
    assert "idx_items_live_pinned_created_asc" in joined
    assert "SCAN items" not in joined
    assert "TEMP B-TREE" not in joined


def test_live_items_state_pinned_created_uses_partial_index(
    sqlite_conn: sqlite3.Connection,
) -> None:
    plan = _explain(
        sqlite_conn,
        "SELECT id, title FROM items "
        "WHERE user_id = ? AND deleted_at IS NULL AND read_state = ? "
        "ORDER BY is_pinned DESC, created_at DESC LIMIT ? OFFSET ?;",
        ("user-1", "unread", 20, 0),
    )
    joined = "\n".join(plan)
    assert "idx_items_live_state_pinned_created" in joined
    assert "SCAN items" not in joined
    assert "TEMP B-TREE" not in joined


def test_import_items_reconciliation_uses_index(
    sqlite_conn: sqlite3.Connection,
) -> None:
    plan = _explain(
        sqlite_conn,
        "SELECT id, state FROM import_items WHERE user_id = ? AND item_id = ?;",
        ("user-1", "item-1"),
    )
    joined = "\n".join(plan)
    assert "idx_import_items_user_item" in joined
    assert "SCAN import_items" not in joined


def test_chunks_ordered_fetch_uses_index(
    sqlite_conn: sqlite3.Connection,
) -> None:
    plan = _explain(
        sqlite_conn,
        "SELECT id AS chunk_id, chunk_index, token_count "
        "FROM chunks WHERE item_id = ? AND user_id = ? ORDER BY chunk_index;",
        ("item-1", "user-1"),
    )
    joined = "\n".join(plan)
    assert "idx_chunks_user_item_idx" in joined
    assert "SCAN chunks" not in joined
    assert "TEMP B-TREE" not in joined
