# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Search results, counts and empty states must tell the same story."""

import re
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from keepfor import app as app_module
from keepfor.auth.service import login_user, register_user
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


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: MockEnv())
    return TestClient(app_module.app)


@pytest.fixture
async def search_setup(db):
    await register_user(db, "search@keepfor.me", "password123")
    user, session_id = await login_user(db, "search@keepfor.me", "password123")
    alpha_ids = []
    for n in range(3):
        item_id = str(uuid.uuid4())
        alpha_ids.append(item_id)
        await db.execute(
            "INSERT INTO items (id, user_id, url, canonical_url, title, status)"
            " VALUES (?, ?, ?, ?, ?, 'saved');",
            (
                item_id,
                user["id"],
                f"https://example.com/alpha{n}",
                f"https://example.com/alpha{n}",
                f"Alpha guide {n}",
            ),
        )
        await db.execute(
            "INSERT INTO items_fts (item_id, user_id, title, content_text)"
            " VALUES (?, ?, ?, ?);",
            (item_id, user["id"], f"Alpha guide {n}", f"alpha content {n}"),
        )
    other_ids = []
    for n in range(5):
        item_id = str(uuid.uuid4())
        other_ids.append(item_id)
        await db.execute(
            "INSERT INTO items (id, user_id, url, canonical_url, title, status)"
            " VALUES (?, ?, ?, ?, ?, 'saved');",
            (
                item_id,
                user["id"],
                f"https://example.com/other{n}",
                f"https://example.com/other{n}",
                f"Other save {n}",
            ),
        )
    return {
        "user": user,
        "session": session_id,
        "alpha_ids": alpha_ids,
        "other_ids": other_ids,
    }


def _login(client, session_id):
    client.cookies["kfm_session"] = session_id


def _rendered_ids(html):
    return re.findall(r'id="item-card-([^"]+)"', html)


def _card_count(html, item_id):
    """How many times a card is rendered (the pinned shelf + the grid)."""
    return html.count(f'id="item-card-{item_id}"')


def _library_total(html):
    match = re.search(r'<span id="library-total"[^>]*>(\d+)</span>', html)
    assert match, "library-total span missing"
    return int(match.group(1))


def _sidebar_total(html):
    match = re.search(r'<span id="sidebar-total"[^>]*>(\d+)</span>', html)
    assert match, "sidebar-total span missing"
    return int(match.group(1))


def _mock_vector_scores(monkeypatch, scores):
    """Fake vector backend: `scores` maps item_id -> cosine similarity."""
    hits = [{"item_id": item_id, "score": score} for item_id, score in scores.items()]
    monkeypatch.setattr(
        "keepfor.search.engine.search_vectorize", AsyncMock(return_value=hits)
    )


async def test_search_count_matches_rendered_items(client, db, search_setup):
    _login(client, search_setup["session"])
    res = client.post("/search", data={"query": "alpha", "tag": ""})
    assert res.status_code == 200
    assert len(_rendered_ids(res.text)) == 3
    assert _library_total(res.text) == 3
    assert "of 3 saves" in res.text


async def test_library_query_count_matches_rendered_items(client, db, search_setup):
    """A direct /?q= load must not show the whole-library count either."""
    _login(client, search_setup["session"])
    res = client.get("/", params={"q": "alpha"})
    assert res.status_code == 200
    assert len(_rendered_ids(res.text)) == 3
    assert _library_total(res.text) == 3
    assert "of 3 saves" in res.text


async def test_search_renders_no_score_label(client, db, search_setup):
    _login(client, search_setup["session"])
    res = client.post("/search", data={"query": "alpha", "tag": ""})
    assert "score:" not in res.text
    assert "rrf_score" not in res.text


async def test_search_zero_results_empty_state(client, db, search_setup):
    _login(client, search_setup["session"])
    res = client.post("/search", data={"query": "nonexistentterm", "tag": ""})
    assert res.status_code == 200
    assert _rendered_ids(res.text) == []
    assert _library_total(res.text) == 0
    assert "No results for" in res.text
    assert "nonexistentterm" in res.text
    assert "Clear search" in res.text


async def test_library_query_zero_results_empty_state(client, db, search_setup):
    _login(client, search_setup["session"])
    res = client.get("/", params={"q": "nonexistentterm"})
    assert res.status_code == 200
    assert _rendered_ids(res.text) == []
    assert _library_total(res.text) == 0
    assert "No results for" in res.text
    assert "Clear search" in res.text


# Measured Oct 2026 on the QA tenant with the production embedding model
# @cf/baai/bge-base-en-v1.5. Vectorize runs metric=cosine, so the score is
# a similarity: nonsense and off-topic queries sit around 0.54-0.61, real
# topical matches at 0.73-0.83.
NONSENSE_SCORES = [
    ("zxqvbnm", 0.587),
    ("asdfgh qwerty", 0.609),
    ("local llms that work on my machine", 0.548),
]


