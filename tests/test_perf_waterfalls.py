# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from keepfor.app import app
from keepfor.auth.service import login_user, register_user
from keepfor.models.db import Database
from keepfor.models.items import (
    get_item,
    get_item_clean_html,
    get_items_by_ids,
    save_note,
)


class QueryCounterDB(Database):
    def __init__(self, base_db: Database) -> None:
        super().__init__(base_db.d1, base_db.sqlite)
        self.queries: list[str] = []
        self.executes: list[str] = []
        self.batches: list[list[str]] = []

    async def query_first(self, sql: str, params: tuple | list = ()):
        self.queries.append(" ".join(sql.split()))
        return await super().query_first(sql, params)

    async def query_all(self, sql: str, params: tuple | list = ()):
        self.queries.append(" ".join(sql.split()))
        return await super().query_all(sql, params)

    async def execute(self, sql: str, params: tuple | list = ()):
        self.executes.append(" ".join(sql.split()))
        return await super().execute(sql, params)

    async def execute_batch(self, statements: list[tuple[str, tuple | list]]):
        self.batches.append([" ".join(sql.split()) for sql, _ in statements])
        return await super().execute_batch(statements)


@pytest.mark.asyncio
async def test_get_items_by_ids_batches_in_two_queries_and_preserves_order(
    db: Database,
):
    await db.execute(
        "INSERT INTO users (id, email, password_hash) "
        "VALUES ('u1', 'u1@example.com', 'h');"
    )
    n1 = await save_note(db, None, "u1", "Note 1", "Body 1", tags=["alpha", "beta"])
    n2 = await save_note(db, None, "u1", "Note 2", "Body 2", tags=["gamma"])
    n3 = await save_note(db, None, "u1", "Note 3", "Body 3", tags=[])

    counting_db = QueryCounterDB(db)
    wanted_order = [n3["id"], n1["id"], n2["id"]]
    items = await get_items_by_ids(counting_db, "u1", wanted_order)

    assert [it["id"] for it in items] == wanted_order
    assert items[0]["tags"] == []
    assert items[1]["tags"] == ["alpha", "beta"]
    assert items[2]["tags"] == ["gamma"]
    # Exactly 2 queries: 1 for items IN (...), 1 for item_tags IN (...)
    assert len(counting_db.queries) == 2

    # Verify >50 IDs chunks into multiple batches of <=50 IDs (D1 parameter cap)
    counting_db.queries.clear()
    many_ids = [n3["id"], *[f"missing-{i}" for i in range(55)], n1["id"]]
    chunked_items = await get_items_by_ids(counting_db, "u1", many_ids)
    assert [it["id"] for it in chunked_items] == [n3["id"], n1["id"]]
    assert len(counting_db.queries) == 4


@pytest.mark.asyncio
async def test_get_item_clean_html_reuses_preloaded_item(db: Database):
    await db.execute(
        "INSERT INTO users (id, email, password_hash) "
        "VALUES ('u1', 'u1@example.com', 'h');"
    )
    n1 = await save_note(db, None, "u1", "My Title", "Paragraph one.\n\nParagraph two.")
    preloaded = await get_item(db, "u1", n1["id"])
    assert preloaded is not None

    counting_db = QueryCounterDB(db)
    html = await get_item_clean_html(counting_db, None, "u1", n1["id"], item=preloaded)
    assert html is not None
    assert "Paragraph one." in html
    assert len(counting_db.queries) == 0


@pytest.mark.asyncio
async def test_favicon_memory_cache_avoids_repeated_r2_reads():
    import base64
    import json
    import time

    from keepfor.utils.favicon import (
        FALLBACK_ICON,
        clear_favicon_mem_cache,
        resolve_favicon,
    )

    clear_favicon_mem_cache()
    png_bytes = FALLBACK_ICON + b"extra"
    r2_calls = 0

    class FakeObj:
        async def text(self) -> str:
            return json.dumps(
                {
                    "ct": "image/png",
                    "b64": base64.b64encode(png_bytes).decode("ascii"),
                    "ts": time.time(),
                }
            )

    class FakeBucket:
        async def get(self, key: str):
            nonlocal r2_calls
            r2_calls += 1
            return FakeObj()

        async def put(self, key: str, payload: str, meta: dict):
            pass

    class FakeEnv:
        def __init__(self):
            # In Cloudflare Workers Python (Pyodide), FFI binding wrappers on env
            # (e.g. env.BUCKET) are re-wrapped per request with distinct identities.
            self.BUCKET = FakeBucket()

    env1 = FakeEnv()
    env2 = FakeEnv()
    assert id(env1.BUCKET) != id(env2.BUCKET)

    b1, ct1 = await resolve_favicon("example.org", env1, may_fetch=True)
    assert b1 == png_bytes
    assert ct1 == "image/png"
    assert r2_calls == 1

    # Second call with distinct FakeEnv / FakeBucket hits isolate cache (0 new R2 reads)
    b2, ct2 = await resolve_favicon("example.org", env2, may_fetch=True)
    assert b2 == png_bytes
    assert ct2 == "image/png"
    assert r2_calls == 1
    clear_favicon_mem_cache()


