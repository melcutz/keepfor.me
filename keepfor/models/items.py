# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from keepfor.models.db import Database
from keepfor.utils.logging import logger
from keepfor.utils.url import canonicalize_url

if TYPE_CHECKING:
    from keepfor.spi import TenantScope


async def save_item(
    db: Database,
    env: Any,
    user_id: str,
    url: str,
    tags: list[str] | None = None,
    *,
    scope: TenantScope | None = None,
) -> tuple[dict[str, Any], bool]:
    """Saves a URL to the user library with canonicalization and deduplication.
    Returns (item_dict, is_new).
    """
    if scope is None:
        from keepfor.defaults import default_scope

        scope = default_scope(db, env, user_id)

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
        await queue.send({"item_id": item_id, "url": clean_url, "user_id": user_id})
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


async def add_tags_to_item(
    db: Database, user_id: str, item_id: str, tags: list[str]
) -> None:
    clean_tags = list(dict.fromkeys(t.strip().lower() for t in tags if t.strip()))
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
            "INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?);",
            (item_id, tag_map[t]),
        )
        for t in clean_tags
        if t in tag_map
    ]
    if link_stmts:
        await db.execute_batch(link_stmts)


async def remove_tags_from_item(
    db: Database, user_id: str, item_id: str, tags: list[str]
) -> None:
    clean_tags = list(dict.fromkeys(t.strip().lower() for t in tags if t.strip()))
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
            f"DELETE FROM item_tags WHERE item_id = ? AND tag_id IN ({id_ph});",
            (item_id, *tag_ids),
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
    db: Database,
    env: Any,
    user_id: str,
    item_id: str,
    *,
    scope: TenantScope | None = None,
) -> str:
    """Get clean reader HTML from R2, or format the stored content text."""
    if scope is None:
        from keepfor.defaults import default_scope

        scope = default_scope(db, env, user_id)

    # Verify ownership
    item = await get_item(db, user_id, item_id)
    if not item:
        return "<p>Article not found.</p>"

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
    # .get('title', url) would render a literal "None" heading — use or.
    heading = item.get("title") or item["url"]
    if item.get("status") == "failed":
        reason = item.get("fail_reason") or "unknown error"
        return (
            f"<h1>{heading}</h1>"
            f"<p>Content extraction failed: {reason}</p>"
            f"<p><a href='{item['url']}' target='_blank'>Visit original link</a></p>"
        )
    text = (
        item.get("content_text")
        or item.get("excerpt")
        or "Content extraction in progress..."
    )
    paragraphs = "".join(f"<p>{p.strip()}</p>" for p in text.split("\n\n") if p.strip())
    return f"<h1>{heading}</h1>{paragraphs}"


async def delete_item(
    db: Database,
    env: Any,
    user_id: str,
    item_id: str,
    *,
    scope: TenantScope | None = None,
) -> bool:
    """Deletes item, its D1 records, R2 snapshots, and Vectorize chunks."""
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
        "SELECT id FROM chunks WHERE item_id = ?;", (item_id,)
    )
    chunk_ids = [r["id"] for r in chunk_rows]

    if chunk_ids and hasattr(env, "VECTORIZE") and env.VECTORIZE is not None:
        try:
            await env.VECTORIZE.deleteByIds(chunk_ids)
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
    await db.execute_batch(
        [
            ("DELETE FROM items_fts WHERE item_id = ?;", (item_id,)),
            ("DELETE FROM items WHERE id = ? AND user_id = ?;", (item_id, user_id)),
        ]
    )
    return True


