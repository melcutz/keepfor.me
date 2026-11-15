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
