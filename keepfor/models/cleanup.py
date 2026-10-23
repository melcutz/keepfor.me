# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Library cleanup suggestions (read-only).

Finds what a user could clean up: duplicate items (same normalized url),
dead links (404, 410, or DNS/connection failures), failed extractions (every
other recorded failure, including blocked fetches), never-opened old items,
untagged items and near-duplicate tags. Every query is a SELECT scoped to the
owning user_id; nothing here writes. Trashed items are invisible: the report
only suggests cleaning up what is still in the library.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from keepfor.models.db import Database
from keepfor.models.tags import find_duplicate_tags, find_untagged_items
from keepfor.utils.url import url_dedupe_key

# The categories find_cleanup can report, in report order.
CLEANUP_CATEGORIES = (
    "duplicates",
    "dead_links",
    "failed_extraction",
    "never_opened",
    "untagged",
    "near_duplicate_tags",
)

DEFAULT_CLEANUP_LIMIT = 20
MAX_CLEANUP_LIMIT = 100
DEFAULT_OLDER_THAN_DAYS = 90
# Longest stored failure reason kept in the report.
_MAX_REASON_CHARS = 200
# Max ids listed for one duplicate group; the rest are only counted.
_MAX_GROUP_IDS = 10

BLOCKED_HTTP_STATUSES = frozenset({401, 403, 429})
DEAD_HTTP_STATUSES = frozenset({404, 410})
BLOCKED_FETCH_COPY = "The site blocked our fetch. Open the original or retry."

_URL_RE = re.compile(r"https?://[^\s)]+", re.IGNORECASE)
_HTTP_STATUS_RE = re.compile(
    r"\b(?:http\s*(?:status|error|code)?\s*[:=]?\s*|status\s*[:=]?\s*)"
    r"([1-5]\d{2})\b"
    r"|^\s*([1-5]\d{2})\b"
    r"|\b(401|403|404|410|429)\b",
    re.IGNORECASE,
)

_DEAD_CONTENT_MARKERS = (
    "dead link",
    "page not found",
    "404 not found",
    "error 404",
    "410 gone",
    "this page doesn't exist",
    "this page does not exist",
    "page has been removed",
    "page has been moved",
    "this content is no longer available",
    "the page you are looking for",
)

_DEAD_NETWORK_MARKERS = (
    "dns",
    "name or service not known",
    "nodename nor servname",
    "getaddrinfo",
    "nxdomain",
    "no address associated",
    "temporary failure in name resolution",
    "name resolution",
    "failed to resolve",
    "cannot resolve",
    "could not resolve",
    "unknown host",
    "no such host",
    "enotfound",
    "eai_again",
    "eai_noname",
    "connection refused",
    "connection reset",
    "connection error",
    "connection aborted",
    "connection closed",
    "connection failed",
    "connection failure",
    "connectionerror",
    "connectionrefused",
    "connectionreset",
    "connectionaborted",
    "connect error",
    "failed to connect",
    "unable to connect",
    "cannot connect",
    "network is unreachable",
    "no route to host",
    "host is unreachable",
    "unreachable",
    "econnrefused",
    "econnreset",
    "ehostunreach",
    "enetunreach",
)

_BLOCKED_MARKERS = (
    "the site blocked our fetch",
    "blocked",
    "forbidden",
    "unauthorized",
    "too many requests",
    "rate limit",
    "rate-limit",
    "access denied",
    "login wall",
    "bot detection",
    "checking your browser",
    "verify you are human",
    "authentication required",
    "sign in to continue",
    "sign in to view",
    "log in to continue",
    "you do not have permission",
    "unusual traffic",
    "security check",
    "just a moment",
    "attention required",
    "ddos",
)


def classify_failure(reason_or_status: str | int | None) -> str:
    """Classify a failure reason or HTTP status into 'blocked', 'dead',
    or 'failed_extraction'.

    - HTTP 401, 403, 429 and auth/bot walls -> 'blocked'
    - HTTP 404, 410 and DNS/connection failures -> 'dead'
    - Everything else (5xx, timeouts, parse errors, unknown) -> 'failed_extraction'
    """
    if isinstance(reason_or_status, int) and not isinstance(reason_or_status, bool):
        if reason_or_status in BLOCKED_HTTP_STATUSES:
            return "blocked"
        if reason_or_status in DEAD_HTTP_STATUSES:
            return "dead"
        return "failed_extraction"

    if not isinstance(reason_or_status, str):
        return "failed_extraction"

    raw = reason_or_status.strip()
    if not raw:
        return "failed_extraction"

    low = _URL_RE.sub("", raw).lower()

    match = _HTTP_STATUS_RE.search(low)
    if match:
        code = int(match.group(1) or match.group(2) or match.group(3))
        if code in BLOCKED_HTTP_STATUSES:
            return "blocked"
        if code in DEAD_HTTP_STATUSES:
            return "dead"
        return "failed_extraction"

    if any(marker in low for marker in _DEAD_CONTENT_MARKERS):
        return "dead"

    if "timed out" not in low and "timeout" not in low:
        if any(marker in low for marker in _DEAD_NETWORK_MARKERS):
            return "dead"

    if any(marker in low for marker in _BLOCKED_MARKERS):
        return "blocked"

    return "failed_extraction"


