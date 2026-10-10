# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for automatic tagging inside extraction (processor hook)."""

import pytest

from src.auth.service import register_user
from src.models.items import add_tags_to_item, get_item


class FakeQueue:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, message: dict) -> None:
        self.sent.append(message)


class MockEnv:
    def __init__(self):
        self.DB = None
        self.QUEUE = None
        self.AI = None
        self.VECTORIZE = None
        self.BUCKET = None


HTML = (
    "<html><head><title>Postgres 16 release notes</title>"
    '<meta property="og:site_name" content="Example">'
    '<meta property="og:description" content="Postgres tuning guide.">'
    "</head><body><article>"
    "<p>Postgres 16 brings parallel vacuuming. Postgres connection tuning "
    "matters for production. Vector databases power semantic search and "
    "a vector database stores embeddings for similarity lookup.</p>"
    "<p>More text about Postgres performance and edge caching strategies "
    "for production workloads at scale.</p>"
    "</article></body></html>"
)


@pytest.fixture
async def user_env(db):
    user = await register_user(db, "tagger@keepfor.me", "password123")
    return user, MockEnv(), db


@pytest.mark.asyncio
async def test_extraction_auto_applies_existing_tag(user_env, monkeypatch):
    from src.models.items import save_item

    user, env, db = user_env
    # Seed the user's vocabulary on an unrelated item.
    seed_id = "seed-item"
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status)"
        " VALUES (?, ?, ?, ?, 'ok');",
        (seed_id, user["id"], "https://example.com/seed", "https://example.com/seed"),
    )
    await add_tags_to_item(db, user["id"], seed_id, ["postgres"])

    async def fake_fetch(url: str, headers=None) -> str:
        return HTML

    monkeypatch.setattr("src.consumer.processor.fetch_page_html", fake_fetch)
    item, _ = await save_item(db, env, user["id"], "https://example.com/pg16", [])
    fetched = await get_item(db, user["id"], item["id"])
    assert fetched["status"] == "ok"
    assert "postgres" in fetched["tags"]


@pytest.mark.asyncio
async def test_extraction_records_suggestions(user_env, monkeypatch):
    from src.models.items import save_item

    user, env, db = user_env

    async def fake_fetch(url: str, headers=None) -> str:
        return HTML

    monkeypatch.setattr("src.consumer.processor.fetch_page_html", fake_fetch)
    item, _ = await save_item(db, env, user["id"], "https://example.com/pg16", [])
    rows = await db.query_all(
        "SELECT phrase, status FROM suggested_tags WHERE item_id = ?;", (item["id"],)
    )
    assert rows, "expected novel keyphrases to be recorded as suggestions"
    assert all(r["status"] == "pending" for r in rows)
    assert len(rows) <= 8


@pytest.mark.asyncio
async def test_tagger_failure_never_fails_extraction(user_env, monkeypatch):
    from src.models.items import save_item

    user, env, db = user_env

    async def fake_fetch(url: str, headers=None) -> str:
        return HTML

    monkeypatch.setattr("src.consumer.processor.fetch_page_html", fake_fetch)

    def boom(*args, **kwargs):
        raise RuntimeError("tagger exploded")

    monkeypatch.setattr("src.utils.tagger.match_existing_tags", boom)
    item, _ = await save_item(db, env, user["id"], "https://example.com/pg16", [])
    fetched = await get_item(db, user["id"], item["id"])
    assert fetched["status"] == "ok"
