# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""#87: the items that hold only some of the words of a query come last.

"local LLM" finds the item that holds "local" but not "LLM" (the
open-webui save) after the item that holds both of them. Before #87 the
any-word results only showed up when no item held every word, so the
item that held both of them hid the one that held only "local".

The filler words ("my", "saved") still name no content, a nonsense query
still finds nothing and a short word still matches exactly.
"""

import json
import re
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from keepfor import app as app_module
from keepfor.auth.service import login_user, register_user
from keepfor.mcp.server import process_mcp_request
from keepfor.search.engine import hybrid_search


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


# The library of the tests:
# - both: holds "local" and "llm".
# - local_only: holds "local" only. This is the item of the issue.
# - llm_only: holds "llm" only (the plural, #76).
# - tagged: holds "llm" only, in a tag (#55).
# - two: holds "llm" and "rust": two of the three words of a query.
# - three: holds "local", "llm" and "rust".
# - library: holds a filler word and "rust", but neither "local" nor
#   "llm": it is no partial match.
# - air: holds "air", not "ai" (a short word matches exactly).
# - saved_link: holds the filler words "my" and "saved" only.
LIBRARY = (
    ("both", "Running a local LLM", "a local llm on my machine", ()),
    ("local_only", "Local news", "local news from my town", ()),
    ("llm_only", "Chat with LLMs", "chat with LLMs on your own machine", ()),
    ("tagged", "Open WebUI", "chat on your own machine", ("llm",)),
    ("two", "Rust and an LLM", "a note about rust and an llm", ()),
    ("three", "Rust with a local LLM", "a note about rust and a local llm", ()),
    ("library", "The library of Rust", "a library of rust books", ()),
    ("air", "A flight to Rome", "the plane flew by air", ()),
    ("saved_link", "A saved link", "a saved link to my favourite pages", ()),
)


async def _insert_item(db, user_id, key, title, text, tags=()):
    """An item of the library, with its FTS row and its tags."""
    item_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title, content_text,"
        " status) VALUES (?, ?, ?, ?, ?, ?, 'saved');",
        (
            item_id,
            user_id,
            f"https://example.com/{key}",
            f"https://example.com/{key}",
            title,
            text,
        ),
    )
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?, ?, ?, ?);",
        (item_id, user_id, title, text),
    )
    for tag in tags:
        await db.execute(
            "INSERT OR IGNORE INTO tags (id, user_id, name) VALUES (?, ?, ?);",
            (f"tag-{tag}", user_id, tag),
        )
        await db.execute(
            "INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?);",
            (item_id, f"tag-{tag}"),
        )
    return item_id


@pytest.fixture
async def library(db):
    """A user with the items of the tests."""
    await register_user(db, "partial@keepfor.me", "password123")
    user, session_id = await login_user(db, "partial@keepfor.me", "password123")
    ids = {}
    for key, title, text, tags in LIBRARY:
        ids[key] = await _insert_item(db, user["id"], key, title, text, tags)
    return {"db": db, "user": user, "session": session_id, "ids": ids}


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: MockEnv())
    return TestClient(app_module.app)


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


# --- the order of the results


async def test_the_items_that_hold_every_word_come_first(library):
    """The full matches lead, the partial ones follow them."""
    db = library["db"]
    user_id = library["user"]["id"]
    ids = library["ids"]

    results, total = await hybrid_search(db, MockEnv(), user_id, "local LLM")
    order = [i["id"] for i in results]

    # The items that hold "local" and "llm" come first.
    assert set(order[:2]) == {ids["both"], ids["three"]}
    # The items that hold only one of the words follow them.
    assert set(order[2:]) == {
        ids["local_only"],
        ids["llm_only"],
        ids["tagged"],
        ids["two"],
    }
    # The item that holds neither word is no match at all.
    assert ids["library"] not in order
    assert total == len(order)


async def test_a_partial_only_item_is_found(library):
    """The item that holds only "local" comes after the full matches."""
    db = library["db"]
    user_id = library["user"]["id"]
    ids = library["ids"]

    results, total = await hybrid_search(db, MockEnv(), user_id, "local LLM")
    order = [i["id"] for i in results]

    assert ids["local_only"] in order
    assert order.index(ids["local_only"]) > order.index(ids["both"])
    assert total == len(order)


async def test_the_partials_are_ordered_by_the_number_of_words(library):
    """The more content words an item holds, the earlier it comes."""
    db = library["db"]
    user_id = library["user"]["id"]
    ids = library["ids"]

    results, _ = await hybrid_search(db, MockEnv(), user_id, "local llm rust")
    order = [i["id"] for i in results]

    # "three" holds the three words, "both" and "two" hold two of them.
    assert order[0] == ids["three"]
    assert set(order[1:3]) == {ids["both"], ids["two"]}
    # The items that hold a single word come last.
    assert set(order[3:]) == {
        ids["local_only"],
        ids["llm_only"],
        ids["tagged"],
        ids["library"],
    }
    # The item that holds none of them is no match.
    assert ids["air"] not in order


async def test_the_partial_matches_are_found_in_every_mode(library, monkeypatch):
    """The keyword, the hybrid and the semantic mode see them too."""
    db = library["db"]
    user_id = library["user"]["id"]
    ids = library["ids"]

    # The vectors put the items of the query close to it; the items that
    # hold no content word are the farthest.
    _mock_vector_scores(
        monkeypatch,
        {
            item_id: (0.65 if key not in ("library", "air") else 0.5)
            for key, item_id in ids.items()
        },
    )
    for mode in ("keyword", "hybrid", "semantic"):
        results, total = await hybrid_search(
            db, MockEnv(), user_id, "local LLM", mode=mode
        )
        assert ids["local_only"] in {i["id"] for i in results}, mode
        assert ids["library"] not in {i["id"] for i in results}, mode
        assert total == len(results), mode


# --- the rules that must not change


NONSENSE_QUERIES = ["zxqvbnm", "my saved zxqvbnm", "zxqvbnm in my library"]


@pytest.mark.parametrize("query", NONSENSE_QUERIES)
async def test_a_nonsense_query_is_still_empty(library, monkeypatch, query):
    """The library words are no keyword hit, in either mode."""
    db = library["db"]
    user_id = library["user"]["id"]
    _mock_vector_scores(monkeypatch, dict.fromkeys(library["ids"].values(), 0.727))

    for mode in ("hybrid", "semantic"):
        results, total = await hybrid_search(db, MockEnv(), user_id, query, mode=mode)
        assert results == [], mode
        assert total == 0, mode


@pytest.mark.parametrize("query", NONSENSE_QUERIES)
async def test_ask_gets_no_source_for_a_nonsense_query(library, query):
    """The passages of a nonsense query are empty: Ask has no source."""
    db = library["db"]
    user = library["user"]

    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "search_passages",
            "arguments": {"query": query, "limit": 5},
        },
    }
    res = await process_mcp_request(req, db, MockEnv(), user)
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["passages"] == [], query
    assert payload["total"] == 0, query


async def test_the_library_words_are_no_partial_match(library):
    """A filler word never counts towards a partial match."""
    db = library["db"]
    user_id = library["user"]["id"]
    ids = library["ids"]

    results, total = await hybrid_search(db, MockEnv(), user_id, "library local LLM")
    order = [i["id"] for i in results]

    # The item that holds "library" holds no content word of the query.
    assert ids["library"] not in order
    # The filler word does not change the rank of the partial matches:
    # "local_only" still holds one content word.
    assert order.index(ids["local_only"]) > order.index(ids["both"])
    assert total == len(order)


async def test_a_short_word_does_not_match_a_longer_one(library):
    """ "ai" matches "ai" exactly, so "air" is no partial match."""
    db = library["db"]
    user_id = library["user"]["id"]
    ids = library["ids"]

    results, _ = await hybrid_search(db, MockEnv(), user_id, "ai rust")
    order = [i["id"] for i in results]

    assert ids["air"] not in order
    # The item that holds "rust" is a partial match and comes.
    assert ids["library"] in order


async def test_a_short_word_matches_a_tag_exactly(db, library):
    """ "ai" answers the tag "ai" and not the tag "air"."""
    user_id = library["user"]["id"]
    tagged = await _insert_item(
        db, user_id, "tag-ai", "A tagged item", "a page about something", ("ai",)
    )
    other = await _insert_item(
        db, user_id, "tag-air", "Another tagged item", "a page about nothing", ("air",)
    )

    results, _ = await hybrid_search(db, MockEnv(), user_id, "ai", mode="keyword")
    ids = {i["id"] for i in results}
    assert tagged in ids
    assert other not in ids


async def test_a_query_of_library_words_matches_as_a_whole(library):
    """A query that names no content ("my saved") matches as a whole.

    The items that hold every word are the matches; an item that holds a
    single word of the query comes only when none holds them all.
    """
    db = library["db"]
    user_id = library["user"]["id"]
    ids = library["ids"]

    results, _ = await hybrid_search(db, MockEnv(), user_id, "my saved")
    order = [i["id"] for i in results]

    # The item that holds "my" and "saved" is the match.
    assert order == [ids["saved_link"]]


# --- the page


async def test_the_search_page_shows_the_partial_matches(client, library):
    """The page shows the results and the count agrees with them."""
    client.cookies["kfm_session"] = library["session"]

    res = client.post("/search", data={"query": "local LLM", "tag": ""})
    assert res.status_code == 200
    rendered = _rendered_ids(res.text)
    assert library["ids"]["local_only"] in rendered
    assert library["ids"]["library"] not in rendered
    # The count of the search is the number of the results and the pager
    # tells the same story.
    assert _library_total(res.text) == len(rendered)
    assert f"of {len(rendered)} saves" in res.text


async def test_the_pager_counts_the_partial_matches(client, library):
    """The partial matches are on the pager too, not only on the page."""
    db = library["db"]
    user_id = library["user"]["id"]
    # Six more items that hold "local": the results fill two pages.
    for n in range(6):
        await _insert_item(db, user_id, f"extra{n}", "Local news", "local news")

    client.cookies["kfm_session"] = library["session"]
    res = client.post("/search", data={"query": "local LLM", "per_page": 10})
    assert res.status_code == 200
    first = _rendered_ids(res.text)
    assert len(first) == 10
    # The count is the number of every match, the partial ones included.
    assert _library_total(res.text) == 12
    assert "of 12 saves" in res.text
    assert "Showing 1–10 of 12 saves" in res.text

    res = client.post("/search", data={"query": "local LLM", "per_page": 10, "page": 2})
    assert res.status_code == 200
    second = _rendered_ids(res.text)
    assert len(second) == 2
    assert not set(first) & set(second)
    assert "Showing 11–12 of 12 saves" in res.text
