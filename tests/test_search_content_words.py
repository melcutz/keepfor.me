# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""#63: only the content words of a query are keyword hits.

The library words ("saved", "my", "library") are in the text of every
item of a library, so a keyword hit on one of them proves nothing:
"my saved zxqvbnm" used to keep unrelated semantic results instead of
showing the empty state. The filter is ported from keepfor-cloud#142.
"""

import re
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from keepfor import app as app_module
from keepfor.auth.service import login_user, register_user
from keepfor.search.engine import (
    LIBRARY_META_WORDS,
    _content_terms,
    hybrid_search,
)


class FakeQueue:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


class MockEnv:
    def __init__(self):
        self.DB = None
        self.QUEUE = FakeQueue()
        self.AI = None
        self.VECTORIZE = None
        self.BUCKET = None


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: MockEnv())
    return TestClient(app_module.app)


# The library of the tests: the text of an item holds the library words
# ("saved", "my"), and one item is tagged "saved".
LIBRARY = (
    (
        "saved_link",
        "A saved link to my favourite pages",
        "a saved link to my favourite pages",
    ),
    ("local_llm", "Running a local LLM", "a local llm on my machine"),
    ("library", "The library of Rust", "a library of rust books"),
)


@pytest.fixture
async def library(db):
    """A user with three saved items (one of them tagged "saved")."""
    await register_user(db, "words@keepfor.me", "password123")
    user, session_id = await login_user(db, "words@keepfor.me", "password123")
    ids = {}
    for key, title, text in LIBRARY:
        item_id = str(uuid.uuid4())
        ids[key] = item_id
        await db.execute(
            "INSERT INTO items (id, user_id, url, canonical_url, title, content_text,"
            " status) VALUES (?, ?, ?, ?, ?, ?, 'saved');",
            (
                item_id,
                user["id"],
                f"https://example.com/{key}",
                f"https://example.com/{key}",
                title,
                text,
            ),
        )
        await db.execute(
            "INSERT INTO items_fts (item_id, user_id, title, content_text)"
            " VALUES (?, ?, ?, ?);",
            (item_id, user["id"], title, text),
        )
        if key == "saved_link":
            await db.execute(
                "INSERT OR IGNORE INTO tags (id, user_id, name) VALUES (?, ?, ?);",
                ("tag-saved", user["id"], "saved"),
            )
            await db.execute(
                "INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?);",
                (item_id, "tag-saved"),
            )
    return {"db": db, "user": user, "session": session_id, "ids": ids}


def _mock_vector_scores(monkeypatch, scores):
    """Fake vector backend: `scores` maps item_id -> cosine similarity."""
    hits = [{"item_id": item_id, "score": score} for item_id, score in scores.items()]
    monkeypatch.setattr(
        "keepfor.search.engine.search_vectorize", AsyncMock(return_value=hits)
    )


def _rendered_ids(html):
    return re.findall(r'id="item-card-([^"]+)"', html)


def _library_total(html):
    match = re.search(r'<span id="library-total"[^>]*>(\d+)</span>', html)
    assert match, "library-total span missing"
    return int(match.group(1))


def _sidebar_total(html):
    match = re.search(r'<span id="sidebar-total"[^>]*>(\d+)</span>', html)
    assert match, "sidebar-total span missing"
    return int(match.group(1))


# --- the term filter


def test_the_library_words_are_no_content_words():
    """The meta words and the stopwords name no content."""
    assert _content_terms("my saved zxqvbnm") == ["zxqvbnm"]
    assert _content_terms("zxqvbnm in my library") == ["zxqvbnm"]
    assert _content_terms("what have I saved about zxqvbnm") == ["zxqvbnm"]


@pytest.mark.parametrize("word", sorted(LIBRARY_META_WORDS))
def test_every_library_word_is_dropped(word):
    """No library word survives next to a content word."""
    assert _content_terms(f"{word} rust") == ["rust"]


def test_the_content_words_are_kept():
    """The words that name content are the terms of the keyword hit."""
    assert _content_terms("local LLM") == ["local", "llm"]
    assert _content_terms("Running a local LLM") == ["running", "local", "llm"]
    assert _content_terms("rust async") == ["rust", "async"]


def test_a_query_of_library_words_only_falls_back_to_its_raw_words():
    """A plain "library" search still searches for "library"."""
    assert _content_terms("library") == ["library"]
    assert _content_terms("my saved") == ["my", "saved"]
    assert _content_terms("in") == ["in"]


def test_a_query_without_words_has_no_terms():
    assert _content_terms("") == []
    assert _content_terms("!!! ???") == []


# --- the empty state


# A nonsense query with library words: the library holds no word of it.
NONSENSE_QUERIES = ["zxqvbnm", "my saved zxqvbnm", "zxqvbnm in my library"]

# 0.70 is the band of a real hit (0.63-0.67 measured in prod): it is
# kept when the query has a keyword hit and dropped when it has none.
NOISE_SCORE = 0.70


@pytest.mark.parametrize("query", NONSENSE_QUERIES)
async def test_a_nonsense_query_is_the_empty_state(library, monkeypatch, query):
    """The library words are no keyword hit, in either mode."""
    db = library["db"]
    user_id = library["user"]["id"]
    _mock_vector_scores(
        monkeypatch, dict.fromkeys(library["ids"].values(), NOISE_SCORE)
    )

    for mode in ("hybrid", "semantic"):
        results, total = await hybrid_search(db, MockEnv(), user_id, query, mode=mode)
        assert results == [], mode
        assert total == 0, mode


@pytest.mark.parametrize("query", NONSENSE_QUERIES)
async def test_a_nonsense_query_renders_the_empty_state(
    client, library, monkeypatch, query
):
    """The page shows the empty state and the counts agree."""
    client.cookies["kfm_session"] = library["session"]
    _mock_vector_scores(
        monkeypatch, dict.fromkeys(library["ids"].values(), NOISE_SCORE)
    )

    res = client.post("/search", data={"query": query, "tag": ""})
    assert res.status_code == 200
    assert _rendered_ids(res.text) == []
    assert "No results for" in res.text
    assert query in res.text
    # The count of the search is 0 and the pager tells the same story.
    assert _library_total(res.text) == 0
    assert "of 0 saves" in res.text
    # The sidebar counts the library, not the hits of the search.
    assert _sidebar_total(res.text) == len(LIBRARY)


async def test_a_library_word_does_not_keep_a_semantic_hit(library, monkeypatch):
    """The hit of "saved" is no proof that the item is about the query."""
    db = library["db"]
    user_id = library["user"]["id"]
    saved_link = library["ids"]["saved_link"]
    _mock_vector_scores(monkeypatch, {saved_link: NOISE_SCORE})

    # Without the filter the text of the item holds "saved" and "my",
    # and the tag "saved" vouches for it: the hit would survive.
    results, total = await hybrid_search(
        db, MockEnv(), user_id, "my saved zxqvbnm", mode="hybrid"
    )
    assert results == []
    assert total == 0


# --- the real searches still work


async def test_a_real_query_still_matches(library, monkeypatch):
    """ "local LLM" finds the item that names it."""
    db = library["db"]
    user_id = library["user"]["id"]
    local_llm = library["ids"]["local_llm"]
    _mock_vector_scores(monkeypatch, {local_llm: 0.63})

    for mode in ("hybrid", "keyword", "semantic"):
        results, total = await hybrid_search(
            db, MockEnv(), user_id, "local LLM", mode=mode
        )
        assert local_llm in {i["id"] for i in results}, mode
        assert total == 1, mode


async def test_a_plain_library_search_still_matches(library, monkeypatch):
    """A search of a library word finds the item titled with it."""
    db = library["db"]
    user_id = library["user"]["id"]
    library_item = library["ids"]["library"]
    _mock_vector_scores(monkeypatch, {library_item: 0.63})

    for mode in ("hybrid", "keyword", "semantic"):
        results, total = await hybrid_search(
            db, MockEnv(), user_id, "library", mode=mode
        )
        assert library_item in {i["id"] for i in results}, mode
        assert total == 1, mode


async def test_the_search_page_shows_the_real_match(client, library, monkeypatch):
    """The page of a real search shows the item and the right count."""
    client.cookies["kfm_session"] = library["session"]
    _mock_vector_scores(monkeypatch, {library["ids"]["local_llm"]: 0.63})

    res = client.post("/search", data={"query": "local LLM", "tag": ""})
    assert res.status_code == 200
    assert _rendered_ids(res.text) == [library["ids"]["local_llm"]]
    assert _library_total(res.text) == 1
