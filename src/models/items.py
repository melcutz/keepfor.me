import uuid
from typing import Any

from src.models.db import Database
from src.utils.logging import logger
from src.utils.url import canonicalize_url


async def save_item(
    db: Database, env: Any, user_id: str, url: str, tags: list[str] | None = None
) -> tuple[dict[str, Any], bool]:
    """Saves a URL to the user library with canonicalization and deduplication.
    Returns (item_dict, is_new).
    """
    clean_url = canonicalize_url(url)
    tags = [t.strip().lower() for t in (tags or []) if t.strip()]

    # Check for existing item
    existing = await db.query_first(
        "SELECT id, url, canonical_url, title, status, created_at "
        "FROM items WHERE user_id = ? AND canonical_url = ?;",
        (user_id, clean_url),
    )

    if existing:
        item_id = existing["id"]
        # Merge new tags if provided
        if tags:
            await add_tags_to_item(db, user_id, item_id, tags)
        item_tags = await get_item_tags(db, item_id)
        res = dict(existing)
        res["tags"] = item_tags
        logger.debug(f"Item already exists: {item_id}, user: {user_id}")
        return res, False

    # Create new item
    item_id = str(uuid.uuid4())
    await db.execute(
        """
        INSERT INTO items (id, user_id, url, canonical_url, status)
        VALUES (?, ?, ?, ?, 'queued');
        """,
        (item_id, user_id, url.strip(), clean_url),
    )

    if tags:
        await add_tags_to_item(db, user_id, item_id, tags)

    # Enqueue extraction job; without a queue binding (local dev without
    # queue delivery, tests) extract inline so items never stick in 'queued'.
    queue = getattr(env, "QUEUE", None) if env is not None else None
    if queue is not None:
        await queue.send({"item_id": item_id, "url": clean_url})
        logger.info(f"Enqueued extraction: item={item_id}, url={clean_url}")
    else:
        logger.warning(f"No QUEUE binding; extracting inline: item={item_id}")
        try:
            from src.consumer.processor import extract_and_store

            await extract_and_store(db, env, item_id, clean_url)
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


async def add_tags_to_item(
    db: Database, user_id: str, item_id: str, tags: list[str]
) -> None:
    for tag_name in tags:
        clean_tag = tag_name.strip().lower()
        if not clean_tag:
            continue
        # Ensure tag exists in tags table
        tag_row = await db.query_first(
            "SELECT id FROM tags WHERE user_id = ? AND name = ?;", (user_id, clean_tag)
        )
        if not tag_row:
            tag_id = str(uuid.uuid4())
            await db.execute(
                "INSERT OR IGNORE INTO tags (id, user_id, name) VALUES (?, ?, ?);",
                (tag_id, user_id, clean_tag),
            )
            tag_row = await db.query_first(
                "SELECT id FROM tags WHERE user_id = ? AND name = ?;",
                (user_id, clean_tag),
            )

        if tag_row:
            await db.execute(
                "INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?);",
                (item_id, tag_row["id"]),
            )


async def remove_tags_from_item(
    db: Database, user_id: str, item_id: str, tags: list[str]
) -> None:
    for tag_name in tags:
        clean_tag = tag_name.strip().lower()
        tag_row = await db.query_first(
            "SELECT id FROM tags WHERE user_id = ? AND name = ?;", (user_id, clean_tag)
        )
        if tag_row:
            await db.execute(
                "DELETE FROM item_tags WHERE item_id = ? AND tag_id = ?;",
                (item_id, tag_row["id"]),
            )


async def get_item_tags(db: Database, item_id: str) -> list[str]:
    rows = await db.query_all(
        """
        SELECT t.name
        FROM tags t
        JOIN item_tags it ON t.id = it.tag_id
        WHERE it.item_id = ?
        ORDER BY t.name ASC;
        """,
        (item_id,),
    )
    return [r["name"] for r in rows]


async def get_item(db: Database, user_id: str, item_id: str) -> dict[str, Any] | None:
    row = await db.query_first(
        "SELECT * FROM items WHERE id = ? AND user_id = ?;", (item_id, user_id)
    )
    if not row:
        return None
    item = dict(row)
    item["tags"] = await get_item_tags(db, item_id)
    return item


async def get_item_clean_html(
    db: Database, env: Any, user_id: str, item_id: str
) -> str:
    """Get clean reader HTML from R2, or format the stored content text."""
    # Verify ownership
    item = await get_item(db, user_id, item_id)
    if not item:
        return "<p>Article not found.</p>"

    # Attempt fetch from R2
    if hasattr(env, "BUCKET") and env.BUCKET is not None:
        try:
            obj = await env.BUCKET.get(f"items/{item_id}/clean.html")
            if obj is not None:
                return await obj.text()
        except Exception:
            pass

    # Fallback to plain text / excerpt formatted into HTML paragraphs
    if item.get("status") == "failed":
        reason = item.get("fail_reason") or "unknown error"
        return (
            f"<h1>{item.get('title', item['url'])}</h1>"
            f"<p>Content extraction failed: {reason}</p>"
            f"<p><a href='{item['url']}' target='_blank'>Visit original link</a></p>"
        )
    text = (
        item.get("content_text")
        or item.get("excerpt")
        or "Content extraction in progress..."
    )
    paragraphs = "".join(f"<p>{p.strip()}</p>" for p in text.split("\n\n") if p.strip())
    return f"<h1>{item.get('title', item['url'])}</h1>{paragraphs}"


