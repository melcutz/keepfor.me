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


def _grid_ids(html):
    """The cards of the feed (the items list is the only list of items)."""
    grid = html.split('id="items-list"', 1)[-1]
    return re.findall(r'id="item-card-([^"]+)"', grid)


def _card_count(html, item_id):
    """How many times a card is rendered."""
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
    """Only a strong hit survives without a keyword match.

    Measured Oct 2026 on a real prod library: the real hits score
    0.63-0.67, the nonsense query 0.727. A semantic-only hit therefore
    has to be strong (>= 0.80) to count on its own.
    """
    strong = search_setup["other_ids"][1]
    _mock_vector_scores(
        monkeypatch,
        {
            search_setup["other_ids"][0]: 0.742,
            strong: 0.832,
        },
    )
    env = MockEnv()

    semantic, semantic_total = await hybrid_search(
        db, env, search_setup["user"]["id"], "example domain", mode="semantic"
    )
    assert {i["id"] for i in semantic} == {strong}
    assert semantic_total == 1

    hybrid, hybrid_total = await hybrid_search(
        db, env, search_setup["user"]["id"], "example domain", mode="hybrid"
    )
    assert {i["id"] for i in hybrid} == {strong}
    assert hybrid_total == 1


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
    """Without a keyword hit, 0.637 is neither strong nor near the top."""
    other = search_setup["other_ids"][0]
    _mock_vector_scores(monkeypatch, {other: 0.637})
    env = MockEnv()

    semantic, semantic_total = await hybrid_search(
        db, env, search_setup["user"]["id"], "alpha", mode="semantic"
    )
    assert other not in {i["id"] for i in semantic}
    assert semantic_total == 0

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
    """A strong hit is a result even without a keyword match."""
    _login(client, search_setup["session"])
    _mock_vector_scores(monkeypatch, dict.fromkeys(search_setup["other_ids"], 0.742))
    res = client.post("/search", data={"query": "alpha", "tag": ""})
    assert res.status_code == 200
    assert len(_rendered_ids(res.text)) == 8
    assert _library_total(res.text) == 8


async def test_nonsense_query_at_prod_score_returns_nothing(
    client, db, search_setup, monkeypatch
):
    """The prod score of a nonsense query is above every real hit.

    Measured Oct 2026 on a real prod library: "zxqvbnm" scores 0.727
    while the real hits score 0.63-0.67. No absolute floor separates
    them, so a nonsense query without a keyword hit must return nothing
    in either mode.
    """
    _login(client, search_setup["session"])
    _mock_vector_scores(monkeypatch, dict.fromkeys(search_setup["other_ids"], 0.727))

    res = client.post("/search", data={"query": "zxqvbnm", "tag": ""})
    assert res.status_code == 200
    assert _rendered_ids(res.text) == []
    assert _library_total(res.text) == 0
    assert "No results for" in res.text
    assert "zxqvbnm" in res.text

    semantic, semantic_total = await hybrid_search(
        db, MockEnv(), search_setup["user"]["id"], "zxqvbnm", mode="semantic"
    )
    assert semantic == []
    assert semantic_total == 0


async def test_real_hits_with_keyword_match_are_returned(
    client, db, search_setup, monkeypatch
):
    """The prod scores of the real hits (0.63-0.67) with a keyword hit."""
    _login(client, search_setup["session"])
    user_id = search_setup["user"]["id"]
    scores = (0.63, 0.65, 0.67)
    ids = []
    for n, score in enumerate(scores):
        item_id = str(uuid.uuid4())
        ids.append(item_id)
        await db.execute(
            "INSERT INTO items (id, user_id, url, canonical_url, title, status)"
            " VALUES (?, ?, ?, ?, ?, 'saved');",
            (
                item_id,
                user_id,
                f"https://example.com/llm{n}",
                f"https://example.com/llm{n}",
                "Running a local LLM",
            ),
        )
        await db.execute(
            "INSERT INTO items_fts (item_id, user_id, title, content_text)"
            " VALUES (?, ?, ?, ?);",
            (item_id, user_id, "Running a local LLM", "a local llm on my machine"),
        )
    _mock_vector_scores(monkeypatch, dict(zip(ids, scores)))

    res = client.post("/search", data={"query": "local LLM", "tag": ""})
    assert res.status_code == 200
    assert set(_rendered_ids(res.text)) == set(ids)
    assert _library_total(res.text) == 3