@pytest.mark.parametrize(("query", "score"), NONSENSE_SCORES)
async def test_nonsense_query_drops_semantic_hits(
    client, db, search_setup, monkeypatch, query, score
):
    """A nonsense query must not fall back to unrelated semantic hits."""
    _login(client, search_setup["session"])
    _mock_vector_scores(monkeypatch, dict.fromkeys(search_setup["other_ids"], score))
    res = client.post("/search", data={"query": query, "tag": ""})
    assert res.status_code == 200
    assert _rendered_ids(res.text) == []
    assert _library_total(res.text) == 0
    assert "No results for" in res.text
    assert query in res.text

    # The floor holds in semantic-only mode too: a 0.55 floor would let
    # 0.587 and 0.609 through as "the closest vectors".
    semantic, semantic_total = await hybrid_search(
        db, MockEnv(), search_setup["user"]["id"], query, mode="semantic"
    )
    assert semantic == []
    assert semantic_total == 0


async def test_real_semantic_scores_survive_in_both_modes(
    db, search_setup, monkeypatch
):
    """Measured real-match scores (0.742, 0.832) are results in either mode."""
    _mock_vector_scores(
        monkeypatch,
        {
            search_setup["other_ids"][0]: 0.742,
            search_setup["other_ids"][1]: 0.832,
        },
    )
    env = MockEnv()
    hits = {search_setup["other_ids"][0], search_setup["other_ids"][1]}

    semantic, semantic_total = await hybrid_search(
        db, env, search_setup["user"]["id"], "example domain", mode="semantic"
    )
    assert {i["id"] for i in semantic} == hits
    assert semantic_total == 2

    hybrid, hybrid_total = await hybrid_search(
        db, env, search_setup["user"]["id"], "example domain", mode="hybrid"
    )
    assert {i["id"] for i in hybrid} == hits
    assert hybrid_total == 2


async def test_keyword_hit_at_measured_score_is_kept_in_hybrid(
    db, search_setup, monkeypatch
):
    """A short real query ("work" 0.637) has a keyword hit: it stays a result."""
    _mock_vector_scores(monkeypatch, {search_setup["alpha_ids"][0]: 0.637})
    env = MockEnv()
    results, total = await hybrid_search(
        db, env, search_setup["user"]["id"], "alpha", mode="hybrid"
    )
    assert search_setup["alpha_ids"][0] in {i["id"] for i in results}
    assert total == 3


async def test_semantic_only_hit_at_measured_score_is_dropped_in_hybrid(
    db, search_setup, monkeypatch
):
    """Without a keyword hit, 0.637 clears the floor but not the hybrid bar."""
    other = search_setup["other_ids"][0]
    _mock_vector_scores(monkeypatch, {other: 0.637})
    env = MockEnv()

    semantic, _ = await hybrid_search(
        db, env, search_setup["user"]["id"], "alpha", mode="semantic"
    )
    assert other in {i["id"] for i in semantic}

    hybrid, total = await hybrid_search(
        db, env, search_setup["user"]["id"], "alpha", mode="hybrid"
    )
    assert other not in {i["id"] for i in hybrid}
    assert total == 3


async def test_keyword_match_survives_a_low_semantic_score(
    client, db, search_setup, monkeypatch
):
    """A keyword hit is proof of relevance and is kept at any similarity."""
    _login(client, search_setup["session"])
    _mock_vector_scores(monkeypatch, dict.fromkeys(search_setup["alpha_ids"], 0.42))
    res = client.post("/search", data={"query": "alpha", "tag": ""})
    assert res.status_code == 200
    assert len(_rendered_ids(res.text)) == 3
    assert _library_total(res.text) == 3


async def test_strong_semantic_hit_without_keyword_match_is_kept(
    client, db, search_setup, monkeypatch
):
    """Above the hybrid bar a semantic-only hit is still a result."""
    _login(client, search_setup["session"])
    _mock_vector_scores(monkeypatch, dict.fromkeys(search_setup["other_ids"], 0.742))
    res = client.post("/search", data={"query": "alpha", "tag": ""})
    assert res.status_code == 200
    assert len(_rendered_ids(res.text)) == 8
    assert _library_total(res.text) == 8


async def test_pinned_item_is_rendered_once(client, db, search_setup):
    """Pinned items live on the shelf: they must not show up in the grid too."""
    _login(client, search_setup["session"])
    pinned_id = search_setup["alpha_ids"][0]
    await db.execute("UPDATE items SET is_pinned = 1 WHERE id = ?;", (pinned_id,))

    res = client.get("/")
    assert res.status_code == 200
    assert _card_count(res.text, pinned_id) == 1

    res = client.get("/", params={"q": "alpha"})
    assert res.status_code == 200
    assert _card_count(res.text, pinned_id) == 1

    # The htmx search only swaps the grid: the pinned card must be gone
    # from it, the shelf keeps showing it.
    res = client.post("/search", data={"query": "alpha", "tag": ""})
    assert res.status_code == 200
    assert _card_count(res.text, pinned_id) == 0
    assert len(_rendered_ids(res.text)) == 2


async def test_sidebar_all_items_count_stays_the_library_total(
    client, db, search_setup
):
    """A search must not shrink the library count in the sidebar."""
    _login(client, search_setup["session"])
    res = client.post("/search", data={"query": "alpha", "tag": ""})
    assert res.status_code == 200
    assert _sidebar_total(res.text) == 8
    assert _library_total(res.text) == 3

    res = client.get("/", params={"q": "alpha"})
    assert res.status_code == 200
    assert _sidebar_total(res.text) == 8
    assert _library_total(res.text) == 3
