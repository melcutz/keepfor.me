# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Tests for keepfor.consumer.processor."""

import asyncio

import pytest

from keepfor.auth.service import register_user
from keepfor.consumer import processor
from keepfor.models.db import Database
from keepfor.spi import TenantScope
from tests.fakes import FakeEnv, FakeR2Bucket


class _ConcurrentR2Bucket(FakeR2Bucket):
    """Fake R2 bucket that requires two concurrent puts to make progress."""

    def __init__(self):
        super().__init__()
        self.in_flight = 0
        self.max_in_flight = 0
        self._two_in_flight = asyncio.Event()

    async def put(self, key: str, value: str | bytes | bytearray, options=None):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        if self.in_flight >= 2:
            self._two_in_flight.set()
        try:
            await asyncio.wait_for(self._two_in_flight.wait(), timeout=0.5)
            return await super().put(key, value, options)
        finally:
            self.in_flight -= 1


@pytest.mark.asyncio
async def test_extract_and_store_puts_raw_and_clean_html_concurrently(
    db: Database, monkeypatch
):
    user = await register_user(db, "concurrent@example.com", "password123")
    item_id = "item-concurrent-r2"
    url = "https://example.com/concurrent"
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status) "
        "VALUES (?, ?, ?, ?, 'queued');",
        (item_id, user["id"], url, url),
    )

    async def fake_fetch(target_url, headers=None, **kwargs):
        return (
            "<html><head><title>Concurrent Article</title></head>"
            "<body><article><h1>Concurrent Article</h1>"
            "<p>Enough body text for extraction to succeed cleanly.</p>"
            "</article></body></html>"
        )

    monkeypatch.setattr(processor, "fetch_page_html", fake_fetch)

    bucket = _ConcurrentR2Bucket()
    env = FakeEnv(bucket=bucket)
    scope = TenantScope(
        tenant_id=user["id"],
        user_id=user["id"],
        db=db,
        blob_prefix=f"t/{user['id']}/",
    )

    await processor.extract_and_store(
        db, env, item_id, url, scope=scope, user_id=user["id"]
    )

    assert bucket.max_in_flight == 2
    assert f"t/{user['id']}/items/{item_id}/raw.html" in bucket.store
    assert f"t/{user['id']}/items/{item_id}/clean.html" in bucket.store


@pytest.mark.asyncio
async def test_extract_and_store_skips_raw_html_for_proxied_markdown(
    db: Database, monkeypatch
):
    user = await register_user(db, "proxy@example.com", "password123")
    item_id = "item-proxy-md"
    url = "https://example.com/blocked"
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status) "
        "VALUES (?, ?, ?, ?, 'queued');",
        (item_id, user["id"], url, url),
    )

    async def fake_fetch(target_url, headers=None, **kwargs):
        raise processor.OriginHttpError(403, target_url)

    async def fake_jina(target_url, **kwargs):
        return (
            "# Proxied Article\n\n"
            "Some proxied markdown content with enough text for extraction."
        )

    monkeypatch.setattr(processor, "fetch_page_html", fake_fetch)
    monkeypatch.setattr(processor, "fetch_jina_reader", fake_jina)

    bucket = FakeR2Bucket()
    env = FakeEnv(bucket=bucket)
    env.READER_PROXY_BASE = "https://r.jina.ai/"
    scope = TenantScope(
        tenant_id=user["id"],
        user_id=user["id"],
        db=db,
        blob_prefix=f"t/{user['id']}/",
    )

    await processor.extract_and_store(
        db, env, item_id, url, scope=scope, user_id=user["id"]
    )

    raw_key = f"t/{user['id']}/items/{item_id}/raw.html"
    clean_key = f"t/{user['id']}/items/{item_id}/clean.html"
    assert raw_key not in bucket.store
    assert clean_key in bucket.store
    assert bucket.puts == [clean_key]