@pytest.mark.asyncio
async def test_favicon_memory_cache_eviction_and_expiration():
    import base64
    import json
    import time

    from keepfor.utils.favicon import (
        _FAVICON_MEM_CACHE,
        FALLBACK_ICON,
        FAVICON_MEM_CACHE_MAX,
        clear_favicon_mem_cache,
        resolve_favicon,
    )

    clear_favicon_mem_cache()
    png_bytes = FALLBACK_ICON + b"extra"

    class FakeObj:
        async def text(self) -> str:
            return json.dumps(
                {
                    "ct": "image/png",
                    "b64": base64.b64encode(png_bytes).decode("ascii"),
                    "ts": time.time(),
                }
            )

    class FakeBucket:
        def __init__(self):
            self.calls = 0

        async def get(self, key: str):
            self.calls += 1
            return FakeObj()

        async def put(self, key: str, payload: str, meta: dict):
            pass

    class FakeEnv:
        def __init__(self):
            self.BUCKET = FakeBucket()

    env = FakeEnv()

    # Test TTL expiration:
    await resolve_favicon("site-ttl.org", env, may_fetch=True)
    assert env.BUCKET.calls == 1
    cache_key = "favicons/site-ttl.org"
    assert cache_key in _FAVICON_MEM_CACHE
    # Manually expire entry in mem cache
    b, ct, _ = _FAVICON_MEM_CACHE[cache_key]
    _FAVICON_MEM_CACHE[cache_key] = (b, ct, time.time() - 1.0)
    await resolve_favicon("site-ttl.org", env, may_fetch=True)
    assert env.BUCKET.calls == 2

    # Test LRU capacity bound:
    clear_favicon_mem_cache()
    for i in range(FAVICON_MEM_CACHE_MAX + 10):
        await resolve_favicon(f"site-{i}.com", env, may_fetch=True)
    assert len(_FAVICON_MEM_CACHE) == FAVICON_MEM_CACHE_MAX
    # First 10 items should have been evicted
    for i in range(10):
        assert f"favicons/site-{i}.com" not in _FAVICON_MEM_CACHE
    # Last item is present
    assert f"favicons/site-{FAVICON_MEM_CACHE_MAX + 9}.com" in _FAVICON_MEM_CACHE
    clear_favicon_mem_cache()


@pytest.fixture
def client(db, monkeypatch):
    def mock_get_db(request):
        return db

    class FakeQueue:
        async def send(self, message: dict):
            pass

    class MockEnv:
        def __init__(self):
            self.QUEUE = FakeQueue()

    monkeypatch.setattr("keepfor.deps.get_db", mock_get_db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: MockEnv())

    return TestClient(app)


@pytest.mark.asyncio
async def test_html_payload_optimizations(client, db):
    user_record = await register_user(db, "test@example.com", "password")
    user_id = user_record["id"]

    # 160 tags total, 20 items with 2 tags each, 1 archived item with 120 tags
    tags = [f"tag-{i}" for i in range(160)]
    await save_note(db, None, user_id, "Archived Note", "Content", tags=tags[40:])
    await db.execute(
        "UPDATE items SET read_state = 'archived', "
        "updated_at = '2000-01-01 00:00:00', created_at = '2000-01-01 00:00:00' "
        "WHERE title = 'Archived Note'"
    )

    for i in range(20):
        item_tags = tags[i * 2 : (i + 1) * 2]
        await save_note(db, None, user_id, f"Note {i}", "Content", tags=item_tags)

    user, session_id = await login_user(db, "test@example.com", "password")
    client.cookies.set("kfm_session", session_id)

    res = client.get("/")
    html = res.text

    assert "Archived Note" not in html, "Archived Note should not be on page 1"
    assert (
        '<script defer src="https://unpkg.com/htmx.org@2.0.3/dist/htmx.min.js"></script>'
        in html
    )
    assert '<svg id="kfm-svg-sprite"' in html
    assert '<use href="#kfm-icon-' in html
    assert '<datalist id="tag-suggest-all"></datalist>' in html

    html_size = len(html.encode("utf-8"))
    assert html_size < 360000, f"HTML payload is {html_size} bytes, expected < 360000"


