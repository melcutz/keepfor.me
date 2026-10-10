# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for core hooks: AI routing, entitlements, and event bus."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from keepfor.app import create_app
from keepfor.consumer.processor import extract_and_store
from keepfor.models.db import Database
from keepfor.models.items import delete_item, save_item
from keepfor.search.engine import search_vectorize
from keepfor.spi import (
    Action,
    AIProvider,
    AuthProvider,
    Decision,
    Entitlements,
    EventBus,
    Principal,
    Providers,
    ScopeProvider,
    TenantScope,
)
from tests.fakes import FakeEnv, FakeQueue, FakeR2Bucket, FakeVectorize


class StubAuth(AuthProvider):
    def __init__(self, principal: Principal):
        self.principal = principal

    async def authenticate(self, request: Request) -> Principal | None:
        return self.principal


class StubScope(ScopeProvider):
    def __init__(self, db: Database, bucket: Any = None, index: Any = None):
        self.db = db
        self.bucket = bucket
        self.index = index

    async def scope_for_principal(self, principal: Principal, env: Any) -> TenantScope:
        return TenantScope(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            db=self.db,
            bucket=self.bucket,
            index=self.index,
        )

    async def scope_for_tenant(self, tenant_id: str, env: Any) -> TenantScope:
        return TenantScope(
            tenant_id=tenant_id,
            user_id=tenant_id,
            db=self.db,
            bucket=self.bucket,
            index=self.index,
        )


class MockEntitlements(Entitlements):
    def __init__(
        self,
        deny_actions: set[str] | None = None,
        reason: str = "limit",
        upgrade_url: str | None = "https://example.com/upgrade",
    ):
        self.deny_actions = deny_actions or set()
        self.reason = reason
        self.upgrade_url = upgrade_url
        self.checks: list[tuple[str, str, int]] = []
        self.records: list[tuple[str, str, int]] = []

    async def check(self, tenant_id: str, action: Action, qty: int = 1) -> Decision:
        self.checks.append((tenant_id, action, qty))
        if action in self.deny_actions:
            return Decision(
                allowed=False,
                reason=self.reason,
                upgrade_url=self.upgrade_url,
            )
        return Decision(allowed=True)

    async def record(self, tenant_id: str, action: Action, qty: int = 1) -> None:
        self.records.append((tenant_id, action, qty))


class RecordingEventBus(EventBus):
    def __init__(self):
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))


