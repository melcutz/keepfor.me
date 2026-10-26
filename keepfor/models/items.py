# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from keepfor.models.db import Database
from keepfor.utils.logging import logger
from keepfor.utils.tagger import normalize_tag
from keepfor.utils.url import canonicalize_url, extract_url

if TYPE_CHECKING:
    from keepfor.spi import TenantScope

# D1 allows 100 bound parameters per query; the user_id takes one of them.
_ID_CHUNK = 90

# Cap on the URLs one save_urls call may carry.
MAX_SAVE_URLS = 50


# Extra per-item rows removed by the permanent delete, beyond what the item
# row's ON DELETE CASCADE already takes. Each statement gets the item id and
# the owner id. PR #32 (highlights, collections) plugs its tables in here:
# the trash and the restore never run them, so a restored item keeps them.
ITEM_EXTRA_DELETES: tuple[str, ...] = ()

# The read states an item can hold.
READ_STATES = ("unread", "read", "archived")

# Caps on the metadata an agent may set on an item.
MAX_ITEM_TITLE_CHARS = 500
MAX_ITEM_NOTES_CHARS = 10000

# The chunk index of the vector that carries an item's title and notes. It
# is negative: that vector is metadata, not a passage of the content, so
# the reader and the passage search skip it.
META_CHUNK_INDEX = -1


def meta_chunk_id(item_id: str) -> str:
    """The id of the vector that carries an item's title and notes."""
    return f"item_{item_id}_chunk_meta"


