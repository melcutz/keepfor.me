# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Persisted per-import status and per-import item rows.

One row per upload (per user) in `import_status` holding the counters and a
capped list of error messages, plus one row per queued link, skipped link,
or non-imported attachment in `import_items`.

Every query is scoped by user_id: both tables are tenant tables.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from keepfor.models.db import Database
from keepfor.utils.url import canonicalize_url

if TYPE_CHECKING:
    from keepfor.spi import Providers, TenantScope

# At most this many error messages are kept per import. The list is a hint
# for the user, not a log.
MAX_IMPORT_ERRORS = 20
MAX_ERROR_LENGTH = 200


class LinkSkipReason(str, Enum):
    """Fixed enum of reasons a link in an import was not queued."""

    DUPLICATE_IN_FILE = "duplicate_in_file"
    ALREADY_IN_LIBRARY = "already_in_library"
    PLAN_LIMIT = "plan_limit"
    UNSUPPORTED_SCHEME = "unsupported_scheme"
    QUEUE_FAILED = "queue_failed"
    IMPORT_LIMIT = "import_limit"

    def __str__(self) -> str:
        return self.value


LINK_SKIP_REASONS: tuple[str, ...] = tuple(r.value for r in LinkSkipReason)

SKIP_REASON_LABELS: dict[str, str] = {
    LinkSkipReason.DUPLICATE_IN_FILE.value: "Duplicate URL in upload",
    LinkSkipReason.ALREADY_IN_LIBRARY.value: "Already in your library",
    LinkSkipReason.PLAN_LIMIT.value: "Skipped due to plan limit",
    LinkSkipReason.UNSUPPORTED_SCHEME.value: "Unsupported URL scheme",
    LinkSkipReason.QUEUE_FAILED.value: "Failed to queue extraction",
    LinkSkipReason.IMPORT_LIMIT.value: "Import URL limit (500) exceeded",
}


class AttachmentSkipReason(str, Enum):
    """Fixed enum of reasons an attachment in an import was not imported."""

    ATTACHMENTS_NOT_SUPPORTED = "attachments_not_supported"

    def __str__(self) -> str:
        return self.value


ATTACHMENT_REASON: str = AttachmentSkipReason.ATTACHMENTS_NOT_SUPPORTED.value

ATTACHMENT_REASON_LABELS: dict[str, str] = {
    ATTACHMENT_REASON: "Attachments are not imported",
}

LINK_STATE_LABELS: dict[str, str] = {
    "queued": "Queued",
    "extracting": "Extracting",
    "ready": "Ready",
    "failed": "Failed",
}


def derive_link_state(item_status: str | None) -> str:
    """Maps an `items.status` value to the canonical import link state."""
    if not item_status or item_status in ("queued", "pending"):
        return "queued"
    if item_status in ("extracting", "processing", "running"):
        return "extracting"
    if item_status == "failed":
        return "failed"
    if item_status in ("ok", "ready", "archived", "read", "unread"):
        return "ready"
    return "queued"


