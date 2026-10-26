# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import asyncio
import datetime
import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

from keepfor.models.db import Database
from keepfor.models.tags import expand_tag_names, tag_filter_clause
from keepfor.search.vectors import VectorIndex
from keepfor.utils.logging import logger
from keepfor.utils.url import canonicalize_url

if TYPE_CHECKING:
    from keepfor.spi import TenantScope

RRF_K = 60
# Default max_chars budget for search_passages
DEFAULT_MAX_CHARS = 4000
# Default max_tokens for read_item
DEFAULT_MAX_TOKENS = 2000  # Standard RRF constant
# Bound on the embedding call and the vector query. Search must stay
# responsive even when Workers AI or Vectorize is slow; both fall back to
# keyword-only results on timeout.
VECTOR_SEARCH_TIMEOUT = 5.0
# Similarity floor for semantic hits. Vectorize is queried with
# metric=cosine, so `score` is the cosine SIMILARITY of the two
# embeddings (1.0 = identical), not a distance. Measured Oct 2026 with
# the production embedding model @cf/baai/bge-base-en-v1.5: on the QA
# tenant's 7-item library a nonsense query scored 0.587 ("zxqvbnm"),
# but on a real prod library the same nonsense query scores 0.727 while
# the real matches score 0.63-0.67. The baseline climbs with the library
# size, so no absolute floor separates noise from a match: the rule
# below needs a keyword hit or a strong near-top hit instead.
SEMANTIC_MIN_SIMILARITY = 0.62
# Floor for a semantic-only hit (no keyword match) that clusters with
# the top of the vector results. Short real queries ("work" 0.637) come
# back through the keyword path, not through this one.
HYBRID_SEMANTIC_ONLY_MIN_SIMILARITY = 0.68
# A semantic-only hit this strong is kept on its own.
SEMANTIC_ONLY_STRONG_SIMILARITY = 0.80
# How close to the top vector score a candidate must be to count as
# "near the top".
NEAR_TOP_WINDOW = 0.05

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
    if category == "read":
        return f" AND {a}.read_state = 'read'", ()
    if category == "unread":
        return f" AND {a}.read_state = 'unread'", ()
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
        f"WHERE user_id = ? AND deleted_at IS NULL {extra} GROUP BY status;",
        params,
    )
    raw = {r["status"]: r["count"] for r in rows}
    counts = {"all": 0, "saved": 0, "extracting": 0, "failed": 0}
    for group, values in STATUS_GROUPS.items():
        counts[group] = sum(raw.get(v, 0) for v in values)
    counts["all"] = sum(raw.values())
    return counts


# Tag names are matched on word boundaries, like the FTS tokenizer:
# "llm-tools" answers the query "llm", "email" does not answer "ai".
_TAG_SEPARATORS = ("-", "_", "/", ".")


def _tag_name_expr(alias: str = "t") -> str:
    """The lowercased tag name, its separators turned into spaces.

    The tag names are matched on word boundaries and case-insensitively,
    like the FTS tokenizer.
    """
    name = f"LOWER({alias}.name)"
    for sep in _TAG_SEPARATORS:
        name = f"REPLACE({name}, '{sep}', ' ')"
    return name


# The words of a query that name no content: the function words of the
# language ("the", "in", "what") and the words of the library ("saved",
# "my", "library"). They are in the text of every item of a library
# ("a bookmark is a saved link to my favourite pages"), so a keyword
# hit on one of them proves nothing: "my saved zxqvbnm" used to keep
# unrelated semantic results because "saved" was a keyword (#63, ported
# from keepfor-cloud#142).
SEARCH_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "after",
        "all",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "before",
        "between",
        "but",
        "by",
        "can",
        "compare",
        "comparison",
        "could",
        "describe",
        "did",
        "difference",
        "do",
        "does",
        "during",
        "explain",
        "for",
        "from",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "like",
        "make",
        "many",
        "more",
        "most",
        "much",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "out",
        "over",
        "same",
        "shall",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "us",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)

# The words that name the library and not its content. They are in the
# text of every item of a library, so a keyword hit on one of them is
# no hit at all: "what have I saved about zxqvbnm?" names no content
# and the library holds no word of it.
LIBRARY_META_WORDS = frozenset(
    {
        "bookmark",
        "bookmarked",
        "bookmarks",
        "collection",
        "collections",
        "item",
        "items",
        "keep",
        "kept",
        "libraries",
        "library",
        "me",
        "mine",
        "my",
        "save",
        "saved",
        "saves",
        "saving",
        "show",
        "stored",
    }
)


def _query_terms(query: str) -> list[str]:
    """The lowercased terms of a query (the tokens the FTS matcher sees)."""
    clean = "".join(c for c in query if c.isalnum() or c.isspace()).strip()
    return [t.lower() for t in clean.split() if t]


def _content_words(terms: list[str]) -> list[str]:
    """The terms that name content (the stopwords and the library words do not)."""
    return [
        t for t in terms if t not in SEARCH_STOPWORDS and t not in LIBRARY_META_WORDS
    ]


def _content_terms(query: str) -> list[str]:
    """The terms of a query that name content (the terms of a keyword hit).

    The stopwords and the library words ("saved", "my", "library") name
    no content, so they are no keyword hit. A query made of these words
    only ("library") falls back to its raw words, so it still finds the
    item titled with it.
    """
    terms = _query_terms(query)
    return _content_words(terms) or terms


# A content word of at least this many letters matches every word that
# starts with it, so a word finds its inflected forms: "llm" finds
# "LLMs", "model" finds "models" (#76). The index stems with the porter
# tokenizer, but the tag names and the short words are matched here, so
# the rule is spelled out.
MIN_PREFIX_LEN = 3


def _keyword_match(term: str) -> str:
    """The FTS match expression of a content word.

    A word of at least MIN_PREFIX_LEN letters matches every word that
    starts with it ("llm" finds "LLMs"). A shorter word matches
    exactly: "ai" must not match "air".
    """
    if len(term) >= MIN_PREFIX_LEN:
        return f'"{term}"*'
    return f'"{term}"'


def _tag_word_match(term: str) -> str:
    """The SQL that matches a tag name against one word.

    A word of at least MIN_PREFIX_LEN letters matches every tag word
    that starts with it ("llm" answers the tag "llms"); a shorter word
    matches the whole tag word ("ai" does not answer "air"). The word
    is the parameter of the clause.
    """
    name = _tag_name_expr()
    if len(term) >= MIN_PREFIX_LEN:
        return f"instr(' ' || {name} || ' ', ' ' || ?) > 0"
    return f"instr(' ' || {name} || ' ', ' ' || ? || ' ') > 0"


def _stems_for_term(term: str) -> set[str]:
    """Derives root stems for prefix matching of inflections."""
    stems = {term}
    if len(term) > 5 and term.endswith("ing"):
        stems.add(term[:-3])
        if len(term) > 6 and term[-4] == term[-5]:
            stems.add(term[:-4])
    if len(term) > 4 and term.endswith("ed"):
        stems.add(term[:-2])
        if len(term) > 5 and term[-3] == term[-4]:
            stems.add(term[:-3])
    if len(term) > 4 and term.endswith("ies"):
        stems.add(term[:-3] + "y")
        stems.add(term[:-3])
    if len(term) > 4 and term.endswith("es"):
        stems.add(term[:-2])
    if len(term) > 3 and term.endswith("s"):
        stems.add(term[:-1])
    return {s for s in stems if len(s) >= MIN_PREFIX_LEN}


def highlight_query_terms(text: str, terms: list[str]) -> str:
    """Highlights all query terms, stems and inflections with <mark> tags (#97).

    Preserves existing <mark> tags without nesting. Short words (< 3 chars)
    match whole words only; words >= 3 chars match their inflections and
    stems consistent with #76.
    """
    if not text or not terms:
        return text or ""
    patterns = []
    for t in terms:
        t_clean = "".join(c for c in t.strip().lower() if c.isalnum())
        if not t_clean:
            continue
        if len(t_clean) < MIN_PREFIX_LEN:
            patterns.append(r"\b" + re.escape(t_clean) + r"\b")
        else:
            stems = _stems_for_term(t_clean)
            if not stems:
                patterns.append(r"\b" + re.escape(t_clean) + r"[a-zA-Z0-9]*\b")
            else:
                min_stem = min(stems, key=len)
                patterns.append(r"\b" + re.escape(min_stem) + r"[a-zA-Z0-9]*\b")
    if not patterns:
        return text
    combined = re.compile("|".join(dict.fromkeys(patterns)), re.IGNORECASE)
    parts = re.split(r"(<mark>.*?</mark>)", text, flags=re.IGNORECASE | re.DOTALL)
    out = []
    for part in parts:
        if part.lower().startswith("<mark>") and part.lower().endswith("</mark>"):
            out.append(part)
        else:
            out.append(combined.sub(r"<mark>\g<0></mark>", part))
    return "".join(out)


async def search_tag_hits(
    db: Database, user_id: str, query: str, limit: int = 50
) -> list[dict[str, Any]]:
    """Items whose tags name one of the query terms (a keyword hit).

    FTS5 indexes the title and the extracted text only, so an item that
    names the concept in a tag ("llm") had no keyword hit and the
    hybrid rule dropped it (#55). Tag names are matched on word
    boundaries and case-insensitively, like the FTS tokenizer.

    Only the content words of the query count (#63): a tag named "saved"
    is no proof that the item is about what the query asks for.

    A tag word that starts with a content word answers it, so the tag
    "llms" answers "llm" (#76). A short word matches the whole tag word
    ("ai" does not answer "air").

    The join order is pinned to the tags: without it SQLite scans the
    live items of the user first (57 ms on a 2k-item library against
    0.1 ms with the tag index).
    """
    terms = _content_terms(query)
    if not terms:
        return []
    match = " OR ".join(_tag_word_match(term) for term in terms)
    sql = f"""
        SELECT it.item_id AS item_id
        FROM tags t
        JOIN item_tags it INDEXED BY idx_item_tags_tag ON it.tag_id = t.id
        JOIN items i ON i.id = it.item_id
        WHERE t.user_id = ? AND i.user_id = ? AND i.deleted_at IS NULL
          AND ({match})
        LIMIT ?;
    """
    try:
        rows = await db.query_all(sql, (user_id, user_id, *terms, limit))
    except Exception as exc:
        logger.warning("Tag keyword search failed: %s", exc)
        return []
    return [{"item_id": r["item_id"], "rank": -1.0} for r in rows]


async def _word_hits(
    db: Database, user_id: str, term: str, limit: int
) -> list[dict[str, Any]]:
    """The items that hold one content word: in the text, the title or a tag.

    The text rows carry the bm25 rank and the snippet of the match; a tag
    row carries no rank: a tag names the word, it does not hold the text
    of it (#55).

    A content word matches the words that start with it ("llm" finds
    "LLMs"); a short word matches exactly ("ai" does not match "air").

    The tag join order is pinned to the tags: without it SQLite scans the
    live items of the user first (57 ms on a 2k-item library against
    0.1 ms with the tag index).
    """
    tag_match = _tag_word_match(term)
    sql = f"""
        SELECT item_id, rank, snippet FROM (
            SELECT item_id, rank,
                   snippet(items_fts, 3, '<mark>', '</mark>', '...', 25) as snippet
            FROM items_fts
            WHERE items_fts MATCH ? AND user_id = ?
              AND EXISTS (SELECT 1 FROM items i
                          WHERE i.id = items_fts.item_id AND i.user_id = ?
                            AND i.deleted_at IS NULL)
            ORDER BY rank
            LIMIT ?
        )
        UNION ALL
        SELECT item_id, NULL as rank, NULL as snippet FROM (
            SELECT it.item_id
            FROM tags t
            JOIN item_tags it INDEXED BY idx_item_tags_tag ON it.tag_id = t.id
            JOIN items i ON i.id = it.item_id
            WHERE t.user_id = ? AND i.user_id = ? AND i.deleted_at IS NULL
              AND ({tag_match})
            LIMIT ?);
    """
    rows = await db.query_all(
        sql,
        (
            _keyword_match(term),
            user_id,
            user_id,
            limit,
            user_id,
            user_id,
            term,
            limit,
        ),
    )
    return rows