def _reason(text: str | None, fallback: str) -> str:
    clean = (text or "").strip()
    if len(clean) > _MAX_REASON_CHARS:
        clean = clean[: _MAX_REASON_CHARS - 1] + "…"
    return clean or fallback


async def _duplicates(
    db: Database, user_id: str, limit: int, older_than_days: int
) -> dict[str, Any]:
    """Items whose urls normalize to the same value.

    One entry per group of duplicates: the group's ids (capped), how many
    items it holds and whether the id list was cut. The limit caps groups,
    not ids, so a group of 300 saves of one url still reports one entry.
    """
    rows = await db.query_all(
        "SELECT id, url, canonical_url FROM items"
        " WHERE user_id = ? AND deleted_at IS NULL ORDER BY created_at ASC;",
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
    entries = [
        {
            "ids": ids[:_MAX_GROUP_IDS],
            "group_size": len(ids),
            "truncated": len(ids) > _MAX_GROUP_IDS,
        }
        for ids in groups.values()
        if len(ids) > 1
    ]
    return _report(entries, limit)


async def _failed_items(
    db: Database, user_id: str, limit: int, older_than_days: int, *, dead: bool
) -> dict[str, Any]:
    """Failed items split by cause: dead links vs failed extractions (and blocked).

    Only HTTP 404, 410 and DNS/connection failures count as dead links;
    blocked fetches (401, 403, 429, login/bot walls) and every other failure
    (5xx, timeout, parse error, no reason) are folded into failed_extraction
    so dead_links + failed_extraction always equals the Library Failed count.
    No network requests: the split reads only what is already recorded.
    """
    rows = await db.query_all(
        "SELECT id, fail_reason FROM items"
        " WHERE user_id = ? AND status = 'failed' AND deleted_at IS NULL"
        " ORDER BY created_at DESC;",
        (user_id,),
    )
    matched = [
        row for row in rows if (classify_failure(row["fail_reason"]) == "dead") == dead
    ]
    total = len(matched)
    entries = [
        {"id": row["id"], "reason": _reason(row["fail_reason"], "extraction failed")}
        for row in matched[:limit]
    ]
    return _report(entries, limit, total)


async def _dead_links(
    db: Database, user_id: str, limit: int, older_than_days: int
) -> dict[str, Any]:
    """Items whose recorded failure is 404, 410, or a DNS/connection failure."""
    return await _failed_items(db, user_id, limit, older_than_days, dead=True)


async def _failed_extraction(
    db: Database, user_id: str, limit: int, older_than_days: int
) -> dict[str, Any]:
    """Items whose recorded failure is blocked or another extraction failure."""
    return await _failed_items(db, user_id, limit, older_than_days, dead=False)


async def _never_opened(
    db: Database, user_id: str, limit: int, older_than_days: int
) -> dict[str, Any]:
    """Old items that were never opened in the reader."""
    params = (user_id, f"-{older_than_days} days", user_id)
    total_row = await db.query_first(
        "SELECT COUNT(*) as c FROM items i"
        " WHERE i.user_id = ? AND i.deleted_at IS NULL"
        " AND datetime(i.created_at) <= datetime('now', ?)"
        " AND NOT EXISTS (SELECT 1 FROM item_opens o"
        " WHERE o.item_id = i.id AND o.user_id = ?)"
        " AND i.read_state != 'archived';",
        params,
    )
    total = total_row["c"] if total_row else 0
    rows = await db.query_all(
        "SELECT i.id, i.created_at FROM items i"
        " WHERE i.user_id = ? AND i.deleted_at IS NULL"
        " AND datetime(i.created_at) <= datetime('now', ?)"
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
        " AND i.deleted_at IS NULL"
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

    Returns {category: {"items": [...], "count": int, "truncated": bool}}.
    Item categories list {"id", "reason"}; the group categories list one
    entry per group: duplicates gives the group's ids (capped), its size and
    whether the id list was truncated, near_duplicate_tags gives the group's
    tag names. Never writes.
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
    "failed_extraction": _failed_extraction,
    "never_opened": _never_opened,
    "untagged": _untagged,
    "near_duplicate_tags": _near_duplicate_tags,
}