def _now() -> str:
    """Sortable UTC timestamp (microseconds, so equal-second rows order)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_filename(filename: str) -> str:
    """Strips directory components so only the upload basename is stored."""
    if not filename:
        return ""
    normalized = str(filename).replace("\\", "/").strip()
    name = PurePosixPath(normalized).name
    if name in ("", ".", ".."):
        return ""
    return name[:120]


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
    data["filename"] = _clean_filename(data.get("filename") or "")
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
    clean_name = _clean_filename(filename)
    await db.execute(
        """
        INSERT INTO import_status (
            id, user_id, source, filename, status, errors, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'running', '[]', ?, ?);
        """,
        (import_id, user_id, source[:32], clean_name, now, now),
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

    The counters and completion status are updated atomically in SQL
    (chunks may be processed concurrently); the error list is a
    read-modify-write, so a rare concurrent chunk can drop an error
    message but never a count or completion state.
    """
    row = await db.query_first(
        "SELECT errors, notes_total FROM import_status WHERE id = ? AND user_id = ?;",
        (import_id, user_id),
    )
    if not row:
        return
    processed_delta = int(counts.get("notes_processed") or 0)
    attachments_delta = int(counts.get("attachments_not_imported") or 0)
    new_errors = _clean_errors(counts.get("errors"))
    now = _now()
    statements: list[tuple[str, tuple[Any, ...]]] = [
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
                attachments_not_imported = CASE
                    WHEN notes_total = 0 THEN attachments_not_imported + ?
                    ELSE attachments_not_imported
                END,
                status = CASE
                    WHEN status = 'running'
                     AND notes_total > 0
                     AND notes_processed + ? >= notes_total
                    THEN 'completed'
                    ELSE status
                END,
                updated_at = ?
            WHERE id = ? AND user_id = ?;
            """,
            (
                int(counts.get("notes_imported") or 0),
                processed_delta,
                int(counts.get("duplicates_skipped") or 0),
                int(counts.get("urls_queued") or 0),
                int(counts.get("urls_skipped") or 0),
                int(counts.get("parse_errors") or 0),
                int(counts.get("plan_skipped") or 0),
                attachments_delta,
                processed_delta,
                now,
                import_id,
                user_id,
            ),
        )
    ]
    if new_errors:
        errors = _decode_errors(row["errors"])
        errors.extend(new_errors)
        statements.append(
            (
                "UPDATE import_status SET errors = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ?;",
                (
                    json.dumps(errors[:MAX_IMPORT_ERRORS]),
                    now,
                    import_id,
                    user_id,
                ),
            )
        )
    await db.execute_batch(statements)


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


async def record_import_items(
    db: Database,
    user_id: str,
    import_id: str,
    items: list[dict[str, Any]],
) -> list[str]:
    """Persists a batch of per-import link or attachment rows.

    Never stores note titles, note bodies, or raw attachment filenames.
    Returns the generated row IDs in the same order as `items`.
    """
    if not items:
        return []
    base_dt = datetime.now(timezone.utc)
    row_ids: list[str] = []
    statements: list[tuple[str, tuple[Any, ...]]] = []
    for idx, entry in enumerate(items):
        row_now = (
            (base_dt + timedelta(microseconds=idx)).isoformat().replace("+00:00", "Z")
        )
        row_id = str(entry.get("id") or uuid.uuid4())
        row_ids.append(row_id)
        kind = "attachment" if entry.get("kind") == "attachment" else "link"
        default_state = "skipped" if kind == "attachment" else "queued"
        state = str(entry.get("state") or default_state)
        url = str(entry["url"])[:2048] if entry.get("url") else None
        item_id = str(entry["item_id"]) if entry.get("item_id") else None
        reason = str(entry["reason"])[:64] if entry.get("reason") else None
        mime_or_ext = (
            str(entry["mime_or_ext"])[:64] if entry.get("mime_or_ext") else None
        )
        statements.append(
            (
                """
                INSERT INTO import_items (
                    id, import_id, user_id, kind, state, url, item_id,
                    reason, mime_or_ext, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    row_id,
                    import_id,
                    user_id,
                    kind,
                    state,
                    url,
                    item_id,
                    reason,
                    mime_or_ext,
                    row_now,
                    row_now,
                ),
            )
        )
    await db.execute_batch(statements)
    return row_ids


async def get_import_prior_link_state(
    db: Database, user_id: str, import_id: str
) -> tuple[set[str], int]:
    """Returns (seen_canonical_urls, queued_link_count) already recorded for an import.

    Used when an import is split across multiple queue chunks so cross-chunk
    in-file duplicate detection and the 500-URL cap remain consistent.
    """
    rows = await db.query_all(
        "SELECT state, reason, url FROM import_items "
        "WHERE user_id = ? AND import_id = ? AND kind = 'link';",
        (user_id, import_id),
    )
    seen: set[str] = set()
    queued_count = 0
    for r in rows:
        if r.get("state") == "queued":
            queued_count += 1
        url = r.get("url")
        reason = r.get("reason")
        if url and reason not in (
            LinkSkipReason.UNSUPPORTED_SCHEME.value,
            LinkSkipReason.IMPORT_LIMIT.value,
        ):
            seen.add(url)
    return seen, queued_count


