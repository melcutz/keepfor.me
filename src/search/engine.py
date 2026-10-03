import asyncio
from typing import Any
from src.models.db import Database

RRF_K = 60  # Standard RRF constant

async def search_fts(db: Database, user_id: str, query: str, limit: int = 50) -> list[dict[str, Any]]:
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
        SELECT item_id, rank, snippet(items_fts, 3, '<mark>', '</mark>', '...', 25) as snippet
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

async def search_vectorize(env: Any, user_id: str, query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Generates embedding for query and searches Vectorize."""
    if not hasattr(env, "AI") or not hasattr(env, "VECTORIZE") or env.AI is None or env.VECTORIZE is None:
        return []

    try:
        ai_res = await env.AI.run("@cf/baai/bge-base-en-v1.5", {"text": [query]})
        raw_data = getattr(ai_res, "data", ai_res)
        if hasattr(raw_data, "to_py"):
            raw_data = raw_data.to_py()
        embeddings = raw_data.get("data", raw_data) if isinstance(raw_data, dict) else raw_data
        if not embeddings:
            return []
        query_vector = embeddings[0]

        # Vectorize query
        vec_res = await env.VECTORIZE.query(query_vector, {
            "topK": limit,
            "filter": {"user_id": user_id}
        })
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
    limit: int = 20
) -> list[dict[str, Any]]:
    """Executes hybrid search with Reciprocal Rank Fusion, with mode and tag filtering."""
    query = query.strip()
    if not query:
        # Return recent items
        return await get_recent_items(db, user_id, tag=tag, limit=limit)

    fts_results: list[dict[str, Any]] = []
    vec_results: list[dict[str, Any]] = []

    if mode in ("hybrid", "keyword"):
        fts_results = await search_fts(db, user_id, query, limit=50)

    if mode in ("hybrid", "semantic") and env is not None:
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
        return []

    # Sort item_ids by RRF score descending
    sorted_item_ids = [item_id for item_id, _ in sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)]
    top_ids = sorted_item_ids[:limit * 2]  # Fetch extra in case tag filter drops some

    # Fetch full item details from D1
    placeholders = ",".join("?" for _ in top_ids)
    sql = f"""
        SELECT i.id, i.url, i.canonical_url, i.title, i.byline, i.site_name, 
               i.published_date, i.excerpt, i.status, i.is_fallback, i.word_count, i.created_at
        FROM items i
        WHERE i.id IN ({placeholders}) AND i.user_id = ?;
    """
    rows = await db.query_all(sql, (*top_ids, user_id))
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
            if tag and tag.lower() not in [t.lower() for t in item_tags]:
                continue
            item["tags"] = item_tags
            item["rrf_score"] = round(rrf_scores[item_id], 4)
            item["snippet"] = snippets.get(item_id) or item["excerpt"]
            final_items.append(item)
            if len(final_items) >= limit:
                break

    return final_items

async def get_recent_items(db: Database, user_id: str, tag: str | None = None, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    """Retrieves recent items for user with pagination and optional tag filtering."""
    if tag:
        sql = """
            SELECT i.id, i.url, i.canonical_url, i.title, i.byline, i.site_name,
                   i.published_date, i.excerpt, i.status, i.is_fallback, i.word_count, i.created_at
            FROM items i
            JOIN item_tags it ON i.id = it.item_id
            JOIN tags t ON it.tag_id = t.id
            WHERE i.user_id = ? AND LOWER(t.name) = LOWER(?)
            ORDER BY i.created_at DESC
            LIMIT ? OFFSET ?;
        """
        rows = await db.query_all(sql, (user_id, tag, limit, offset))
    else:
        sql = """
            SELECT id, url, canonical_url, title, byline, site_name,
                   published_date, excerpt, status, is_fallback, word_count, created_at
            FROM items
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?;
        """
        rows = await db.query_all(sql, (user_id, limit, offset))

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
