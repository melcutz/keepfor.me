# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Collection model functions: named buckets of library items.

Every function is tenant-scoped by user_id. A collection never owns the
items it holds: deleting one drops the links, the items survive.

A collection is either manual (its members are the linked items) or smart
(it carries a query and/or a tag filter, and its members are resolved at
read time through the search).
"""

from __future__ import annotations

import uuid
from typing import Any

from keepfor.models.db import Database

# Cap on a collection name.
MAX_COLLECTION_NAME = 64
# Cap on a collection description.
MAX_COLLECTION_DESCRIPTION = 200
# Cap on the query of a smart collection.
MAX_COLLECTION_QUERY = 200
# Cap on the tag filter of a smart collection.
MAX_COLLECTION_TAG = 64
# Cap on the items one add/remove call may carry.
MAX_COLLECTION_ITEMS = 100
# Cap on the number of collections one list call returns.
MAX_COLLECTIONS_LIMIT = 50
# Cap on the item ids listed for a single collection.
MAX_LISTED_ITEMS = 20
# D1 allows 100 bound parameters per query. collection_id and user_id take
# two of them, the rest is a margin: 100 ids still need two statements.
_ID_CHUNK = 90


def collection_kind(row: dict[str, Any]) -> str:
    """The kind of a collection: "smart" when it carries a filter.

    A smart collection resolves its members at read time; a manual one
    holds the items that were linked into it.
    """
    return "smart" if (row.get("query") or row.get("tag")) else "manual"


async def _resolve_collection(
    db: Database, user_id: str, ref: str
) -> dict[str, Any] | None:
    """Resolves a collection by name or by id, scoped to the user."""
    if not ref or not isinstance(ref, str):
        return None
    row = await db.query_first(
        "SELECT id, name, description, query, tag, created_at, updated_at"
        " FROM collections WHERE user_id = ? AND (name = ? OR id = ?);",
        (user_id, ref, ref),
    )
    return dict(row) if row else None


async def _collection_item_count(db: Database, user_id: str, collection_id: str) -> int:
    """Number of the user's own live items in one owned collection.

    A trashed item is not in the library: it is not counted.
    """
    row = await db.query_first(
        "SELECT COUNT(*) AS c FROM collection_items ci"
        " JOIN collections c ON c.id = ci.collection_id AND c.user_id = ?"
        " JOIN items i ON i.id = ci.item_id AND i.user_id = ?"
        " WHERE ci.collection_id = ? AND i.deleted_at IS NULL;",
        (user_id, user_id, collection_id),
    )
    return int(row["c"] or 0) if row else 0


async def create_collection(
    db: Database,
    user_id: str,
    name: str,
    *,
    description: str | None = None,
    query: str | None = None,
    tag: str | None = None,
) -> dict[str, Any] | None:
    """Creates a collection; idempotent. Returns the collection row.

    An existing collection with the same name is returned untouched, with
    `created` False. Returns None when the name or description is invalid.

    A query and/or a tag make the collection a smart one: its members are
    resolved at read time instead of being linked into it.
    """
    clean = (name or "").strip()
    if not clean or len(clean) > MAX_COLLECTION_NAME:
        return None
    desc = (description or "").strip() or None
    if desc and len(desc) > MAX_COLLECTION_DESCRIPTION:
        return None
    existing = await db.query_first(
        "SELECT id, name, description, query, tag, created_at, updated_at"
        " FROM collections WHERE user_id = ? AND name = ?;",
        (user_id, clean),
    )
    if existing:
        row = dict(existing)
        return {**row, "created": False, "kind": collection_kind(row)}
    collection_id = str(uuid.uuid4())
    await db.execute(
        "INSERT OR IGNORE INTO collections (id, user_id, name, description,"
        " query, tag) VALUES (?, ?, ?, ?, ?, ?);",
        (collection_id, user_id, clean, desc, query or None, tag or None),
    )
    # Read back by name: a concurrent create of the same name wins, and this
    # returns its row instead of raising on the unique constraint.
    row = await db.query_first(
        "SELECT id, name, description, query, tag, created_at, updated_at"
        " FROM collections WHERE user_id = ? AND name = ?;",
        (user_id, clean),
    )
    if not row:
        return None
    stored = dict(row)
    return {
        **stored,
        "created": row["id"] == collection_id,
        "kind": collection_kind(stored),
    }


async def list_collections(
    db: Database, user_id: str, *, limit: int = 20, offset: int = 0
) -> list[dict[str, Any]]:
    """Lists the user's collections with their item count and item ids.

    A trashed item is not in the library: it is neither counted nor listed.

    A smart collection has no links: its item count and ids are resolved
    at read time by the caller, through the search.
    """
    # Cap the limit here too: every listed collection binds its own id, and
    # D1 rejects a statement with more than 100 parameters.
    limit = min(limit, MAX_COLLECTIONS_LIMIT)
    rows = await db.query_all(
        "SELECT c.id, c.name, c.description, c.created_at, c.updated_at,"
        " c.query, c.tag,"
        " (SELECT COUNT(*) FROM collection_items ci JOIN items i"
        "  ON i.id = ci.item_id AND i.user_id = c.user_id"
        "  AND i.deleted_at IS NULL"
        "  WHERE ci.collection_id = c.id)"
        " AS item_count FROM collections c WHERE c.user_id = ?"
        " ORDER BY c.created_at ASC, c.name ASC LIMIT ? OFFSET ?;",
        (user_id, limit, offset),
    )
    collections = []
    for r in rows:
        row = dict(r)
        row["kind"] = collection_kind(row)
        collections.append(row)
    if not collections:
        return collections

    # Only a manual collection holds links.
    ids = [c["id"] for c in collections if c["kind"] == "manual"]
    by_collection: dict[str, list[str]] = {}
    if ids:
        placeholders = ",".join("?" for _ in ids)
        item_rows = await db.query_all(
            "SELECT collection_id, item_id FROM ("
            " SELECT ci.collection_id AS collection_id, ci.item_id AS item_id,"
            " ROW_NUMBER() OVER (PARTITION BY ci.collection_id"
            " ORDER BY ci.created_at, ci.rowid)"
            " AS rn FROM collection_items ci"
            " JOIN collections c ON c.id = ci.collection_id AND c.user_id = ?"
            " JOIN items i ON i.id = ci.item_id AND i.user_id = ?"
            " AND i.deleted_at IS NULL"
            f" WHERE ci.collection_id IN ({placeholders})) WHERE rn <= ?;",
            (user_id, user_id, *ids, MAX_LISTED_ITEMS),
        )
        for row in item_rows:
            by_collection.setdefault(row["collection_id"], []).append(row["item_id"])
    for collection in collections:
        item_ids = by_collection.get(collection["id"], [])
        collection["item_ids"] = item_ids
        collection["items_truncated"] = collection["item_count"] > len(item_ids)
    return collections


async def add_items_to_collection(
    db: Database, user_id: str, collection: str, item_ids: list[str]
) -> dict[str, Any]:
    """Adds owned items to a collection.

    Ids that are not the user's, that do not exist, that are in the Trash,
    or that are already in the collection are skipped: `added` counts the
    new links only. At most MAX_COLLECTION_ITEMS ids are processed per call.

    A smart collection takes no items: its members are resolved by its
    query, so the result says `smart` True and nothing is linked.
    """
    coll = await _resolve_collection(db, user_id, collection)
    if not coll:
        return {
            "found": False,
            "collection": None,
            "added": 0,
            "skipped": 0,
            "item_count": 0,
            "truncated": False,
        }
    if collection_kind(coll) == "smart":
        return {
            "found": True,
            "smart": True,
            "collection": coll["name"],
            "added": 0,
            "skipped": 0,
            "item_count": 0,
            "truncated": False,
        }
    ids = [i for i in (item_ids or []) if i]
    truncated = len(ids) > MAX_COLLECTION_ITEMS
    ids = ids[:MAX_COLLECTION_ITEMS]
    before = await _collection_item_count(db, user_id, coll["id"])
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start : start + _ID_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        await db.execute(
            "INSERT OR IGNORE INTO collection_items (collection_id, item_id)"
            " SELECT ?, i.id FROM items i"
            " WHERE i.user_id = ? AND i.deleted_at IS NULL"
            f" AND i.id IN ({placeholders});",
            (coll["id"], user_id, *chunk),
        )
    after = await _collection_item_count(db, user_id, coll["id"])
    added = max(after - before, 0)
    return {
        "found": True,
        "smart": False,
        "collection": coll["name"],
        "added": added,
        "skipped": len(ids) - added,
        "item_count": after,
        "truncated": truncated,
    }


async def remove_items_from_collection(
    db: Database, user_id: str, collection: str, item_ids: list[str]
) -> dict[str, Any]:
    """Removes items from a collection. The items themselves are never deleted.

    A smart collection holds no links: its members are resolved by its
    query, so the result says `smart` True and nothing is removed.
    """
    coll = await _resolve_collection(db, user_id, collection)
    if not coll:
        return {
            "found": False,
            "collection": None,
            "removed": 0,
            "item_count": 0,
            "truncated": False,
        }
    if collection_kind(coll) == "smart":
        return {
            "found": True,
            "smart": True,
            "collection": coll["name"],
            "removed": 0,
            "item_count": 0,
            "truncated": False,
        }
    ids = [i for i in (item_ids or []) if i]
    truncated = len(ids) > MAX_COLLECTION_ITEMS
    ids = ids[:MAX_COLLECTION_ITEMS]
    before = await _collection_item_count(db, user_id, coll["id"])
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start : start + _ID_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        await db.execute(
            "DELETE FROM collection_items WHERE collection_id IN"
            " (SELECT id FROM collections WHERE id = ? AND user_id = ?)"
            f" AND item_id IN ({placeholders});",
            (coll["id"], user_id, *chunk),
        )
    after = await _collection_item_count(db, user_id, coll["id"])
    return {
        "found": True,
        "smart": False,
        "collection": coll["name"],
        "removed": max(before - after, 0),
        "item_count": after,
        "truncated": truncated,
    }


async def rename_collection(
    db: Database,
    user_id: str,
    collection: str,
    new_name: str,
    *,
    query: str | None = None,
    tag: str | None = None,
) -> dict[str, Any]:
    """Renames a collection and sets its smart filter.

    Returns {'outcome': 'renamed'} with the updated collection, or
    {'outcome': 'not_found' | 'invalid' | 'exists'}. The 'exists' outcome
    means the name is already taken by another collection: the two are
    never merged into.

    A query and/or a tag set the smart filter: an empty string removes it.
    A filter that is not given stays as it is.
    """
    coll = await _resolve_collection(db, user_id, collection)
    if not coll:
        return {"outcome": "not_found"}
    clean = (new_name or "").strip()
    if not clean or len(clean) > MAX_COLLECTION_NAME:
        return {"outcome": "invalid"}
    if clean != coll["name"]:
        taken = await db.query_first(
            "SELECT id FROM collections WHERE user_id = ? AND name = ?;",
            (user_id, clean),
        )
        if taken:
            return {"outcome": "exists"}
    new_query = coll["query"] if query is None else (query or None)
    new_tag = coll["tag"] if tag is None else (tag or None)
    if (clean, new_query, new_tag) != (coll["name"], coll["query"], coll["tag"]):
        await db.execute(
            "UPDATE collections SET name = ?, query = ?, tag = ?,"
            " updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?;",
            (clean, new_query, new_tag, coll["id"], user_id),
        )
    stored = {
        **coll,
        "name": clean,
        "query": new_query,
        "tag": new_tag,
    }
    return {
        "outcome": "renamed",
        "collection": {**stored, "kind": collection_kind(stored)},
    }


async def delete_collection(
    db: Database, user_id: str, collection: str
) -> dict[str, Any] | None:
    """Deletes a collection and its links. The items it held are untouched."""
    coll = await _resolve_collection(db, user_id, collection)
    if not coll:
        return None
    item_count = await _collection_item_count(db, user_id, coll["id"])
    await db.execute(
        "DELETE FROM collection_items WHERE collection_id IN"
        " (SELECT id FROM collections WHERE id = ? AND user_id = ?);",
        (coll["id"], user_id),
    )
    await db.execute(
        "DELETE FROM collections WHERE id = ? AND user_id = ?;", (coll["id"], user_id)
    )
    return {
        "collection": coll["name"],
        "deleted": True,
        "items_untouched": item_count,
    }
