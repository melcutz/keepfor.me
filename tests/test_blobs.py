# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Tests for BlobStore and tenant-scoped blob storage plumbing."""

import pytest

from keepfor.auth.service import register_user
from keepfor.consumer.processor import extract_and_store
from keepfor.models.db import Database
from keepfor.spi import TenantScope
from keepfor.storage.blobs import BlobStore
from tests.fakes import FakeEnv, FakeR2Bucket


def test_blob_store_key_join_and_prefix():
    bucket = FakeR2Bucket()
    store = BlobStore(bucket, prefix="t/tenant1")
    assert (
        store.key("items", "item1", "clean.html") == "t/tenant1/items/item1/clean.html"
    )
    assert store.key("raw.html") == "t/tenant1/raw.html"

    # Trailing slash in prefix normalized
    store_slash = BlobStore(bucket, prefix="t/tenant1/")
    assert store_slash.key("items", "item1") == "t/tenant1/items/item1"

    # Empty prefix
    store_empty = BlobStore(bucket, prefix="")
    assert store_empty.key("items", "item1", "clean.html") == "items/item1/clean.html"


@pytest.mark.asyncio
async def test_blob_store_key_rejections():
    store = BlobStore(FakeR2Bucket(), prefix="t/tenant1")

    with pytest.raises(ValueError, match=r"\.\."):
        store.key("..", "escape.html")

    with pytest.raises(ValueError, match=r"\.\."):
        store.key("items", "foo/../bar")

    with pytest.raises(ValueError, match=r"start with '/'"):
        store.key("/items", "bar")

    with pytest.raises(ValueError, match=r"\.\."):
        await store.delete_prefix("../escape")


@pytest.mark.asyncio
async def test_blob_store_put_get_html():
    bucket = FakeR2Bucket()
    store = BlobStore(bucket, prefix="t/tenant1")
    key = store.key("items", "item1", "clean.html")

    html = "<article><h1>Hello World</h1></article>"
    await store.put_html(key, html)

    # Verify R2 object stored with text/html content type
    assert key in bucket.store
    meta = bucket.metadata.get(key, {})
    assert meta.get("httpMetadata", {}).get("contentType") == "text/html; charset=utf-8"

    # Get text
    content = await store.get_text(key)
    assert content == html

    # Non-existent key
    assert await store.get_text("t/tenant1/items/missing") is None

    # Bucket is None
    store_none = BlobStore(None, prefix="t/tenant1")
    assert await store_none.get_text("any") is None
    await store_none.put_html("any", html)  # Should not raise


@pytest.mark.asyncio
async def test_blob_store_delete_many():
    bucket = FakeR2Bucket()
    store = BlobStore(bucket, prefix="t/tenant1")
    k1 = store.key("a")
    k2 = store.key("b")
    k3 = store.key("c")

    await store.put_html(k1, "1")
    await store.put_html(k2, "2")
    await store.put_html(k3, "3")

    await store.delete_many([k1, k3])
    assert k1 not in bucket.store
    assert k2 in bucket.store
    assert k3 not in bucket.store

    # None bucket or empty list safe
    await store.delete_many([])
    await BlobStore(None).delete_many([k1])


@pytest.mark.asyncio
async def test_blob_store_delete_prefix_paging():
    bucket = FakeR2Bucket()
    tenant_a_prefix = "t/tenant_a/"
    tenant_b_prefix = "t/tenant_b/"

    # Seed 2,500 keys for tenant A to test paging across 1,000-item chunks
    for i in range(2500):
        key = f"{tenant_a_prefix}items/{i:04d}/clean.html"
        bucket.store[key] = b"data"

    # Seed 10 keys for tenant B to ensure isolation
    for i in range(10):
        key = f"{tenant_b_prefix}items/{i:04d}/clean.html"
        bucket.store[key] = b"data"

    store_a = BlobStore(bucket, prefix="t/tenant_a")
    total_deleted = await store_a.delete_prefix("")

    assert total_deleted == 2500

    # Ensure no tenant_a keys remain
    assert not any(k.startswith("t/tenant_a/") for k in bucket.store)

    # Ensure tenant_b keys are untouched
    b_keys = [k for k in bucket.store if k.startswith("t/tenant_b/")]
    assert len(b_keys) == 10

    # Test bucket None
    assert await BlobStore(None).delete_prefix("") == 0


@pytest.mark.asyncio
async def test_queue_mismatched_user_id_defense(db: Database):
    user_victim = await register_user(db, "victim@example.com", "password123")
    user_attacker = await register_user(
        db, "attacker@example.com", "password123", allow_public_signups=True
    )

    item_id = "test-item-poison"
    await db.execute(
        """
        INSERT INTO items (id, user_id, url, canonical_url, status)
        VALUES (?, ?, ?, ?, 'queued');
        """,
        (
            item_id,
            user_victim["id"],
            "https://example.com/poison",
            "https://example.com/poison",
        ),
    )

    fake_bucket = FakeR2Bucket()
    env = FakeEnv(bucket=fake_bucket)

    # Attacker tries to trigger processing with their user_id
    await extract_and_store(
        db,
        env,
        item_id,
        "https://example.com/poison",
        user_id=user_attacker["id"],
    )

    # Verify D1 row status unchanged
    row = await db.query_first("SELECT status FROM items WHERE id = ?;", (item_id,))
    assert row is not None
    assert row["status"] == "queued"

    # Verify R2 bucket has not been touched
    assert len(fake_bucket.store) == 0


@pytest.mark.asyncio
async def test_queue_matching_user_id_success(db: Database, monkeypatch):
    user = await register_user(db, "user@example.com", "password123")
    item_id = "test-item-success"
    await db.execute(
        """
        INSERT INTO items (id, user_id, url, canonical_url, status)
        VALUES (?, ?, ?, ?, 'queued');
        """,
        (item_id, user["id"], "https://example.com/good", "https://example.com/good"),
    )

    fake_bucket = FakeR2Bucket()
    env = FakeEnv(bucket=fake_bucket)

    async def fake_fetch_and_extract(url, item_id):
        return {
            "title": "Good Article",
            "byline": "Author",
            "site_name": "example.com",
            "published_date": "2026-01-01",
            "excerpt": "Short excerpt",
            "content_text": "Good article content text here",
            "word_count": 5,
            "is_fallback": 0,
            "clean_html": "<article><p>Good article content text here</p></article>",
        }, "<html>raw</html>"

    monkeypatch.setattr(
        "keepfor.consumer.processor._fetch_and_extract", fake_fetch_and_extract
    )

    # Scoped BlobStore test
    scope = TenantScope(
        tenant_id=user["id"],
        user_id=user["id"],
        db=db,
        blob_prefix=f"t/{user['id']}/",
    )
    await extract_and_store(
        db,
        env,
        item_id,
        "https://example.com/good",
        scope=scope,
        user_id=user["id"],
    )

    row = await db.query_first(
        "SELECT status, title FROM items WHERE id = ?;", (item_id,)
    )
    assert row is not None
    assert row["status"] == "ok"
    assert row["title"] == "Good Article"

    # Check blobs under prefix
    raw_key = f"t/{user['id']}/items/{item_id}/raw.html"
    clean_key = f"t/{user['id']}/items/{item_id}/clean.html"
    assert raw_key in fake_bucket.store
    assert clean_key in fake_bucket.store