async def search_fts(
    db: Database, user_id: str, query: str, limit: int = 50
) -> list[dict[str, Any]]:
    """Runs FTS5 keyword query returning ranked item_ids with snippets.

    The keyword side of the search: the title and the extracted text
    (FTS5), the url (LIKE) and the tags (#55). `limit` caps the
    candidates per source.

    A content word matches the words that start with it ("llm" finds
    "LLMs"). The items that hold every content word of the query come
    first; the items that hold only some of them follow them, the ones
    that hold more of them first (#87).
    """
    url_matches: list[dict[str, Any]] = []
    if "." in query or "/" in query or ":" in query:
        url_sql = """
            SELECT id as item_id, -100.0 as rank, excerpt as snippet
            FROM items
            WHERE user_id = ? AND deleted_at IS NULL
              AND (canonical_url LIKE ? OR url LIKE ?)
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
    # Only the content words of the query are matched (#63): the library
    # words ("saved", "my") are in the text of every item of a library,
    # so they prove nothing about the item. A content word matches the
    # words that start with it, so "llm" finds "LLMs" (#76).
    raw_terms = _query_terms(query)
    content = _content_words(raw_terms)
    # A word that is named twice counts once: "rust rust" holds one word.
    terms = list(dict.fromkeys(content or raw_terms))
    if not terms:
        return url_matches[:limit]

    # The matches of every content word, in the text, the title and the
    # tags. The words are matched one by one: the number of words an item
    # holds decides its place in the results (#87).
    fallback_rows: list[dict[str, Any]] = []
    try:
        hits = await asyncio.gather(
            *[_word_hits(db, user_id, term, limit) for term in terms]
        )
    except Exception:
        # Fallback to simple title/url LIKE search if FTS query syntax error
        fallback_sql = """
            SELECT id as item_id, 0.0 as rank, excerpt as snippet
            FROM items
            WHERE user_id = ? AND deleted_at IS NULL
              AND (title LIKE ? OR url LIKE ?)
            LIMIT ?;
        """
        like_term = f"%{clean_q}%"
        fallback_rows = await db.query_all(
            fallback_sql, (user_id, like_term, like_term, limit)
        )
        hits = [[] for _ in terms]

    counts: dict[str, int] = {}
    scores: dict[str, float] = {}
    best: dict[str, float] = {}
    snippets: dict[str, str] = {}
    tag_ids: list[str] = []
    matched: list[set[str]] = []
    for rows in hits:
        ids: set[str] = set()
        for r in rows:
            item_id = r["item_id"]
            ids.add(item_id)
            rank = r.get("rank")
            if rank is None:
                # A tag names the word, it does not hold the text of it.
                tag_ids.append(item_id)
                continue
            # The bm25 score of a word adds up: the score of the whole
            # query is the sum of the scores of its words.
            scores[item_id] = scores.get(item_id, 0.0) + rank
            snippet = r.get("snippet")
            if snippet and (item_id not in best or rank < best[item_id]):
                best[item_id] = rank
                snippets[item_id] = snippet
        matched.append(ids)

    # When multiple query terms exist, fetch multi-term snippets so SQLite
    # FTS5 selects a window containing as many matched terms as possible (#97).
    if len(terms) > 1 and scores:
        match_all = " OR ".join(_keyword_match(t) for t in terms)
        item_ids_list = list(scores.keys())
        placeholders = ",".join("?" for _ in item_ids_list)
        multi_sql = f"""
            SELECT item_id,
                   snippet(items_fts, 3, '<mark>', '</mark>', '...', 25) as snippet
            FROM items_fts
            WHERE items_fts MATCH ? AND user_id = ?
              AND item_id IN ({placeholders});
        """
        try:
            multi_rows = await db.query_all(
                multi_sql, (match_all, user_id, *item_ids_list)
            )
            for mr in multi_rows:
                if mr.get("snippet"):
                    snippets[mr["item_id"]] = mr["snippet"]
        except Exception as exc:
            logger.warning("Multi-term snippet fetch failed: %s", exc)

    for item_id, snip in list(snippets.items()):
        snippets[item_id] = highlight_query_terms(snip, terms)

    # Only the content words count towards a partial match: the library
    # words ("saved", "my") are in the text of every item of a library,
    # so they prove nothing (#63, #87).
    if content:
        for ids in matched:
            for item_id in ids:
                counts[item_id] = counts.get(item_id, 0) + 1

    candidates: list[tuple[dict[str, Any], int]] = []
    seen: set[str] = set()

    def _add(row: dict[str, Any], count: int) -> None:
        if row["item_id"] in seen:
            return
        seen.add(row["item_id"])
        candidates.append((row, count))

    # The tag hits come before the text matches: they are the keyword
    # hits the hybrid rule needs to keep a real hit (#55), so they win
    # the tie with a text match that holds as many words as they do.
    for item_id in tag_ids:
        _add(
            {"item_id": item_id, "rank": -1.0, "snippet": None}, counts.get(item_id, 0)
        )
    for item_id in sorted(scores, key=lambda i: scores[i]):
        _add(
            {
                "item_id": item_id,
                "rank": scores[item_id],
                "snippet": snippets.get(item_id),
            },
            counts.get(item_id, 0),
        )
    for r in fallback_rows:
        _add(r, len(terms))

    if not content and len(terms) > 1:
        # The query names no content ("my saved"): it matches as a whole,
        # like before. The items that hold every word are the matches; the
        # others only when none holds them all.
        strict = set.intersection(*matched)
        if strict:
            candidates = [c for c in candidates if c[0]["item_id"] in strict]

    # The url matches the whole query: they come first.
    combined: list[tuple[dict[str, Any], int]] = [
        (r, len(terms)) for r in url_matches
    ] + candidates

    out: list[tuple[dict[str, Any], int]] = []
    unique: set[str] = set()
    for row, count in combined:
        if row["item_id"] in unique:
            continue
        unique.add(row["item_id"])
        out.append((row, count))

    # The items that hold every content word come first, the items that
    # hold only some of them after them, the ones that hold more of them
    # first (#87). The sort is stable: inside a tier the order of the
    # sources holds.
    out.sort(key=lambda pair: -pair[1])
    return [row for row, _ in out[:limit]]


async def search_fts_whole_words(
    db: Database, user_id: str, query: str, limit: int = 50
) -> set[str]:
    """Ids whose title, text or tags name one of the query terms.

    FTS5 matches whole tokens (case-insensitive), so this is the keyword
    signal of the semantic-only mode, where no keyword ranking runs. A
    content word also matches the words that start with it, so "llm"
    finds "LLMs" (#76).

    Only the content words of the query count (#63): the library words
    ("saved", "my") are in the text of every item of a library, so they
    are no proof that the item is about what the query asks for.
    """
    terms = _content_terms(query)
    if not terms:
        return set()
    match = " OR ".join(_keyword_match(t) for t in terms)
    sql = """
        SELECT item_id FROM items_fts
        WHERE items_fts MATCH ? AND user_id = ?
          AND EXISTS (SELECT 1 FROM items i
                      WHERE i.id = items_fts.item_id AND i.user_id = ?
                        AND i.deleted_at IS NULL)
        LIMIT ?;
    """
    try:
        rows = await db.query_all(sql, (match, user_id, user_id, limit))
    except Exception as exc:
        logger.warning("Whole-word keyword search failed: %s", exc)
        return set()
    ids = {r["item_id"] for r in rows}
    # The tags count as a keyword hit as well (#55).
    ids.update(
        r["item_id"] for r in await search_tag_hits(db, user_id, query, limit=limit)
    )
    return ids


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


async def _drop_trashed(db: Database, user_id: str, item_ids: list[str]) -> list[str]:
    """Keeps only the ids of items that are not in the Trash.

    Trashed items keep their vectors, so a vector query still finds them;
    they must not reach the results.
    """
    if not item_ids:
        return []
    placeholders = ",".join("?" for _ in item_ids)
    rows = await db.query_all(
        f"SELECT id FROM items WHERE user_id = ? AND deleted_at IS NULL"
        f" AND id IN ({placeholders});",
        (user_id, *item_ids),
    )
    live = {r["id"] for r in rows}
    return [i for i in item_ids if i in live]


def _keep_vector_hit(
    score: float,
    *,
    keyword_hit: bool,
    has_keyword_hits: bool,
    top_score: float,
) -> bool:
    """Decides whether a vector candidate is a result or just a neighbour.

    A keyword hit (an FTS match in hybrid mode, a whole query term in the
    title or text in semantic-only mode) proves relevance and is always
    kept. A semantic-only hit has to be strong
    (SEMANTIC_ONLY_STRONG_SIMILARITY) and near the top of the vector
    results, or - when the query did match something by keyword - close
    to that top (NEAR_TOP_WINDOW) and above
    HYBRID_SEMANTIC_ONLY_MIN_SIMILARITY.

    The keyword guard is what drops a nonsense query: it produces no
    keyword hit, so a cluster of near-top scores cannot vouch for it.
    """
    if keyword_hit:
        return True
    near_top = score >= top_score - NEAR_TOP_WINDOW
    if score >= SEMANTIC_ONLY_STRONG_SIMILARITY:
        return near_top
    return (
        has_keyword_hits and score >= HYBRID_SEMANTIC_ONLY_MIN_SIMILARITY and near_top
    )


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
    tags_all: list[str] | None = None,
    tags_any: list[str] | None = None,
    tags_none: list[str] | None = None,
    read_state: str | None = None,
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
    # Child tags count: a filter on "work" also matches "work/research".
    all_sets: dict[str, set[str]] = {}
    any_sets: dict[str, set[str]] = {}
    none_sets: dict[str, set[str]] = {}
    expand_tasks = []

    async def _expand(tag_list, target_dict):
        if tag_list:
            target_dict.update(await expand_tag_names(db, user_id, tag_list))

    if tags_all:
        expand_tasks.append(_expand(tags_all, all_sets))
    if tags_any:
        expand_tasks.append(_expand(tags_any, any_sets))
    if tags_none:
        expand_tasks.append(_expand(tags_none, none_sets))

    if expand_tasks:
        await asyncio.gather(*expand_tasks)

    any_names = {n for names in any_sets.values() for n in names}
    none_names = {n for names in none_sets.values() for n in names}
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

    # Trashed items keep their vectors: drop them before they take a slot
    # in the fused ranking.
    if vec_results:
        live = set(
            await _drop_trashed(db, user_id, [r["item_id"] for r in vec_results])
        )
        vec_results = [r for r in vec_results if r["item_id"] in live]

    # Relevance filter on the vector scores only (the FTS/RRF scores are
    # untouched): a candidate that is neither a keyword hit nor a strong
    # near-top hit is noise, not a match. Without it a nonsense query
    # ("zxqvbnm") still returns the closest vectors.
    if vec_results:
        vec_results = [
            r for r in vec_results if r.get("score", 0.0) >= SEMANTIC_MIN_SIMILARITY
        ]
    if vec_results:
        if mode == "hybrid":
            keyword_ids = {r["item_id"] for r in fts_results}
        else:
            # Semantic-only mode runs no keyword ranking: a whole query
            # term in the title or the text is the keyword signal.
            keyword_ids = await search_fts_whole_words(db, user_id, query)
        top_score = max(r.get("score", 0.0) for r in vec_results)
        vec_results = [
            r
            for r in vec_results
            if _keep_vector_hit(
                r.get("score", 0.0),
                keyword_hit=r["item_id"] in keyword_ids,
                has_keyword_hits=bool(keyword_ids),
                top_score=top_score,
            )
        ]

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
    read_params = (read_state,) if read_state else ()
    read_clause = " AND i.read_state = ?" if read_state else ""
    sql = f"""
        SELECT {RECENT_COLUMNS}
        FROM items i
        WHERE i.id IN ({placeholders})
          AND i.user_id = ? AND i.deleted_at IS NULL
          {status_clause}{cat_clause}{quick_clause}{read_clause};
    """
    tag_sql = f"""
        SELECT it.item_id, t.name as tag_name
        FROM item_tags it
        JOIN items i ON i.id = it.item_id AND i.user_id = ?
        JOIN tags t ON t.id = it.tag_id AND t.user_id = i.user_id
        WHERE it.item_id IN ({placeholders});
    """
    rows, tag_rows = await asyncio.gather(
        db.query_all(
            sql, (*top_ids, user_id, *status_params, *cat_params, *read_params)
        ),
        db.query_all(tag_sql, (user_id, *top_ids)),
    )
    item_map = {row["id"]: row for row in rows}
    item_tags_map: dict[str, list[str]] = {}
    for tr in tag_rows:
        item_tags_map.setdefault(tr["item_id"], []).append(tr["tag_name"])

    query_terms = list(
        dict.fromkeys(_content_words(_query_terms(query)) or _query_terms(query))
    )

    # Assemble final list in RRF order
    final_items = []
    for item_id in sorted_item_ids:
        if item_id in item_map:
            item = dict(item_map[item_id])
            item_tags = item_tags_map.get(item_id, [])
            lower = {t.lower() for t in item_tags}
            if tag_list and any(t not in lower for t in tag_list):
                continue
            if all_sets and any(
                not lower.intersection(names) for names in all_sets.values()
            ):
                continue
            if any_names and not lower.intersection(any_names):
                continue
            if none_names and lower.intersection(none_names):
                continue
            if untagged and item_tags:
                continue
            item["tags"] = item_tags
            item["rrf_score"] = round(rrf_scores[item_id], 4)
            raw_snippet = snippets.get(item_id) or item["excerpt"]
            item["snippet"] = (
                highlight_query_terms(raw_snippet, query_terms) if raw_snippet else ""
            )
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
    """Retrieves recent items for user with pagination and optional filters.

    Browse order: pinned items first (is_pinned DESC), then newest first
    (created_at DESC). The feed is one list, so a pinned item leads page 1
    instead of sinking below newer saves.
    """
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
        WHERE i.user_id = ? AND i.deleted_at IS NULL
          {status_clause}{cat_clause}{quick_clause} {exists}
        ORDER BY i.is_pinned DESC, i.created_at DESC
        LIMIT ? OFFSET ?;
    """
        rows = await db.query_all(
            sql, (user_id, *status_params, *cat_params, *tag_list, limit, offset)
        )
    elif untagged:
        sql = f"""
            SELECT {RECENT_COLUMNS_BARE}
            FROM items
            WHERE user_id = ? AND deleted_at IS NULL
              {bare_status}{bare_cat}{bare_quick}
              AND NOT EXISTS (SELECT 1 FROM item_tags itx WHERE itx.item_id = items.id)
            ORDER BY is_pinned DESC, created_at DESC
            LIMIT ? OFFSET ?;
        """
        rows = await db.query_all(
            sql, (user_id, *status_params, *bare_cat_params, limit, offset)
        )
    else:
        sql = f"""
            SELECT {RECENT_COLUMNS_BARE}
            FROM items
            WHERE user_id = ? AND deleted_at IS NULL
              {bare_status}{bare_cat}{bare_quick}
            ORDER BY is_pinned DESC, created_at DESC
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
            f"WHERE i.user_id = ? AND i.deleted_at IS NULL"
            f"{tag_clause}{cat_clause_i}{quick_clause_i} {exists};",
            (user_id, *status_params, *cat_params, *tag_list),
        )
    elif untagged:
        rows = await db.query_all(
            "SELECT COUNT(*) as count FROM items "
            "WHERE user_id = ? AND deleted_at IS NULL"
            f"{status_clause}{cat_clause}{quick_clause} "
            "AND NOT EXISTS (SELECT 1 FROM item_tags itx "
            "WHERE itx.item_id = items.id);",
            (user_id, *status_params, *cat_params),
        )
    else:
        rows = await db.query_all(
            "SELECT COUNT(*) as count FROM items"
            " WHERE user_id = ? AND deleted_at IS NULL"
            f"{status_clause}{cat_clause}{quick_clause};",
            (user_id, *status_params, *cat_params),
        )
    return rows[0]["count"] if rows else 0


# Host of a canonical url, lowercased: "https://a.example/x" -> "a.example".
_URL_HOST_EXPR = (
    "LOWER(SUBSTR(SUBSTR(i.canonical_url, INSTR(i.canonical_url, '://') + 3), 1,"
    " INSTR(SUBSTR(i.canonical_url, INSTR(i.canonical_url, '://') + 3), '/') - 1))"
)


def _like_escape(value: str) -> str:
    """Escapes LIKE wildcards so a title prefix is matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def match_items_by_url(db: Database, user_id: str, url: str) -> list[str]:
    """Ids of owned items matching a url exactly or after normalization."""
    raw = (url or "").strip()
    if not raw:
        return []
    canon = canonicalize_url(raw)
    parsed = urlparse(canon)
    alt_netloc = (
        parsed.netloc[4:]
        if parsed.netloc.startswith("www.")
        else f"www.{parsed.netloc}"
    )
    alt_canon = urlunparse(
        (parsed.scheme, alt_netloc, parsed.path, parsed.params, parsed.query, "")
    )
    rows = await db.query_all(
        "SELECT id FROM items WHERE user_id = ? AND deleted_at IS NULL"
        " AND (url = ? OR canonical_url IN (?, ?));",
        (user_id, raw, canon, alt_canon),
    )
    return [r["id"] for r in rows]


async def resolve_item(
    db: Database,
    user_id: str,
    *,
    item_id: str | None = None,
    url: str | None = None,
    title: str | None = None,
) -> dict[str, Any] | None:
    """Resolves one owned item by id, url, or exact title.

    The url match is exact on the raw url and on the canonical form, so
    tracking parameters, trailing slashes and a "www." prefix do not
    matter. The title match is case-insensitive and exact; the most recent
    item wins.
    """
    if item_id:
        row = await db.query_first(
            "SELECT * FROM items WHERE id = ? AND user_id = ? AND deleted_at IS NULL;",
            (item_id, user_id),
        )
        return dict(row) if row else None
    if url:
        ids = await match_items_by_url(db, user_id, url)
        if not ids:
            return None
        row = await db.query_first(
            "SELECT * FROM items WHERE id = ? AND user_id = ? AND deleted_at IS NULL;",
            (ids[0], user_id),
        )
        return dict(row) if row else None
    if title:
        row = await db.query_first(
            "SELECT * FROM items WHERE user_id = ? AND deleted_at IS NULL"
            " AND LOWER(title) = LOWER(?)"
            " ORDER BY created_at DESC LIMIT 1;",
            (user_id, title.strip()),
        )
        return dict(row) if row else None
    return None


async def _attach_tags(
    db: Database, user_id: str, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Adds the tag list to each item (one extra query for the whole page)."""
    if not items:
        return items
    ids = [it["id"] for it in items]
    placeholders = ",".join("?" for _ in ids)
    rows = await db.query_all(
        "SELECT it.item_id, t.name AS tag_name FROM item_tags it"
        " JOIN items i ON i.id = it.item_id AND i.user_id = ?"
        " JOIN tags t ON t.id = it.tag_id AND t.user_id = i.user_id"
        f" WHERE it.item_id IN ({placeholders});",
        (user_id, *ids),
    )
    tags_map: dict[str, list[str]] = {}
    for r in rows:
        tags_map.setdefault(r["item_id"], []).append(r["tag_name"])
    for it in items:
        it["tags"] = sorted(tags_map.get(it["id"], []))
    return items


async def _item_filter_clause(
    db: Database,
    user_id: str,
    *,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    domain: str | None = None,
    pinned: bool | None = None,
    item_type: str | None = None,
    read_state: str | None = None,
    tags_all: list[str] | None = None,
    tags_any: list[str] | None = None,
    tags_none: list[str] | None = None,
    untagged: bool = False,
) -> tuple[str, list[Any]]:
    """Builds the WHERE fragment shared by the item filters.

    The items table must be aliased as `i`. `status` uses the UI status
    groups ('saved', 'extracting', 'failed'); see STATUS_GROUPS.
    `read_state` is one of 'unread', 'read' or 'archived'.
    """
    extra: list[str] = [" AND i.deleted_at IS NULL"]
    params: list[Any] = []
    status_values = _status_values(status)
    if status_values:
        extra.append(f" AND i.status IN ({','.join('?' for _ in status_values)})")
        params.extend(status_values)
    if since:
        extra.append(" AND datetime(i.created_at) >= datetime(?)")
        params.append(since)
    if until:
        extra.append(" AND datetime(i.created_at) <= datetime(?)")
        params.append(until)
    if domain:
        host = domain.strip().lower()
        extra.append(f" AND ({_URL_HOST_EXPR} = ? OR {_URL_HOST_EXPR} LIKE ?)")
        params.extend([host, "%." + host])
    if pinned is not None:
        extra.append(" AND i.is_pinned = ?")
        params.append(1 if pinned else 0)
    if item_type:
        extra.append(" AND i.item_type = ?")
        params.append(item_type)
    if read_state:
        extra.append(" AND i.read_state = ?")
        params.append(read_state)

    tag_clause, tag_params = await tag_filter_clause(
        db,
        user_id,
        tags_all=tags_all,
        tags_any=tags_any,
        tags_none=tags_none,
        untagged=untagged,
    )
    if tag_clause:
        extra.append(tag_clause)
    params.extend(tag_params)
    return "".join(extra), params


async def match_items(
    db: Database,
    user_id: str,
    *,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    domain: str | None = None,
    pinned: bool | None = None,
    item_type: str | None = None,
    read_state: str | None = None,
    tags_all: list[str] | None = None,
    tags_any: list[str] | None = None,
    tags_none: list[str] | None = None,
    untagged: bool = False,
    limit: int = 200,
) -> tuple[int, list[str]]:
    """Counts the items matching the filters and returns up to `limit` ids.

    Returns (total_matched, ids), newest first. The count covers every
    match, so a caller can tell a truncated page from a complete one.
    """
    extra, params = await _item_filter_clause(
        db,
        user_id,
        status=status,
        since=since,
        until=until,
        domain=domain,
        pinned=pinned,
        item_type=item_type,
        read_state=read_state,
        tags_all=tags_all,
        tags_any=tags_any,
        tags_none=tags_none,
        untagged=untagged,
    )
    count_row = await db.query_first(
        f"SELECT COUNT(*) as c FROM items i WHERE i.user_id = ?{extra};",
        (user_id, *params),
    )
    total_matched = int(count_row["c"]) if count_row else 0
    rows = await db.query_all(
        f"SELECT i.id FROM items i WHERE i.user_id = ?{extra}"
        " ORDER BY i.created_at DESC, i.id DESC LIMIT ?;",
        (user_id, *params, limit),
    )
    return total_matched, [r["id"] for r in rows]


async def list_items_compact(
    db: Database,
    user_id: str,
    *,
    sort: str = "saved",
    since: str | None = None,
    until: str | None = None,
    domain: str | None = None,
    pinned: bool | None = None,
    item_type: str | None = None,
    read_state: str | None = None,
    tags_all: list[str] | None = None,
    tags_any: list[str] | None = None,
    tags_none: list[str] | None = None,
    untagged: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Compact item listing: id, title, url, saved_at and tags only.

    sort: "saved" (newest first), "opened" (most recently opened first),
    "title" (alphabetical). Filters: since/until (saved date range),
    domain (host), pinned (true/false), item_type ("note" or "url"),
    read_state ("unread", "read" or "archived").
    """
    extra, params = await _item_filter_clause(
        db,
        user_id,
        since=since,
        until=until,
        domain=domain,
        pinned=pinned,
        item_type=item_type,
        read_state=read_state,
        tags_all=tags_all,
        tags_any=tags_any,
        tags_none=tags_none,
        untagged=untagged,
    )

    if sort == "opened":
        order = "(last_opened_at IS NULL), last_opened_at DESC, i.created_at DESC"
    elif sort == "title":
        order = "i.title COLLATE NOCASE ASC, i.created_at DESC"
    else:
        order = "i.created_at DESC, i.id DESC"

    sql = f"""
        SELECT i.id, i.title, i.url, i.created_at AS saved_at, i.item_type,
               i.is_pinned, i.status,
               (SELECT MAX(io.opened_at) FROM item_opens io
                WHERE io.item_id = i.id AND io.user_id = i.user_id) AS last_opened_at
        FROM items i
        WHERE i.user_id = ?{extra}
        ORDER BY {order}
        LIMIT ? OFFSET ?;
    """
    rows = await db.query_all(sql, (user_id, *params, limit, offset))
    return await _attach_tags(db, user_id, [dict(r) for r in rows])


async def find_items(
    db: Database,
    user_id: str,
    *,
    title: str | None = None,
    url: str | None = None,
    tags_all: list[str] | None = None,
    tags_any: list[str] | None = None,
    tags_none: list[str] | None = None,
    untagged: bool = False,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Finds items by exact/prefix title or by url. No search, no embeddings.

    The title matches exactly (case-insensitive) or as a prefix. The url
    matches exactly or after normalization (canonical form, no www.).
    """
    extra: list[str] = [" AND i.deleted_at IS NULL"]
    params: list[Any] = []
    if title:
        clean = title.strip()
        extra.append(
            " AND (LOWER(i.title) = LOWER(?) OR LOWER(i.title) LIKE ? ESCAPE '\\')"
        )
        params.extend([clean, _like_escape(clean.lower()) + "%"])
    if url:
        ids = await match_items_by_url(db, user_id, url)
        if not ids:
            return []
        extra.append(" AND i.id IN (" + ",".join("?" for _ in ids) + ")")
        params.extend(ids)
    if not title and not url:
        return []

    tag_clause, tag_params = await tag_filter_clause(
        db,
        user_id,
        tags_all=tags_all,
        tags_any=tags_any,
        tags_none=tags_none,
        untagged=untagged,
    )
    if tag_clause:
        extra.append(tag_clause)
    params.extend(tag_params)

    sql = f"""
        SELECT i.id, i.title, i.url, i.created_at AS saved_at, i.item_type
        FROM items i
        WHERE i.user_id = ?{"".join(extra)}
        ORDER BY i.created_at DESC
        LIMIT ?;
    """
    rows = await db.query_all(sql, (user_id, *params, limit))
    return await _attach_tags(db, user_id, [dict(r) for r in rows])


async def search_chunks(
    db: Database,
    env: Any,
    user_id: str,
    query: str,
    limit: int = 10,
    max_chars: int = DEFAULT_MAX_CHARS,
    mode: str = "hybrid",
    tag: str | None = None,
    tags: list[str] | None = None,
    untagged: bool = False,
    *,
    tags_all: list[str] | None = None,
    tags_any: list[str] | None = None,
    tags_none: list[str] | None = None,
    scope: TenantScope | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Searches chunks (passages) using hybrid search with a max_chars budget.

    Returns (passages, total_matches). Each passage includes:
    - item_id, chunk_id, char offsets, snapshot_url, saved_at, score
    - item metadata: title, url, tags

    The max_chars budget limits the total character count of returned passages
    (approximately 4 chars per token). snapshot_url points to the R2 clean.html
    snapshot which persists even if the origin URL is deleted.
    """
    if scope is None and user_id:
        from keepfor.defaults import default_scope

        scope = default_scope(db, env, user_id)

    query = query.strip()
    tag_list = [t.lower() for t in (list(tags) if tags else ([tag] if tag else []))][
        :MAX_TAG_FILTERS
    ]

    # Search for relevant items using hybrid search
    items, total = await hybrid_search(
        db,
        env,
        user_id,
        query,
        mode=mode,
        tag=tag,
        tags=tag_list,
        untagged=untagged,
        limit=limit * 3,  # Get more items to find good chunks
        tags_all=tags_all,
        tags_any=tags_any,
        tags_none=tags_none,
        scope=scope,
    )

    if not items:
        return [], 0

    item_ids = [item["id"] for item in items]

    # Fetch chunks for these items from D1
    placeholders = ",".join("?" for _ in item_ids)
    chunk_sql = f"""
        SELECT c.id as chunk_id, c.item_id, c.chunk_index, c.token_count
        FROM chunks c
        WHERE c.user_id = ? AND c.item_id IN ({placeholders})
        ORDER BY c.chunk_index ASC;
    """
    chunk_rows = await db.query_all(chunk_sql, (user_id, *item_ids))

    if not chunk_rows:
        return [], total

    # Build item lookup
    item_map = {item["id"]: item for item in items}

    # Get FTS snippets for the top items
    fts_results = await search_fts(db, user_id, query, limit=len(items))
    fts_map = {r["item_id"]: r.get("snippet") for r in fts_results}

    passages = []
    total_chars = 0

    for item_id in item_ids:
        if total_chars >= max_chars:
            break

        item = item_map.get(item_id)
        if not item:
            continue

        # Get chunks for this item
        item_chunks = [c for c in chunk_rows if c["item_id"] == item_id]
        if not item_chunks:
            continue

        # Use FTS snippet or excerpt as the passage
        snippet = fts_map.get(item_id) or item.get("excerpt") or ""
        if not snippet:
            continue

        # Build snapshot URL (R2 clean.html)
        snapshot_url = f"/api/items/{item_id}/content"

        # For each item, we only return one passage (the best matching chunk)
        # to avoid duplicating the same snippet across chunks
        # Split the snippet proportionally across chunks if needed
        remaining_chars = max_chars - total_chars
        if remaining_chars <= 0:
            break

        # Take only the first chunk's worth of text per item
        chunk = item_chunks[0]
        chunk_chars = min(chunk["token_count"] * 4, remaining_chars)
        passage_text = snippet[:chunk_chars]

        passage = {
            "item_id": item_id,
            "chunk_id": chunk["chunk_id"],
            "chunk_index": chunk["chunk_index"],
            "text": passage_text,
            "char_start": 0,
            "char_end": len(passage_text),
            "snapshot_url": snapshot_url,
            "saved_at": item["created_at"],
            "score": item.get("rrf_score", 0.0),
            "title": item.get("title") or item["url"],
            "url": item["url"],
            "tags": item.get("tags", []),
        }
        passages.append(passage)
        total_chars += len(passage_text)

    return passages, total


def _strip_markdown(text: str) -> str:
    """Strips common Markdown syntax into plain text."""
    if not text:
        return ""
    s = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"(\*\*|__)(.*?)\1", r"\2", s)
    s = re.sub(r"(\*|_)(.*?)\1", r"\2", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    return s


async def read_item_chunks(
    db: Database,
    env: Any,
    user_id: str,
    item_id: str,
    cursor: int = 0,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    format: str = "markdown",
) -> dict[str, Any]:
    """Reads an item's content in chunks for token-efficient retrieval.

    Returns dict with:
    - text: concatenated chunk text
    - next_cursor: next chunk index to read, or None if complete
    - total_chunks: total number of chunks
    - outline: list of (chunk_index, first_100_chars) for navigation
    """
    # Verify ownership
    item = await db.query_first(
        "SELECT * FROM items WHERE id = ? AND user_id = ? AND deleted_at IS NULL;",
        (item_id, user_id),
    )
    if not item:
        raise ValueError(f"Item not found with id: {item_id}")

    # Get all chunks for this item
    chunk_rows = await db.query_all(
        "SELECT id as chunk_id, chunk_index, token_count "
        "FROM chunks WHERE item_id = ? AND user_id = ? ORDER BY chunk_index;",
        (item_id, user_id),
    )

    if not chunk_rows:
        # No chunks stored, fall back to content_text
        content = item.get("content_text") or item.get("excerpt") or ""
        if format == "text":
            content = _strip_markdown(content)
        return {
            "text": content,
            "next_cursor": None,
            "total_chunks": 0,
            "outline": [],
        }

    total_chunks = len(chunk_rows)
    if cursor >= total_chunks:
        return {
            "text": "",
            "next_cursor": None,
            "total_chunks": total_chunks,
            "outline": [],
        }

    # Build outline
    outline = []
    for c in chunk_rows:
        outline.append({"chunk_index": c["chunk_index"], "preview": ""})

    # Read chunks from cursor until max_tokens reached
    # Use content_text and split by the same chunking logic
    content = item.get("content_text") or item.get("excerpt") or ""
    if not content:
        return {
            "text": "",
            "next_cursor": cursor + 1 if cursor + 1 < total_chunks else None,
            "total_chunks": total_chunks,
            "outline": outline,
        }

    # Split content into approximate chunks matching the stored chunks
    from keepfor.utils.chunker import recursive_character_split

    text_chunks_obj = recursive_character_split(
        content, target_tokens=400, overlap_tokens=50
    )
    text_chunks = [c.text for c in text_chunks_obj]

    # If we have fewer text chunks than stored chunks, pad with empty
    if len(text_chunks) < total_chunks:
        text_chunks.extend([""] * (total_chunks - len(text_chunks)))

    # Select chunks from cursor
    selected_chunks = text_chunks[cursor:]
    tokens_used = 0
    result_chunks = []
    chunks_read = 0

    for chunk_text in selected_chunks:
        # Estimate tokens for this chunk
        chunk_tokens = max(1, len(chunk_text) // 4)
        if tokens_used + chunk_tokens > max_tokens:
            break
        result_chunks.append(chunk_text)
        tokens_used += chunk_tokens
        chunks_read += 1

    end = cursor + chunks_read
    next_cursor: int | None = None if end >= total_chunks else end

    # Update outline with previews
    for i, chunk_text in enumerate(text_chunks):
        if i < len(outline):
            preview = _strip_markdown(chunk_text) if format == "text" else chunk_text
            outline[i]["preview"] = preview[:100]

    text = "\n\n".join(result_chunks)
    if format == "text":
        text = _strip_markdown(text)

    return {
        "text": text,
        "next_cursor": next_cursor,
        "total_chunks": total_chunks,
        "outline": outline,
    }


async def reading_history(
    db: Database,
    user_id: str,
    since: str | None = None,
    until: str | None = None,
    query: str | None = None,
    tag: str | None = None,
    event: str | None = None,  # "saved" | "opened" | "highlighted"
    limit: int = 20,
    offset: int = 0,
    *,
    tags_all: list[str] | None = None,
    tags_any: list[str] | None = None,
    tags_none: list[str] | None = None,
    untagged: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Returns reading history with date filtering.

    Uses created_at (saved), item_opens (opened), and highlights (highlighted).
    Supports hybrid search over history with optional query and tag filter.
    """
    # Build base query
    params: list[Any] = [user_id]

    # Handle date filters
    since_clause = ""
    until_clause = ""
    if since:
        since_clause = " AND datetime(i.created_at) >= datetime(?)"
        params.append(since)
    if until:
        until_clause = " AND datetime(i.created_at) <= datetime(?)"
        params.append(until)

    # Tag filter
    tag_list = [t.lower() for t in ([tag] if tag else [])][:MAX_TAG_FILTERS]
    tag_clause = ""
    if tag_list:
        tag_clause = (
            " AND EXISTS (SELECT 1 FROM item_tags it JOIN tags t ON "
            "t.id = it.tag_id AND t.user_id = i.user_id WHERE "
            "it.item_id = i.id AND LOWER(t.name) = LOWER(?))"
        )
        params.append(tag_list[0])

    # Tag filters (all/any/none, child tags included)
    new_clause, new_params = await tag_filter_clause(
        db,
        user_id,
        tags_all=tags_all,
        tags_any=tags_any,
        tags_none=tags_none,
        untagged=untagged,
    )
    if new_clause:
        tag_clause += new_clause
        params.extend(new_params)

    # Search query filter
    query_clause = ""
    if query:
        clean_q = "".join(c for c in query if c.isalnum() or c.isspace()).strip()
        if clean_q:
            fts_terms = " ".join(f'"{term}"*' for term in clean_q.split() if term)
            query_clause = (
                " AND i.id IN (SELECT item_id FROM items_fts "
                "WHERE items_fts MATCH ? AND user_id = ?)"
            )
            params.extend([fts_terms, user_id])

    if event == "saved":
        count_row = await db.query_first(
            f"SELECT COUNT(*) AS n FROM items i"
            f" WHERE i.user_id = ? AND i.deleted_at IS NULL"
            f" {since_clause}{until_clause}{tag_clause}{query_clause};",
            tuple(params),
        )
        total = int(count_row["n"] or 0) if count_row else 0
        sql = f"""
            SELECT i.id, i.title, i.url, i.created_at as event_time,
                   'saved' as event_type, i.item_type
            FROM items i
            WHERE i.user_id = ? AND i.deleted_at IS NULL
              {since_clause}{until_clause}{tag_clause}{query_clause}
            ORDER BY i.created_at DESC
            LIMIT ? OFFSET ?;
        """
        rows = await db.query_all(sql, (*params, limit, offset))
    elif event == "opened":
        # Item opens
        opens_since = ""
        opens_until = ""
        opens_params: list[Any] = [user_id]
        if since:
            opens_since = " AND datetime(io.opened_at) >= datetime(?)"
            opens_params.append(since)
        if until:
            opens_until = " AND datetime(io.opened_at) <= datetime(?)"
            opens_params.append(until)
        opens_params.extend(params[1 + int(bool(since)) + int(bool(until)) :])
        count_row = await db.query_first(
            f"SELECT COUNT(*) AS n FROM item_opens io"
            f" JOIN items i ON i.id = io.item_id AND i.user_id = io.user_id"
            f" AND i.deleted_at IS NULL"
            f" WHERE io.user_id = ?{opens_since}{opens_until}"
            f"{tag_clause}{query_clause};",
            tuple(opens_params),
        )
        total = int(count_row["n"] or 0) if count_row else 0

        sql = f"""
            SELECT i.id, i.title, i.url, io.opened_at as event_time,
                   'opened' as event_type, i.item_type
            FROM item_opens io
            JOIN items i ON i.id = io.item_id AND i.user_id = io.user_id
                AND i.deleted_at IS NULL
            WHERE io.user_id = ?{opens_since}{opens_until}{tag_clause}{query_clause}
            ORDER BY io.opened_at DESC
            LIMIT ? OFFSET ?;
        """
        rows = await db.query_all(sql, (*opens_params, limit, offset))
    elif event == "highlighted":
        hl_since = ""
        hl_until = ""
        hl_params: list[Any] = [user_id]
        if since:
            hl_since = " AND datetime(h.created_at) >= datetime(?)"
            hl_params.append(since)
        if until:
            hl_until = " AND datetime(h.created_at) <= datetime(?)"
            hl_params.append(until)
        hl_params.extend(params[1 + int(bool(since)) + int(bool(until)) :])
        count_row = await db.query_first(
            f"SELECT COUNT(*) AS n FROM highlights h"
            f" JOIN items i ON i.id = h.item_id AND i.user_id = h.user_id"
            f" AND i.deleted_at IS NULL"
            f" WHERE h.user_id = ?{hl_since}{hl_until}"
            f"{tag_clause}{query_clause};",
            tuple(hl_params),
        )
        total = int(count_row["n"] or 0) if count_row else 0
        sql = f"""
            SELECT i.id, i.title, i.url, h.created_at as event_time,
                   'highlighted' as event_type, i.item_type, h.text as highlight_text
            FROM highlights h
            JOIN items i ON i.id = h.item_id AND i.user_id = h.user_id
                AND i.deleted_at IS NULL
            WHERE h.user_id = ?{hl_since}{hl_until}{tag_clause}{query_clause}
            ORDER BY h.created_at DESC
            LIMIT ? OFFSET ?;
        """
        rows = await db.query_all(sql, (*hl_params, limit, offset))
    else:
        # Both saved and opened: one row per item, flagged when it was opened.
        count_row = await db.query_first(
            f"SELECT COUNT(*) AS n FROM items i"
            f" WHERE i.user_id = ? AND i.deleted_at IS NULL"
            f" {since_clause}{until_clause}{tag_clause}{query_clause};",
            tuple(params),
        )
        total = int(count_row["n"] or 0) if count_row else 0
        sql = f"""
            SELECT i.id, i.title, i.url, i.created_at as event_time,
                   'saved' as event_type, i.item_type,
                   EXISTS(
                       SELECT 1 FROM item_opens io
                       WHERE io.item_id = i.id AND io.user_id = i.user_id
                   ) as was_opened
            FROM items i
            WHERE i.user_id = ? AND i.deleted_at IS NULL
              {since_clause}{until_clause}{tag_clause}{query_clause}
            ORDER BY i.created_at DESC
            LIMIT ? OFFSET ?;
        """
        rows = await db.query_all(sql, (*params, limit, offset))

    # Get tags for items
    item_ids = [r["id"] for r in rows] if rows else []
    if item_ids:
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
    else:
        item_tags_map = {}

    results = []
    for r in rows:
        item = dict(r)
        item["tags"] = item_tags_map.get(item["id"], [])
        results.append(item)

    return results, total


async def related_items(
    db: Database,
    env: Any,
    user_id: str,
    item_id: str | None = None,
    text: str | None = None,
    limit: int = 10,
    *,
    scope: TenantScope | None = None,
) -> list[dict[str, Any]]:
    """Returns related items by vector similarity.

    Provide either item_id (uses item's chunks as query) or text (embeds the text).
    Excludes the source item itself. Includes one-line reasons (shared tags/terms).
    """
    if scope is None and user_id:
        from keepfor.defaults import default_scope

        scope = default_scope(db, env, user_id)

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

    query_vector = None
    source_item = None

    if item_id:
        # Verify ownership and get item
        source_item = await db.query_first(
            "SELECT * FROM items WHERE id = ? AND user_id = ? AND deleted_at IS NULL;",
            (item_id, user_id),
        )
        if not source_item:
            raise ValueError(f"Item not found with id: {item_id}")

        # Get item's chunks to use as query
        chunk_rows = await db.query_all(
            "SELECT chunk_index FROM chunks WHERE item_id = ? "
            "AND user_id = ? ORDER BY chunk_index;",
            (item_id, user_id),
        )
        if not chunk_rows:
            return []

        # Use first chunk's vector (or average) - for simplicity, embed
        # the title+excerpt
        query_text = f"{source_item.get('title', '')} {source_item.get('excerpt', '')}"
    elif text:
        query_text = text
    else:
        raise ValueError("Either item_id or text must be provided")

    try:
        embeddings = await asyncio.wait_for(
            providers.ai.embed(env, [query_text]),
            timeout=VECTOR_SEARCH_TIMEOUT,
        )
        if not embeddings:
            return []
        query_vector = embeddings[0]
    except Exception as exc:
        logger.warning("Related items embedding failed: %s", exc)
        return []

    try:
        query_top_k = min(limit * 3, 50)
        matches = await asyncio.wait_for(
            vectors.query(query_vector, query_top_k),
            timeout=VECTOR_SEARCH_TIMEOUT,
        )
    except Exception as exc:
        logger.warning("Related items vector query failed: %s", exc)
        return []

    # Extract unique item_ids from matches, excluding source
    item_scores: dict[str, float] = {}
    for m in matches:
        meta = m.get("metadata") or {}
        match_user_id = meta.get("user_id")
        if match_user_id and match_user_id != user_id:
            continue
        match_item_id = meta.get("item_id")
        if not match_item_id:
            chunk_id = m.get("id", "")
            if chunk_id.startswith("item_") and "_chunk_" in chunk_id:
                match_item_id = chunk_id[len("item_") : chunk_id.rfind("_chunk_")]

        if match_item_id == item_id:
            continue

        score = m.get("score", 0.0)
        if match_item_id:
            if match_item_id not in item_scores or score > item_scores[match_item_id]:
                item_scores[match_item_id] = score

    if not item_scores:
        return []

    # Sort by score
    sorted_items = sorted(item_scores.items(), key=lambda x: x[1], reverse=True)
    top_item_ids = [item_id for item_id, _ in sorted_items[:limit]]

    # Fetch full item details
    placeholders = ",".join("?" for _ in top_item_ids)
    sql = f"""
        SELECT {RECENT_COLUMNS}
        FROM items i
        WHERE i.id IN ({placeholders}) AND i.user_id = ? AND i.deleted_at IS NULL;
    """
    rows = await db.query_all(sql, (*top_item_ids, user_id))
    item_map = {row["id"]: row for row in rows}

    # Get tags for items
    if top_item_ids:
        placeholders = ",".join("?" for _ in top_item_ids)
        tag_sql = f"""
            SELECT it.item_id, t.name as tag_name
            FROM item_tags it
            JOIN items i ON i.id = it.item_id AND i.user_id = ?
            JOIN tags t ON t.id = it.tag_id AND t.user_id = i.user_id
            WHERE it.item_id IN ({placeholders});
        """
        tag_rows = await db.query_all(tag_sql, (user_id, *top_item_ids))
        item_tags_map: dict[str, list[str]] = {}
        for tr in tag_rows:
            item_tags_map.setdefault(tr["item_id"], []).append(tr["tag_name"])
    else:
        item_tags_map = {}

    # Get source item tags for reason generation
    source_tags = set()
    if source_item:
        rows = await db.query_all(
            "SELECT t.name FROM item_tags it JOIN tags t ON t.id = it.tag_id "
            "WHERE it.item_id = ? AND t.user_id = ?;",
            (item_id, user_id),
        )
        source_tags = {r["name"] for r in rows}

    results = []
    for item_id, score in sorted_items[:limit]:
        if item_id not in item_map:
            continue
        item = dict(item_map[item_id])
        item["tags"] = item_tags_map.get(item_id, [])
        item["rrf_score"] = round(score, 4)

        # Generate reason
        item_tags = set(item["tags"])
        shared_tags = source_tags & item_tags
        if shared_tags:
            reason = f"Shared tags: {', '.join(sorted(shared_tags))}"
        else:
            reason = "Semantic similarity"

        item["reason"] = reason
        results.append(item)

    return results


# --------------------------------------------------------------------------
# Library digest (MCP library_digest tool)
# --------------------------------------------------------------------------

# Caps that keep the digest compact: agents read it, so every list in the
# response is bounded (and so are the inputs that shape it).
MAX_DIGEST_TAGS = 10
MAX_DIGEST_HIGHLIGHTS = 5
MAX_DIGEST_ITEM_TAGS = 5
MAX_DIGEST_TITLE_CHARS = 120
MAX_DIGEST_URL_CHARS = 200
MAX_DIGEST_DATE_CHARS = 32
MAX_DIGEST_TAG_CHARS = 64
# Hard byte budget for the whole digest payload: agents pay for every byte,
# so a pathological library (long titles, many tags) is trimmed, not shipped.
MAX_DIGEST_BYTES = 4096


def _cap_text(value: Any, limit: int) -> str:
    """Shortens a string to a cap; an ellipsis marks the cut."""
    text = (value or "").strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _digest_bytes(payload: dict[str, Any]) -> int:
    """Size of the payload as it goes over the wire (the MCP server dumps it)."""
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def normalize_digest_bound(value: str) -> str:
    """Normalizes an ISO date/datetime to SQLite's 'YYYY-MM-DD HH:MM:SS' (UTC).

    A naive value is read as UTC, exactly like SQLite's datetime() reads it.
    """
    parsed = datetime.datetime.fromisoformat(value.strip())
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


async def library_digest(
    db: Database,
    user_id: str,
    *,
    since: str | None = None,
    until: str | None = None,
    tag: str | None = None,
    limit_tags: int = MAX_DIGEST_TAGS,
    limit_highlights: int = MAX_DIGEST_HIGHLIGHTS,
) -> dict[str, Any]:
    """A compact recap of the library for a period.

    Reports how many items were saved and opened, the most used tags and a
    short list of unread highlights. Every query is scoped to user_id.
    """
    tag_clause, tag_params = await tag_filter_clause(
        db, user_id, tags_all=[tag] if tag else None
    )

    def _window(column: str) -> tuple[str, list[Any]]:
        """Date window on a timestamp column (saved vs opened)."""
        clause = ""
        params: list[Any] = []
        if since:
            clause += f" AND datetime({column}) >= datetime(?)"
            params.append(since)
        if until:
            clause += f" AND datetime({column}) <= datetime(?)"
            params.append(until)
        return clause, params

    # Saves and highlights are bounded by the save date, opens by the open
    # date: the two events have their own clocks.
    saved_window, saved_params = _window("i.created_at")
    opened_window, opened_params = _window("io.opened_at")

    saves_sql = (
        "SELECT COUNT(*) AS n FROM items i WHERE i.user_id = ? AND i.deleted_at IS NULL"
        f"{saved_window}{tag_clause};"
    )
    unread_sql = (
        "SELECT COUNT(*) AS n FROM items i"
        " WHERE i.user_id = ? AND i.deleted_at IS NULL AND i.read_state = 'unread'"
        f"{saved_window}{tag_clause};"
    )
    opens_sql = (
        "SELECT COUNT(DISTINCT io.item_id) AS n FROM item_opens io"
        " JOIN items i ON i.id = io.item_id AND i.user_id = io.user_id"
        f" WHERE io.user_id = ? AND i.deleted_at IS NULL{opened_window}{tag_clause};"
    )
    top_tags_sql = (
        "SELECT t.name AS name, COUNT(it.item_id) AS count FROM tags t"
        " JOIN item_tags it ON it.tag_id = t.id"
        " JOIN items i ON i.id = it.item_id AND i.user_id = t.user_id"
        f" WHERE t.user_id = ? AND i.deleted_at IS NULL{saved_window}{tag_clause}"
        " GROUP BY t.id, t.name ORDER BY count DESC, t.name ASC LIMIT ?;"
    )
    highlights_sql = (
        "SELECT i.id, i.title, i.url, i.created_at AS saved_at FROM items i"
        " WHERE i.user_id = ? AND i.deleted_at IS NULL AND i.read_state = 'unread'"
        f"{saved_window}{tag_clause}"
        " ORDER BY i.created_at DESC, i.id DESC LIMIT ?;"
    )

    # Independent reads: one roundtrip instead of five sequential ones.
    saves_row, unread_row, opens_row, tag_rows, highlight_rows = await asyncio.gather(
        db.query_first(saves_sql, (user_id, *saved_params, *tag_params)),
        db.query_first(unread_sql, (user_id, *saved_params, *tag_params)),
        db.query_first(opens_sql, (user_id, *opened_params, *tag_params)),
        db.query_all(
            top_tags_sql, (user_id, *saved_params, *tag_params, limit_tags + 1)
        ),
        db.query_all(
            highlights_sql,
            (user_id, *saved_params, *tag_params, limit_highlights + 1),
        ),
    )

    top_tags = [
        {"name": _cap_text(r["name"], MAX_DIGEST_TAG_CHARS), "count": r["count"]}
        for r in tag_rows[:limit_tags]
    ]
    highlights = await _attach_tags(
        db, user_id, [dict(r) for r in highlight_rows[:limit_highlights]]
    )
    truncated = len(tag_rows) > limit_tags or len(highlight_rows) > limit_highlights
    for highlight in highlights:
        for field, limit in (
            ("title", MAX_DIGEST_TITLE_CHARS),
            ("url", MAX_DIGEST_URL_CHARS),
        ):
            original = (highlight.get(field) or "").strip()
            capped = _cap_text(original, limit)
            if len(capped) < len(original):
                truncated = True
            highlight[field] = capped
        tags = highlight.get("tags", [])
        if len(tags) > MAX_DIGEST_ITEM_TAGS:
            truncated = True
            tags = tags[:MAX_DIGEST_ITEM_TAGS]
        highlight["tags"] = [_cap_text(t, MAX_DIGEST_TAG_CHARS) for t in tags]

    payload = {
        "period": {"since": since, "until": until},
        "tag": tag,
        "saves": (saves_row or {}).get("n", 0),
        "opens": (opens_row or {}).get("n", 0),
        "unread": (unread_row or {}).get("n", 0),
        "top_tags": top_tags,
        "unread_highlights": highlights,
        "truncated": truncated,
    }
    # Byte budget: drop the oldest highlights until the payload fits.
    while highlights and _digest_bytes(payload) > MAX_DIGEST_BYTES:
        highlights.pop()
        payload["truncated"] = True
    return payload