async def delete_item(db: Database, env: Any, user_id: str, item_id: str) -> bool:
    """Deletes item, its D1 records, R2 snapshots, and Vectorize chunks."""
    item = await db.query_first(
        "SELECT id FROM items WHERE id = ? AND user_id = ?;", (item_id, user_id)
    )
    if not item:
        return False

    # 1. Fetch chunk IDs to delete from Vectorize
    chunk_rows = await db.query_all(
        "SELECT id FROM chunks WHERE item_id = ?;", (item_id,)
    )
    chunk_ids = [r["id"] for r in chunk_rows]

    if chunk_ids and hasattr(env, "VECTORIZE") and env.VECTORIZE is not None:
        try:
            await env.VECTORIZE.deleteByIds(chunk_ids)
        except Exception:
            pass

    # 2. Delete R2 snapshots
    if hasattr(env, "BUCKET") and env.BUCKET is not None:
        try:
            await env.BUCKET.delete(f"items/{item_id}/raw.html")
            await env.BUCKET.delete(f"items/{item_id}/clean.html")
        except Exception:
            pass

    # 3. Delete from D1 (triggers cascade deletions on chunks, item_tags)
    await db.execute("DELETE FROM items_fts WHERE item_id = ?;", (item_id,))
    await db.execute(
        "DELETE FROM items WHERE id = ? AND user_id = ?;", (item_id, user_id)
    )
    return True


async def create_tag(db: Database, user_id: str, raw_name: str) -> str | None:
    """Creates an empty tag (zero items allowed); idempotent. Returns name or None."""
    from src.utils.tagger import validate_tag_name

    clean = validate_tag_name(raw_name or "")
    if not clean:
        return None
    existing = await db.query_first(
        "SELECT id FROM tags WHERE user_id = ? AND name = ?;", (user_id, clean)
    )
    if existing:
        return clean
    await db.execute(
        "INSERT OR IGNORE INTO tags (id, user_id, name) VALUES (?, ?, ?);",
        (str(uuid.uuid4()), user_id, clean),
    )
    return clean


async def rename_tag(db: Database, user_id: str, old_name: str, new_name: str) -> str:
    """Renames a tag; renaming onto an existing name merges the two.

    Returns 'merged', 'unchanged', 'not_found' or 'invalid'.
    """
    from src.utils.tagger import normalize_tag, validate_tag_name

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
            "SELECT item_id FROM item_tags WHERE tag_id = ?;", (old_row["id"],)
        )
        for row in linked:
            await db.execute(
                "INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?);",
                (row["item_id"], new_row["id"]),
            )
        await db.execute("DELETE FROM item_tags WHERE tag_id = ?;", (old_row["id"],))
        await db.execute("DELETE FROM tags WHERE id = ?;", (old_row["id"],))
        return "merged"
    await db.execute(
        "UPDATE tags SET name = ? WHERE id = ?;", (new_clean, old_row["id"])
    )
    return "merged"


async def delete_tag(db: Database, user_id: str, raw_name: str) -> bool:
    """Deletes a tag; items survive untagged. Also dismisses pending suggestions."""
    from src.utils.tagger import normalize_tag

    clean = normalize_tag(raw_name or "")
    if not clean:
        return False
    row = await db.query_first(
        "SELECT id FROM tags WHERE user_id = ? AND name = ?;", (user_id, clean)
    )
    if not row:
        return False
    # Explicit deletes (no reliance on ON DELETE CASCADE pragma state).
    await db.execute("DELETE FROM item_tags WHERE tag_id = ?;", (row["id"],))
    await db.execute("DELETE FROM tags WHERE id = ?;", (row["id"],))
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
    from src.utils.tagger import validate_tag_name

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
        await db.execute("DELETE FROM tags WHERE id = ?;", (row["id"],))
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
        (q or "")
        .strip()
        .lower()
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    excluded = {e.strip().lower() for e in (exclude or []) if e.strip()}
    fetch_limit = limit + min(len(excluded), limit)
    rows = await db.query_all(
        """
        SELECT t.name, COUNT(it.item_id) AS count, MAX(i.created_at) AS recent
        FROM tags t
        LEFT JOIN item_tags it ON t.id = it.tag_id
        LEFT JOIN items i ON i.id = it.item_id
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
        if r["name"].lower() not in excluded
    ]
    return out[:limit]


async def add_suggestions(
    db: Database,
    user_id: str,
    item_id: str,
    phrases: list[tuple[str, float]],
    limit: int = 8,
) -> int:
    """Records pending tag suggestions; idempotent per (item, phrase)."""
    from src.utils.tagger import normalize_tag

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
        "UPDATE suggested_tags SET status = 'accepted' WHERE id = ?;", (sugg_id,)
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
        "UPDATE suggested_tags SET status = 'dismissed' WHERE id = ?;", (sugg_id,)
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
            " ORDER BY s.score DESC LIMIT ?;",
            (user_id, item_id, limit),
        )
    return await db.query_all(
        "SELECT s.id, s.item_id, s.phrase, s.score, i.title"
        " FROM suggested_tags s LEFT JOIN items i ON i.id = s.item_id"
        " WHERE s.user_id = ? AND s.status = 'pending'"
        " ORDER BY s.score DESC LIMIT ?;",
        (user_id, limit),
    )


async def list_user_tags(db: Database, user_id: str) -> list[dict[str, Any]]:
    return await db.query_all(
        """
        SELECT t.name, COUNT(it.item_id) as count
        FROM tags t
        LEFT JOIN item_tags it ON t.id = it.tag_id
        WHERE t.user_id = ?
        GROUP BY t.id, t.name
        ORDER BY count DESC, t.name ASC;
        """,
        (user_id,),
    )
