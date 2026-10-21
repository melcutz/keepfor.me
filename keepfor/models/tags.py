# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tag management model functions: metadata, hierarchy, listing, search,
co-occurrence, cleanup finding, and bulk operations with dry_run safety.

All functions are tenant-scoped by user_id. Bulk/destructive operations
accept dry_run and return a match count plus a sample so callers can preview
before applying. The create/rename/merge/delete primitives live in
keepfor.models.items and are shared with the web UI.
"""

from __future__ import annotations

from typing import Any

from keepfor.models.db import Database
from keepfor.models.items import (
    BULK_TAG_LIMIT,
    add_tags_to_item,
    bulk_update_tags,
    get_item_tags,
    remove_tags_from_item,
)
from keepfor.models.items import (
    delete_tag as _delete_tag_primitive,
)
from keepfor.utils.tagger import normalize_tag

# Cap on preview samples returned by bulk/destructive operations.
SAMPLE_LIMIT = 20
# Cap on tag filter lists, mirroring keepfor.search.engine.MAX_TAG_FILTERS.
MAX_TAG_FILTERS = 10
# D1 allows 100 bound parameters per query; user_id takes one of them.
_ID_CHUNK = 99
# Cap on the ids scanned per call when counting matches. It bounds the
# number of D1 roundtrips a huge request can trigger.
BULK_SCAN_LIMIT = 1000


async def get_tag(db: Database, user_id: str, raw_name: str) -> dict[str, Any] | None:
    """Returns a single tag with its metadata, or None."""
    clean = normalize_tag(raw_name or "")
    if not clean:
        return None
    row = await db.query_first(
        "SELECT t.id, t.name, t.description, t.color, t.parent_id, p.name AS parent"
        " FROM tags t LEFT JOIN tags p ON p.id = t.parent_id AND p.user_id = t.user_id"
        " WHERE t.user_id = ? AND t.name = ?;",
        (user_id, clean),
    )
    return dict(row) if row else None


async def update_tag(
    db: Database,
    user_id: str,
    raw_name: str,
    *,
    description: str | None = None,
    color: str | None = None,
    parent: str | None = None,
) -> bool:
    """Edits a tag's description, color, and/or parent. Returns owned.

    Only the arguments that are not None are written: description="" clears
    the description, parent="" detaches the tag from its parent.
    """
    clean = normalize_tag(raw_name or "")
    if not clean:
        return False
    row = await db.query_first(
        "SELECT id FROM tags WHERE user_id = ? AND name = ?;", (user_id, clean)
    )
    if not row:
        return False

    sets: list[str] = []
    params: list[Any] = []
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    if color is not None:
        sets.append("color = ?")
        params.append(color)
    if parent is not None:
        if not parent.strip():
            # Empty string detaches the tag from its parent.
            sets.append("parent_id = ?")
            params.append(None)
        else:
            parent_clean = normalize_tag(parent)
            if parent_clean and parent_clean != clean:
                prow = await db.query_first(
                    "SELECT id FROM tags WHERE user_id = ? AND name = ?;",
                    (user_id, parent_clean),
                )
                if prow:
                    sets.append("parent_id = ?")
                    params.append(prow["id"])
                # An unknown parent name leaves the hierarchy untouched
                # instead of silently detaching the tag.
    if not sets:
        return True

    params.extend([row["id"], user_id])
    await db.execute(
        f"UPDATE tags SET {', '.join(sets)} WHERE id = ? AND user_id = ?;",
        tuple(params),
    )
    return True


async def list_tags(
    db: Database,
    user_id: str,
    *,
    sort: str = "count",
    prefix: str | None = None,
    min_count: int | None = None,
) -> list[dict[str, Any]]:
    """Lists tags with item count and last-used date.

    sort: "count" (most used), "name" (alphabetical), "recent" (last used).
    prefix: only tags whose name starts with this prefix.
    min_count: only tags with at least this many items.
    """
    prefix_clause = ""
    params: list[Any] = [user_id]
    if prefix:
        prefix_clause = " AND t.name LIKE ? ESCAPE '\\'"
        params.append(
            prefix.strip()
            .lower()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
            + "%"
        )

    having = ""
    if min_count is not None:
        having = " HAVING COUNT(i.id) >= ?"
        params.append(min_count)

    if sort == "name":
        order = " t.name ASC"
    elif sort == "recent":
        order = " last_used DESC, t.name ASC"
    else:  # count
        order = " count DESC, t.name ASC"

    rows = await db.query_all(
        "SELECT t.name, t.description, t.color, t.parent_id, p.name AS parent,"
        " COUNT(i.id) AS count, MAX(i.created_at) AS last_saved,"
        " MAX(COALESCE((SELECT MAX(io.opened_at) FROM item_opens io"
        "   JOIN items io_i ON io_i.id = io.item_id AND io_i.user_id = t.user_id"
        "     AND io_i.deleted_at IS NULL"
        "   JOIN item_tags io_it ON io_it.item_id = io_i.id AND io_it.tag_id = t.id),"
        "   i.created_at)) AS last_used"
        " FROM tags t"
        " LEFT JOIN tags p ON p.id = t.parent_id AND p.user_id = t.user_id"
        " LEFT JOIN item_tags it ON it.tag_id = t.id"
        " LEFT JOIN items i ON i.id = it.item_id AND i.user_id = t.user_id"
        "     AND i.deleted_at IS NULL"
        f" WHERE t.user_id = ?{prefix_clause}"
        " GROUP BY t.id, t.name, t.description, t.color, t.parent_id, p.name"
        f"{having} ORDER BY {order};",
        tuple(params),
    )
    return [dict(r) for r in rows]


async def search_tags(
    db: Database, user_id: str, query: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Fuzzy matching on tag names and descriptions.

    Matches the query as a substring of the name or description, plus a
    separator/case-folded variant (so "machinelearning" finds "machine-learning").
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    folded = q.replace("-", "").replace("_", "").replace(" ", "")
    like = f"%{q}%"
    like_folded = f"%{folded}%"
    # Strip separators from the stored name/description so "machinelearning"
    # matches "machine-learning".
    fold_name = "REPLACE(REPLACE(REPLACE(t.name, '-', ''), '_', ''), ' ', '')"
    fold_desc = "REPLACE(REPLACE(REPLACE(t.description, '-', ''), '_', ''), ' ', '')"
    rows = await db.query_all(
        "SELECT t.name, t.description, t.color, t.parent_id, p.name AS parent"
        " FROM tags t LEFT JOIN tags p ON p.id = t.parent_id AND p.user_id = t.user_id"
        " WHERE t.user_id = ? AND ("
        "   t.name LIKE ? ESCAPE '\\' OR t.description LIKE ? ESCAPE '\\'"
        "   OR t.name LIKE ? ESCAPE '\\' OR t.description LIKE ? ESCAPE '\\'"
        f"   OR {fold_name} LIKE ? ESCAPE '\\' OR {fold_desc} LIKE ? ESCAPE '\\'"
        " )"
        " ORDER BY (CASE WHEN t.name LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END), t.name ASC"
        " LIMIT ?;",
        (
            user_id,
            like,
            like,
            like_folded,
            like_folded,
            like_folded,
            like_folded,
            like,
            limit,
        ),
    )
    return [dict(r) for r in rows]


async def related_tags(
    db: Database, user_id: str, raw_name: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Co-occurrence: tags that appear on the same items as the given tag."""
    clean = normalize_tag(raw_name or "")
    if not clean:
        return []
    rows = await db.query_all(
        "SELECT t2.name, COUNT(*) as cooccurrence"
        " FROM item_tags it1"
        " JOIN items i ON i.id = it1.item_id AND i.user_id = ?"
        "   AND i.deleted_at IS NULL"
        " JOIN item_tags it2 ON it2.item_id = it1.item_id AND it2.tag_id != it1.tag_id"
        " JOIN tags t1 ON t1.id = it1.tag_id AND t1.user_id = ?"
        " JOIN tags t2 ON t2.id = it2.tag_id AND t2.user_id = t1.user_id"
        " WHERE t1.name = ?"
        " GROUP BY t2.id, t2.name"
        " ORDER BY cooccurrence DESC, t2.name ASC"
        " LIMIT ?;",
        (user_id, user_id, clean, limit),
    )
    return [dict(r) for r in rows]