@pytest.mark.asyncio
async def test_search_fts_runs_url_and_fts_concurrently(db: Database):
    import asyncio

    from keepfor.search.engine import search_fts

    await db.execute(
        "INSERT INTO users (id, email, password_hash) "
        "VALUES ('u1', 'u1@example.com', 'h');"
    )
    n1 = await save_note(db, None, "u1", "Python Guide", "Learn python basics")
    n2 = await save_note(db, None, "u1", "Example Site", "Nothing here")
    await db.execute(
        "UPDATE items SET url = 'https://example.com/docs', "
        "canonical_url = 'https://example.com/docs' WHERE id = ?;",
        (n2["id"],),
    )

    class ConcurrentBarrierDB(Database):
        def __init__(self, base_db: Database) -> None:
            super().__init__(base_db.d1, base_db.sqlite)
            self.url_entered = asyncio.Event()
            self.fts_entered = asyncio.Event()
            self.timed_out = False

        async def query_all(self, sql: str, params: tuple | list = ()):
            norm = " ".join(sql.split())
            try:
                if "canonical_url LIKE ?" in norm:
                    self.url_entered.set()
                    await asyncio.wait_for(self.fts_entered.wait(), timeout=0.5)
                elif "FROM items_fts" in norm:
                    self.fts_entered.set()
                    await asyncio.wait_for(self.url_entered.wait(), timeout=0.5)
            except TimeoutError:
                self.timed_out = True
                raise
            return await super().query_all(sql, params)

    barrier_db = ConcurrentBarrierDB(db)
    results = await search_fts(barrier_db, "u1", "example.com python", limit=10)
    assert not barrier_db.timed_out
    ids = [r["item_id"] for r in results]
    assert n1["id"] in ids
    assert n2["id"] in ids

    # URL-only early return when no terms or symbols exist
    counting_db = QueryCounterDB(db)
    url_only = await search_fts(counting_db, "u1", "://", limit=10)
    assert any(r["item_id"] == n2["id"] for r in url_only)
    assert len(counting_db.queries) == 1
    assert "canonical_url LIKE ?" in counting_db.queries[0]


@pytest.mark.asyncio
async def test_search_fts_independent_url_and_fts_fallbacks(db: Database):
    from keepfor.search.engine import search_fts

    await db.execute(
        "INSERT INTO users (id, email, password_hash) "
        "VALUES ('u1', 'u1@example.com', 'h');"
    )
    n1 = await save_note(db, None, "u1", "Python Guide", "Learn python basics")
    await db.execute(
        "UPDATE items SET url = 'https://example.com/python', "
        "canonical_url = 'https://example.com/python' WHERE id = ?;",
        (n1["id"],),
    )

    class UrlFailDB(Database):
        async def query_all(self, sql: str, params: tuple | list = ()):
            if "canonical_url LIKE ?" in " ".join(sql.split()):
                raise RuntimeError("url query failed")
            return await super().query_all(sql, params)

    res1 = await search_fts(UrlFailDB(db.d1, db.sqlite), "u1", "example.com python")
    assert [r["item_id"] for r in res1] == [n1["id"]]

    class FtsFailDB(Database):
        async def query_all(self, sql: str, params: tuple | list = ()):
            if "FROM items_fts" in " ".join(sql.split()):
                raise RuntimeError("fts query failed")
            return await super().query_all(sql, params)

    res2 = await search_fts(FtsFailDB(db.d1, db.sqlite), "u1", "example.com/python")
    assert [r["item_id"] for r in res2] == [n1["id"]]