async def create_tag(db: Database, user_id: str, raw_name: str) -> str | None:
    """Creates an empty tag (zero items allowed); idempotent. Returns name or None."""
    from keepfor.utils.tagger import validate_tag_name

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
            "SELECT id FROM items WHERE id = ? AND user_id = ?;",
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
    await db.execute("DELETE FROM tag_rules WHERE id = ?;", (rule_id,))
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
        " JOIN items i ON i.id = it.item_id"
        " JOIN tags t ON t.id = it.tag_id"
        " WHERE i.user_id = ?;",
        (user_id,),
    )
    url_rows = await db.query_all(
        "SELECT canonical_url FROM items WHERE user_id = ?;", (user_id,)
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
        key = (host, row["name"])
        if row["canonical_url"] not in pair_urls.setdefault(key, set()):
            pair_urls[key].add(row["canonical_url"])
            pair[key] += 1
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
        key = f"domain:{host}:{tag}"
        if (
            support >= min_support
            and precision >= min_precision
            and ("domain", host, tag) not in existing
            and key not in dismissed
        ):
            out.append(
                {
                    "domain": host,
                    "tag": tag,
                    "precision": round(precision, 3),
                    "support": support,
                    "key": key,
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


async def _refresh_fts_for_item(db: Database, user_id: str, item_id: str) -> None:
    """Rebuild the FTS5 row for an item, folding user_notes into the index."""
    row = await db.query_first(
        "SELECT title, content_text, user_notes FROM items"
        " WHERE id = ? AND user_id = ?;",
        (item_id, user_id),
    )
    if not row:
        return
    combined = " ".join(
        p for p in [row["content_text"] or "", row["user_notes"] or ""] if p
    )
    await db.execute("DELETE FROM items_fts WHERE item_id = ?;", (item_id,))
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?, ?, ?, ?);",
        (item_id, user_id, row["title"] or "", combined),
    )


async def toggle_pin_item(db: Database, user_id: str, item_id: str) -> bool | None:
    """Toggle is_pinned; returns new pinned state, or None if not owned."""
    row = await db.query_first(
        "SELECT is_pinned FROM items WHERE id = ? AND user_id = ?;",
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


async def get_pinned_items(db: Database, user_id: str) -> list[dict[str, Any]]:
    """Pinned shelf items, newest first, with tags attached."""
    rows = await db.query_all(
        "SELECT * FROM items WHERE user_id = ? AND is_pinned = 1"
        " ORDER BY created_at DESC;",
        (user_id,),
    )
    if not rows:
        return []

    item_ids = [r["id"] for r in rows]
    placeholders = ",".join("?" for _ in item_ids)
    tag_rows = await db.query_all(
        f"""
        SELECT it.item_id, t.name as tag_name
        FROM item_tags it
        JOIN tags t ON it.tag_id = t.id
        WHERE it.item_id IN ({placeholders});
        """,
        tuple(item_ids),
    )
    item_tags_map: dict[str, list[str]] = {}
    for tr in tag_rows:
        item_tags_map.setdefault(tr["item_id"], []).append(tr["tag_name"])

    out: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        item["tags"] = item_tags_map.get(r["id"], [])
        out.append(item)
    return out


async def update_user_notes(
    db: Database, user_id: str, item_id: str, user_notes: str | None
) -> bool:
    """Persist 'why I kept this' notes; re-indexes FTS. Returns owned."""
    row = await db.query_first(
        "SELECT id FROM items WHERE id = ? AND user_id = ?;",
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
) -> dict[str, Any]:
    """Save a Markdown quick note sharing the unified FTS + vector pipeline."""
    if scope is None:
        from keepfor.defaults import default_scope

        scope = default_scope(db, env, user_id)

    first_line = (content_text or "").strip().split("\n")[0][:120]
    clean_title = (title or "").strip() or first_line or "Untitled note"
    body = (content_text or "").strip()
    item_id = str(uuid.uuid4())
    urn = f"urn:note:{item_id}"
    word_count = len(body.split()) if body else 0
    await db.execute(
        """
        INSERT INTO items (id, user_id, url, canonical_url, title, content_text,
            word_count, status, item_type, is_pinned, read_state)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'saved', 'note', ?, 'unread');
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
        ),
    )
    clean_tags = [t.strip().lower() for t in (tags or []) if t.strip()]
    if clean_tags:
        await add_tags_to_item(db, user_id, item_id, clean_tags)
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?, ?, ?, ?);",
        (item_id, user_id, clean_title, body),
    )
    # Best-effort single-chunk Vectorize embedding (fail-open; no AI in tests).
    try:
        if (
            env is not None
            and getattr(env, "AI", None) is not None
            and getattr(env, "VECTORIZE", None) is not None
            and body
        ):
            from keepfor.utils.chunker import recursive_character_split

            chunks = recursive_character_split(
                body, target_tokens=400, overlap_tokens=50
            )[:1]
            if chunks:
                ai_res = await env.AI.run(
                    "@cf/baai/bge-base-en-v1.5",
                    {"text": [chunks[0].text]},
                )
                raw = getattr(ai_res, "data", ai_res)
                if hasattr(raw, "to_py"):
                    raw = raw.to_py()
                vecs = raw.get("data", raw) if isinstance(raw, dict) else raw
                if vecs:
                    chunk_id = f"item_{item_id}_chunk_0"
                    await env.VECTORIZE.upsert(
                        [
                            {
                                "id": chunk_id,
                                "values": vecs[0],
                                "metadata": {
                                    "item_id": item_id,
                                    "user_id": user_id,
                                    "chunk_index": 0,
                                },
                            }
                        ]
                    )
                    await db.execute(
                        "INSERT INTO chunks (id, item_id, user_id, chunk_index,"
                        " token_count) VALUES (?, ?, ?, ?, ?);",
                        (chunk_id, item_id, user_id, 0, chunks[0].token_count),
                    )
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
    }


async def archive_item(db: Database, user_id: str, item_id: str) -> bool:
    """Move a reading-queue item to the archive. Returns owned."""
    row = await db.query_first(
        "SELECT id FROM items WHERE id = ? AND user_id = ?;",
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
        "SELECT id FROM items WHERE id = ? AND user_id = ?;",
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


async def record_open(db: Database, user_id: str, item_id: str) -> None:
    """Log a reader visit. Lets errors propagate; callers must wrap in
    try/except or BackgroundTasks so a logging failure never breaks
    the reader."""
    await db.execute(
        "INSERT INTO item_opens (user_id, item_id) VALUES (?, ?);",
        (user_id, item_id),
    )
