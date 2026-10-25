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

    async def query_first(self, sql: str, params: tuple | list = ()):
        self.queries.append(" ".join(sql.split()))
        return await super().query_first(sql, params)

    async def query_all(self, sql: str, params: tuple | list = ()):
        self.queries.append(" ".join(sql.split()))
        return await super().query_all(sql, params)


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
