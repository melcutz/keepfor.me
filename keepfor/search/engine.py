# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from keepfor.models.db import Database
from keepfor.search.vectors import VectorIndex
from keepfor.utils.logging import logger

if TYPE_CHECKING:
    from keepfor.spi import TenantScope

RRF_K = 60  # Standard RRF constant
# Bound on the embedding call and the vector query. Search must stay
# responsive even when Workers AI or Vectorize is slow; both fall back to
# keyword-only results on timeout.
VECTOR_SEARCH_TIMEOUT = 5.0

# UI status groups: the items table stores raw statuses ('queued', 'fetching',
# 'ok', 'failed'); the library pills group them for humans. 'fetching' is
# legacy (nothing writes it) but counted with 'queued' defensively.
STATUS_GROUPS = {
    "extracting": ("queued", "fetching"),
    "saved": ("ok", "saved"),
    "failed": ("failed",),
}


def _status_values(status: str | None) -> tuple[str, ...] | None:
    """Map a UI status filter to raw item statuses, or None for no filter."""
    if not status or status == "all":
        return None
    return STATUS_GROUPS.get(status)


UNTAGGED_SENTINEL = "__untagged__"
MAX_TAG_FILTERS = 10
REFERENCE_TAGS = ("tools", "docs", "reference")


def _category_clause(category: str | None, alias: str = "i") -> tuple[str, tuple]:
    """Map a triage category tab to a SQL fragment (with params)."""
    if not category or category == "all":
        return "", ()
    a = alias
    if category == "notes":
        return f" AND {a}.item_type = 'note'", ()
    if category == "archive":
        return f" AND {a}.read_state = 'archived'", ()
    if category == "inbox":
        return (
            f" AND {a}.item_type = 'url' AND {a}.read_state = 'unread'",
            (),
        )
    if category == "reference":
        placeholders = ",".join("?" for _ in REFERENCE_TAGS)
        return (
            f" AND (EXISTS (SELECT 1 FROM item_tags it_ref JOIN tags t_ref"
            f" ON it_ref.tag_id = t_ref.id AND t_ref.user_id = {a}.user_id"
            f" WHERE it_ref.item_id = {a}.id"
            f" AND LOWER(t_ref.name) IN ({placeholders}))"
            f" OR LOWER({a}.canonical_url) LIKE '%github.com%'"
            f" OR LOWER({a}.canonical_url) LIKE '%docs.%'"
            f" OR LOWER({a}.canonical_url) LIKE '%api.%')",
            tuple(t for t in REFERENCE_TAGS),
        )
    return "", ()


RECENT_COLUMNS = (
    "i.id, i.url, i.canonical_url, i.title, i.byline, i.site_name,"
    " i.published_date, i.excerpt, i.status, i.fail_reason, i.is_fallback,"
    " i.word_count, i.created_at, i.item_type, i.is_pinned, i.user_notes,"
    " i.image_url, i.summary, i.read_state"
)
RECENT_COLUMNS_BARE = (
    "id, url, canonical_url, title, byline, site_name,"
    " published_date, excerpt, status, fail_reason, is_fallback,"
    " word_count, created_at, item_type, is_pinned, user_notes,"
    " image_url, summary, read_state"
)