async def save_item(
    db: Database,
    env: Any,
    user_id: str,
    url: str,
    tags: list[str] | None = None,
    *,
    scope: TenantScope | None = None,
    agent_summary: str | None = None,
    why_saved: str | None = None,
    source_context: str | None = None,
    saved_by: str | None = None,
    queue_messages: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Saves a URL to the user library with canonicalization and deduplication.
    Returns (item_dict, is_new).

    v2 fields:
    - agent_summary: AI-generated summary of the content
    - why_saved: User's reason for saving
    - source_context: Context from the conversation/task that led to saving
    - saved_by: Name of the agent/client that saved the item

    queue_messages: when a list is given, the extraction message is collected
    in it instead of being sent, so a batch save can send them all at once.
    """
    if scope is None:
        from keepfor.defaults import default_scope

        scope = default_scope(db, env, user_id)

    clean_url = canonicalize_url(url)
    tags = [normalize_tag(t) for t in (tags or []) if normalize_tag(t)]

    # Check for existing item
    existing = await db.query_first(
        "SELECT id, url, canonical_url, title, status, created_at, deleted_at "
        "FROM items WHERE user_id = ? AND canonical_url = ?;",
        (user_id, clean_url),
    )

    if existing:
        item_id = existing["id"]
        if existing["deleted_at"]:
            # Saving a trashed url again brings the item back: the canonical
            # url is unique per user, so a second row is not an option.
            await restore_item(db, user_id, item_id)
            existing = {**existing, "deleted_at": None}
        # Merge new tags if provided
        if tags:
            await add_tags_to_item(db, user_id, item_id, tags)
        item_tags = await get_item_tags(db, user_id, item_id)
        res = dict(existing)
        res["tags"] = item_tags
        logger.debug(f"Item already exists: {item_id}, user: {user_id}")
        return res, False

    # Check entitlements before inserting
    from keepfor.runtime import get_providers
    from keepfor.spi import EntitlementDenied

    providers = get_providers()
    decision = await providers.entitlements.check(scope.tenant_id, "save_item", qty=1)
    if not decision.allowed:
        raise EntitlementDenied(decision)

    # Create new item
    item_id = str(uuid.uuid4())
    await db.execute(
        """
        INSERT INTO items (id, user_id, url, canonical_url, status,
            agent_summary, why_saved, source_context, saved_by)
        VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?);
        """,
        (
            item_id,
            user_id,
            url.strip(),
            clean_url,
            agent_summary,
            why_saved,
            source_context,
            saved_by,
        ),
    )

    try:
        await providers.entitlements.record(scope.tenant_id, "save_item", qty=1)
    except Exception as exc:
        logger.warning("Failed to record entitlement for save_item: %s", exc)

    try:
        await providers.events.publish(
            "item.saved",
            {
                "tenant_id": scope.tenant_id,
                "user_id": user_id,
                "item_id": item_id,
            },
        )
    except Exception as exc:
        logger.warning("Failed to publish item.saved event for %s: %s", item_id, exc)

    if tags:
        await add_tags_to_item(db, user_id, item_id, tags)

    # Enqueue extraction job; without a queue binding (local dev without
    # queue delivery, tests) extract inline so items never stick in 'queued'.
    queue = getattr(env, "QUEUE", None) if env is not None else None
    if queue is not None:
        message = {"item_id": item_id, "url": clean_url, "user_id": user_id}
        if queue_messages is None:
            await queue.send(message)
        else:
            # A batch save collects the messages and sends them as one batch.
            queue_messages.append(message)
        logger.info(
            f"Enqueued extraction: item={item_id}, url={clean_url}, user_id={user_id}"
        )
    else:
        logger.warning(f"No QUEUE binding; extracting inline: item={item_id}")
        try:
            from keepfor.consumer.processor import extract_and_store

            await extract_and_store(
                db, env, item_id, clean_url, scope=scope, user_id=user_id
            )
        except Exception as exc:
            # extract_and_store already marked the item failed in D1;
            # never fail the save itself because of extraction.
            logger.warning(f"Inline extraction failed: item={item_id}: {exc}")

    logger.info(f"New item saved: {item_id}, user: {user_id}, url: {clean_url}")
    return {
        "id": item_id,
        "url": url.strip(),
        "canonical_url": clean_url,
        "status": "queued",
        "tags": tags,
    }, True


async def save_urls(
    db: Database,
    env: Any,
    user_id: str,
    urls: list[str],
    tags: list[str] | None = None,
    *,
    scope: TenantScope | None = None,
    why_saved: str | None = None,
    saved_by: str | None = None,
) -> list[dict[str, Any]]:
    """Saves many URLs in one call and reports a status for each of them.

    Every URL goes through save_item, so the canonicalization, the dedupe
    and the save entitlement are the ones of a single save. A URL the save
    limit refuses comes back as "over_limit" instead of failing the call.

    Returns one result per input URL: {id, url, status, is_new}.
    """
    from keepfor.spi import EntitlementDenied

    if scope is None:
        from keepfor.defaults import default_scope

        scope = default_scope(db, env, user_id)

    results: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    for raw in urls:
        url = extract_url(raw)
        if not url:
            results.append(
                {"id": None, "url": raw, "status": "invalid", "is_new": False}
            )
            continue
        try:
            item, is_new = await save_item(
                db,
                env,
                user_id,
                url,
                tags,
                scope=scope,
                why_saved=why_saved,
                saved_by=saved_by,
                queue_messages=messages,
            )
        except EntitlementDenied:
            results.append(
                {"id": None, "url": url, "status": "over_limit", "is_new": False}
            )
            continue
        results.append(
            {
                "id": item["id"],
                "url": item["url"],
                "status": "queued" if is_new else "exists",
                "is_new": is_new,
            }
        )

    if messages:
        await _send_extraction_batch(env, messages)
    return results


async def _send_extraction_batch(env: Any, messages: list[dict[str, Any]]) -> None:
    """Sends the extraction messages of a batch save as one queue batch."""
    queue = getattr(env, "QUEUE", None) if env is not None else None
    if queue is None:
        return
    # The binding spells the batch call camelCase; send_batch is the shape
    # the test doubles use.
    send_batch = getattr(queue, "sendBatch", None) or getattr(queue, "send_batch", None)
    if send_batch is not None:
        await send_batch([{"body": message} for message in messages])
        return
    for message in messages:
        await queue.send(message)


async def add_tags_to_item(
    db: Database, user_id: str, item_id: str, tags: list[str]
) -> None:
    clean_tags = list(dict.fromkeys(normalize_tag(t) for t in tags if normalize_tag(t)))
    if not clean_tags:
        return

    placeholders = ",".join("?" for _ in clean_tags)
    existing_rows = await db.query_all(
        f"SELECT id, name FROM tags WHERE user_id = ? AND name IN ({placeholders});",
        (user_id, *clean_tags),
    )
    tag_map = {r["name"]: r["id"] for r in existing_rows}

    missing = [t for t in clean_tags if t not in tag_map]
    if missing:
        insert_stmts = []
        for t in missing:
            new_id = str(uuid.uuid4())
            tag_map[t] = new_id
            insert_stmts.append(
                (
                    "INSERT OR IGNORE INTO tags (id, user_id, name) VALUES (?, ?, ?);",
                    (new_id, user_id, t),
                )
            )
        await db.execute_batch(insert_stmts)

        # In case of concurrent conflict where our generated uuid was ignored,
        # re-fetch missing tags to ensure accurate IDs
        missing_ph = ",".join("?" for _ in missing)
        check_rows = await db.query_all(
            f"SELECT id, name FROM tags WHERE user_id = ? AND name IN ({missing_ph});",
            (user_id, *missing),
        )
        for r in check_rows:
            tag_map[r["name"]] = r["id"]

    link_stmts = [
        (
            "INSERT OR IGNORE INTO item_tags (item_id, tag_id) "
            "SELECT i.id, t.id FROM items i JOIN tags t ON t.user_id = i.user_id "
            "WHERE i.id = ? AND i.user_id = ? AND t.id = ?"
            " AND i.deleted_at IS NULL;",
            (item_id, user_id, tag_map[t]),
        )
        for t in clean_tags
        if t in tag_map
    ]
    if link_stmts:
        await db.execute_batch(link_stmts)


async def remove_tags_from_item(
    db: Database, user_id: str, item_id: str, tags: list[str]
) -> None:
    clean_tags = list(dict.fromkeys(normalize_tag(t) for t in tags if normalize_tag(t)))
    if not clean_tags:
        return
    placeholders = ",".join("?" for _ in clean_tags)
    rows = await db.query_all(
        f"SELECT id FROM tags WHERE user_id = ? AND name IN ({placeholders});",
        (user_id, *clean_tags),
    )
    if rows:
        tag_ids = [r["id"] for r in rows]
        id_ph = ",".join("?" for _ in tag_ids)
        await db.execute(
            "DELETE FROM item_tags WHERE item_id IN "
            "(SELECT id FROM items WHERE id = ? AND user_id = ?) "
            f"AND tag_id IN ({id_ph});",
            (item_id, user_id, *tag_ids),
        )


async def get_item_tags(db: Database, user_id: str, item_id: str) -> list[str]:
    rows = await db.query_all(
        """
        SELECT t.name
        FROM item_tags it
        JOIN items i ON i.id = it.item_id AND i.user_id = ?
        JOIN tags t ON t.id = it.tag_id AND t.user_id = i.user_id
        WHERE it.item_id = ?
        ORDER BY t.name ASC;
        """,
        (user_id, item_id),
    )
    return [r["name"] for r in rows]


async def get_item(
    db: Database,
    user_id: str,
    item_id: str,
    include_content: bool = True,
) -> dict[str, Any] | None:
    """Retrieves an item by ID.

    Args:
        include_content: If False, returns only excerpt/snippet instead of
            full content_text. The MCP `get_item` tool passes False to cap
            content for token efficiency; every other caller (UI, API) wants
            the full body, so the default is True.

    A trashed item is not found: it left the library. The Trash view reads
    the trashed rows through `list_trash`.

    The result always carries `content_length` (the length of the stored
    content) and `truncated` (True when the returned text is shorter than
    the stored content), so a caller can tell that the content was capped.
    """
    row = await db.query_first(
        "SELECT * FROM items WHERE id = ? AND user_id = ? AND deleted_at IS NULL;",
        (item_id, user_id),
    )
    if not row:
        return None
    item = dict(row)
    item["tags"] = await get_item_tags(db, user_id, item_id)
    full_length = len(item.get("content_text") or "")
    item["content_length"] = full_length
    if not include_content:
        # Cap content_text to excerpt for token efficiency. Items without an
        # excerpt (notes) keep their body: there is nothing to cap it to.
        item["content_text"] = item.get("excerpt") or item.get("content_text") or ""
    item["truncated"] = len(item.get("content_text") or "") < full_length
    return item


async def get_items_by_ids(
    db: Database, user_id: str, item_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """Fetch non-trashed items in <=50-ID batches in `item_ids` order."""
    from keepfor.search.engine import RECENT_COLUMNS_BARE

    clean_ids = list(dict.fromkeys(i for i in item_ids if i))
    if not clean_ids:
        return []
    rows: list[Any] = []
    tag_rows: list[Any] = []
    for i in range(0, len(clean_ids), 50):
        chunk = clean_ids[i : i + 50]
        placeholders = ",".join("?" for _ in chunk)
        item_sql = (
            f"SELECT {RECENT_COLUMNS_BARE} FROM items "
            f"WHERE user_id = ? AND deleted_at IS NULL AND id IN ({placeholders});"
        )
        tag_sql = (
            "SELECT it.item_id, t.name AS tag_name "
            "FROM item_tags it "
            "JOIN items i ON i.id = it.item_id AND i.user_id = ? "
            "JOIN tags t ON t.id = it.tag_id AND t.user_id = i.user_id "
            f"WHERE it.item_id IN ({placeholders}) "
            "ORDER BY t.name ASC;"
        )
        c_rows, c_tags = await asyncio.gather(
            db.query_all(item_sql, (user_id, *chunk)),
            db.query_all(tag_sql, (user_id, *chunk)),
        )
        rows.extend(c_rows)
        tag_rows.extend(c_tags)
    if not rows:
        return []
    tags_by_id: dict[str, list[str]] = {}
    for tr in tag_rows:
        tags_by_id.setdefault(tr["item_id"], []).append(tr["tag_name"])
    by_id: dict[str, dict[str, Any]] = {}
    for r in rows:
        item = dict(r)
        item["tags"] = tags_by_id.get(r["id"], [])
        by_id[r["id"]] = item
    return [by_id[iid] for iid in item_ids if iid in by_id]


async def get_item_clean_html(
    db: Database,
    env: Any,
    user_id: str,
    item_id: str,
    *,
    scope: TenantScope | None = None,
    item: dict[str, Any] | None = None,
) -> str | None:
    """Get clean reader HTML from R2, or format the stored content text."""
    if scope is None:
        from keepfor.defaults import default_scope

        scope = default_scope(db, env, user_id)

    # Verify ownership
    if item is None:
        item = await get_item(db, user_id, item_id)
    if not item:
        return None

    # Attempt fetch from blob store
    try:
        blobs = scope.blobs(env)
        obj_text = await blobs.get_text(blobs.key("items", item_id, "clean.html"))
        if obj_text is not None:
            return obj_text
    except Exception:
        pass

    # Fallback to plain text / excerpt formatted into HTML paragraphs.
    # NOTE: title is NULL (not absent) until extraction completes, so
    # .get('title', url) would render a literal "None" heading: use or.
    import html

    heading = html.escape(str(item.get("title") or item["url"]), quote=True)
    if item.get("status") == "failed":
        from keepfor.models.cleanup import BLOCKED_FETCH_COPY, classify_failure

        reason = item.get("fail_reason") or "unknown error"
        if classify_failure(reason) == "blocked" and BLOCKED_FETCH_COPY not in reason:
            reason = f"{BLOCKED_FETCH_COPY} ({reason})"
        safe_reason = html.escape(str(reason), quote=True)
        safe_url = html.escape(str(item["url"]), quote=True)
        return (
            f"<h1>{heading}</h1>"
            f"<p>Content extraction failed: {safe_reason}</p>"
            f"<p><a href='{safe_url}' target='_blank'>Visit original link</a></p>"
        )
    text = (
        item.get("content_text")
        or item.get("excerpt")
        or "Content extraction in progress..."
    )
    paragraphs = "".join(
        f"<p>{html.escape(p.strip(), quote=True)}</p>"
        for p in text.split("\n\n")
        if p.strip()
    )
    return f"<h1>{heading}</h1>{paragraphs}"


async def purge_item_children(
    db: Database, user_id: str, item_ids: Sequence[str]
) -> None:
    """Drops the highlights and the collection links of the given items.

    The item rows are left alone: only the rows that hang off them go.

    The hard delete calls this before it removes the items. The Trash
    purge ("delete forever", GitHub issue #35) MUST call it too: trashing
    an item keeps its highlights, so only the permanent purge drops them.
    Trashing and restoring must not touch this function.

    A missing table (migration 0013 not applied yet) must never block the
    delete, so a failure is logged and swallowed.
    """
    ids = [i for i in item_ids if i]
    if not ids:
        return
    try:
        for start in range(0, len(ids), _ID_CHUNK):
            chunk = ids[start : start + _ID_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            await db.execute_batch(
                [
                    (
                        "DELETE FROM highlights WHERE item_id IN"
                        f" ({placeholders}) AND user_id = ?;",
                        (*chunk, user_id),
                    ),
                    (
                        "DELETE FROM collection_items WHERE item_id IN"
                        " (SELECT id FROM items WHERE id IN"
                        f" ({placeholders}) AND user_id = ?);",
                        (*chunk, user_id),
                    ),
                ]
            )
    except Exception as exc:
        logger.warning("Failed to clear highlights for items %s: %s", ids, exc)


async def delete_item(
    db: Database,
    env: Any,
    user_id: str,
    item_id: str,
    *,
    scope: TenantScope | None = None,
) -> bool:
    """Deletes item, its D1 records, R2 snapshots, and Vectorize chunks.

    This is the only permanent delete path: the Trash view ("Delete forever")
    and the 30-day purge both come through here. Trashing and restoring
    never touch the extra per-item tables (ITEM_EXTRA_DELETES), so a
    restored item keeps them.
    """
    if scope is None:
        from keepfor.defaults import default_scope

        scope = default_scope(db, env, user_id)

    item = await db.query_first(
        "SELECT id FROM items WHERE id = ? AND user_id = ?;", (item_id, user_id)
    )
    if not item:
        return False

    # 1. Fetch chunk IDs to delete from Vectorize
    chunk_rows = await db.query_all(
        "SELECT id FROM chunks WHERE item_id = ? AND user_id = ?;", (item_id, user_id)
    )
    # The metadata vector (the title and the notes) has no chunks row: it is
    # deleted by its stable id.
    chunk_ids = [r["id"] for r in chunk_rows] + [meta_chunk_id(item_id)]

    try:
        vectors = scope.vectors(env)
        await vectors.delete(chunk_ids)
    except Exception as exc:
        logger.warning(
            "Failed to delete vector embeddings for item %s: %s", item_id, exc
        )

    # 2. Delete R2 snapshots
    try:
        blobs = scope.blobs(env)
        await blobs.delete_many(
            [
                blobs.key("items", item_id, "raw.html"),
                blobs.key("items", item_id, "clean.html"),
            ]
        )
    except Exception as exc:
        logger.warning("Failed to delete R2 snapshots for item %s: %s", item_id, exc)

    # 3. Delete from D1 (triggers cascade deletions on chunks, item_tags)
    # Highlights and collection links point at the item: drop them before
    # the item row goes, the foreign key cascade is not guaranteed to be on.
    await purge_item_children(db, user_id, [item_id])

    # The extra per-item tables go first: their rows may be selected through
    # the item row.
    statements = [(sql, (item_id, user_id)) for sql in ITEM_EXTRA_DELETES]
    statements += [
        (
            "DELETE FROM items_fts WHERE item_id = ? AND user_id = ?;",
            (item_id, user_id),
        ),
        ("DELETE FROM items WHERE id = ? AND user_id = ?;", (item_id, user_id)),
    ]
    await db.execute_batch(statements)
    from keepfor.runtime import get_providers

    providers = get_providers()
    try:
        await providers.events.publish(
            "item.deleted",
            {
                "tenant_id": scope.tenant_id,
                "user_id": user_id,
                "item_id": item_id,
            },
        )
    except Exception as exc:
        logger.warning("Failed to publish item.deleted event for %s: %s", item_id, exc)

    return True


# ==========================================
# Trash (soft delete)
# ==========================================

# How long a trashed item stays restorable before the purge takes it.
TRASH_RETENTION_DAYS = 30

# Cap on the items one restore call may touch.
RESTORE_LIMIT = 50


async def trash_item(db: Database, user_id: str, item_id: str) -> bool:
    """Moves an item to the Trash: it leaves the library but stays restorable.

    Nothing is destroyed. The row, the R2 snapshots and the vector chunks
    stay, so a restore is instant. Only the Trash view ("Delete forever")
    or `purge_trash` removes the data for good.
    """
    row = await db.query_first(
        "SELECT id, deleted_at FROM items WHERE id = ? AND user_id = ?;",
        (item_id, user_id),
    )
    if not row:
        return False
    if not row["deleted_at"]:
        await db.execute(
            "UPDATE items SET deleted_at = CURRENT_TIMESTAMP,"
            " updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?;",
            (item_id, user_id),
        )
    return True


async def trash_items(db: Database, user_id: str, item_ids: Sequence[str]) -> list[str]:
    """Moves items to the Trash and returns the ids that were trashed.

    Nothing is destroyed: the rows, the snapshots and the vectors stay, so a
    restore is instant. Only the items that are still live are trashed.
    """
    ids = list(dict.fromkeys(i for i in (item_ids or []) if i))
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = await db.query_all(
        "SELECT id FROM items"
        f" WHERE user_id = ? AND deleted_at IS NULL AND id IN ({placeholders});",  # nosec B608 # placeholders are generated "?" marks, values are bound parameters
        (user_id, *ids),
    )
    live = [r["id"] for r in rows]
    if live:
        live_placeholders = ",".join("?" for _ in live)
        await db.execute(
            "UPDATE items SET deleted_at = CURRENT_TIMESTAMP,"
            " updated_at = CURRENT_TIMESTAMP"
            f" WHERE user_id = ? AND id IN ({live_placeholders});",  # nosec B608 # placeholders are generated "?" marks, values are bound parameters
            (user_id, *live),
        )
    return live


async def restore_item(db: Database, user_id: str, item_id: str) -> bool:
    """Brings a trashed item back into the library."""
    result = await restore_items(db, user_id, [item_id])
    return bool(result["restored"])


async def restore_items(
    db: Database, user_id: str, item_ids: Sequence[str]
) -> dict[str, list[str]]:
    """Restores trashed items and reports what happened to every id.

    The three buckets: "restored" (the item was in the Trash and is back in
    the library), "already_active" (the item was never trashed: there was
    nothing to restore) and "not_found" (the id is unknown to this user: it
    never existed, it belongs to somebody else, or it was purged for good).

    At most RESTORE_LIMIT ids are processed per call.
    """
    ids = list(dict.fromkeys(i for i in (item_ids or []) if i))[:RESTORE_LIMIT]
    if not ids:
        return {"restored": [], "already_active": [], "not_found": []}
    placeholders = ",".join("?" for _ in ids)
    rows = await db.query_all(
        "SELECT id, deleted_at FROM items"
        f" WHERE user_id = ? AND id IN ({placeholders});",  # nosec B608 # placeholders are generated "?" marks, values are bound parameters
        (user_id, *ids),
    )
    trashed = {r["id"] for r in rows if r["deleted_at"]}
    live = {r["id"] for r in rows if not r["deleted_at"]}
    restored = [i for i in ids if i in trashed]
    if restored:
        restore_placeholders = ",".join("?" for _ in restored)
        await db.execute(
            "UPDATE items SET deleted_at = NULL, updated_at = CURRENT_TIMESTAMP"
            f" WHERE user_id = ? AND id IN ({restore_placeholders});",  # nosec B608 # placeholders are generated "?" marks, values are bound parameters
            (user_id, *restored),
        )
    return {
        "restored": restored,
        "already_active": [i for i in ids if i in live],
        "not_found": [i for i in ids if i not in trashed and i not in live],
    }


async def list_trash(
    db: Database,
    user_id: str,
    limit: int = 50,
    offset: int = 0,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """The user's trashed items, most recently trashed first.

    `query` keeps only the items whose title or url holds it. Every row
    carries the `purge_at` date: the day the 30-day purge takes it.
    """
    sql = (
        "SELECT id, url, canonical_url, title, item_type, created_at, deleted_at,"
        " datetime(deleted_at, ?) AS purge_at"
        " FROM items WHERE user_id = ? AND deleted_at IS NOT NULL"
    )
    params: list[Any] = [f"+{TRASH_RETENTION_DAYS} days", user_id]
    clean_query = (query or "").strip()
    if clean_query:
        sql += " AND (title LIKE ? OR url LIKE ?)"
        params += [f"%{clean_query}%", f"%{clean_query}%"]
    sql += " ORDER BY deleted_at DESC LIMIT ? OFFSET ?;"
    params += [limit, offset]
    rows = await db.query_all(sql, params)
    return [dict(r) for r in rows]


# Cap on the items a single purge call may destroy. A host cron that has
# more trash runs the purge again.
PURGE_LIMIT = 1000


async def list_purgeable_items(
    db: Database,
    user_id: str,
    older_than_days: int = TRASH_RETENTION_DAYS,
    limit: int = PURGE_LIMIT,
) -> list[str]:
    """The ids of the trashed items older than the window, oldest first.

    The selection lives here so a host never copies the SQL: it can ask
    what the purge would take before it takes it.
    """
    rows = await db.query_all(
        "SELECT id FROM items WHERE user_id = ? AND deleted_at IS NOT NULL"
        " AND datetime(deleted_at) < datetime('now', ?)"
        " ORDER BY deleted_at ASC LIMIT ?;",
        (user_id, f"-{int(older_than_days)} days", int(limit)),
    )
    return [r["id"] for r in rows]


async def purge_trash(
    db: Database,
    env: Any,
    user_id: str | None = None,
    *,
    older_than_days: int = TRASH_RETENTION_DAYS,
    scope: TenantScope | None = None,
    limit: int = PURGE_LIMIT,
) -> int:
    """Hard-deletes trashed items older than `older_than_days`.

    `user_id` limits the purge to one user; None purges every user in the
    database, which is the shape a single-tenant host cron wants. It must
    name a user: an empty string is refused. The data goes through the
    same hard delete as before: row, R2 snapshots, vectors, highlights
    and collection links.

    `scope` is the tenant scope: it carries the R2 prefix and the
    Vectorize namespace of the tenant whose data is destroyed. A
    multi-tenant host MUST pass it: without it the snapshots and the
    vectors of the tenant are orphaned (or the wrong prefix is hit), so
    such a call is refused. The purge is then limited to the scope's own
    user, because a shared database holds the rows of every tenant: a
    scope that names no user is refused too. A single-tenant self-host
    may omit it: the default scope is used.

    A bad argument raises ValueError before anything is destroyed.

    `limit` caps how many items one call destroys.
    """
    from keepfor.runtime import is_multi_tenant

    if user_id is not None and not user_id:
        raise ValueError("purge_trash(): the user_id must not be empty.")

    if scope is None:
        if is_multi_tenant():
            raise ValueError(
                "purge_trash() needs a tenant scope in multi-tenant mode: "
                "the R2 prefix and the Vectorize namespace are per tenant."
            )
    else:
        scope_user = scope.user_id
        if not scope_user:
            raise ValueError(
                "purge_trash(): the tenant scope must name a user: "
                "the purge would take the rows of every user."
            )
        if user_id is None:
            user_id = scope_user
        elif user_id != scope_user:
            raise ValueError(
                "purge_trash(): the user_id does not match the tenant scope."
            )

    if user_id is None:
        users = await db.query_all("SELECT id FROM users;")
        targets = [r["id"] for r in users]
    else:
        targets = [user_id]

    purged = 0
    for uid in targets:
        if purged >= limit:
            break
        item_ids = await list_purgeable_items(db, uid, older_than_days, limit - purged)
        for item_id in item_ids:
            if await delete_item(db, env, uid, item_id, scope=scope):
                purged += 1
                if purged >= limit:
                    break
    return purged


async def create_tag(
    db: Database,
    user_id: str,
    raw_name: str,
    *,
    description: str | None = None,
    color: str | None = None,
    parent: str | None = None,
) -> str | None:
    """Creates an empty tag (zero items allowed); idempotent. Returns name or None.

    Optional metadata (description, color) and a parent tag name for hierarchy
    ("research" under "work"). A missing parent is created on the fly. An
    existing tag is left untouched: use update_tag to edit its metadata.
    """
    from keepfor.utils.tagger import validate_tag_name

    clean = validate_tag_name(raw_name or "")
    if not clean:
        return None
    existing = await db.query_first(
        "SELECT id FROM tags WHERE user_id = ? AND name = ?;", (user_id, clean)
    )
    if existing:
        return clean
    parent_id = None
    if parent:
        parent_clean = validate_tag_name(parent)
        if parent_clean and parent_clean != clean:
            parent_id = await _ensure_tag_id(db, user_id, parent_clean)
    await db.execute(
        "INSERT OR IGNORE INTO tags (id, user_id, name, description, color, parent_id)"
        " VALUES (?, ?, ?, ?, ?, ?);",
        (str(uuid.uuid4()), user_id, clean, description, color, parent_id),
    )
    return clean


async def _ensure_tag_id(db: Database, user_id: str, name: str) -> str | None:
    """Returns the id of a tag, creating it when missing."""
    row = await db.query_first(
        "SELECT id FROM tags WHERE user_id = ? AND name = ?;", (user_id, name)
    )
    if row:
        return row["id"]
    new_id = str(uuid.uuid4())
    await db.execute(
        "INSERT OR IGNORE INTO tags (id, user_id, name) VALUES (?, ?, ?);",
        (new_id, user_id, name),
    )
    row = await db.query_first(
        "SELECT id FROM tags WHERE user_id = ? AND name = ?;", (user_id, name)
    )
    return row["id"] if row else new_id


async def rename_tag(db: Database, user_id: str, old_name: str, new_name: str) -> str:
    """Renames a tag; renaming onto an existing name merges the two.

    Returns 'merged', 'unchanged', 'not_found' or 'invalid'.
    """
    from keepfor.utils.tagger import normalize_tag, validate_tag_name

    old_clean = normalize_tag(old_name or "")
    new_clean = validate_tag_name(new_name or "")
    if not new_clean:
        return "invalid"
    if old_clean == new_clean:
        return "unchanged"
    old_row = await db.query_first(
        "SELECT id FROM tags WHERE user_id = ? AND name = ?;", (user_id, old_clean)
    )
    if not old_row:
        return "not_found"
    new_row = await db.query_first(
        "SELECT id FROM tags WHERE user_id = ? AND name = ?;", (user_id, new_clean)
    )
    if new_row and new_row["id"] != old_row["id"]:
        # Merge: move item links onto the surviving tag, drop the source.
        linked = await db.query_all(
            "SELECT it.item_id FROM item_tags it "
            "JOIN tags t ON t.id = it.tag_id AND t.user_id = ? "
            "WHERE it.tag_id = ?;",
            (user_id, old_row["id"]),
        )
        for row in linked:
            await db.execute(
                "INSERT OR IGNORE INTO item_tags (item_id, tag_id) "
                "SELECT i.id, t.id FROM items i JOIN tags t ON t.user_id = i.user_id "
                "WHERE i.id = ? AND i.user_id = ? AND t.id = ?;",
                (row["item_id"], user_id, new_row["id"]),
            )
        await db.execute(
            "DELETE FROM item_tags WHERE tag_id IN "
            "(SELECT id FROM tags WHERE id = ? AND user_id = ?);",
            (old_row["id"], user_id),
        )
        await db.execute(
            "DELETE FROM tags WHERE id = ? AND user_id = ?;",
            (old_row["id"], user_id),
        )
        return "merged"
    await db.execute(
        "UPDATE tags SET name = ? WHERE id = ? AND user_id = ?;",
        (new_clean, old_row["id"], user_id),
    )
    return "merged"


async def delete_tag(db: Database, user_id: str, raw_name: str) -> bool:
    """Deletes a tag; items survive untagged. Also dismisses pending suggestions."""
    from keepfor.utils.tagger import normalize_tag

    clean = normalize_tag(raw_name or "")
    if not clean:
        return False
    row = await db.query_first(
        "SELECT id FROM tags WHERE user_id = ? AND name = ?;", (user_id, clean)
    )
    if not row:
        return False
    # Explicit deletes (no reliance on ON DELETE CASCADE pragma state).
    await db.execute(
        "DELETE FROM item_tags WHERE tag_id IN "
        "(SELECT id FROM tags WHERE id = ? AND user_id = ?);",
        (row["id"], user_id),
    )
    await db.execute(
        "DELETE FROM tags WHERE id = ? AND user_id = ?;",
        (row["id"], user_id),
    )
    await db.execute(
        "UPDATE suggested_tags SET status = 'dismissed'"
        " WHERE user_id = ? AND phrase = ? AND status = 'pending';",
        (user_id, clean),
    )
    return True


async def merge_tags(
    db: Database, user_id: str, old_names: list[str], new_name: str
) -> str:
    """Merges several tags into one via rename-onto-existing.

    Returns 'merged', 'unchanged', 'not_found' or 'invalid'.
    """
    from keepfor.utils.tagger import validate_tag_name

    if not validate_tag_name(new_name or ""):
        return "invalid"
    seen = "not_found"
    for raw in old_names or []:
        res = await rename_tag(db, user_id, raw, new_name)
        if res in ("merged", "unchanged"):
            seen = "merged"
    return seen


async def prune_unused_tags(db: Database, user_id: str) -> int:
    """Deletes zero-item tags for the user; returns the deleted count."""
    rows = await db.query_all(
        """
        SELECT t.id
        FROM tags t
        LEFT JOIN item_tags it ON t.id = it.tag_id
        WHERE t.user_id = ? AND it.item_id IS NULL;
        """,
        (user_id,),
    )
    for row in rows:
        await db.execute(
            "DELETE FROM tags WHERE id = ? AND user_id = ?;",
            (row["id"], user_id),
        )
    return len(rows)


async def suggest_tags(
    db: Database,
    user_id: str,
    q: str = "",
    exclude: list[str] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Tags ranked most-used then most-recently-used, prefix-filtered.

    Empty `q` returns the recents list. `exclude` skips attached tags.
    """
    prefix = (
        normalize_tag(q or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    excluded = {normalize_tag(e) for e in (exclude or []) if normalize_tag(e)}
    fetch_limit = limit + min(len(excluded), limit)
    rows = await db.query_all(
        """
        SELECT t.name, COUNT(i.id) AS count, MAX(i.created_at) AS recent
        FROM tags t
        LEFT JOIN item_tags it ON t.id = it.tag_id
        LEFT JOIN items i ON i.id = it.item_id AND i.deleted_at IS NULL
        WHERE t.user_id = ? AND LOWER(t.name) LIKE ? ESCAPE '\\'
        GROUP BY t.id, t.name
        ORDER BY count DESC, recent DESC
        LIMIT ?;
        """,
        (user_id, prefix + "%", fetch_limit),
    )
    out = [
        {"name": r["name"], "count": r["count"]}
        for r in rows
        if normalize_tag(r["name"]) not in excluded
    ]
    return out[:limit]


BULK_TAG_LIMIT = 100


async def bulk_update_tags(
    db: Database,
    user_id: str,
    item_ids: list[str],
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> int:
    """Adds/removes tags across owned items; skips foreign and missing ids."""
    add = [t for t in (add or []) if t.strip()]
    remove = [t for t in (remove or []) if t.strip()]
    done = 0
    for item_id in (item_ids or [])[:BULK_TAG_LIMIT]:
        row = await db.query_first(
            "SELECT id FROM items WHERE id = ? AND user_id = ? AND deleted_at IS NULL;",
            (item_id, user_id),
        )
        if not row:
            continue
        if add:
            await add_tags_to_item(db, user_id, item_id, add)
        if remove:
            await remove_tags_from_item(db, user_id, item_id, remove)
        done += 1
    return done


# Cap on the items a single bulk update may touch.
BULK_UPDATE_LIMIT = 200
# D1 allows 100 bound parameters per query; user_id takes one of them.
_UPDATE_CHUNK = 99


def _clean_tag_names(names: list[str] | None) -> list[str]:
    """Normalizes and validates tag names, dropping the invalid ones.

    The same rules as create_tag: 2-64 characters of [a-z0-9 _-] once
    lowercased. A name the tag API would reject never reaches the tags
    table.
    """
    from keepfor.utils.tagger import validate_tag_name

    clean: list[str] = []
    for raw in names or []:
        name = validate_tag_name(raw or "")
        if name and name not in clean:
            clean.append(name)
    return clean


async def bulk_update_items(
    db: Database,
    user_id: str,
    item_ids: list[str],
    *,
    set_status: str | None = None,
    pin: bool | None = None,
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
    read_state: str | None = None,
) -> int:
    """Applies status, pin, read state and tag changes to owned items.

    Every statement is scoped by user_id, so a foreign id is never
    touched. The whole update runs as one batch (one transaction): either
    every change lands or none does. Returns the number of items the
    update was applied to.
    """
    ids = [i for i in (item_ids or []) if i][:BULK_UPDATE_LIMIT]
    if not ids:
        return 0
    add = _clean_tag_names(add_tags)
    remove = _clean_tag_names(remove_tags)

    statements: list[tuple[str, tuple[Any, ...]]] = []
    if set_status is not None or pin is not None:
        pin_value = None if pin is None else (1 if pin else 0)
        # status, pin and user_id take three of the hundred parameters.
        chunk_size = _UPDATE_CHUNK - 2
        for start in range(0, len(ids), chunk_size):
            chunk = ids[start : start + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            statements.append(
                (
                    "UPDATE items SET status = COALESCE(?, status),"
                    " is_pinned = COALESCE(?, is_pinned),"
                    " updated_at = CURRENT_TIMESTAMP"
                    f" WHERE user_id = ? AND id IN ({placeholders});",
                    (set_status, pin_value, user_id, *chunk),
                )
            )

    if read_state is not None:
        # The archive is the stronger state: marking an archived item read
        # or unread leaves it archived.
        chunk_size = _UPDATE_CHUNK - 1
        for start in range(0, len(ids), chunk_size):
            chunk = ids[start : start + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            statements.append(
                (
                    "UPDATE items SET read_state = ?,"
                    " updated_at = CURRENT_TIMESTAMP"
                    f" WHERE user_id = ? AND id IN ({placeholders})"
                    " AND read_state != 'archived';",
                    (read_state, user_id, *chunk),
                )
            )

    if add:
        # Create the tags first; the link below joins on the name, so a
        # concurrent insert that wins the race still links the right tag.
        for name in add:
            statements.append(
                (
                    "INSERT OR IGNORE INTO tags (id, user_id, name) VALUES (?, ?, ?);",
                    (str(uuid.uuid4()), user_id, name),
                )
            )
        # user_id and the tag names take the rest of the parameters.
        chunk_size = max(1, _UPDATE_CHUNK - len(add))
        for start in range(0, len(ids), chunk_size):
            chunk = ids[start : start + chunk_size]
            id_placeholders = ",".join("?" for _ in chunk)
            name_placeholders = ",".join("?" for _ in add)
            statements.append(
                (
                    "INSERT OR IGNORE INTO item_tags (item_id, tag_id)"
                    " SELECT i.id, t.id FROM items i"
                    " JOIN tags t ON t.user_id = i.user_id"
                    f" WHERE i.user_id = ? AND i.id IN ({id_placeholders})"
                    f" AND t.name IN ({name_placeholders});",
                    (user_id, *chunk, *add),
                )
            )

    if remove:
        # user_id twice plus the tag names.
        chunk_size = max(1, _UPDATE_CHUNK - 1 - len(remove))
        for start in range(0, len(ids), chunk_size):
            chunk = ids[start : start + chunk_size]
            id_placeholders = ",".join("?" for _ in chunk)
            name_placeholders = ",".join("?" for _ in remove)
            statements.append(
                (
                    "DELETE FROM item_tags WHERE item_id IN"
                    " (SELECT id FROM items WHERE user_id = ?"
                    f" AND id IN ({id_placeholders}))"
                    " AND tag_id IN (SELECT id FROM tags WHERE user_id = ?"
                    f" AND name IN ({name_placeholders}));",
                    (user_id, *chunk, user_id, *remove),
                )
            )

    if statements:
        await db.execute_batch(statements)
    return len(ids)


RULE_FIELDS = ("domain", "title", "url")
RULES_PER_USER_MAX = 50


async def create_rule(
    db: Database, user_id: str, field: str, substr: str, tag: str
) -> str | None:
    """Creates a tagging rule; returns id or None when invalid/capped."""
    from keepfor.utils.tagger import validate_tag_name

    field = (field or "").strip().lower()
    clean_sub = (substr or "").strip().lower()
    clean_tag = validate_tag_name(tag or "")
    if field not in RULE_FIELDS or not (2 <= len(clean_sub) <= 64):
        return None
    if not clean_tag or clean_tag == "__untagged__":
        return None
    existing = await db.query_all(
        "SELECT id FROM tag_rules WHERE user_id = ?;", (user_id,)
    )
    if len(existing) >= RULES_PER_USER_MAX:
        return None
    dup = await db.query_first(
        "SELECT id FROM tag_rules"
        " WHERE user_id = ? AND field = ? AND substr = ? AND tag = ?;",
        (user_id, field, clean_sub, clean_tag),
    )
    if dup:
        return dup["id"]
    rule_id = str(uuid.uuid4())
    await db.execute(
        "INSERT OR IGNORE INTO tag_rules (id, user_id, field, substr, tag)"
        " VALUES (?, ?, ?, ?, ?);",
        (rule_id, user_id, field, clean_sub, clean_tag),
    )
    return rule_id


async def list_rules(db: Database, user_id: str) -> list[dict[str, Any]]:
    return await db.query_all(
        "SELECT id, field, substr, tag FROM tag_rules"
        " WHERE user_id = ? ORDER BY created_at ASC;",
        (user_id,),
    )


async def delete_rule(db: Database, user_id: str, rule_id: str) -> bool:
    row = await db.query_first(
        "SELECT id FROM tag_rules WHERE id = ? AND user_id = ?;",
        (rule_id, user_id),
    )
    if not row:
        return False
    await db.execute(
        "DELETE FROM tag_rules WHERE id = ? AND user_id = ?;", (rule_id, user_id)
    )
    return True


def match_rules(rules: list[dict], url: str, title: str) -> list[str]:
    """Pure matcher: domain/title/url substring rules. No I/O, no raises."""
    from urllib.parse import urlparse

    try:
        host = urlparse(url or "").netloc.lower()
    except Exception:
        host = ""
    lowered_title = (title or "").lower()
    lowered_url = (url or "").lower()
    out: list[str] = []
    for rule in rules:
        field, sub = rule.get("field"), rule.get("substr") or ""
        hit = (
            (field == "domain" and sub in host)
            or (field == "title" and sub in lowered_title)
            or (field == "url" and sub in lowered_url)
        )
        if hit and rule.get("tag") not in out:
            out.append(rule["tag"])
    return out


async def suggest_rules(
    db: Database,
    user_id: str,
    min_precision: float = 0.8,
    min_support: int = 5,
) -> list[dict[str, Any]]:
    """Mine domain->tag pairs worth turning into rules.

    Precision denominator counts ALL of the user's items per host (tagged
    and untagged); support counts only tagged items. No input LIMIT is
    applied so support counts stay exact (correctness over scan cost at
    current scale). Output is bounded to the top 20, ordered by (host, tag).

    Skips existing rules and dismissed keys (`domain:<d>:<t>`).
    """
    rows = await db.query_all(
        "SELECT i.canonical_url, t.name FROM item_tags it"
        " JOIN items i ON i.id = it.item_id AND i.deleted_at IS NULL"
        " JOIN tags t ON t.id = it.tag_id"
        " WHERE i.user_id = ?;",
        (user_id,),
    )
    url_rows = await db.query_all(
        "SELECT canonical_url FROM items WHERE user_id = ? AND deleted_at IS NULL;",
        (user_id,),
    )
    from collections import Counter
    from urllib.parse import urlparse

    pair: Counter[tuple[str, str]] = Counter()
    pair_urls: dict[tuple[str, str], set[str]] = {}
    per_host_urls: dict[str, set[str]] = {}
    for url_row in url_rows:
        try:
            host = urlparse(url_row["canonical_url"] or "").netloc.lower()
        except Exception:
            continue
        if not host or not url_row["canonical_url"]:
            continue
        per_host_urls.setdefault(host, set()).add(url_row["canonical_url"])
    for row in rows:
        try:
            host = urlparse(row["canonical_url"] or "").netloc.lower()
        except Exception:
            continue
        if not host or not row["canonical_url"]:
            continue
        pair_key = (host, row["name"])
        if row["canonical_url"] not in pair_urls.setdefault(pair_key, set()):
            pair_urls[pair_key].add(row["canonical_url"])
            pair[pair_key] += 1
    existing = {
        (r["field"], r["substr"], r["tag"]) for r in await list_rules(db, user_id)
    }
    dismissed = {
        r["key"]
        for r in await db.query_all(
            "SELECT key FROM rule_suggestion_dismissals WHERE user_id = ?;",
            (user_id,),
        )
    }
    out = []
    for (host, tag), support in sorted(pair.items()):
        if not (2 <= len(host) <= 64):
            continue
        total = len(per_host_urls.get(host, set()))
        precision = (support / total) if total else 0.0
        rule_key = f"domain:{host}:{tag}"
        if (
            support >= min_support
            and precision >= min_precision
            and ("domain", host, tag) not in existing
            and rule_key not in dismissed
        ):
            out.append(
                {
                    "domain": host,
                    "tag": tag,
                    "precision": round(precision, 3),
                    "support": support,
                    "key": rule_key,
                }
            )
    return out[:20]


async def add_suggestions(
    db: Database,
    user_id: str,
    item_id: str,
    phrases: list[tuple[str, float]],
    limit: int = 8,
) -> int:
    """Records pending tag suggestions; idempotent per (item, phrase)."""
    from keepfor.utils.tagger import normalize_tag

    inserted = 0
    for raw_phrase, score in phrases[:limit]:
        phrase = normalize_tag(raw_phrase or "")
        if len(phrase) < 3:
            continue
        try:
            await db.execute(
                "INSERT OR IGNORE INTO suggested_tags"
                " (id, item_id, user_id, phrase, score) VALUES (?, ?, ?, ?, ?);",
                (str(uuid.uuid4()), item_id, user_id, phrase, float(score)),
            )
            inserted += 1
        except Exception:
            continue
    return inserted


async def accept_suggestion(db: Database, user_id: str, sugg_id: str) -> bool:
    """Applies a pending suggestion as a real tag; marks it accepted."""
    row = await db.query_first(
        "SELECT item_id, phrase FROM suggested_tags"
        " WHERE id = ? AND user_id = ? AND status = 'pending';",
        (sugg_id, user_id),
    )
    if not row:
        return False
    await add_tags_to_item(db, user_id, row["item_id"], [row["phrase"]])
    await db.execute(
        "UPDATE suggested_tags SET status = 'accepted' WHERE id = ? AND user_id = ?;",
        (sugg_id, user_id),
    )
    return True


async def dismiss_suggestion(db: Database, user_id: str, sugg_id: str) -> bool:
    """Marks a pending suggestion dismissed (never suggested again)."""
    row = await db.query_first(
        "SELECT id FROM suggested_tags"
        " WHERE id = ? AND user_id = ? AND status = 'pending';",
        (sugg_id, user_id),
    )
    if not row:
        return False
    await db.execute(
        "UPDATE suggested_tags SET status = 'dismissed' WHERE id = ? AND user_id = ?;",
        (sugg_id, user_id),
    )
    return True


async def list_pending_suggestions(
    db: Database, user_id: str, item_id: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Pending tag suggestions, optionally scoped to one item."""
    if item_id:
        return await db.query_all(
            "SELECT s.id, s.item_id, s.phrase, s.score, i.title"
            " FROM suggested_tags s LEFT JOIN items i ON i.id = s.item_id"
            " WHERE s.user_id = ? AND s.item_id = ? AND s.status = 'pending'"
            " AND i.deleted_at IS NULL"
            " ORDER BY s.score DESC LIMIT ?;",
            (user_id, item_id, limit),
        )
    return await db.query_all(
        "SELECT s.id, s.item_id, s.phrase, s.score, i.title"
        " FROM suggested_tags s LEFT JOIN items i ON i.id = s.item_id"
        " WHERE s.user_id = ? AND s.status = 'pending'"
        " AND i.deleted_at IS NULL"
        " ORDER BY s.score DESC LIMIT ?;",
        (user_id, limit),
    )


async def list_user_tags(db: Database, user_id: str) -> list[dict[str, Any]]:
    return await db.query_all(
        """
        SELECT t.name, COUNT(i.id) as count
        FROM tags t
        LEFT JOIN item_tags it ON t.id = it.tag_id
        LEFT JOIN items i ON i.id = it.item_id AND i.user_id = t.user_id
            AND i.deleted_at IS NULL
        WHERE t.user_id = ?
        GROUP BY t.id, t.name
        ORDER BY count DESC, t.name ASC;
        """,
        (user_id,),
    )


async def _refresh_fts_for_item(db: Database, user_id: str, item_id: str) -> None:
    """Rebuild the FTS5 row for an item, folding user_notes, v2 fields,
    the summary and the page description into the index."""
    row = await db.query_first(
        "SELECT title, content_text, user_notes, agent_summary, "
        "why_saved, source_context, saved_by, excerpt, summary FROM items"
        " WHERE id = ? AND user_id = ?;",
        (item_id, user_id),
    )
    if not row:
        return
    v2_fields = " ".join(
        p
        for p in [
            row.get("agent_summary"),
            row.get("why_saved"),
            row.get("source_context"),
            row.get("saved_by"),
        ]
        if p
    )
    combined = " ".join(
        p
        for p in [
            row["content_text"] or "",
            row["user_notes"] or "",
            v2_fields,
            # The summary and the page description are keyword sources
            # too: an item that names the concept only there had no
            # keyword hit and was dropped (#55).
            row["summary"] or "",
            row["excerpt"] or "",
        ]
        if p
    )
    await db.execute(
        "DELETE FROM items_fts WHERE item_id = ? AND user_id = ?;",
        (item_id, user_id),
    )
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?, ?, ?, ?);",
        (item_id, user_id, row["title"] or "", combined),
    )


async def toggle_pin_item(db: Database, user_id: str, item_id: str) -> bool | None:
    """Toggle is_pinned; returns new pinned state, or None if not owned."""
    row = await db.query_first(
        "SELECT is_pinned FROM items WHERE id = ? AND user_id = ?"
        " AND deleted_at IS NULL;",
        (item_id, user_id),
    )
    if not row:
        return None
    new_val = 0 if (row["is_pinned"] or 0) else 1
    await db.execute(
        "UPDATE items SET is_pinned = ?, updated_at = CURRENT_TIMESTAMP"
        " WHERE id = ? AND user_id = ?;",
        (new_val, item_id, user_id),
    )
    return bool(new_val)


async def update_user_notes(
    db: Database, user_id: str, item_id: str, user_notes: str | None
) -> bool:
    """Persist 'why I kept this' notes; re-indexes FTS. Returns owned."""
    row = await db.query_first(
        "SELECT id FROM items WHERE id = ? AND user_id = ? AND deleted_at IS NULL;",
        (item_id, user_id),
    )
    if not row:
        return False
    clean = (user_notes or "").strip() or None
    await db.execute(
        "UPDATE items SET user_notes = ?, updated_at = CURRENT_TIMESTAMP"
        " WHERE id = ? AND user_id = ?;",
        (clean, item_id, user_id),
    )
    await _refresh_fts_for_item(db, user_id, item_id)
    return True


async def _reindex_item_vectors(
    db: Database, env: Any, user_id: str, item_id: str, *, scope: TenantScope
) -> None:
    """Re-embeds the title and the notes of an item into the vector index.

    The content chunks stay as they are: only the metadata changed. The
    vector id is stable, so an edit replaces the vector instead of piling
    one up per edit.

    Best-effort: a missing AI binding or a failing Vectorize must never
    fail the update (the FTS index already holds the new text).
    """
    vectors = scope.vectors(env)
    if vectors.index is None:
        return
    row = await db.query_first(
        "SELECT title, user_notes FROM items WHERE id = ? AND user_id = ?;",
        (item_id, user_id),
    )
    if not row:
        return
    text = "\n\n".join(p for p in (row["title"], row["user_notes"]) if p)
    chunk_id = meta_chunk_id(item_id)
    try:
        if not text:
            await vectors.delete([chunk_id])
            return
        from keepfor.runtime import get_providers

        providers = get_providers()
        embeddings = await providers.ai.embed(env, [text])
        if not embeddings:
            return
        await vectors.upsert(
            [
                {
                    "id": chunk_id,
                    "values": embeddings[0],
                    "metadata": {
                        "item_id": item_id,
                        "user_id": user_id,
                        "chunk_index": META_CHUNK_INDEX,
                    },
                }
            ]
        )
    except Exception as exc:
        logger.warning("Failed to re-index the vectors of item %s: %s", item_id, exc)


async def update_item_fields(
    db: Database,
    env: Any,
    user_id: str,
    item_id: str,
    *,
    title: str | None = None,
    user_notes: str | None = None,
    notes_mode: str = "replace",
    read_state: str | None = None,
    is_pinned: bool | None = None,
    scope: TenantScope | None = None,
) -> dict[str, Any] | None:
    """Updates the metadata of one owned item (GitHub issue #91).

    Only the fields that are passed are changed. A title or a notes change
    re-indexes the item (FTS, and the vectors when a vector index is
    configured), so the new text shows up in the search.

    `notes_mode` is "replace" (the default) or "append". An invalid
    argument raises ValueError; the caller turns it into its own error.

    Returns the new values, or None when the item does not belong to the
    user or is in the Trash: every statement is scoped by user_id, so a
    foreign id is never touched.
    """
    if scope is None:
        from keepfor.defaults import default_scope

        scope = default_scope(db, env, user_id)

    if notes_mode not in ("replace", "append"):
        raise ValueError("notes_mode must be one of: replace, append")

    row = await db.query_first(
        "SELECT title, user_notes, read_state, is_pinned FROM items"
        " WHERE id = ? AND user_id = ? AND deleted_at IS NULL;",
        (item_id, user_id),
    )
    if not row:
        return None

    sets: list[str] = []
    params: list[Any] = []
    reindex = False

    if title is not None:
        clean_title = title.strip()
        if len(clean_title) > MAX_ITEM_TITLE_CHARS:
            raise ValueError(
                f"A title is limited to {MAX_ITEM_TITLE_CHARS} characters."
            )
        sets.append("title = ?")
        params.append(clean_title)
        reindex = True

    if user_notes is not None:
        if len(user_notes) > MAX_ITEM_NOTES_CHARS:
            raise ValueError(
                f"The notes are limited to {MAX_ITEM_NOTES_CHARS} characters."
            )
        existing = (row["user_notes"] or "").strip()
        if notes_mode == "append" and existing:
            clean_notes: str | None = f"{existing}\n\n{user_notes.strip()}"
        else:
            clean_notes = user_notes.strip() or None
        if clean_notes and len(clean_notes) > MAX_ITEM_NOTES_CHARS:
            raise ValueError(
                f"The notes are limited to {MAX_ITEM_NOTES_CHARS} characters."
            )
        sets.append("user_notes = ?")
        params.append(clean_notes)
        reindex = True

    if read_state is not None:
        if read_state not in READ_STATES:
            raise ValueError("read_state must be one of: " + ", ".join(READ_STATES))
        sets.append("read_state = ?")
        params.append(read_state)

    if is_pinned is not None:
        sets.append("is_pinned = ?")
        params.append(1 if is_pinned else 0)

    if sets:
        sets.append("updated_at = CURRENT_TIMESTAMP")
        await db.execute(
            f"UPDATE items SET {', '.join(sets)} WHERE id = ? AND user_id = ?;",
            (*params, item_id, user_id),
        )

    if reindex:
        await _refresh_fts_for_item(db, user_id, item_id)
        await _reindex_item_vectors(db, env, user_id, item_id, scope=scope)

    updated = await db.query_first(
        "SELECT title, user_notes, read_state, is_pinned FROM items"
        " WHERE id = ? AND user_id = ?;",
        (item_id, user_id),
    )
    return {
        "id": item_id,
        "title": (updated or row)["title"],
        "user_notes": (updated or row)["user_notes"],
        "read_state": (updated or row)["read_state"],
        "is_pinned": bool((updated or row)["is_pinned"]),
    }


async def save_note(
    db: Database,
    env: Any,
    user_id: str,
    title: str,
    content_text: str,
    tags: list[str] | None = None,
    is_pinned: bool = False,
    *,
    scope: TenantScope | None = None,
    read_state: str = "unread",
    created_at: str | None = None,
    source: str | None = None,
    source_id: str | None = None,
    agent_summary: str | None = None,
    why_saved: str | None = None,
    source_context: str | None = None,
    saved_by: str | None = None,
) -> dict[str, Any]:
    """Save a Markdown quick note sharing the unified FTS + vector pipeline.

    v2 fields:
    - agent_summary: AI-generated summary of the content
    - why_saved: User's reason for saving
    - source_context: Context from the conversation/task that led to saving
    - saved_by: Name of the agent/client that saved the item
    """
    if scope is None:
        from keepfor.defaults import default_scope

        scope = default_scope(db, env, user_id)

    from keepfor.runtime import get_providers
    from keepfor.spi import EntitlementDenied

    providers = get_providers()
    if source is None or source == "manual":
        decision = await providers.entitlements.check(
            scope.tenant_id, "save_item", qty=1
        )
        if not decision.allowed:
            raise EntitlementDenied(decision)

    first_line = (content_text or "").strip().split("\n")[0][:120]
    clean_title = (title or "").strip() or first_line or "Untitled note"
    body = (content_text or "").strip()
    clean_read_state = (
        read_state if read_state in ("unread", "read", "archived") else "unread"
    )
    item_id = str(uuid.uuid4())
    urn = f"urn:note:{item_id}"
    word_count = len(body.split()) if body else 0
    await db.execute(
        """
        INSERT INTO items (id, user_id, url, canonical_url, title, content_text,
            word_count, status, item_type, is_pinned, read_state, created_at,
            source, source_id, agent_summary, why_saved, source_context, saved_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'saved', 'note', ?, ?,
            COALESCE(?, CURRENT_TIMESTAMP), ?, ?, ?, ?, ?, ?);
        """,
        (
            item_id,
            user_id,
            urn,
            urn,
            clean_title,
            body,
            word_count,
            1 if is_pinned else 0,
            clean_read_state,
            created_at,
            source,
            source_id,
            agent_summary,
            why_saved,
            source_context,
            saved_by,
        ),
    )
    if source is None or source == "manual":
        try:
            await providers.entitlements.record(scope.tenant_id, "save_item", qty=1)
        except Exception as exc:
            logger.warning("Failed to record entitlement for save_item: %s", exc)

    clean_tags = [normalize_tag(t) for t in (tags or []) if normalize_tag(t)]
    if clean_tags:
        await add_tags_to_item(db, user_id, item_id, clean_tags)
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?, ?, ?, ?);",
        (
            item_id,
            user_id,
            clean_title,
            " ".join(
                p
                for p in [body, agent_summary, why_saved, source_context, saved_by]
                if p
            ),
        ),
    )
    # Best-effort multi-chunk Vectorize embedding (fail-open; no AI in tests).
    try:
        vectors = scope.vectors(env)
        if vectors.index is not None and body:
            from keepfor.utils.chunker import recursive_character_split

            chunks = recursive_character_split(
                body, target_tokens=400, overlap_tokens=50
            )
            if chunks:
                embed_decision = await providers.entitlements.check(
                    scope.tenant_id, "embed", qty=len(chunks)
                )
                if embed_decision.allowed:
                    vectors_to_upsert = []
                    chunk_queries = []
                    for i in range(0, len(chunks), 10):
                        batch_chunks = chunks[i : i + 10]
                        vecs = await providers.ai.embed(
                            env, [c.text for c in batch_chunks]
                        )
                        for chunk, vec in zip(batch_chunks, vecs or []):
                            chunk_id = f"item_{item_id}_chunk_{chunk.index}"
                            vectors_to_upsert.append(
                                {
                                    "id": chunk_id,
                                    "values": vec,
                                    "metadata": {
                                        "item_id": item_id,
                                        "user_id": user_id,
                                        "chunk_index": chunk.index,
                                    },
                                }
                            )
                            chunk_queries.append(
                                (
                                    "INSERT INTO chunks (id, item_id, user_id, "
                                    "chunk_index, token_count) VALUES (?, ?, ?, ?, ?);",
                                    (
                                        chunk_id,
                                        item_id,
                                        user_id,
                                        chunk.index,
                                        chunk.token_count,
                                    ),
                                )
                            )
                    if vectors_to_upsert:
                        await vectors.upsert(vectors_to_upsert)
                        try:
                            await providers.entitlements.record(
                                scope.tenant_id, "embed", qty=len(vectors_to_upsert)
                            )
                        except Exception as exc:
                            logger.warning(
                                "Entitlement record for embed failed: %s", exc
                            )
                        await db.execute_batch(chunk_queries)
    except Exception:
        pass
    return {
        "id": item_id,
        "url": urn,
        "canonical_url": urn,
        "title": clean_title,
        "status": "saved",
        "item_type": "note",
        "is_pinned": bool(is_pinned),
        "tags": clean_tags,
        "created_at": created_at,
        "source": source,
        "source_id": source_id,
    }


async def archive_item(db: Database, user_id: str, item_id: str) -> bool:
    """Move a reading-queue item to the archive. Returns owned."""
    row = await db.query_first(
        "SELECT id FROM items WHERE id = ? AND user_id = ? AND deleted_at IS NULL;",
        (item_id, user_id),
    )
    if not row:
        return False
    await db.execute(
        "UPDATE items SET read_state = 'archived',"
        " updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?;",
        (item_id, user_id),
    )
    return True


async def unarchive_item(db: Database, user_id: str, item_id: str) -> bool:
    """Return an archived item to the unread inbox. Returns owned."""
    row = await db.query_first(
        "SELECT id FROM items WHERE id = ? AND user_id = ? AND deleted_at IS NULL;",
        (item_id, user_id),
    )
    if not row:
        return False
    await db.execute(
        "UPDATE items SET read_state = 'unread',"
        " updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?;",
        (item_id, user_id),
    )
    return True


async def mark_item_read(db: Database, user_id: str, item_id: str) -> bool:
    """Mark an item read (read_state='read'); it stays in the library.

    Archive stays a separate action (here: archive_item). An archived item
    is left alone: the archive is the stronger state, so reading it must not
    pull it back into the library. False means "missing or archived".
    """
    row = await db.query_first(
        "SELECT id, read_state FROM items"
        " WHERE id = ? AND user_id = ? AND deleted_at IS NULL;",
        (item_id, user_id),
    )
    if not row or row["read_state"] == "archived":
        return False
    await db.execute(
        "UPDATE items SET read_state = 'read',"
        " updated_at = CURRENT_TIMESTAMP"
        " WHERE id = ? AND user_id = ? AND read_state != 'archived';",
        (item_id, user_id),
    )
    return True


async def mark_item_unread(db: Database, user_id: str, item_id: str) -> bool:
    """Undo mark_item_read: back to 'unread'.

    An archived item is left alone, like mark_item_read does.
    """
    row = await db.query_first(
        "SELECT id, read_state FROM items"
        " WHERE id = ? AND user_id = ? AND deleted_at IS NULL;",
        (item_id, user_id),
    )
    if not row or row["read_state"] == "archived":
        return False
    await db.execute(
        "UPDATE items SET read_state = 'unread',"
        " updated_at = CURRENT_TIMESTAMP"
        " WHERE id = ? AND user_id = ? AND read_state != 'archived';",
        (item_id, user_id),
    )
    return True


async def record_open(db: Database, user_id: str, item_id: str) -> None:
    """Log a reader visit. Lets errors propagate; callers must wrap in
    try/except or BackgroundTasks so a logging failure never breaks
    the reader."""
    await db.execute(
        "INSERT INTO item_opens (user_id, item_id) VALUES (?, ?);",
        (user_id, item_id),
    )