async def list_import_items(
    db: Database, user_id: str, import_id: str
) -> list[dict[str, Any]]:
    """Lists all `import_items` rows for an import, enriched with live item status.

    Never selects `items.title` or any note content so note titles/bodies can
    never leak through this query.
    """
    rows = await db.query_all(
        """
        SELECT
            ii.id,
            ii.import_id,
            ii.user_id,
            ii.kind,
            ii.state,
            ii.url,
            ii.item_id,
            ii.reason,
            ii.mime_or_ext,
            ii.created_at,
            ii.updated_at,
            i.id AS existing_item_id,
            i.status AS item_status,
            i.deleted_at AS item_deleted_at
        FROM import_items ii
        LEFT JOIN items i
            ON i.id = ii.item_id AND i.user_id = ii.user_id
        WHERE ii.user_id = ? AND ii.import_id = ?
        ORDER BY ii.created_at ASC, ii.id ASC;
        """,
        (user_id, import_id),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        has_live_item = bool(d.get("existing_item_id")) and not d.get("item_deleted_at")
        d["item_exists"] = has_live_item
        d["item_id"] = d["item_id"] if has_live_item else d.get("item_id")
        d["item_href"] = f"/items/{d['existing_item_id']}" if has_live_item else None
        if d.get("kind") == "link" and d.get("state") == "queued":
            live_state = (
                derive_link_state(d.get("item_status")) if has_live_item else "queued"
            )
            d["live_state"] = live_state
            d["live_state_label"] = LINK_STATE_LABELS.get(live_state, live_state)
            d["can_retry"] = live_state == "failed"
        elif d.get("kind") == "link":
            reason = d.get("reason") or ""
            d["reason_label"] = SKIP_REASON_LABELS.get(reason, reason)
            d["live_state"] = "skipped"
            d["live_state_label"] = "Skipped"
            d["can_retry"] = reason == LinkSkipReason.QUEUE_FAILED.value
        else:
            reason = d.get("reason") or ATTACHMENT_REASON
            d["reason"] = reason
            d["reason_label"] = ATTACHMENT_REASON_LABELS.get(reason, reason)
            d["can_retry"] = False
        out.append(d)
    return out


async def get_import_detail(
    db: Database, user_id: str, import_id: str
) -> dict[str, Any] | None:
    """Returns the owner-scoped import record and categorized item rows, or None."""
    record = await get_import(db, user_id, import_id)
    if record is None:
        return None
    items = await list_import_items(db, user_id, import_id)
    queued_links: list[dict[str, Any]] = []
    skipped_links: list[dict[str, Any]] = []
    plan_skipped_links: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    skip_reason_counts: dict[str, int] = {r.value: 0 for r in LinkSkipReason}
    live_state_counts: dict[str, int] = {
        "queued": 0,
        "extracting": 0,
        "ready": 0,
        "failed": 0,
    }

    for row in items:
        if row["kind"] == "link" and row["state"] == "queued":
            queued_links.append(row)
            ls = row.get("live_state") or "queued"
            live_state_counts[ls] = live_state_counts.get(ls, 0) + 1
        elif row["kind"] == "link":
            reason = row.get("reason") or ""
            if reason in skip_reason_counts:
                skip_reason_counts[reason] += 1
            if reason == LinkSkipReason.PLAN_LIMIT.value:
                plan_skipped_links.append(row)
            else:
                skipped_links.append(row)
        elif row["kind"] == "attachment":
            attachments.append(row)

    retryable_count = sum(
        1 for r in (queued_links + skipped_links) if r.get("can_retry")
    )

    return {
        "import": record,
        "queued_links": queued_links,
        "skipped_links": skipped_links,
        "plan_skipped_links": plan_skipped_links,
        "all_skipped_links": skipped_links + plan_skipped_links,
        "attachments": attachments,
        "skip_reason_counts": skip_reason_counts,
        "live_state_counts": live_state_counts,
        "retryable_count": retryable_count,
    }


async def retry_import_item(
    db: Database,
    env: Any,
    user_id: str,
    import_id: str,
    item_row_id: str,
    *,
    providers: Providers | None = None,
    scope: TenantScope | None = None,
) -> dict[str, Any]:
    """Retries one `import_items` link row in `queue_failed` or failed extraction.

    - When the linked item already exists in `items`, re-queues or re-runs
      extraction without checking or recording plan entitlements again.
    - When no item exists yet (`queue_failed` before item creation), performs
      a fresh save (checking entitlement once) and updates the row.
    - Never reports `ok=True` when the retry fails or inline extraction fails.
    """
    from keepfor.models.items import _send_extraction_batch, restore_item, save_item

    if scope is None:
        from keepfor.defaults import default_scope

        scope = default_scope(db, env, user_id)

    if providers is None:
        from keepfor.runtime import get_providers

        providers = get_providers()

    row = await db.query_first(
        "SELECT * FROM import_items WHERE id = ? AND import_id = ? AND user_id = ?;",
        (item_row_id, import_id, user_id),
    )
    if not row or row.get("kind") != "link":
        return {
            "status": "not_found",
            "ok": False,
            "retried": False,
            "error": "Import link row not found.",
        }

    url = row.get("url") or ""
    clean_url = canonicalize_url(url) if url else ""

    item_row: dict[str, Any] | None = None
    if row.get("item_id"):
        item_row = await db.query_first(
            "SELECT id, url, canonical_url, status, deleted_at "
            "FROM items WHERE id = ? AND user_id = ?;",
            (row["item_id"], user_id),
        )
    if item_row is None and clean_url:
        item_row = await db.query_first(
            "SELECT id, url, canonical_url, status, deleted_at "
            "FROM items WHERE user_id = ? AND canonical_url = ?;",
            (user_id, clean_url),
        )

    is_queue_failed = (
        row.get("state") == "skipped"
        and row.get("reason") == LinkSkipReason.QUEUE_FAILED.value
    )
    is_failed_extraction = (
        row.get("state") == "queued"
        and item_row is not None
        and item_row.get("status") == "failed"
    )

    if not is_queue_failed and not is_failed_extraction:
        return {
            "status": "not_retryable",
            "ok": False,
            "retried": False,
            "error": "Link row is not in a retryable state.",
        }

    now = _now()
    queue = getattr(env, "QUEUE", None) if env is not None else None

    # Case 1: Linked item already exists in `items`. Do NOT re-check or
    # double-count plan entitlements; re-queue or re-run extraction directly.
    if item_row is not None:
        item_id = item_row["id"]
        target_url = item_row.get("canonical_url") or item_row.get("url") or clean_url
        if item_row.get("deleted_at"):
            await restore_item(db, user_id, item_id)

        await db.execute(
            "UPDATE items SET status = 'queued', fail_reason = NULL, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?;",
            (item_id, user_id),
        )

        if queue is not None:
            try:
                await _send_extraction_batch(
                    env,
                    [{"item_id": item_id, "url": target_url, "user_id": user_id}],
                )
            except Exception:
                if is_failed_extraction:
                    await db.execute(
                        "UPDATE items SET status = 'failed', "
                        "fail_reason = 'Queue unavailable on retry' "
                        "WHERE id = ? AND user_id = ?;",
                        (item_id, user_id),
                    )
                else:
                    await db.execute(
                        "UPDATE import_items SET item_id = ?, updated_at = ? "
                        "WHERE id = ? AND import_id = ? AND user_id = ?;",
                        (item_id, now, item_row_id, import_id, user_id),
                    )
                return {
                    "status": "failed",
                    "ok": False,
                    "retried": False,
                    "item_id": item_id,
                    "live_state": "failed" if is_failed_extraction else "skipped",
                    "error": "link: could not be queued",
                }
        else:
            from keepfor.consumer.processor import extract_and_store

            try:
                await extract_and_store(
                    db, env, item_id, target_url, scope=scope, user_id=user_id
                )
            except Exception:
                pass

        if is_queue_failed:
            await db.execute_batch(
                [
                    (
                        "UPDATE import_items SET state = 'queued', reason = NULL, "
                        "item_id = ?, updated_at = ? "
                        "WHERE id = ? AND import_id = ? AND user_id = ?;",
                        (item_id, now, item_row_id, import_id, user_id),
                    ),
                    (
                        "UPDATE import_status "
                        "SET urls_queued = urls_queued + 1, "
                        "    urls_skipped = CASE WHEN urls_skipped > 0 "
                        "        THEN urls_skipped - 1 ELSE 0 END, "
                        "    updated_at = ? "
                        "WHERE id = ? AND user_id = ?;",
                        (now, import_id, user_id),
                    ),
                ]
            )
        else:
            await db.execute(
                "UPDATE import_items SET item_id = ?, updated_at = ? "
                "WHERE id = ? AND import_id = ? AND user_id = ?;",
                (item_id, now, item_row_id, import_id, user_id),
            )

        after_row = await db.query_first(
            "SELECT status FROM items WHERE id = ? AND user_id = ?;",
            (item_id, user_id),
        )
        live_state = derive_link_state(after_row["status"] if after_row else "queued")
        if live_state == "failed":
            return {
                "status": "failed",
                "ok": False,
                "retried": True,
                "item_id": item_id,
                "live_state": live_state,
                "error": "Extraction failed on retry.",
            }
        return {
            "status": "ok",
            "ok": True,
            "retried": True,
            "item_id": item_id,
            "live_state": live_state,
        }

    # Case 2: No item exists yet (`queue_failed` before item creation).
    # Perform a fresh save and check/record entitlement once.
    decision = await providers.entitlements.check(scope.tenant_id, "import", qty=1)
    if not decision.allowed:
        await db.execute_batch(
            [
                (
                    "UPDATE import_items SET reason = ?, updated_at = ? "
                    "WHERE id = ? AND import_id = ? AND user_id = ?;",
                    (
                        LinkSkipReason.PLAN_LIMIT.value,
                        now,
                        item_row_id,
                        import_id,
                        user_id,
                    ),
                ),
                (
                    "UPDATE import_status "
                    "SET plan_skipped = plan_skipped + 1, "
                    "    urls_skipped = CASE WHEN urls_skipped > 0 "
                    "        THEN urls_skipped - 1 ELSE 0 END, "
                    "    updated_at = ? "
                    "WHERE id = ? AND user_id = ?;",
                    (now, import_id, user_id),
                ),
            ]
        )
        return {
            "status": "failed",
            "ok": False,
            "retried": False,
            "item_id": None,
            "live_state": "skipped",
            "error": "Plan limit reached.",
        }

    messages: list[dict[str, Any]] = []
    try:
        saved, is_new = await save_item(
            db=db,
            env=env,
            user_id=user_id,
            url=clean_url,
            tags=["from-notes"],
            scope=scope,
            queue_messages=messages,
            check_entitlement=False,
        )
    except Exception:
        return {
            "status": "failed",
            "ok": False,
            "retried": False,
            "item_id": None,
            "live_state": "skipped",
            "error": "link: could not be queued",
        }

    item_id = saved["id"]
    if is_new:
        try:
            await providers.entitlements.record(scope.tenant_id, "import", qty=1)
        except Exception:
            pass

    if messages:
        try:
            await _send_extraction_batch(env, messages)
        except Exception:
            await db.execute(
                "UPDATE import_items SET item_id = ?, updated_at = ? "
                "WHERE id = ? AND import_id = ? AND user_id = ?;",
                (item_id, now, item_row_id, import_id, user_id),
            )
            return {
                "status": "failed",
                "ok": False,
                "retried": False,
                "item_id": item_id,
                "live_state": "skipped",
                "error": "link: could not be queued",
            }

    await db.execute_batch(
        [
            (
                "UPDATE import_items SET state = 'queued', reason = NULL, "
                "item_id = ?, updated_at = ? "
                "WHERE id = ? AND import_id = ? AND user_id = ?;",
                (item_id, now, item_row_id, import_id, user_id),
            ),
            (
                "UPDATE import_status "
                "SET urls_queued = urls_queued + 1, "
                "    urls_skipped = CASE WHEN urls_skipped > 0 "
                "        THEN urls_skipped - 1 ELSE 0 END, "
                "    updated_at = ? "
                "WHERE id = ? AND user_id = ?;",
                (now, import_id, user_id),
            ),
        ]
    )

    after_row = await db.query_first(
        "SELECT status FROM items WHERE id = ? AND user_id = ?;",
        (item_id, user_id),
    )
    live_state = derive_link_state(after_row["status"] if after_row else "queued")
    if live_state == "failed":
        return {
            "status": "failed",
            "ok": False,
            "retried": True,
            "item_id": item_id,
            "live_state": live_state,
            "error": "Extraction failed on retry.",
        }
    return {
        "status": "ok",
        "ok": True,
        "retried": True,
        "item_id": item_id,
        "live_state": live_state,
    }


async def retry_all_failed_import_items(
    db: Database,
    env: Any,
    user_id: str,
    import_id: str,
    *,
    providers: Providers | None = None,
    scope: TenantScope | None = None,
) -> dict[str, Any]:
    """Retries all retryable rows (`queue_failed` or failed extraction) for an import.

    Never reports full success (`status == 'ok'`, `ok == True`) when any row
    fails (`status == 'partial'` when some succeed and some fail; `'failed'`
    when none succeed).
    """
    detail = await get_import_detail(db, user_id, import_id)
    if detail is None:
        return {
            "status": "not_found",
            "ok": False,
            "succeeded": 0,
            "failed": 0,
            "results": [],
        }

    candidates = [
        r
        for r in (detail["queued_links"] + detail["skipped_links"])
        if r.get("can_retry")
    ]
    if not candidates:
        return {
            "status": "noop",
            "ok": False,
            "succeeded": 0,
            "failed": 0,
            "results": [],
        }

    results: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0
    for row in candidates:
        res = await retry_import_item(
            db,
            env,
            user_id,
            import_id,
            row["id"],
            providers=providers,
            scope=scope,
        )
        results.append({"row_id": row["id"], **res})
        if res.get("ok"):
            succeeded += 1
        else:
            failed += 1

    if succeeded > 0 and failed == 0:
        status = "ok"
        ok = True
    elif succeeded > 0 and failed > 0:
        status = "partial"
        ok = False
    else:
        status = "failed"
        ok = False

    return {
        "status": status,
        "ok": ok,
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }
