# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Persisted per-import status.

One row per upload (per user) holding the counters and a capped list of
error messages. It is the durable version of the one-shot import toast: the
counts stay visible after a refresh and the progress grows while the queue
consumer works through the uploaded notes.

Every query is scoped by user_id: the table is a tenant table.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from keepfor.models.db import Database

# At most this many error messages are kept per import. The list is a hint
# for the user, not a log.
MAX_IMPORT_ERRORS = 20
MAX_ERROR_LENGTH = 200


def _now() -> str:
    """Sortable UTC timestamp (microseconds, so equal-second rows order)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _decode_errors(raw: Any) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(e) for e in data] if isinstance(data, list) else []


def _clean_errors(errors: list[str] | None) -> list[str]:
    out: list[str] = []
    for err in errors or []:
        text = str(err).strip()
        if text:
            out.append(text[:MAX_ERROR_LENGTH])
    return out[:MAX_IMPORT_ERRORS]


def _row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalizes a DB row for the templates."""
    data = dict(row)
    data["errors"] = _decode_errors(data.get("errors"))
    total = int(data.get("notes_total") or 0)
    processed = int(data.get("notes_processed") or 0)
    if total:
        data["progress"] = min(100, int(100 * processed / total))
    else:
        data["progress"] = 100 if data.get("status") == "completed" else 0
    return data


async def create_import(
    db: Database,
    user_id: str,
    *,
    source: str = "",
    filename: str = "",
) -> str:
    """Creates a 'running' import record and returns its id."""
    import_id = str(uuid.uuid4())
    now = _now()
    await db.execute(
        """
        INSERT INTO import_status (
            id, user_id, source, filename, status, errors, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'running', '[]', ?, ?);
        """,
        (import_id, user_id, source[:32], filename[:120], now, now),
    )
    return import_id


async def set_import_counts(
    db: Database,
    user_id: str,
    import_id: str,
    *,
    source: str,
    notes_total: int,
    trashed_skipped: int,
    urls_found: int,
    attachments_not_imported: int,
    parse_errors: int,
    errors: list[str] | None = None,
) -> None:
    """Stores the parse-stage counts of an upload (before the queue runs)."""
    await db.execute(
        """
        UPDATE import_status
        SET source = ?, notes_total = ?, trashed_skipped = ?, urls_found = ?,
            attachments_not_imported = ?, parse_errors = ?, errors = ?,
            updated_at = ?
        WHERE id = ? AND user_id = ?;
        """,
        (
            source[:32],
            notes_total,
            trashed_skipped,
            urls_found,
            attachments_not_imported,
            parse_errors,
            json.dumps(_clean_errors(errors)),
            _now(),
            import_id,
            user_id,
        ),
    )


async def fail_import(db: Database, user_id: str, import_id: str, error: str) -> None:
    """Marks an import as failed with a message for the user."""
    await db.execute(
        "UPDATE import_status SET status = 'failed', error = ?, updated_at = ? "
        "WHERE id = ? AND user_id = ?;",
        (str(error)[:500], _now(), import_id, user_id),
    )


async def apply_chunk_counts(
    db: Database, user_id: str, import_id: str, counts: dict[str, Any]
) -> None:
    """Adds the counts of one processed chunk to the import record.

    The counters are incremented in SQL (chunks may be processed
    concurrently); the error list is a read-modify-write, so a rare
    concurrent chunk can drop an error message but never a count.
    """
    row = await db.query_first(
        "SELECT notes_total, notes_processed, errors, status FROM import_status "
        "WHERE id = ? AND user_id = ?;",
        (import_id, user_id),
    )
    if not row:
        return
    total = int(row["notes_total"] or 0)
    processed = int(row["notes_processed"] or 0) + int(
        counts.get("notes_processed") or 0
    )
    status = row["status"]
    if status == "running" and total and processed >= total:
        status = "completed"
    errors = _decode_errors(row["errors"])
    errors.extend(_clean_errors(counts.get("errors")))
    await db.execute_batch(
        [
            (
                """
                UPDATE import_status
                SET notes_imported = notes_imported + ?,
                    notes_processed = notes_processed + ?,
                    duplicates_skipped = duplicates_skipped + ?,
                    urls_queued = urls_queued + ?,
                    urls_skipped = urls_skipped + ?,
                    parse_errors = parse_errors + ?,
                    plan_skipped = plan_skipped + ?,
                    updated_at = ?
                WHERE id = ? AND user_id = ?;
                """,
                (
                    int(counts.get("notes_imported") or 0),
                    int(counts.get("notes_processed") or 0),
                    int(counts.get("duplicates_skipped") or 0),
                    int(counts.get("urls_queued") or 0),
                    int(counts.get("urls_skipped") or 0),
                    int(counts.get("parse_errors") or 0),
                    int(counts.get("plan_skipped") or 0),
                    _now(),
                    import_id,
                    user_id,
                ),
            ),
            (
                "UPDATE import_status SET errors = ?, status = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ?;",
                (
                    json.dumps(errors[:MAX_IMPORT_ERRORS]),
                    status,
                    _now(),
                    import_id,
                    user_id,
                ),
            ),
        ]
    )


async def get_import(
    db: Database, user_id: str, import_id: str
) -> dict[str, Any] | None:
    """One import record, or None when it does not belong to this user."""
    row = await db.query_first(
        "SELECT * FROM import_status WHERE id = ? AND user_id = ?;",
        (import_id, user_id),
    )
    return _row(row) if row else None


async def list_imports(
    db: Database, user_id: str, limit: int = 5
) -> list[dict[str, Any]]:
    """The user's import records, newest first."""
    rows = await db.query_all(
        "SELECT * FROM import_status WHERE user_id = ? "
        "ORDER BY created_at DESC LIMIT ?;",
        (user_id, limit),
    )
    return [_row(r) for r in rows]