async def tag_stats(db: Database, user_id: str) -> dict[str, Any]:
    """Overall tag usage statistics for the user."""
    total_tags = await db.query_first(
        "SELECT COUNT(*) as c FROM tags WHERE user_id = ?;", (user_id,)
    )
    total_items = await db.query_first(
        "SELECT COUNT(*) as c FROM items WHERE user_id = ? AND deleted_at IS NULL;",
        (user_id,),
    )
    tagged_items = await db.query_first(
        "SELECT COUNT(DISTINCT it.item_id) as c FROM item_tags it"
        " JOIN items i ON i.id = it.item_id AND i.user_id = ?"
        " WHERE i.user_id = ? AND i.deleted_at IS NULL;",
        (user_id, user_id),
    )
    untagged = await db.query_first(
        "SELECT COUNT(*) as c FROM items i WHERE i.user_id = ?"
        " AND i.deleted_at IS NULL AND NOT EXISTS"
        " (SELECT 1 FROM item_tags it WHERE it.item_id = i.id);",
        (user_id,),
    )
    top = await db.query_all(
        "SELECT t.name, COUNT(it.item_id) as count FROM tags t"
        " JOIN item_tags it ON it.tag_id = t.id"
        " JOIN items i ON i.id = it.item_id AND i.user_id = t.user_id"
        " AND i.deleted_at IS NULL"
        " WHERE t.user_id = ? GROUP BY t.id, t.name"
        " ORDER BY count DESC, t.name ASC LIMIT 5;",
        (user_id,),
    )
    return {
        "total_tags": total_tags["c"] if total_tags else 0,
        "total_items": total_items["c"] if total_items else 0,
        "tagged_items": tagged_items["c"] if tagged_items else 0,
        "untagged_items": untagged["c"] if untagged else 0,
        "top_tags": [{"name": r["name"], "count": r["count"]} for r in top],
    }


