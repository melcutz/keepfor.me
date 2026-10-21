# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""#76: a content word matches its inflected forms.

"local LLM" finds an item whose text only says "LLMs", and "model"
matches "models". The stopwords and the library words stay no keyword
hit (#72) and a nonsense query stays empty.
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from keepfor.auth.service import login_user, register_user
from keepfor.search.engine import hybrid_search


class MockEnv:
    def __init__(self):
        self.DB = None
        self.QUEUE = None
        self.AI = None
        self.VECTORIZE = None
        self.BUCKET = None


# The library of the tests:
# - open_webui: the item of the issue. The concept is in the text only as
#   the plural ("LLMs") and the text never says "local".
# - tagged: the plural of the concept is a tag, not a word of the text.
# - models: the plural is in the text.
# - library: a library word, for the rules of #72.
# - rust_note: holds "rust" and "note", like the query "rust notes".
LIBRARY = (
    ("open_webui", "Open WebUI", "chat with LLMs on your own machine", ()),
    ("tagged", "Notes on rust", "a page about the borrow checker", ("llms",)),
    ("models", "Model docs", "a list of models and their sizes", ()),
    ("library", "The library of Rust", "a library of rust books", ()),
    ("rust_note", "Rust notes", "a note about rust", ()),
)


@pytest.fixture
async def library(db):
    """A user with five saved items."""
    await register_user(db, "inflections@keepfor.me", "password123")
    user, session_id = await login_user(db, "inflections@keepfor.me", "password123")
    ids = {}
    for key, title, text, tags in LIBRARY:
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
        for tag in tags:
            await db.execute(
                "INSERT OR IGNORE INTO tags (id, user_id, name) VALUES (?, ?, ?);",
                (f"tag-{tag}", user["id"], tag),
            )
            await db.execute(
                "INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?);",
                (item_id, f"tag-{tag}"),
            )
    return {"db": db, "user": user, "session": session_id, "ids": ids}


def _mock_vector_scores(monkeypatch, scores):
    """Fake vector backend: `scores` maps item_id -> cosine similarity."""
    hits = [{"item_id": item_id, "score": score} for item_id, score in scores.items()]
    monkeypatch.setattr(
        "keepfor.search.engine.search_vectorize", AsyncMock(return_value=hits)
    )


# 0.65 is the similarity of a real hit (0.63-0.67 measured in prod): it
# is kept when the query has a keyword hit and dropped when it has none.
REAL_HIT_SCORE = 0.65


async def test_the_plural_of_a_content_word_is_found(library, monkeypatch):
    """`local LLM` finds the item whose text only says "LLMs"."""
    db = library["db"]
    user_id = library["user"]["id"]
    open_webui = library["ids"]["open_webui"]
    _mock_vector_scores(monkeypatch, {open_webui: REAL_HIT_SCORE})

    for mode in ("hybrid", "keyword", "semantic"):
        results, total = await hybrid_search(
            db, MockEnv(), user_id, "local LLM", mode=mode
        )
        assert open_webui in {i["id"] for i in results}, mode
        assert total >= 1, mode


async def test_the_plural_of_a_tag_is_found(library, monkeypatch):
    """A tag in the plural answers the singular query."""
    db = library["db"]
    user_id = library["user"]["id"]
    tagged = library["ids"]["tagged"]
    _mock_vector_scores(monkeypatch, {tagged: REAL_HIT_SCORE})

    for mode in ("hybrid", "keyword", "semantic"):
        results, total = await hybrid_search(db, MockEnv(), user_id, "llm", mode=mode)
        assert tagged in {i["id"] for i in results}, mode
        assert total >= 1, mode


async def test_a_content_word_matches_its_plural_in_the_text(library, monkeypatch):
    """`model` matches "models"."""
    db = library["db"]
    user_id = library["user"]["id"]
    models = library["ids"]["models"]
    _mock_vector_scores(monkeypatch, {models: REAL_HIT_SCORE})

    for mode in ("hybrid", "keyword", "semantic"):
        results, total = await hybrid_search(db, MockEnv(), user_id, "model", mode=mode)
        assert models in {i["id"] for i in results}, mode
        assert total >= 1, mode


async def test_a_nonsense_query_is_still_empty(library, monkeypatch):
    """`zxqvbnm` returns nothing."""
    db = library["db"]
    user_id = library["user"]["id"]
    _mock_vector_scores(monkeypatch, dict.fromkeys(library["ids"].values(), 0.727))

    for mode in ("hybrid", "keyword", "semantic"):
        results, total = await hybrid_search(
            db, MockEnv(), user_id, "zxqvbnm", mode=mode
        )
        assert results == [], mode
        assert total == 0, mode


async def test_the_library_words_are_still_no_keyword_hit(library, monkeypatch):
    """The stopwords and the library words name no content (#72)."""
    db = library["db"]
    user_id = library["user"]["id"]
    _mock_vector_scores(monkeypatch, dict.fromkeys(library["ids"].values(), 0.727))

    for query in ("my saved zxqvbnm", "zxqvbnm in my library"):
        results, total = await hybrid_search(
            db, MockEnv(), user_id, query, mode="hybrid"
        )
        assert results == [], query
        assert total == 0, query


async def test_a_meta_only_query_still_finds_the_item_titled_with_it(
    library, monkeypatch
):
    """A query of library words alone falls back to its raw words."""
    db = library["db"]
    user_id = library["user"]["id"]
    library_item = library["ids"]["library"]
    _mock_vector_scores(monkeypatch, {library_item: REAL_HIT_SCORE})

    results, total = await hybrid_search(
        db, MockEnv(), user_id, "library", mode="keyword"
    )
    assert library_item in {i["id"] for i in results}
    assert total == 1


async def test_a_short_word_does_not_match_a_longer_one(db, library):
    """A word of two letters matches exactly: "ai" does not match "air"."""
    user_id = library["user"]["id"]
    item_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title, content_text,"
        " status) VALUES (?, ?, ?, ?, ?, ?, 'saved');",
        (
            item_id,
            user_id,
            "https://example.com/air",
            "https://example.com/air",
            "A flight to Rome",
            "the plane flew by air",
        ),
    )
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?, ?, ?, ?);",
        (item_id, user_id, "A flight to Rome", "the plane flew by air"),
    )

    results, total = await hybrid_search(db, MockEnv(), user_id, "ai", mode="keyword")
    assert results == []
    assert total == 0


async def test_the_strict_query_wins_when_it_matches(library):
    """The union of the words is a fallback, not the default.

    The items that hold every word of the query are the matches, and an
    item that holds only one of them does not come with them.
    """
    db = library["db"]
    user_id = library["user"]["id"]
    library_item = library["ids"]["library"]

    results, total = await hybrid_search(
        db, MockEnv(), user_id, "rust notes", mode="keyword"
    )
    # The library item holds "rust" but not "note": it is not a match.
    assert library_item not in {i["id"] for i in results}
    assert {i["id"] for i in results} == {
        library["ids"]["tagged"],
        library["ids"]["rust_note"],
    }
    assert total == 2
