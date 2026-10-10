# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Tenant isolation tests for Queue consumers and extraction processing.

Enforces:
- Queue message with mismatched user_id (e.g. {item_id: A_item, user_id: B})
  writes nothing and acks.
- Queue message with matching user_id writes only tenant A rows and R2 keys
  under A's prefix.
- Import batch for tenant B never creates rows for tenant A.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from keepfor.consumer.processor import process_queue_batch
from keepfor.models.items import save_item
from tests.isolation.conftest import IsolationHarness, snapshot


class FakeQueueMessage:
    def __init__(self, body: dict[str, Any]):
        self.body = body
        self.acked = False
        self.retried = False

    def ack(self) -> None:
        self.acked = True

    def retry(self) -> None:
        self.retried = True


class FakeQueueBatch:
    def __init__(self, messages: list[FakeQueueMessage]):
        self.messages = messages


@pytest.mark.asyncio
async def test_queue_mismatched_user_id(harness: IsolationHarness):
    """Queue message {item_id: a.html_item_id, user_id: B} writes nothing & acks."""
    a = harness.tenant_a
    b = harness.tenant_b

    snap_a_before = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    snap_b_before = await snapshot(b.db, b.user["id"], env=harness.env, scope=b.scope)
    bucket_keys_before = set(getattr(harness.env.BUCKET, "store", {}).keys())

    msg_spoofed = FakeQueueMessage(
        {
            "item_id": a.html_item_id,
            "url": "https://example.com/spoofed",
            "user_id": b.user["id"],
        }
    )
    batch_spoofed = FakeQueueBatch([msg_spoofed])

    # Process spoofed message
    await process_queue_batch(batch_spoofed, harness.env)

    # Must be acked (not retried forever)
    assert msg_spoofed.acked is True
    assert msg_spoofed.retried is False

    # Snapshot A and B must be completely unchanged
    snap_a_after = await snapshot(a.db, a.user["id"], env=harness.env, scope=a.scope)
    snap_b_after = await snapshot(b.db, b.user["id"], env=harness.env, scope=b.scope)
    assert snap_a_after == snap_a_before
    assert snap_b_after == snap_b_before

    # No new R2 keys written
    bucket_keys_after = set(getattr(harness.env.BUCKET, "store", {}).keys())
    assert bucket_keys_after == bucket_keys_before


@pytest.mark.asyncio
async def test_queue_legitimate_processing(harness: IsolationHarness):
    """Queue message with correct user writes only A's rows & R2 keys under A's prefix."""
    a = harness.tenant_a
    b = harness.tenant_b

    # Save a new pending item for tenant A
    new_url_a = "https://example.com/tenant-a-fresh-article"
    new_item_a, _ = await save_item(
        a.db, harness.env, a.user["id"], new_url_a, ["fresh"], scope=a.scope
    )
    new_item_a_id = new_item_a["id"]

    mock_extracted = {
        "title": "Fresh Article Title",
        "clean_html": "<html><body><p>Extracted clean html for A</p></body></html>",
        "byline": "Author A",
        "site_name": "Example",
        "published_date": "2026-10-10",
        "word_count": 120,
        "is_fallback": 0,
        "content_text": "Extracted text content for A",
        "excerpt": "Extracted text...",
        "image_url": None,
    }
    mock_raw_html = "<html><body><h1>Raw HTML for A</h1></body></html>"

    msg_legit = FakeQueueMessage(
        {
            "item_id": new_item_a_id,
            "url": new_url_a,
            "user_id": a.user["id"],
        }
    )
    batch_legit = FakeQueueBatch([msg_legit])

    with patch(
        "keepfor.consumer.processor._fetch_and_extract",
        return_value=(mock_extracted, mock_raw_html),
    ):
        await process_queue_batch(batch_legit, harness.env)

    assert msg_legit.acked is True

    # Verify R2 keys written: must start with A's blob_prefix
    expected_clean_key = a.scope.blobs(harness.env).key(
        "items", new_item_a_id, "clean.html"
    )
    expected_raw_key = a.scope.blobs(harness.env).key(
        "items", new_item_a_id, "raw.html"
    )

    store = getattr(harness.env.BUCKET, "store", {})
    assert expected_clean_key in store
    assert expected_raw_key in store

    # Verify no keys were written under B's prefix
    if b.scope.blob_prefix:
        for k in store:
            if new_item_a_id in k:
                assert k.startswith(a.scope.blob_prefix)
                assert not k.startswith(b.scope.blob_prefix)

    # Verify D1 row for A was updated to status 'ok'
    row_a = await a.db.query_first(
        "SELECT status, title, word_count FROM items WHERE id = ? AND user_id = ?;",
        (new_item_a_id, a.user["id"]),
    )
    assert row_a is not None
    assert row_a["status"] == "ok"
    assert row_a["title"] == "Fresh Article Title"

    # Verify Tenant B has no row for new_item_a_id
    row_b = await b.db.query_first(
        "SELECT * FROM items WHERE id = ? AND user_id = ?;",
        (new_item_a_id, b.user["id"]),
    )
    assert row_b is None


@pytest.mark.asyncio
async def test_queue_import_batch_isolation(harness: IsolationHarness):
    """Import batch for Tenant B never creates rows for Tenant A."""
    a = harness.tenant_a
    b = harness.tenant_b

    snap_a_before_import = await snapshot(
        a.db, a.user["id"], env=harness.env, scope=a.scope
    )

    import_bookmarks = [
        {"url": "https://example.com/b-import-1", "tags": ["imported"]},
        {"url": "https://example.com/b-import-2", "tags": ["imported", "b"]},
    ]
    msg_import = FakeQueueMessage(
        {
            "import_batch": import_bookmarks,
            "user_id": b.user["id"],
        }
    )
    batch_import = FakeQueueBatch([msg_import])

    await process_queue_batch(batch_import, harness.env)
    assert msg_import.acked is True

    # Snapshot A must be completely unchanged
    snap_a_after_import = await snapshot(
        a.db, a.user["id"], env=harness.env, scope=a.scope
    )
    assert snap_a_after_import == snap_a_before_import

    # Tenant B has the imported items
    b_rows = await b.db.query_all(
        "SELECT url FROM items WHERE user_id = ? AND url LIKE 'https://example.com/b-import-%';",
        (b.user["id"],),
    )
    b_urls = {r["url"] for r in b_rows}
    assert b_urls == {
        "https://example.com/b-import-1",
        "https://example.com/b-import-2",
    }

    # Tenant A has none of the imported URLs
    a_rows = await a.db.query_all(
        "SELECT url FROM items WHERE user_id = ? AND url LIKE 'https://example.com/b-import-%';",
        (a.user["id"],),
    )
    assert len(a_rows) == 0