def _fold_tag_key(name: str) -> str:
    """Normalization key for near-duplicate detection.

    Lowercases, strips separators, and folds a trailing plural so that
    "tags" and "tag" collapse together.
    """
    key = name.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    if len(key) > 3 and key.endswith("s") and not key.endswith("ss"):
        key = key[:-1]
    return key


def _acronym(name: str) -> str:
    """Initials of a multi-word tag ("machine-learning" -> "ml")."""
    words = [w for w in name.replace("-", " ").replace("_", " ").split() if w]
    if len(words) < 2:
        return name.strip().lower()
    return "".join(w[0].lower() for w in words)


async def find_duplicate_tags(db: Database, user_id: str) -> list[dict[str, Any]]:
    """Find near-duplicate tags: case/plural/separator/acronym variants.

    "machine-learning", "machine_learning" and "machinelearning" collapse
    together, as do "ml" and "machine-learning" (same acronym) and "tags"
    and "tag" (same singular form).
    """
    rows = await db.query_all("SELECT name FROM tags WHERE user_id = ?;", (user_id,))
    names = [r["name"] for r in rows]
    if not names:
        return []

    # Union tags that share a key: folded name or acronym.
    parent = {name: name for name in names}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    owner: dict[str, str] = {}
    for name in names:
        for key in {_fold_tag_key(name), _acronym(name)}:
            if not key:
                continue
            if key in owner:
                union(name, owner[key])
            else:
                owner[key] = name

    groups: dict[str, set[str]] = {}
    for name in names:
        groups.setdefault(find(name), set()).add(name)
    return sorted(
        (
            {"key": key, "tags": sorted(members)}
            for key, members in groups.items()
            if len(members) > 1
        ),
        key=lambda g: g["key"],
    )


async def find_unused_tags(db: Database, user_id: str) -> list[dict[str, Any]]:
    """Tags with zero live items attached (trashed items do not count)."""
    rows = await db.query_all(
        "SELECT t.name, t.description FROM tags t"
        " LEFT JOIN item_tags it ON it.tag_id = t.id"
        " LEFT JOIN items i ON i.id = it.item_id AND i.user_id = t.user_id"
        " AND i.deleted_at IS NULL"
        " WHERE t.user_id = ? AND i.id IS NULL"
        " ORDER BY t.name ASC;",
        (user_id,),
    )
    return [dict(r) for r in rows]