class FaultyEventBus(EventBus):
    async def publish(self, event: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("EventBus explosion!")


class CustomAIProvider(AIProvider):
    def __init__(self):
        self.embed_calls: list[list[str]] = []
        self.summarize_calls: list[str] = []

    async def embed(self, env: Any, texts: list[str]) -> list[list[float]]:
        self.embed_calls.append(texts)
        return [[0.42] * 768 for _ in texts]

    async def summarize(
        self, env: Any, text: str, *, max_words: int = 40
    ) -> str | None:
        self.summarize_calls.append(text)
        return "Custom summary of article."

    async def generate(
        self,
        env: Any,
        system: str,
        user: str,
        *,
        max_tokens: int = 400,
        json_schema: dict | None = None,
        timeout_s: float = 20.0,
    ) -> str | None:
        return None


@pytest.fixture
def test_principal() -> Principal:
    return Principal(
        user_id="user-hook-1",
        tenant_id="tenant-hook-1",
        email="hook@example.com",
    )


@pytest.mark.asyncio
async def test_default_providers_transparent(db: Database):
    """Default providers work transparently without breaking existing flows."""
    providers = Providers().resolved()
    assert providers.entitlements is not None
    decision = await providers.entitlements.check("t1", "save_item", 1)
    assert decision.allowed is True
    # Recording and publishing should not raise
    await providers.entitlements.record("t1", "save_item", 1)
    await providers.events.publish("item.saved", {"tenant_id": "t1"})


@pytest.mark.asyncio
async def test_entitlements_denying_save_item(db: Database, test_principal: Principal):
    """Denying save_item returns 402 JSON on API, renders HTML on web,
    and writes NO row.
    """
    entitlements = MockEntitlements(
        deny_actions={"save_item"},
        reason="quota_exceeded",
        upgrade_url="https://keepfor.me/pricing",
    )
    providers = Providers(
        auth=StubAuth(test_principal),
        scope=StubScope(db),
        entitlements=entitlements,
    )
    app = create_app(providers=providers)
    client = TestClient(app)

    with patch("keepfor.deps.get_db", return_value=db):
        # 1. /api/save returns 402 JSON
        resp_api = client.post(
            "/api/save", json={"url": "https://example.com/blocked-item"}
        )
        assert resp_api.status_code == 402
        data = resp_api.json()
        assert data["error"] == "entitlement_denied"
        assert data["reason"] == "quota_exceeded"
        assert data["upgrade_url"] == "https://keepfor.me/pricing"

        # 2. /save web form returns 402 HTML with full page
        resp_web = client.post(
            "/save", data={"url": "https://example.com/blocked-item"}
        )
        assert resp_web.status_code == 402
        assert "text/html" in resp_web.headers["content-type"]
        assert "Limit Reached" in resp_web.text
        assert "quota_exceeded" in resp_web.text
        assert "https://keepfor.me/pricing" in resp_web.text

        # 3. HTMX request to /save returns 402 HTML fragment
        resp_htmx = client.post(
            "/save",
            data={"url": "https://example.com/blocked-item"},
            headers={"hx-request": "true"},
        )
        assert resp_htmx.status_code == 402
        assert "Limit Reached" in resp_htmx.text
        # Fragment extends partials/fragment.html, so no <html> or <head> tag
        assert "<html" not in resp_htmx.text
        assert "<head" not in resp_htmx.text

        # 4. Assert NO row was written to D1
        rows = await db.query_all("SELECT * FROM items;")
        assert len(rows) == 0


@pytest.mark.asyncio
async def test_entitlements_denying_summarize(
    db: Database, test_principal: Principal, monkeypatch
):
    """Denying summarize leaves the item saved without summary."""
    entitlements = MockEntitlements(deny_actions={"summarize"})
    ai_provider = CustomAIProvider()
    providers = Providers(
        auth=StubAuth(test_principal),
        scope=StubScope(db),
        entitlements=entitlements,
        ai=ai_provider,
    )
    from keepfor.runtime import set_providers

    set_providers(providers)

    # Insert a queued item
    item_id = "test-item-summarize-denied"
    await db.execute(
        """
        INSERT INTO items (id, user_id, url, canonical_url, status)
        VALUES (?, ?, ?, ?, 'queued');
        """,
        (
            item_id,
            test_principal.user_id,
            "https://example.com/article",
            "https://example.com/article",
        ),
    )

    long_text = "This is a meaningful article body that has plenty of text. " * 20

    async def fake_fetch_and_extract(url, item_id):
        return {
            "title": "Article Title",
            "byline": "Author",
            "site_name": "example.com",
            "published_date": "2026-01-01",
            "excerpt": "Excerpt",
            "content_text": long_text,
            "word_count": len(long_text.split()),
            "is_fallback": 0,
            "clean_html": f"<article><p>{long_text}</p></article>",
        }, "<html>raw</html>"

    monkeypatch.setattr(
        "keepfor.consumer.processor._fetch_and_extract", fake_fetch_and_extract
    )

    fake_bucket = FakeR2Bucket()
    env = FakeEnv(bucket=fake_bucket)
    scope = TenantScope(
        tenant_id=test_principal.tenant_id,
        user_id=test_principal.user_id,
        db=db,
    )

    await extract_and_store(
        db, env, item_id, "https://example.com/article", scope=scope
    )

    row = await db.query_first(
        "SELECT status, summary FROM items WHERE id = ?;", (item_id,)
    )
    assert row is not None
    assert row["status"] == "ok"
    assert row["summary"] is None
    # Custom summarize was never called because entitlement was denied
    assert len(ai_provider.summarize_calls) == 0


@pytest.mark.asyncio
async def test_entitlements_denying_mcp_call(db: Database, test_principal: Principal):
    """Denying mcp_call yields JSON-RPC error with code -32001 and message 'limit'."""
    entitlements = MockEntitlements(deny_actions={"mcp_call"})
    providers = Providers(
        auth=StubAuth(test_principal),
        scope=StubScope(db),
        entitlements=entitlements,
    )
    app = create_app(providers=providers)
    client = TestClient(app)

    with patch("keepfor.deps.get_db", return_value=db):
        resp = client.post(
            "/api/mcp",
            json={"jsonrpc": "2.0", "id": "req-mcp-1", "method": "tools/list"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == "req-mcp-1"
        assert data["error"]["code"] == -32001
        assert data["error"]["message"] == "limit"


@pytest.mark.asyncio
async def test_recording_event_bus(
    db: Database, test_principal: Principal, monkeypatch
):
    """EventBus sees item.saved and item.extracted with exact keys."""
    event_bus = RecordingEventBus()
    providers = Providers(
        auth=StubAuth(test_principal),
        scope=StubScope(db),
        events=event_bus,
    )
    from keepfor.runtime import set_providers

    set_providers(providers)

    async def fake_fetch_and_extract(url, item_id):
        return {
            "title": "Article",
            "byline": "Author",
            "site_name": "example.com",
            "published_date": "2026-01-01",
            "excerpt": "Excerpt",
            "content_text": "Content",
            "word_count": 1,
            "is_fallback": 0,
            "clean_html": "<p>Content</p>",
        }, "<html>raw</html>"

    monkeypatch.setattr(
        "keepfor.consumer.processor._fetch_and_extract", fake_fetch_and_extract
    )

    env = FakeEnv(bucket=FakeR2Bucket(), queue=FakeQueue())
    scope = TenantScope(
        tenant_id=test_principal.tenant_id,
        user_id=test_principal.user_id,
        db=db,
    )

    # 1. save_item
    item_dict, is_new = await save_item(
        db,
        env,
        test_principal.user_id,
        "https://example.com/event-item",
        scope=scope,
    )
    assert is_new is True
    item_id = item_dict["id"]

    # 2. extract_and_store
    await extract_and_store(
        db,
        env,
        item_id,
        "https://example.com/event-item",
        scope=scope,
        user_id=test_principal.user_id,
    )

    # 3. delete_item
    await delete_item(db, env, test_principal.user_id, item_id, scope=scope)

    event_names = [e[0] for e in event_bus.events]
    assert event_names == ["item.saved", "item.extracted", "item.deleted"]

    for name, payload in event_bus.events:
        assert set(payload.keys()) == {"tenant_id", "user_id", "item_id"}
        assert payload["tenant_id"] == test_principal.tenant_id
        assert payload["user_id"] == test_principal.user_id
        assert payload["item_id"] == item_id


@pytest.mark.asyncio
async def test_faulty_event_bus_does_not_break_flow(
    db: Database, test_principal: Principal, monkeypatch
):
    """An EventBus raising exceptions never breaks saving, extraction, or deletion."""
    providers = Providers(
        auth=StubAuth(test_principal),
        scope=StubScope(db),
        events=FaultyEventBus(),
    )
    from keepfor.runtime import set_providers

    set_providers(providers)

    async def fake_fetch_and_extract(url, item_id):
        return {
            "title": "Article",
            "byline": "Author",
            "site_name": "example.com",
            "published_date": "2026-01-01",
            "excerpt": "Excerpt",
            "content_text": "Content",
            "word_count": 1,
            "is_fallback": 0,
            "clean_html": "<p>Content</p>",
        }, "<html>raw</html>"

    monkeypatch.setattr(
        "keepfor.consumer.processor._fetch_and_extract", fake_fetch_and_extract
    )

    env = FakeEnv(bucket=FakeR2Bucket(), queue=FakeQueue())
    scope = TenantScope(
        tenant_id=test_principal.tenant_id,
        user_id=test_principal.user_id,
        db=db,
    )

    item_dict, is_new = await save_item(
        db,
        env,
        test_principal.user_id,
        "https://example.com/faulty-item",
        scope=scope,
    )
    assert is_new is True
    item_id = item_dict["id"]

    await extract_and_store(
        db,
        env,
        item_id,
        "https://example.com/faulty-item",
        scope=scope,
        user_id=test_principal.user_id,
    )

    row = await db.query_first("SELECT status FROM items WHERE id = ?;", (item_id,))
    assert row is not None
    assert row["status"] == "ok"

    deleted = await delete_item(db, env, test_principal.user_id, item_id, scope=scope)
    assert deleted is True


@pytest.mark.asyncio
async def test_custom_ai_provider_routing_and_vectors(
    db: Database, test_principal: Principal, monkeypatch
):
    """Custom AIProvider receives embed and summarize calls and
    vectors reach FakeVectorize.
    """
    ai_provider = CustomAIProvider()
    fake_vectorize = FakeVectorize()
    fake_bucket = FakeR2Bucket()
    providers = Providers(
        auth=StubAuth(test_principal),
        scope=StubScope(db, bucket=fake_bucket, index=fake_vectorize),
        ai=ai_provider,
    )
    from keepfor.runtime import set_providers

    set_providers(providers)

    article_text = (
        "Keepfor is a lightweight self-hosted read-it-later bookmarking service. " * 15
    )

    async def fake_fetch_and_extract(url, item_id):
        return {
            "title": "Keepfor Architecture",
            "byline": "Claudiu",
            "site_name": "keepfor.me",
            "published_date": "2026-01-01",
            "excerpt": "Excerpt",
            "content_text": article_text,
            "word_count": len(article_text.split()),
            "is_fallback": 0,
            "clean_html": f"<p>{article_text}</p>",
        }, "<html>raw</html>"

    monkeypatch.setattr(
        "keepfor.consumer.processor._fetch_and_extract", fake_fetch_and_extract
    )

    env = FakeEnv(
        bucket=fake_bucket,
        vectorize=fake_vectorize,
        queue=FakeQueue(),
    )
    scope = TenantScope(
        tenant_id=test_principal.tenant_id,
        user_id=test_principal.user_id,
        db=db,
        bucket=fake_bucket,
        index=fake_vectorize,
    )

    item_id = "test-custom-ai-item"
    await db.execute(
        """
        INSERT INTO items (id, user_id, url, canonical_url, status)
        VALUES (?, ?, ?, ?, 'queued');
        """,
        (
            item_id,
            test_principal.user_id,
            "https://keepfor.me/arch",
            "https://keepfor.me/arch",
        ),
    )

    await extract_and_store(db, env, item_id, "https://keepfor.me/arch", scope=scope)

    # 1. Custom summarize was called
    assert len(ai_provider.summarize_calls) == 1
    assert "Keepfor is a lightweight" in ai_provider.summarize_calls[0]

    row = await db.query_first("SELECT summary FROM items WHERE id = ?;", (item_id,))
    assert row["summary"] == "Custom summary of article."

    # 2. Custom embed was called
    assert len(ai_provider.embed_calls) >= 1

    # 3. Custom vectors (filled with 0.42) reached FakeVectorize
    assert len(fake_vectorize.store) >= 1
    for (ns, vec_id), vec_data in fake_vectorize.store.items():
        assert vec_data["values"][0] == 0.42

    # 4. Search query embedding routes through custom AI provider
    matches = await search_vectorize(
        env,
        test_principal.user_id,
        "bookmarking service",
        scope=scope,
    )
    # The search query was sent to embed
    assert any("bookmarking service" in call for call in ai_provider.embed_calls)
    assert len(matches) >= 1
