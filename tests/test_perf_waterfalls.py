# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import pytest
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
        BUCKET = FakeBucket()

    b1, ct1 = await resolve_favicon("example.org", FakeEnv(), may_fetch=True)
    assert b1 == png_bytes
    assert ct1 == "image/png"
    assert r2_calls == 1

    # Second call for same host hits in-memory isolate cache -> 0 new R2 reads!
    b2, ct2 = await resolve_favicon("example.org", FakeEnv(), may_fetch=True)
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
        FALLBACK_ICON,
        FAVICON_MEM_CACHE_MAX,
        _FAVICON_MEM_CACHE,
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

    bucket = FakeBucket()

    class FakeEnv:
        BUCKET = bucket

    # Test TTL expiration:
    await resolve_favicon("site-ttl.org", FakeEnv(), may_fetch=True)
    assert bucket.calls == 1
    cache_key = (id(bucket), "favicons/site-ttl.org")
    assert cache_key in _FAVICON_MEM_CACHE
    # Manually expire entry in mem cache
    b, ct, _ = _FAVICON_MEM_CACHE[cache_key]
    _FAVICON_MEM_CACHE[cache_key] = (b, ct, time.time() - 1.0)
    await resolve_favicon("site-ttl.org", FakeEnv(), may_fetch=True)
    assert bucket.calls == 2

    # Test LRU capacity bound:
    clear_favicon_mem_cache()
    for i in range(FAVICON_MEM_CACHE_MAX + 10):
        await resolve_favicon(f"site-{i}.com", FakeEnv(), may_fetch=True)
    assert len(_FAVICON_MEM_CACHE) == FAVICON_MEM_CACHE_MAX
    # First 10 items should have been evicted
    for i in range(10):
        assert (id(bucket), f"favicons/site-{i}.com") not in _FAVICON_MEM_CACHE
    # Last item is present
    assert (
        id(bucket),
        f"favicons/site-{FAVICON_MEM_CACHE_MAX + 9}.com",
    ) in _FAVICON_MEM_CACHE
    clear_favicon_mem_cache()
