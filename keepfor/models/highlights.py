# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Highlight model functions: quoted passages anchored to a library item.

Every function is tenant-scoped by user_id. A highlight stores the quoted
text plus the character offsets it covers inside the item's content_text.
"""

from __future__ import annotations

import uuid
from typing import Any

from keepfor.models.db import Database

# Cap on the quoted text of a single highlight.
MAX_HIGHLIGHT_TEXT = 2000
# Cap on the note attached to a highlight.
MAX_HIGHLIGHT_NOTE = 500
# Cap on the color value of a highlight.
MAX_HIGHLIGHT_COLOR = 32
# Cap on the character offsets a highlight may carry.
MAX_HIGHLIGHT_OFFSET = 10_000_000
# Cap on the number of highlights one list call returns.
MAX_HIGHLIGHTS_LIMIT = 100


async def item_content_length(db: Database, user_id: str, item_id: str) -> int | None:
    """Length of an owned item's stored content, or None when it is not owned.

    A trashed item counts as not owned: it left the library.
    """
    row = await db.query_first(
        "SELECT LENGTH(COALESCE(content_text, '')) AS n FROM items"
        " WHERE id = ? AND user_id = ? AND deleted_at IS NULL;",
        (item_id, user_id),
    )
    return int(row["n"] or 0) if row else None


async def create_highlight(
    db: Database,
    user_id: str,
    item_id: str,
    text: str,
    start_offset: int,
    end_offset: int,
    *,
    note: str | None = None,
    color: str | None = None,
) -> dict[str, Any] | None:
    """Creates a highlight on an item the user owns.

    Returns the stored highlight, or None when the item does not exist, is
    in the Trash, or belongs to another user.
    """
    owned = await db.query_first(
        "SELECT id FROM items WHERE id = ? AND user_id = ? AND deleted_at IS NULL;",
        (item_id, user_id),
    )
    if not owned:
        return None
    highlight_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO highlights (id, user_id, item_id, text, start_offset,"
        " end_offset, note, color) VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
        (
            highlight_id,
            user_id,
            item_id,
            text,
            start_offset,
            end_offset,
            note,
            color,
        ),
    )
    return {
        "id": highlight_id,
        "item_id": item_id,
        "text": text,
        "start_offset": start_offset,
        "end_offset": end_offset,
        "note": note,
        "color": color,
    }


async def list_highlights(
    db: Database, user_id: str, item_id: str, *, limit: int = 20, offset: int = 0
) -> list[dict[str, Any]]:
    """Lists the highlights of one owned item, ordered by their position.

    A trashed item is not in the library: its highlights are not listed.
    """
    rows = await db.query_all(
        "SELECT h.id, h.item_id, h.text, h.start_offset, h.end_offset, h.note,"
        " h.color, h.created_at FROM highlights h"
        " JOIN items i ON i.id = h.item_id AND i.user_id = ?"
        " WHERE h.user_id = ? AND h.item_id = ? AND i.deleted_at IS NULL"
        " ORDER BY h.start_offset ASC, h.created_at ASC LIMIT ? OFFSET ?;",
        (user_id, user_id, item_id, limit, offset),
    )
    return [dict(r) for r in rows]


async def delete_highlight(
    db: Database, user_id: str, highlight_id: str, item_id: str | None = None
) -> bool:
    """Deletes one owned highlight. The item it belongs to is never deleted.

    With an item_id the highlight must belong to that item, so a URL naming
    another item finds nothing.
    """
    sql = "SELECT id FROM highlights WHERE id = ? AND user_id = ?;"
    params: tuple[Any, ...] = (highlight_id, user_id)
    if item_id is not None:
        sql = "SELECT id FROM highlights WHERE id = ? AND user_id = ? AND item_id = ?;"
        params = (highlight_id, user_id, item_id)
    row = await db.query_first(sql, params)
    if not row:
        return False
    await db.execute(
        "DELETE FROM highlights WHERE id = ? AND user_id = ?;", (highlight_id, user_id)
    )
    return True
