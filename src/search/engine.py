import asyncio
from typing import Any

from src.models.db import Database

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
    "saved": ("ok",),
    "failed": ("failed",),
}


def _status_values(status: str | None) -> tuple[str, ...] | None:
    """Map a UI status filter to raw item statuses, or None for no filter."""
    if not status or status == "all":
        return None
    return STATUS_GROUPS.get(status)


UNTAGGED_SENTINEL = "__untagged__"
MAX_TAG_FILTERS = 10


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


async def get_status_counts(db: Database, user_id: str) -> dict[str, int]:
    """Per-group item counts for the library status pills (single query)."""
    rows = await db.query_all(
        "SELECT status, COUNT(*) as count FROM items WHERE user_id = ?"
        " GROUP BY status;",
        (user_id,),
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
    # Escape special FTS characters
    clean_q = "".join(c for c in query if c.isalnum() or c.isspace()).strip()
    if not clean_q:
        return []

    # Format for prefix matching: word1* word2*
    fts_terms = " ".join(f'"{term}"*' for term in clean_q.split() if term)
    if not fts_terms:
        return []

    sql = """
        SELECT item_id, rank,
               snippet(items_fts, 3, '<mark>', '</mark>', '...', 25) as snippet
        FROM items_fts
        WHERE items_fts MATCH ? AND user_id = ?
        ORDER BY rank
        LIMIT ?;
    """
    try:
        return await db.query_all(sql, (fts_terms, user_id, limit))
    except Exception:
        # Fallback to simple title/url LIKE search if FTS query syntax error
        fallback_sql = """
            SELECT id as item_id, 0.0 as rank, excerpt as snippet
            FROM items
            WHERE user_id = ? AND (title LIKE ? OR url LIKE ?)
            LIMIT ?;
        """
        like_term = f"%{clean_q}%"
        return await db.query_all(fallback_sql, (user_id, like_term, like_term, limit))


async def search_vectorize(
    env: Any, user_id: str, query: str, limit: int = 50
) -> list[dict[str, Any]]:
    """Generates embedding for query and searches Vectorize."""
    if (
        not hasattr(env, "AI")
        or not hasattr(env, "VECTORIZE")
        or env.AI is None
        or env.VECTORIZE is None
    ):
        return []

    try:
        # Bound the embedding + vector query so a slow AI binding cannot hang
        # search; on timeout we degrade to FTS-only results instead of erroring.
        ai_res = await asyncio.wait_for(
            env.AI.run("@cf/baai/bge-base-en-v1.5", {"text": [query]}),
            timeout=VECTOR_SEARCH_TIMEOUT,
        )
        raw_data = getattr(ai_res, "data", ai_res)
        if hasattr(raw_data, "to_py"):
            raw_data = raw_data.to_py()
        embeddings = (
            raw_data.get("data", raw_data) if isinstance(raw_data, dict) else raw_data
        )
        if not embeddings:
            return []
        query_vector = embeddings[0]

        # Vectorize query
        vec_res = await asyncio.wait_for(
            env.VECTORIZE.query(
                query_vector, {"topK": limit, "filter": {"user_id": user_id}}
            ),
            timeout=VECTOR_SEARCH_TIMEOUT,
        )
        matches = getattr(vec_res, "matches", [])
        if hasattr(matches, "to_py"):
            matches = matches.to_py()

        # Deduplicate matches by item_id, keeping highest score
        item_scores: dict[str, float] = {}
        for m in matches:
            meta = m.get("metadata", {})
            item_id = meta.get("item_id")
            score = m.get("score", 0.0)
            if item_id:
                if item_id not in item_scores or score > item_scores[item_id]:
                    item_scores[item_id] = score

        # Return sorted by score descending
        sorted_items = sorted(item_scores.items(), key=lambda x: x[1], reverse=True)
        return [{"item_id": item_id, "score": score} for item_id, score in sorted_items]
    except Exception:
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
) -> tuple[list[dict[str, Any]], int]:
    """Execute hybrid search with RRF fusion, filters, and pagination.

    Returns (page_items, total_matches). total is exact for browse, and
    the ranked-candidate count (~100 max) for text queries.
    """
    query = query.strip()
    tag_list = [t.lower() for t in (list(tags) if tags else ([tag] if tag else []))][
        :MAX_TAG_FILTERS
    ]
    if not query:
        # Return recent items
        items = await get_recent_items(
            db,
            user_id,
            tag=tag,
            tags=tag_list,
            untagged=untagged,
            limit=limit,
            offset=offset,
            status=status,
        )
        total = await count_recent_items(
            db, user_id, tag=tag, tags=tag_list, untagged=untagged, status=status
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
        vec_task = asyncio.create_task(search_vectorize(env, user_id, query, limit=50))
        fts_results, vec_results = await asyncio.gather(fts_task, vec_task)
    elif want_fts:
        fts_results = await search_fts(db, user_id, query, limit=50)
    elif want_vec:
        vec_results = await search_vectorize(env, user_id, query, limit=50)

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
    sql = f"""
        SELECT i.id, i.url, i.canonical_url, i.title, i.byline, i.site_name,
               i.published_date, i.excerpt, i.status, i.fail_reason, i.is_fallback,
               i.word_count, i.created_at
        FROM items i
        WHERE i.id IN ({placeholders}) AND i.user_id = ?{status_clause};
    """
    rows = await db.query_all(sql, (*top_ids, user_id, *status_params))
    item_map = {row["id"]: row for row in rows}

    # Fetch tags for these items
    tag_sql = f"""
        SELECT it.item_id, t.name as tag_name
        FROM item_tags it
        JOIN tags t ON it.tag_id = t.id
        WHERE it.item_id IN ({placeholders});
    """
    tag_rows = await db.query_all(tag_sql, tuple(top_ids))
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
) -> list[dict[str, Any]]:
    """Retrieves recent items for user with pagination and optional filters."""
    status_values = _status_values(status)
    status_clause = ""
    status_params: tuple = ()
    if status_values:
        status_clause = f" AND i.status IN ({','.join('?' for _ in status_values)})"
        status_params = tuple(status_values)
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
        SELECT i.id, i.url, i.canonical_url, i.title, i.byline, i.site_name,
               i.published_date, i.excerpt, i.status, i.fail_reason, i.is_fallback,
               i.word_count, i.created_at
        FROM items i
        WHERE i.user_id = ?{status_clause} {exists}
        ORDER BY i.created_at DESC
        LIMIT ? OFFSET ?;
    """
        rows = await db.query_all(
            sql, (user_id, *status_params, *tag_list, limit, offset)
        )
    elif untagged:
        sql = f"""
            SELECT id, url, canonical_url, title, byline, site_name,
                   published_date, excerpt, status, fail_reason, is_fallback,
                   word_count, created_at
            FROM items
            WHERE user_id = ?{status_clause.replace("i.status", "status")}
              AND NOT EXISTS (SELECT 1 FROM item_tags itx WHERE itx.item_id = items.id)
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?;
        """
        rows = await db.query_all(sql, (user_id, *status_params, limit, offset))
    else:
        sql = f"""
            SELECT id, url, canonical_url, title, byline, site_name,
                   published_date, excerpt, status, fail_reason, is_fallback,
                   word_count, created_at
            FROM items
            WHERE user_id = ?{status_clause.replace("i.status", "status")}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?;
        """
        rows = await db.query_all(sql, (user_id, *status_params, limit, offset))

    if not rows:
        return []

    item_ids = [r["id"] for r in rows]
    placeholders = ",".join("?" for _ in item_ids)
    tag_sql = f"""
        SELECT it.item_id, t.name as tag_name
        FROM item_tags it
        JOIN tags t ON it.tag_id = t.id
        WHERE it.item_id IN ({placeholders});
    """
    tag_rows = await db.query_all(tag_sql, tuple(item_ids))
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
) -> int:
    """Count items matching the browse filters (same WHERE as get_recent_items)."""
    status_values = _status_values(status)
    status_clause = ""
    status_params: tuple = ()
    if status_values:
        status_clause = f" AND status IN ({','.join('?' for _ in status_values)})"
        status_params = tuple(status_values)
    tag_list = [t.lower() for t in (list(tags) if tags else ([tag] if tag else []))][
        :MAX_TAG_FILTERS
    ]
    if tag_list:
        tag_clause = status_clause.replace("status", "i.status")
        exists = " ".join(
            "AND EXISTS (SELECT 1 FROM item_tags it%d JOIN tags t%d"
            " ON it%d.tag_id = t%d.id WHERE it%d.item_id = i.id"
            " AND LOWER(t%d.name) = LOWER(?))" % ((i,) * 6)
            for i in range(len(tag_list))
        )
        rows = await db.query_all(
            "SELECT COUNT(*) as count FROM items i "
            f"WHERE i.user_id = ?{tag_clause} {exists};",
            (user_id, *status_params, *tag_list),
        )
    elif untagged:
        rows = await db.query_all(
            "SELECT COUNT(*) as count FROM items "
            f"WHERE user_id = ?{status_clause} "
            "AND NOT EXISTS (SELECT 1 FROM item_tags itx "
            "WHERE itx.item_id = items.id);",
            (user_id, *status_params),
        )
    else:
        rows = await db.query_all(
            f"SELECT COUNT(*) as count FROM items WHERE user_id = ?{status_clause};",
            (user_id, *status_params),
        )
    return rows[0]["count"] if rows else 0