@pytest.mark.asyncio
async def test_save_item_returns_and_updates_custom_title(db: Database):
    """save_item returns initial_title on create and updates title + FTS on re-save."""
    from keepfor.models.items import save_item
    from keepfor.search.engine import search_fts
    from tests.fakes import FakeQueue

    user = await register_user(db, "save-title@example.com", "password123")
    user_id = user["id"]
    queued_env = FakeEnv(queue=FakeQueue())

    # New item with custom title returns "title": initial_title
    created, is_new = await save_item(
        db,
        queued_env,
        user_id,
        "https://example.com/custom",
        title="  Initial Custom Title  ",
    )
    assert is_new is True
    assert created["title"] == "Initial Custom Title"

    # New item without title returns "title": None
    untitled, is_new_untitled = await save_item(
        db,
        queued_env,
        user_id,
        "https://example.com/untitled",
        title="   ",
    )
    assert is_new_untitled is True
    assert untitled["title"] is None

    # Re-saving with None, empty string, or bare URL leaves existing title unchanged
    for ignored_title in (None, "", "   ", "https://example.com/custom"):
        unchanged, is_new_again = await save_item(
            db,
            queued_env,
            user_id,
            "https://example.com/custom",
            title=ignored_title,
        )
        assert is_new_again is False
        assert unchanged["title"] == "Initial Custom Title"

    # Re-saving with a non-empty, non-bare-URL title updates DB, FTS, and dict
    updated, is_new_rename = await save_item(
        db,
        queued_env,
        user_id,
        "https://example.com/custom",
        title="Renamed Custom Title",
    )
    assert is_new_rename is False
    assert updated["title"] == "Renamed Custom Title"

    row = await db.query_first(
        "SELECT title FROM items WHERE id = ? AND user_id = ?;",
        (created["id"], user_id),
    )
    assert row["title"] == "Renamed Custom Title"

    hits_new = await search_fts(db, user_id, "Renamed Custom")
    assert any(h["item_id"] == created["id"] for h in hits_new)
    hits_old = await search_fts(db, user_id, "Initial")
    assert not any(h["item_id"] == created["id"] for h in hits_old)


@pytest.mark.asyncio
async def test_extract_and_store_preserves_custom_title_and_fills_empty_or_bare(
    db: Database, monkeypatch
):
    """extract_and_store keeps custom titles and fills empty or bare-URL titles."""
    from keepfor.models.items import save_item
    from tests.fakes import FakeQueue

    user = await register_user(db, "extract-title@example.com", "password123")
    user_id = user["id"]
    queued_env = FakeEnv(queue=FakeQueue())

    async def fake_extract(url, item_id, **kwargs):
        return (
            {
                "title": "Extracted Page Title",
                "byline": "Author",
                "site_name": "Example",
                "published_date": None,
                "excerpt": "Excerpt text",
                "content_text": "Article body text for extraction.",
                "word_count": 5,
                "is_fallback": 0,
                "clean_html": "<p>Article body text for extraction.</p>",
            },
            "<html><title>Extracted Page Title</title></html>",
        )

    monkeypatch.setattr(processor, "_fetch_and_extract", fake_extract)

    # 1. Custom non-bare-URL title is preserved
    custom_item, _ = await save_item(
        db,
        queued_env,
        user_id,
        "https://example.com/with-custom",
        title="User Custom Title",
    )
    await processor.extract_and_store(
        db,
        FakeEnv(),
        custom_item["id"],
        "https://example.com/with-custom",
        user_id=user_id,
    )
    custom_row = await db.query_first(
        "SELECT title FROM items WHERE id = ? AND user_id = ?;",
        (custom_item["id"], user_id),
    )
    assert custom_row["title"] == "User Custom Title"

    # 2. Empty title is populated from extraction
    empty_item, _ = await save_item(
        db,
        queued_env,
        user_id,
        "https://example.com/with-empty",
        title=None,
    )
    await processor.extract_and_store(
        db,
        FakeEnv(),
        empty_item["id"],
        "https://example.com/with-empty",
        user_id=user_id,
    )
    empty_row = await db.query_first(
        "SELECT title FROM items WHERE id = ? AND user_id = ?;",
        (empty_item["id"], user_id),
    )
    assert empty_row["title"] == "Extracted Page Title"

    # 3. Bare-URL title is populated from extraction
    bare_item, _ = await save_item(
        db,
        queued_env,
        user_id,
        "https://example.com/with-bare",
        title="https://example.com/with-bare",
    )
    await processor.extract_and_store(
        db,
        FakeEnv(),
        bare_item["id"],
        "https://example.com/with-bare",
        user_id=user_id,
    )
    bare_row = await db.query_first(
        "SELECT title FROM items WHERE id = ? AND user_id = ?;",
        (bare_item["id"], user_id),
    )
    assert bare_row["title"] == "Extracted Page Title"