async def find_untagged_items(
    db: Database, user_id: str, limit: int = SAMPLE_LIMIT
) -> list[dict[str, Any]]:
    """Items with no tags attached."""
    rows = await db.query_all(
        "SELECT i.id, i.title, i.url, i.created_at FROM items i"
        " WHERE i.user_id = ? AND i.deleted_at IS NULL AND NOT EXISTS"
        " (SELECT 1 FROM item_tags it WHERE it.item_id = i.id)"
        " ORDER BY i.created_at DESC LIMIT ?;",
        (user_id, limit),
    )
    return [dict(r) for r in rows]


async def delete_tag(
    db: Database, user_id: str, raw_name: str, *, dry_run: bool = True
) -> dict[str, Any]:
    """Removes a tag from all items; items always survive.

    dry_run=True (default) returns the match count and a sample without
    deleting. dry_run=False performs the deletion.
    """
    clean = normalize_tag(raw_name or "")
    if not clean:
        return {"found": False, "match_count": 0, "sample": [], "deleted": False}
    row = await db.query_first(
        "SELECT id FROM tags WHERE user_id = ? AND name = ?;", (user_id, clean)
    )
    if not row:
        return {"found": False, "match_count": 0, "sample": [], "deleted": False}

    linked = await db.query_all(
        "SELECT it.item_id FROM item_tags it"
        " JOIN items i ON i.id = it.item_id AND i.user_id = ?"
        "   AND i.deleted_at IS NULL"
        " WHERE it.tag_id = ? LIMIT ?;",
        (user_id, row["id"], SAMPLE_LIMIT),
    )
    match_count = await db.query_first(
        "SELECT COUNT(*) as c FROM item_tags it"
        " JOIN items i ON i.id = it.item_id AND i.user_id = ?"
        "   AND i.deleted_at IS NULL"
        " WHERE it.tag_id = ?;",
        (user_id, row["id"]),
    )
    result = {
        "found": True,
        "match_count": match_count["c"] if match_count else 0,
        "sample": [r["item_id"] for r in linked],
        "deleted": False,
    }
    if dry_run:
        return result

    await _delete_tag_primitive(db, user_id, clean)
    result["deleted"] = True
    return result


