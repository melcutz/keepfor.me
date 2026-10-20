# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Library cleanup suggestions (read-only).

Finds what a user could clean up: duplicate items (same normalized url),
dead links (the stored extraction failed), never-opened old items, untagged
items and near-duplicate tags. Every query is a SELECT scoped to the owning
user_id; nothing here writes.
"""

from __future__ import annotations

import asyncio
from typing import Any

from keepfor.models.db import Database
from keepfor.models.tags import find_duplicate_tags, find_untagged_items
from keepfor.utils.url import url_dedupe_key

# The categories find_cleanup can report, in report order.
CLEANUP_CATEGORIES = (
    "duplicates",
    "dead_links",
    "never_opened",
    "untagged",
    "near_duplicate_tags",
)

DEFAULT_CLEANUP_LIMIT = 20
MAX_CLEANUP_LIMIT = 100
DEFAULT_OLDER_THAN_DAYS = 90
# Longest stored failure reason kept in the report.
_MAX_REASON_CHARS = 200


def _reason(text: str | None, fallback: str) -> str:
    clean = (text or "").strip()
    if len(clean) > _MAX_REASON_CHARS:
        clean = clean[: _MAX_REASON_CHARS - 1] + "…"
    return clean or fallback


async def _duplicates(
    db: Database, user_id: str, limit: int, older_than_days: int
) -> dict[str, Any]:
    """Items whose urls normalize to the same value."""
    rows = await db.query_all(
        "SELECT id, url, canonical_url FROM items"
        " WHERE user_id = ? ORDER BY created_at ASC;",
        (user_id,),
    )
    groups: dict[str, list[str]] = {}
    for row in rows:
        raw = (row["url"] or row["canonical_url"] or "").strip()
        if not raw:
            continue
        key = url_dedupe_key(raw)
        if key:
            groups.setdefault(key, []).append(row["id"])
    entries: list[dict[str, Any]] = []
    for ids in groups.values():
        if len(ids) < 2:
            continue
        for item_id in ids:
            others = [other for other in ids if other != item_id]
            entries.append(
                {"id": item_id, "reason": "duplicate of " + ", ".join(others)}
            )
    return _report(entries, limit)


async def _dead_links(
    db: Database, user_id: str, limit: int, older_than_days: int
) -> dict[str, Any]:
    """Items whose stored extraction failed (no network requests)."""
    total_row = await db.query_first(
        "SELECT COUNT(*) as c FROM items WHERE user_id = ? AND status = 'failed';",
        (user_id,),
    )
    total = total_row["c"] if total_row else 0
    rows = await db.query_all(
        "SELECT id, fail_reason FROM items"
        " WHERE user_id = ? AND status = 'failed'"
        " ORDER BY created_at DESC LIMIT ?;",
        (user_id, limit),
    )
    entries = [
        {"id": row["id"], "reason": _reason(row["fail_reason"], "extraction failed")}
        for row in rows
    ]
    return _report(entries, limit, total)


async def _never_opened(
    db: Database, user_id: str, limit: int, older_than_days: int
) -> dict[str, Any]:
    """Old items that were never opened in the reader."""
    params = (user_id, f"-{older_than_days} days", user_id)
    total_row = await db.query_first(
        "SELECT COUNT(*) as c FROM items i"
        " WHERE i.user_id = ? AND datetime(i.created_at) <= datetime('now', ?)"
        " AND NOT EXISTS (SELECT 1 FROM item_opens o"
        " WHERE o.item_id = i.id AND o.user_id = ?)"
        " AND i.read_state != 'archived';",
        params,
    )
    total = total_row["c"] if total_row else 0
    rows = await db.query_all(
        "SELECT i.id, i.created_at FROM items i"
        " WHERE i.user_id = ? AND datetime(i.created_at) <= datetime('now', ?)"
        " AND NOT EXISTS (SELECT 1 FROM item_opens o"
        " WHERE o.item_id = i.id AND o.user_id = ?)"
        " AND i.read_state != 'archived'"
        " ORDER BY i.created_at ASC LIMIT ?;",
        (*params, limit),
    )
    entries = [
        {
            "id": row["id"],
            "reason": "never opened (saved " + str(row["created_at"]) + ")",
        }
        for row in rows
    ]
    return _report(entries, limit, total)


async def _untagged(
    db: Database, user_id: str, limit: int, older_than_days: int
) -> dict[str, Any]:
    """Items without any tag."""
    total_row = await db.query_first(
        "SELECT COUNT(*) as c FROM items i WHERE i.user_id = ?"
        " AND NOT EXISTS (SELECT 1 FROM item_tags it WHERE it.item_id = i.id);",
        (user_id,),
    )
    total = total_row["c"] if total_row else 0
    rows = await find_untagged_items(db, user_id, limit=limit)
    entries = [{"id": row["id"], "reason": "no tags"} for row in rows]
    return _report(entries, limit, total)


async def _near_duplicate_tags(
    db: Database, user_id: str, limit: int, older_than_days: int
) -> dict[str, Any]:
    """Tags that differ only by case, separators, plural or acronym."""
    groups = await find_duplicate_tags(db, user_id)
    entries = [
        {"tags": group["tags"], "reason": "near-duplicate tags"} for group in groups
    ]
    return _report(entries, limit)


def _report(
    entries: list[dict[str, Any]], limit: int, total: int | None = None
) -> dict[str, Any]:
    """Caps a candidate list and reports the total it was cut from."""
    items = entries[:limit]
    count = len(entries) if total is None else total
    return {"items": items, "count": count, "truncated": count > len(items)}


async def find_cleanup(
    db: Database,
    user_id: str,
    *,
    categories: list[str] | None = None,
    older_than_days: int = DEFAULT_OLDER_THAN_DAYS,
    limit: int = DEFAULT_CLEANUP_LIMIT,
) -> dict[str, dict[str, Any]]:
    """Reports cleanup candidates per category.

    Returns {category: {"items": [{id, reason}], "count": int,
    "truncated": bool}}. Never writes.
    """
    wanted = [
        name for name in CLEANUP_CATEGORIES if not categories or name in categories
    ]
    reports = await asyncio.gather(
        *(
            _CATEGORY_QUERIES[name](db, user_id, limit, older_than_days)
            for name in wanted
        )
    )
    return dict(zip(wanted, reports))


_CATEGORY_QUERIES = {
    "duplicates": _duplicates,
    "dead_links": _dead_links,
    "never_opened": _never_opened,
    "untagged": _untagged,
    "near_duplicate_tags": _near_duplicate_tags,
}