async def test_hybrid_bar_boundary(db, search_setup, monkeypatch):
    """0.68 is kept (>=), 0.679 is dropped, at the hybrid bar."""
    other = search_setup["other_ids"][0]
    top = search_setup["alpha_ids"][0]
    for score, kept in ((0.68, True), (0.679, False)):
        _mock_vector_scores(monkeypatch, {top: 0.73, other: score})
        results, _ = await hybrid_search(
            db, MockEnv(), search_setup["user"]["id"], "alpha", mode="hybrid"
        )
        assert (other in {i["id"] for i in results}) is kept


async def test_semantic_floor_boundary(db, search_setup, monkeypatch):
    """0.62 is kept (>=), 0.619 is dropped, at the semantic floor."""
    alpha = search_setup["alpha_ids"][0]
    for score, kept in ((0.62, True), (0.619, False)):
        _mock_vector_scores(monkeypatch, {alpha: score})
        results, _ = await hybrid_search(
            db, MockEnv(), search_setup["user"]["id"], "alpha", mode="semantic"
        )
        assert (alpha in {i["id"] for i in results}) is kept


async def test_strong_bar_boundary(db, search_setup, monkeypatch):
    """0.80 is kept (>=), 0.799 is dropped, at the strong bar."""
    strong = search_setup["other_ids"][0]
    weak = search_setup["other_ids"][1]
    _mock_vector_scores(monkeypatch, {strong: 0.80, weak: 0.799})
    results, total = await hybrid_search(
        db, MockEnv(), search_setup["user"]["id"], "zxqvbnm", mode="hybrid"
    )
    ids = {i["id"] for i in results}
    assert strong in ids
    assert weak not in ids
    assert total == 1


async def test_pinned_item_is_rendered_once(client, db, search_setup):
    """A pinned item is listed once, in the feed (the shelf is gone)."""
    _login(client, search_setup["session"])
    pinned_id = search_setup["alpha_ids"][0]
    await db.execute("UPDATE items SET is_pinned = 1 WHERE id = ?;", (pinned_id,))

    res = client.get("/")
    assert res.status_code == 200
    assert _card_count(res.text, pinned_id) == 1
    assert pinned_id in _grid_ids(res.text)

    # A search keeps the pinned match.
    res = client.get("/", params={"q": "alpha"})
    assert res.status_code == 200
    assert _card_count(res.text, pinned_id) == 1
    assert pinned_id in _grid_ids(res.text)

    res = client.post("/search", data={"query": "alpha", "tag": ""})
    assert res.status_code == 200
    assert _card_count(res.text, pinned_id) == 1
    assert len(_rendered_ids(res.text)) == 3


async def test_browse_count_matches_rendered_items_with_pinned(
    client, db, search_setup
):
    """The pinned items are in the feed, so the count includes them."""
    _login(client, search_setup["session"])
    user_id = search_setup["user"]["id"]
    for n in range(20):
        await db.execute(
            "INSERT INTO items (id, user_id, url, canonical_url, title, status)"
            " VALUES (?, ?, ?, ?, ?, 'saved');",
            (
                str(uuid.uuid4()),
                user_id,
                f"https://example.com/bulk{n}",
                f"https://example.com/bulk{n}",
                f"Bulk save {n}",
            ),
        )
    await db.execute(
        "UPDATE items SET is_pinned = 1 WHERE id = ?;", (search_setup["other_ids"][0],)
    )

    res = client.get("/")
    assert res.status_code == 200
    # 28 saves, 1 of them pinned: the feed shows all 28, in full pages.
    assert _library_total(res.text) == 28
    assert len(_grid_ids(res.text)) == 20
    assert "of 28 saves" in res.text

    res = client.get("/", params={"page": 2})
    assert res.status_code == 200
    assert _library_total(res.text) == 28
    assert len(_grid_ids(res.text)) == 8


async def test_search_returns_a_pinned_match(client, db, search_setup):
    """A query that only matches a pinned item still finds it."""
    _login(client, search_setup["session"])
    pinned_id = search_setup["alpha_ids"][0]
    await db.execute("UPDATE items SET is_pinned = 1 WHERE id = ?;", (pinned_id,))

    res = client.post("/search", data={"query": "Alpha guide 0", "tag": ""})
    assert res.status_code == 200
    assert _rendered_ids(res.text) == [pinned_id]
    assert "No results" not in res.text


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