@pytest.mark.asyncio
async def test_hybrid_search_no_filters_skips_drop_trashed_and_over_hydration(
    db: Database, monkeypatch
):
    from keepfor.search.engine import hybrid_search

    await db.execute(
        "INSERT INTO users (id, email, password_hash) "
        "VALUES ('u1', 'u1@example.com', 'h');"
    )
    live_ids: list[str] = []
    for i in range(12):
        note = await save_note(
            db, None, "u1", f"Python note {i}", f"Content about python topic {i}"
        )
        live_ids.append(note["id"])

    trashed = await save_note(db, None, "u1", "Trashed python", "Trashed python body")
    await db.execute(
        "UPDATE items SET deleted_at = '2026-01-01T00:00:00Z' WHERE id = ?;",
        (trashed["id"],),
    )

    # Mock vectorize results with live items and the trashed item
    vec_hits = [
        {"item_id": iid, "score": 0.95 - idx * 0.01}
        for idx, iid in enumerate(live_ids[:8])
    ]

    async def fake_search_vectorize(env, user_id, query, limit=50, scope=None):
        return list(vec_hits)

    monkeypatch.setattr("keepfor.search.engine.search_vectorize", fake_search_vectorize)

    class FakeEnv:
        pass

    counting_db = QueryCounterDB(db)
    items, total = await hybrid_search(
        counting_db, FakeEnv(), "u1", "python", mode="hybrid", limit=4, offset=0
    )

    assert len(items) == 4
    assert total == 12
    # Must NOT execute _drop_trashed query
    assert not any(
        q.startswith("SELECT id FROM items WHERE user_id = ? AND deleted_at IS NULL")
        for q in counting_db.queries
    )
    # Hydration must only fetch the 4 page IDs (not all 12)
    hydration_queries = [
        q for q in counting_db.queries if "FROM items i WHERE i.id IN (" in q
    ]
    assert len(hydration_queries) == 1
    assert hydration_queries[0].count("?") == 4 + 1  # 4 ids + user_id

    # Now put a trashed vector item in page 1 (rank 1) and verify exactness + filtering
    vec_hits.insert(0, {"item_id": trashed["id"], "score": 0.99})
    # Also test semantic mode where trashed item is not pre-filtered by FTS
    counting_db.queries.clear()
    sem_items, sem_total = await hybrid_search(
        counting_db, FakeEnv(), "u1", "python", mode="semantic", limit=4, offset=0
    )
    assert len(sem_items) == 4
    assert all(it["id"] != trashed["id"] for it in sem_items)
    assert sem_total == 8
    assert not any(
        q.startswith("SELECT id FROM items WHERE user_id = ? AND deleted_at IS NULL")
        for q in counting_db.queries
    )


@pytest.mark.asyncio
async def test_add_tags_to_item_skips_second_select_when_tags_exist(db: Database):
    from keepfor.models.items import add_tags_to_item, get_item_tags

    await db.execute(
        "INSERT INTO users (id, email, password_hash) "
        "VALUES ('u1', 'u1@example.com', 'h');"
    )
    await save_note(db, None, "u1", "Seed", "Body", tags=["alpha", "beta"])
    target = await save_note(db, None, "u1", "Target", "Body", tags=[])

    counting_db = QueryCounterDB(db)
    await add_tags_to_item(counting_db, "u1", target["id"], ["alpha", "beta"])

    assert len(counting_db.queries) == 1
    assert len(counting_db.batches) == 1
    assert await get_item_tags(db, "u1", target["id"]) == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_add_suggestions_batches_via_execute_batch(db: Database):
    from keepfor.models.items import add_suggestions

    await db.execute(
        "INSERT INTO users (id, email, password_hash) "
        "VALUES ('u1', 'u1@example.com', 'h');"
    )
    note = await save_note(db, None, "u1", "Note", "Body")

    counting_db = QueryCounterDB(db)
    count = await add_suggestions(
        counting_db,
        "u1",
        note["id"],
        [("alpha", 0.9), ("beta", 0.8), ("x", 0.7), ("gamma", 0.6)],
    )
    assert count == 3
    assert len(counting_db.executes) == 0
    assert len(counting_db.batches) == 1
    assert len(counting_db.batches[0]) == 3

    rows = await db.query_all(
        "SELECT phrase FROM suggested_tags WHERE item_id = ? ORDER BY score DESC;",
        (note["id"],),
    )
    assert [r["phrase"] for r in rows] == ["alpha", "beta", "gamma"]

    counting_db.batches.clear()
    empty_count = await add_suggestions(counting_db, "u1", note["id"], [("a", 0.5)])
    assert empty_count == 0
    assert len(counting_db.batches) == 0
