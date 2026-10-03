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
    else:
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