async def tag_items(
    db: Database,
    user_id: str,
    item_ids: list[str],
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Adds/removes tags across owned items; skips foreign and missing ids.

    dry_run=True (default) previews affected items without writing.

    At most BULK_TAG_LIMIT owned items are processed per call. `total_matched`
    counts every owned item among the supplied ids; `truncated` is True when
    more matched than were processed (or more ids were supplied than were
    scanned, see BULK_SCAN_LIMIT).
    """
    add = [t for t in (add or []) if t.strip()]
    remove = [t for t in (remove or []) if t.strip()]
    ids = [i for i in (item_ids or []) if i]
    truncated = len(ids) > BULK_SCAN_LIMIT
    owned: list[str] = []
    for start in range(0, min(len(ids), BULK_SCAN_LIMIT), _ID_CHUNK):
        chunk = ids[start : start + _ID_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        rows = await db.query_all(
            f"SELECT id FROM items WHERE user_id = ? AND deleted_at IS NULL"
            f" AND id IN ({placeholders});",
            (user_id, *chunk),
        )
        owned.extend(r["id"] for r in rows)
    total_matched = len(owned)
    if total_matched > BULK_TAG_LIMIT:
        truncated = True
    processed = owned[:BULK_TAG_LIMIT]
    result = {
        "match_count": len(processed),
        "total_matched": total_matched,
        "sample": processed[:SAMPLE_LIMIT],
        "applied": False,
        "truncated": truncated,
    }
    if dry_run or not processed:
        return result

    await bulk_update_tags(db, user_id, processed, add=add, remove=remove)
    result["applied"] = True
    return result


async def set_item_tags(
    db: Database, user_id: str, item_id: str, tags: list[str]
) -> dict[str, Any]:
    """Replaces all tags on an item with the given set. Returns owned."""
    row = await db.query_first(
        "SELECT id FROM items WHERE id = ? AND user_id = ? AND deleted_at IS NULL;",
        (item_id, user_id),
    )
    if not row:
        return {"found": False, "tags": []}
    await remove_tags_from_item(
        db, user_id, item_id, await get_item_tags(db, user_id, item_id)
    )
    clean = list(dict.fromkeys(t.strip().lower() for t in tags if t.strip()))
    if clean:
        await add_tags_to_item(db, user_id, item_id, clean)
    return {"found": True, "tags": clean}


async def retag_by_query(
    db: Database,
    user_id: str,
    *,
    query: str | None = None,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    dry_run: bool = True,
    tag_clause: str = "",
    tag_params: tuple = (),
) -> dict[str, Any]:
    """Finds items matching a query (title/url substring) and previews/applies
    tag changes. dry_run=True (default) returns affected items without writing.

    At most BULK_TAG_LIMIT items are processed per call. `total_matched` is
    the number of items the query matched; `truncated` is True when more
    matched than were processed.
    """
    add = [t for t in (add or []) if t.strip()]
    remove = [t for t in (remove or []) if t.strip()]
    q = (query or "").strip().lower()
    if not q and not tag_clause:
        return {
            "match_count": 0,
            "total_matched": 0,
            "sample": [],
            "applied": False,
            "truncated": False,
        }

    params = (user_id, f"%{q}%", f"%{q}%", f"%{q}%", *tag_params)
    count_row = await db.query_first(
        "SELECT COUNT(*) as c FROM items i WHERE i.user_id = ?"
        " AND i.deleted_at IS NULL AND ("
        " LOWER(i.title) LIKE ? OR LOWER(i.url) LIKE ? OR LOWER(i.canonical_url) LIKE ?"
        f"{tag_clause});",
        params,
    )
    total_matched = int(count_row["c"]) if count_row else 0
    truncated = total_matched > BULK_TAG_LIMIT

    rows = await db.query_all(
        "SELECT i.id FROM items i WHERE i.user_id = ? AND i.deleted_at IS NULL AND ("
        " LOWER(i.title) LIKE ? OR LOWER(i.url) LIKE ? OR LOWER(i.canonical_url) LIKE ?"
        f"{tag_clause}) ORDER BY i.created_at DESC LIMIT {BULK_TAG_LIMIT};",
        params,
    )
    item_ids = [r["id"] for r in rows]
    result = await tag_items(
        db, user_id, item_ids, add=add, remove=remove, dry_run=dry_run
    )
    result["query"] = query
    result["total_matched"] = total_matched
    result["truncated"] = truncated
    return result


async def suggest_tags_for_item(
    db: Database, user_id: str, item_id: str
) -> list[dict[str, Any]]:
    """Suggestions for an item drawn from the existing tag vocabulary first."""
    from keepfor.utils.tagger import (
        MAX_SUGGESTIONS,
        match_existing_tags,
        suggest_new_tags,
    )

    item = await db.query_first(
        "SELECT title, excerpt, url, content_text FROM items"
        " WHERE id = ? AND user_id = ? AND deleted_at IS NULL;",
        (item_id, user_id),
    )
    if not item:
        return []
    user_tags = [r["name"] for r in await _list_user_tag_names(db, user_id)]
    match = match_existing_tags(
        item.get("title") or "",
        item.get("excerpt") or "",
        item.get("url") or "",
        item.get("content_text") or "",
        user_tags,
    )
    out: list[dict[str, Any]] = [
        {"name": t, "reason": "existing"} for t in match.auto_apply
    ]
    out += [{"name": t, "reason": "existing"} for t in match.suggest_only]
    for s in suggest_new_tags(
        item.get("title") or "",
        item.get("excerpt") or "",
        item.get("url") or "",
        item.get("content_text") or "",
        user_tags,
    ):
        out.append({"name": s.phrase, "reason": "new", "score": s.score})
    return out[:MAX_SUGGESTIONS]


async def suggest_tags_for_text(
    db: Database, user_id: str, text: str
) -> list[dict[str, Any]]:
    """Suggestions for arbitrary text drawn from the existing vocabulary first."""
    from keepfor.utils.tagger import (
        MAX_SUGGESTIONS,
        match_existing_tags,
        suggest_new_tags,
    )

    user_tags = [r["name"] for r in await _list_user_tag_names(db, user_id)]
    match = match_existing_tags("", "", "", text, user_tags)
    out: list[dict[str, Any]] = [
        {"name": t, "reason": "existing"} for t in match.auto_apply
    ]
    out += [{"name": t, "reason": "existing"} for t in match.suggest_only]
    for s in suggest_new_tags("", "", "", text, user_tags):
        out.append({"name": s.phrase, "reason": "new", "score": s.score})
    return out[:MAX_SUGGESTIONS]


async def _list_user_tag_names(db: Database, user_id: str) -> list[dict[str, Any]]:
    return await db.query_all(
        "SELECT name FROM tags WHERE user_id = ? ORDER BY name ASC;", (user_id,)
    )


async def expand_tag_names(
    db: Database, user_id: str, names: list[str]
) -> dict[str, list[str]]:
    """Maps every requested tag name to itself plus all of its descendants.

    Child tags are included so a filter on "work" also matches "work/research".
    """
    clean: list[str] = []
    for raw in names or []:
        c = normalize_tag(raw or "")
        if c and c not in clean:
            clean.append(c)
    if not clean:
        return {}

    rows = await db.query_all(
        "SELECT id, name, parent_id FROM tags WHERE user_id = ?;", (user_id,)
    )
    id_by_name = {r["name"]: r["id"] for r in rows}
    children: dict[str, list[str]] = {}
    for r in rows:
        if r["parent_id"]:
            children.setdefault(r["parent_id"], []).append(r["name"])

    out: dict[str, list[str]] = {}
    for name in clean:
        found = [name]
        stack = [id_by_name.get(name)]
        while stack:
            current = stack.pop()
            if not current:
                continue
            for child in children.get(current, []):
                if child not in found:
                    found.append(child)
                    stack.append(id_by_name.get(child))
        out[name] = found
    return out


async def tag_filter_clause(
    db: Database,
    user_id: str,
    *,
    tags_all: list[str] | None = None,
    tags_any: list[str] | None = None,
    tags_none: list[str] | None = None,
    untagged: bool = False,
) -> tuple[str, tuple]:
    """Builds a SQL fragment filtering items by tag.

    tags_all: the item must carry every requested tag (child tags count).
    tags_any: the item must carry at least one of the requested tags.
    tags_none: the item must carry none of the requested tags.
    untagged: the item must have no tags at all.

    The fragment assumes the items table is aliased as `i`.
    """
    clause = ""
    params: list[Any] = []

    def _exists(names: list[str], negate: bool = False) -> str:
        placeholders = ",".join("?" for _ in names)
        exists = (
            "EXISTS (SELECT 1 FROM item_tags it JOIN tags t"
            " ON t.id = it.tag_id AND t.user_id = i.user_id"
            f" WHERE it.item_id = i.id AND LOWER(t.name) IN ({placeholders}))"
        )
        return "NOT " + exists if negate else exists

    all_sets = await expand_tag_names(db, user_id, (tags_all or [])[:MAX_TAG_FILTERS])
    for names in all_sets.values():
        clause += f" AND {_exists(names)}"
        params.extend(names)

    any_sets = await expand_tag_names(db, user_id, (tags_any or [])[:MAX_TAG_FILTERS])
    any_names = [n for names in any_sets.values() for n in names]
    if any_names:
        clause += f" AND {_exists(any_names)}"
        params.extend(any_names)

    none_sets = await expand_tag_names(db, user_id, (tags_none or [])[:MAX_TAG_FILTERS])
    none_names = [n for names in none_sets.values() for n in names]
    if none_names:
        clause += f" AND {_exists(none_names, negate=True)}"
        params.extend(none_names)

    if untagged:
        clause += (
            " AND NOT EXISTS (SELECT 1 FROM item_tags itx WHERE itx.item_id = i.id)"
        )

    return clause, tuple(params)