def parse_tag_filter(raw: str | None) -> tuple[list[str], bool]:
    """Split a comma-joined `?tag=` value into (tags, untagged).

    Lowercases, drops empties and duplicates, caps at MAX_TAG_FILTERS.
    The sentinel selects tagless items and is mutually exclusive with tags.
    """
    if not raw:
        return [], False
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    if UNTAGGED_SENTINEL in parts:
        return [], True
    seen: list[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return seen[:MAX_TAG_FILTERS], False


async def get_status_counts(
    db: Database,
    user_id: str,
    category: str | None = None,
    tags: list[str] | None = None,
    untagged: bool = False,
    quick: bool = False,
) -> dict[str, int]:
    """Per-group item counts for the library status pills (single query).

    Optional category/tags/quick scope the counts to the active library
    filter so the pills re-count within the current view. Deliberately
    NOT scoped by search text: that would multiply FTS/vector work per
    status group on every keystroke (query matches surface via the pager
    totals instead).
    """
    cat_clause, cat_params = _category_clause(category, "items")
    quick_clause = " AND word_count < 1000" if quick else ""
    tag_list = [t.lower() for t in (tags or [])][:MAX_TAG_FILTERS]
    if tag_list:
        exists = " ".join(
            f"AND EXISTS (SELECT 1 FROM item_tags it{i} JOIN tags t{i}"
            f" ON it{i}.tag_id = t{i}.id AND t{i}.user_id = items.user_id"
            f" WHERE it{i}.item_id = items.id"
            f" AND LOWER(t{i}.name) = LOWER(?))"
            for i in range(len(tag_list))
        )
        extra = f"{cat_clause}{quick_clause} {exists}"
        params: tuple = (user_id, *cat_params, *tag_list)
    elif untagged:
        extra = (
            f"{cat_clause}{quick_clause}"
            " AND NOT EXISTS (SELECT 1 FROM item_tags itx"
            " WHERE itx.item_id = items.id)"
        )
        params = (user_id, *cat_params)
    else:
        extra = f"{cat_clause}{quick_clause}"
        params = (user_id, *cat_params)

    rows = await db.query_all(
        f"SELECT status, COUNT(*) as count FROM items "
        f"WHERE user_id = ? {extra} GROUP BY status;",
        params,
    )
    raw = {r["status"]: r["count"] for r in rows}
    counts = {"all": 0, "saved": 0, "extracting": 0, "failed": 0}
    for group, values in STATUS_GROUPS.items():
        counts[group] = sum(raw.get(v, 0) for v in values)
    counts["all"] = sum(raw.values())
    return counts


async def search_fts(
    db: Database, user_id: str, query: str, limit: int = 50
) -> list[dict[str, Any]]:
    """Runs FTS5 keyword query returning ranked item_ids with snippets."""
    url_matches: list[dict[str, Any]] = []
    if "." in query or "/" in query or ":" in query:
        url_sql = """
            SELECT id as item_id, -100.0 as rank, excerpt as snippet
            FROM items
            WHERE user_id = ? AND (canonical_url LIKE ? OR url LIKE ?)
            LIMIT ?;
        """
        clean_domain = query.strip()
        like_term = f"%{clean_domain}%"
        try:
            url_matches = await db.query_all(
                url_sql, (user_id, like_term, like_term, limit)
            )
        except Exception as exc:
            logger.warning("URL search fallback failed: %s", exc)
            url_matches = []

    # Escape special FTS characters
    clean_q = "".join(c for c in query if c.isalnum() or c.isspace()).strip()
    fts_matches: list[dict[str, Any]] = []
    if clean_q:
        # Format for prefix matching: word1* word2*
        fts_terms = " ".join(f'"{term}"*' for term in clean_q.split() if term)
        if fts_terms:
            sql = """
                SELECT item_id, rank,
                       snippet(items_fts, 3, '<mark>', '</mark>', '...', 25) as snippet
                FROM items_fts
                WHERE items_fts MATCH ? AND user_id = ?
                ORDER BY rank
                LIMIT ?;
            """
            try:
                fts_matches = await db.query_all(sql, (fts_terms, user_id, limit))
            except Exception:
                # Fallback to simple title/url LIKE search if FTS query syntax error
                fallback_sql = """
                    SELECT id as item_id, 0.0 as rank, excerpt as snippet
                    FROM items
                    WHERE user_id = ? AND (title LIKE ? OR url LIKE ?)
                    LIMIT ?;
                """
                like_term = f"%{clean_q}%"
                fts_matches = await db.query_all(
                    fallback_sql, (user_id, like_term, like_term, limit)
                )

    seen = set()
    combined: list[dict[str, Any]] = []
    for r in url_matches + (fts_matches or []):
        if r["item_id"] not in seen:
            seen.add(r["item_id"])
            combined.append(r)
    return combined[:limit]


async def search_vectorize(
    env: Any,
    user_id: str,
    query: str,
    limit: int = 50,
    *,
    scope: TenantScope | None = None,
) -> list[dict[str, Any]]:
    """Generates embedding for query and searches Vectorize."""
    vectors = (
        scope.vectors(env)
        if scope is not None
        else VectorIndex(
            getattr(env, "VECTORIZE", None) if env else None, namespace=None
        )
    )
    if vectors.index is None:
        return []

    from keepfor.runtime import get_providers

    providers = get_providers()

    try:
        # Bound the embedding + vector query so a slow AI binding cannot hang
        # search; on timeout we degrade to FTS-only results instead of erroring.
        embeddings = await asyncio.wait_for(
            providers.ai.embed(env, [query]),
            timeout=VECTOR_SEARCH_TIMEOUT,
        )
        if not embeddings:
            return []
        query_vector = embeddings[0]

        # Vectorize query with metadata requested (max topK is 50 when
        # returnMetadata is 'all').
        query_top_k = min(limit, 50)
        matches = await asyncio.wait_for(
            vectors.query(query_vector, query_top_k),
            timeout=VECTOR_SEARCH_TIMEOUT,
        )

        # Deduplicate matches by item_id, keeping highest score
        item_scores: dict[str, float] = {}
        for m in matches:
            meta = m.get("metadata") or {}
            match_user_id = meta.get("user_id")
            if match_user_id and match_user_id != user_id:
                if scope and scope.vector_namespace:
                    logger.warning(
                        "Foreign match in namespaced vector query: "
                        "match_id=%s, match_user=%s, expected_user=%s",
                        m.get("id"),
                        match_user_id,
                        user_id,
                    )
                continue

            item_id = meta.get("item_id")
            if not item_id:
                chunk_id = m.get("id", "")
                if chunk_id.startswith("item_") and "_chunk_" in chunk_id:
                    item_id = chunk_id[len("item_") : chunk_id.rfind("_chunk_")]

            score = m.get("score", 0.0)
            if item_id:
                if item_id not in item_scores or score > item_scores[item_id]:
                    item_scores[item_id] = score

        if matches and not item_scores:
            logger.warning(
                "Vector search returned %d matches but could not extract any item IDs",
                len(matches),
            )

        # Return sorted by score descending
        sorted_items = sorted(item_scores.items(), key=lambda x: x[1], reverse=True)
        return [{"item_id": item_id, "score": score} for item_id, score in sorted_items]
    except Exception as exc:
        logger.warning("Vector search query failed: %s", exc, exc_info=True)
        return []


async def hybrid_search(
    db: Database,
    env: Any,
    user_id: str,
    query: str,
    mode: str = "hybrid",
    tag: str | None = None,
    tags: list[str] | None = None,
    untagged: bool = False,
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
    category: str | None = None,
    quick: bool = False,
    *,
    scope: TenantScope | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Execute hybrid search with RRF fusion, filters, and pagination.

    Returns (page_items, total_matches). total is exact for browse, and
    the ranked-candidate count (~100 max) for text queries.
    """
    if scope is None and user_id:
        from keepfor.defaults import default_scope

        scope = default_scope(db, env, user_id)

    query = query.strip()
    tag_list = [t.lower() for t in (list(tags) if tags else ([tag] if tag else []))][
        :MAX_TAG_FILTERS
    ]
    if not query:
        # Fetch recent items and total count concurrently
        items, total = await asyncio.gather(
            get_recent_items(
                db,
                user_id,
                tag=tag,
                tags=tag_list,
                untagged=untagged,
                limit=limit,
                offset=offset,
                status=status,
                category=category,
                quick=quick,
            ),
            count_recent_items(
                db,
                user_id,
                tag=tag,
                tags=tag_list,
                untagged=untagged,
                status=status,
                category=category,
                quick=quick,
            ),
        )
        return items, total

    fts_results: list[dict[str, Any]] = []
    vec_results: list[dict[str, Any]] = []

    # FTS (a D1 round-trip) and the vector path (an AI embedding call) are
    # independent, so run them concurrently: hybrid search was paying for
    # them back to back (~0.39s of the ~0.6s was the embedding).
    want_fts = mode in ("hybrid", "keyword")
    want_vec = mode in ("hybrid", "semantic") and env is not None
    if want_fts and want_vec:
        fts_task = asyncio.create_task(search_fts(db, user_id, query, limit=50))
        vec_task = asyncio.create_task(
            search_vectorize(env, user_id, query, limit=50, scope=scope)
        )
        fts_results, vec_results = await asyncio.gather(fts_task, vec_task)
    elif want_fts:
        fts_results = await search_fts(db, user_id, query, limit=50)
    elif want_vec:
        vec_results = await search_vectorize(env, user_id, query, limit=50, scope=scope)

    # Reciprocal Rank Fusion
    rrf_scores: dict[str, float] = {}
    snippets: dict[str, str] = {}

    if mode == "keyword":
        for rank, r in enumerate(fts_results, start=1):
            item_id = r["item_id"]
            rrf_scores[item_id] = 1.0 / (RRF_K + rank)
            if r.get("snippet"):
                snippets[item_id] = r["snippet"]
    elif mode == "semantic":
        for rank, r in enumerate(vec_results, start=1):
            item_id = r["item_id"]
            rrf_scores[item_id] = 1.0 / (RRF_K + rank)
    else:  # hybrid
        for rank, r in enumerate(fts_results, start=1):
            item_id = r["item_id"]
            rrf_scores[item_id] = rrf_scores.get(item_id, 0.0) + (1.0 / (RRF_K + rank))
            if r.get("snippet"):
                snippets[item_id] = r["snippet"]

        for rank, r in enumerate(vec_results, start=1):
            item_id = r["item_id"]
            rrf_scores[item_id] = rrf_scores.get(item_id, 0.0) + (1.0 / (RRF_K + rank))

    if not rrf_scores:
        return [], 0

    # Sort item_ids by RRF score descending
    sorted_item_ids = [
        item_id
        for item_id, _ in sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
    ]
    top_ids = sorted_item_ids  # Full ranked window; sliced per page below

    # Fetch full item details from D1
    placeholders = ",".join("?" for _ in top_ids)
    status_values = _status_values(status)
    status_clause = ""
    status_params: tuple = ()
    if status_values:
        status_clause = f" AND i.status IN ({','.join('?' for _ in status_values)})"
        status_params = tuple(status_values)
    cat_clause, cat_params = _category_clause(category, "i")
    quick_clause = " AND i.word_count < 1000" if quick else ""
    sql = f"""
        SELECT {RECENT_COLUMNS}
        FROM items i
        WHERE i.id IN ({placeholders})
          AND i.user_id = ?{status_clause}{cat_clause}{quick_clause};
    """
    rows = await db.query_all(sql, (*top_ids, user_id, *status_params, *cat_params))
    item_map = {row["id"]: row for row in rows}

    # Fetch tags for these items
    tag_sql = f"""
        SELECT it.item_id, t.name as tag_name
        FROM item_tags it
        JOIN items i ON i.id = it.item_id AND i.user_id = ?
        JOIN tags t ON t.id = it.tag_id AND t.user_id = i.user_id
        WHERE it.item_id IN ({placeholders});
    """
    tag_rows = await db.query_all(tag_sql, (user_id, *top_ids))
    item_tags_map: dict[str, list[str]] = {}
    for tr in tag_rows:
        item_tags_map.setdefault(tr["item_id"], []).append(tr["tag_name"])

    # Assemble final list in RRF order
    final_items = []
    for item_id in sorted_item_ids:
        if item_id in item_map:
            item = dict(item_map[item_id])
            item_tags = item_tags_map.get(item_id, [])
            if tag_list:
                lower = {t.lower() for t in item_tags}
                if any(t not in lower for t in tag_list):
                    continue
            elif untagged and item_tags:
                continue
            item["tags"] = item_tags
            item["rrf_score"] = round(rrf_scores[item_id], 4)
            item["snippet"] = snippets.get(item_id) or item["excerpt"]
            final_items.append(item)

    return final_items[offset : offset + limit], len(final_items)


async def get_recent_items(
    db: Database,
    user_id: str,
    tag: str | None = None,
    tags: list[str] | None = None,
    untagged: bool = False,
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
    category: str | None = None,
    quick: bool = False,
) -> list[dict[str, Any]]:
    """Retrieves recent items for user with pagination and optional filters."""
    status_values = _status_values(status)
    status_clause = ""
    status_params: tuple = ()
    if status_values:
        status_clause = f" AND i.status IN ({','.join('?' for _ in status_values)})"
        status_params = tuple(status_values)
    cat_clause, cat_params = _category_clause(category, "i")
    quick_clause = " AND i.word_count < 1000" if quick else ""
    bare_status = status_clause.replace("i.status", "status")
    bare_cat, _ = _category_clause(category, "items")
    bare_cat_params = cat_params
    bare_quick = " AND word_count < 1000" if quick else ""
    tag_list = [t.lower() for t in (list(tags) if tags else ([tag] if tag else []))][
        :MAX_TAG_FILTERS
    ]
    if tag_list:
        exists = " ".join(
            "AND EXISTS (SELECT 1 FROM item_tags it%d JOIN tags t%d"
            " ON it%d.tag_id = t%d.id WHERE it%d.item_id = i.id"
            " AND LOWER(t%d.name) = LOWER(?))" % ((i,) * 6)
            for i in range(len(tag_list))
        )
        sql = f"""
        SELECT {RECENT_COLUMNS}
        FROM items i
        WHERE i.user_id = ?{status_clause}{cat_clause}{quick_clause} {exists}
        ORDER BY i.created_at DESC
        LIMIT ? OFFSET ?;
    """
        rows = await db.query_all(
            sql, (user_id, *status_params, *cat_params, *tag_list, limit, offset)
        )
    elif untagged:
        sql = f"""
            SELECT {RECENT_COLUMNS_BARE}
            FROM items
            WHERE user_id = ?{bare_status}{bare_cat}{bare_quick}
              AND NOT EXISTS (SELECT 1 FROM item_tags itx WHERE itx.item_id = items.id)
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?;
        """
        rows = await db.query_all(
            sql, (user_id, *status_params, *bare_cat_params, limit, offset)
        )
    else:
        sql = f"""
            SELECT {RECENT_COLUMNS_BARE}
            FROM items
            WHERE user_id = ?{bare_status}{bare_cat}{bare_quick}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?;
        """
        rows = await db.query_all(
            sql, (user_id, *status_params, *bare_cat_params, limit, offset)
        )

    if not rows:
        return []

    item_ids = [r["id"] for r in rows]
    placeholders = ",".join("?" for _ in item_ids)
    tag_sql = f"""
        SELECT it.item_id, t.name as tag_name
        FROM item_tags it
        JOIN items i ON i.id = it.item_id AND i.user_id = ?
        JOIN tags t ON t.id = it.tag_id AND t.user_id = i.user_id
        WHERE it.item_id IN ({placeholders});
    """
    tag_rows = await db.query_all(tag_sql, (user_id, *item_ids))
    item_tags_map: dict[str, list[str]] = {}
    for tr in tag_rows:
        item_tags_map.setdefault(tr["item_id"], []).append(tr["tag_name"])

    result = []
    for r in rows:
        item = dict(r)
        item["tags"] = item_tags_map.get(r["id"], [])
        result.append(item)
    return result


async def count_recent_items(
    db: Database,
    user_id: str,
    tag: str | None = None,
    tags: list[str] | None = None,
    untagged: bool = False,
    status: str | None = None,
    category: str | None = None,
    quick: bool = False,
) -> int:
    """Count items matching the browse filters (same WHERE as get_recent_items)."""
    status_values = _status_values(status)
    status_clause = ""
    status_params: tuple = ()
    if status_values:
        status_clause = f" AND status IN ({','.join('?' for _ in status_values)})"
        status_params = tuple(status_values)
    cat_clause, cat_params = _category_clause(category, "items")
    cat_clause_i, _ = _category_clause(category, "i")
    quick_clause = " AND word_count < 1000" if quick else ""
    quick_clause_i = " AND i.word_count < 1000" if quick else ""
    tag_list = [t.lower() for t in (list(tags) if tags else ([tag] if tag else []))][
        :MAX_TAG_FILTERS
    ]
    if tag_list:
        tag_clause = status_clause.replace("status", "i.status")
        exists = " ".join(
            f"AND EXISTS (SELECT 1 FROM item_tags it{i} JOIN tags t{i}"
            f" ON it{i}.tag_id = t{i}.id AND t{i}.user_id = i.user_id"
            f" WHERE it{i}.item_id = i.id"
            f" AND LOWER(t{i}.name) = LOWER(?))"
            for i in range(len(tag_list))
        )
        rows = await db.query_all(
            "SELECT COUNT(*) as count FROM items i "
            f"WHERE i.user_id = ?{tag_clause}{cat_clause_i}{quick_clause_i} {exists};",
            (user_id, *status_params, *cat_params, *tag_list),
        )
    elif untagged:
        rows = await db.query_all(
            "SELECT COUNT(*) as count FROM items "
            f"WHERE user_id = ?{status_clause}{cat_clause}{quick_clause} "
            "AND NOT EXISTS (SELECT 1 FROM item_tags itx "
            "WHERE itx.item_id = items.id);",
            (user_id, *status_params, *cat_params),
        )
    else:
        rows = await db.query_all(
            f"SELECT COUNT(*) as count FROM items"
            f" WHERE user_id = ?{status_clause}{cat_clause}{quick_clause};",
            (user_id, *status_params, *cat_params),
        )
    return rows[0]["count"] if rows else 0
