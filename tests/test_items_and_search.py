import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.auth.service import register_user
from src.models.items import delete_item, get_item, save_item
from src.search.engine import hybrid_search, search_vectorize


class FakeQueue:
    """In-test queue: records sends without delivery."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, message: dict) -> None:
        self.sent.append(message)


class MockEnv:
    """Mock Cloudflare environment for testing."""

    def __init__(self):
        self.DB = None
        self.QUEUE = FakeQueue()
        self.AI = None
        self.VECTORIZE = None
        self.BUCKET = None


@pytest.fixture
async def user_with_env(db):
    """Create test user and mock environment."""
    user = await register_user(db, "tester@keepfor.me", "password123")
    env = MockEnv()
    return user, env, db


@pytest.mark.asyncio
async def test_save_and_deduplicate(user_with_env):
    """Test saving items and deduplication."""
    user, env, db = user_with_env
    url = "https://example.com/post?utm_source=twitter"
    # 1. Save new
    item1, is_new1 = await save_item(db, env, user["id"], url, ["tech", "ai"])
    assert is_new1
    assert item1["status"] == "queued"

    # 2. Save duplicate with extra tag
    item2, is_new2 = await save_item(
        db, env, user["id"], "https://example.com/post", ["reading"]
    )
    assert not is_new2
    assert item1["id"] == item2["id"]

    # Tags should be merged
    fetched = await get_item(db, user["id"], item1["id"])
    assert "tech" in fetched["tags"]
    assert "reading" in fetched["tags"]


@pytest.mark.asyncio
async def test_fts5_search(user_with_env):
    """Test FTS5 keyword search functionality."""
    user, env, db = user_with_env
    item, _ = await save_item(db, env, user["id"], "https://example.com/arch", ["tech"])

    # Populate FTS5 table
    await db.execute(
        """
        UPDATE items
        SET title = 'Modern Edge Architecture',
            content_text = 'Cloudflare Python Workers enable scalable ' ||
                'distributed computing with low latency.',
            status = 'ok'
        WHERE id = ?;
        """,
        (item["id"],),
    )
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text) "
        "VALUES (?, ?, 'Modern Edge Architecture', "
        "'Cloudflare Python Workers enable scalable distributed computing "
        "with low latency.');",
        (item["id"], user["id"]),
    )

    # Keyword search
    results, total = await hybrid_search(
        db, env, user["id"], query="Python Workers", mode="keyword"
    )
    assert total == 1
    assert len(results) == 1
    assert results[0]["id"] == item["id"]
    assert "Architecture" in results[0]["title"]


@pytest.mark.asyncio
async def test_delete_item(user_with_env):
    """Test item deletion."""
    user, env, db = user_with_env
    item, _ = await save_item(db, env, user["id"], "https://example.com/delete-me")
    deleted = await delete_item(db, env, user["id"], item["id"])
    assert deleted

    fetched = await get_item(db, user["id"], item["id"])
    assert fetched is None


@pytest.mark.asyncio
async def test_process_import_batch_fans_out(user_with_env, sqlite_conn):
    """Queue consumer saves each import-batch bookmark and re-enqueues it."""
    from types import SimpleNamespace

    from src.consumer.processor import process_queue_batch

    user, env, db = user_with_env
    env.sqlite_conn = sqlite_conn
    batch = SimpleNamespace(
        messages=[
            {
                "user_id": user["id"],
                "import_batch": [
                    {"url": "https://example.com/a", "tags": ["news"]},
                    {"url": "https://example.com/b", "tags": []},
                ],
            }
        ]
    )
    await process_queue_batch(batch, env)

    rows = await db.query_all(
        "SELECT status FROM items WHERE user_id = ?;", (user["id"],)
    )
    assert len(rows) == 2
    assert all(r["status"] == "queued" for r in rows)
    assert len(env.QUEUE.sent) == 2
    sent_urls = {m["url"] for m in env.QUEUE.sent}
    assert sent_urls == {"https://example.com/a", "https://example.com/b"}


@pytest.mark.asyncio
async def test_save_enqueues_extraction_job(user_with_env):
    """Saving with a queue binding enqueues instead of extracting inline."""
    user, env, db = user_with_env
    item, is_new = await save_item(db, env, user["id"], "https://example.com/q")
    assert is_new
    assert item["status"] == "queued"
    assert len(env.QUEUE.sent) == 1
    assert env.QUEUE.sent[0]["item_id"] == item["id"]


@pytest.mark.asyncio
async def test_save_without_queue_extracts_inline(user_with_env, monkeypatch):
    """No queue binding must not leave items stuck in 'queued'."""
    user, env, db = user_with_env
    env.QUEUE = None
    html = (
        "<html><head><title>Inline Article</title>"
        '<meta property="og:site_name" content="Example">'
        '<meta property="og:description" content="An inline excerpt.">'
        "</head><body><p>body text here</p></body></html>"
    )

    async def fake_fetch(url: str) -> str:
        return html

    monkeypatch.setattr("src.consumer.processor.fetch_page_html", fake_fetch)
    item, is_new = await save_item(
        db, env, user["id"], "https://example.com/inline", []
    )
    assert is_new
    fetched = await get_item(db, user["id"], item["id"])
    assert fetched["status"] == "ok"
    assert fetched["title"] == "Inline Article"
    assert fetched["site_name"] == "Example"
    fts = await db.query_first(
        "SELECT title FROM items_fts WHERE item_id = ?;", (item["id"],)
    )
    assert fts is not None


@pytest.mark.asyncio
async def test_save_without_queue_marks_failed_on_fetch_error(
    user_with_env, monkeypatch
):
    """Inline extraction failure surfaces as failed, never stuck extracting."""
    user, env, db = user_with_env
    env.QUEUE = None

    async def boom(url: str) -> str:
        raise RuntimeError("connection refused")

    monkeypatch.setattr("src.consumer.processor.fetch_page_html", boom)
    item, _ = await save_item(db, env, user["id"], "https://example.com/broken", [])
    fetched = await get_item(db, user["id"], item["id"])
    assert fetched["status"] == "failed"
    assert "connection refused" in (fetched["fail_reason"] or "")


JINA_SAMPLE = """Title: Proxied Article
URL Source: https://blocked.example.com/article
Markdown Content:
# Hello

Some body text with a [link](https://example.com/x).
"""


@pytest.mark.asyncio
async def test_forbidden_falls_back_to_reader_proxy(user_with_env, monkeypatch):
    """A 403 direct fetch recovers via the reader proxy instead of failing."""
    from src.consumer.processor import OriginHttpError

    user, env, db = user_with_env
    env.QUEUE = None
    calls = []

    async def fake_fetch(url: str, headers=None) -> str:
        calls.append(url)
        raise OriginHttpError(403, url)

    async def fake_jina(url: str) -> str:
        calls.append("jina:" + url)
        return JINA_SAMPLE

    monkeypatch.setattr("src.consumer.processor.fetch_page_html", fake_fetch)
    monkeypatch.setattr("src.consumer.processor.fetch_jina_reader", fake_jina)
    item, _ = await save_item(db, env, user["id"], "https://blocked.example.com/a", [])
    fetched = await get_item(db, user["id"], item["id"])
    assert fetched["status"] == "ok"
    assert fetched["title"] == "Proxied Article"
    assert fetched["site_name"] == "blocked.example.com"
    assert "Some body text" in (fetched["content_text"] or "")
    assert calls == [
        "https://blocked.example.com/a",
        "jina:https://blocked.example.com/a",
    ]
    assert fetched["is_fallback"] == 0


@pytest.mark.asyncio
async def test_non_forbidden_skips_reader_proxy(user_with_env, monkeypatch):
    """A 404 direct fetch fails without ever calling the reader proxy."""
    from src.consumer.processor import OriginHttpError

    user, env, db = user_with_env
    env.QUEUE = None
    jina_calls = []

    async def fake_fetch(url: str, headers=None) -> str:
        raise OriginHttpError(404, url)

    async def fake_jina(url: str) -> str:
        jina_calls.append(url)
        return JINA_SAMPLE

    monkeypatch.setattr("src.consumer.processor.fetch_page_html", fake_fetch)
    monkeypatch.setattr("src.consumer.processor.fetch_jina_reader", fake_jina)
    item, _ = await save_item(db, env, user["id"], "https://example.com/missing", [])
    fetched = await get_item(db, user["id"], item["id"])
    assert fetched["status"] == "failed"
    assert "404" in (fetched["fail_reason"] or "")
    assert jina_calls == []


@pytest.mark.asyncio
async def test_failed_proxy_keeps_original_403(user_with_env, monkeypatch):
    """If the proxy also fails, the recorded reason stays the original 403."""
    from src.consumer.processor import OriginHttpError

    user, env, db = user_with_env
    env.QUEUE = None

    async def fake_fetch(url: str, headers=None) -> str:
        raise OriginHttpError(403, url)

    async def fake_jina(url: str) -> str:
        raise OriginHttpError(402, "https://r.jina.ai/" + url)

    monkeypatch.setattr("src.consumer.processor.fetch_page_html", fake_fetch)
    monkeypatch.setattr("src.consumer.processor.fetch_jina_reader", fake_jina)
    item, _ = await save_item(db, env, user["id"], "https://blocked.example.com/b", [])
    fetched = await get_item(db, user["id"], item["id"])
    assert fetched["status"] == "failed"
    assert "403" in (fetched["fail_reason"] or "")
    assert "402" not in (fetched["fail_reason"] or "")


def test_reader_markdown_parsing():
    """Jina-style markdown becomes a titled article with safe HTML."""
    from src.consumer.extractor import article_from_reader_markdown

    art = article_from_reader_markdown("https://blocked.example.com/a", JINA_SAMPLE)
    assert art["title"] == "Proxied Article"
    assert art["site_name"] == "blocked.example.com"
    assert "Some body text" in art["content_text"]
    assert '<a href="https://example.com/x"' in art["clean_html"]
    assert "<script" not in art["clean_html"]
    assert art["word_count"] > 0
    assert art["excerpt"]


def test_reader_markdown_without_header_block():
    """Plain markdown without Jina headers still yields a usable article."""
    from src.consumer.extractor import article_from_reader_markdown

    art = article_from_reader_markdown(
        "https://example.com/plain", "Just some text.\n\nSecond paragraph."
    )
    assert art["title"] == "https://example.com/plain"
    assert art["content_text"].startswith("Just some text.")
    assert art["clean_html"].count("<p>") == 2


@pytest.mark.asyncio
async def test_urllib_http_error_maps_to_origin_error(monkeypatch):
    """Local urllib 403s surface as OriginHttpError (proxy-eligible)."""
    import urllib.error
    import urllib.request

    from src.consumer.processor import OriginHttpError, fetch_page_html

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    try:
        await fetch_page_html("https://example.com/blocked")
        raise AssertionError("expected OriginHttpError")
    except OriginHttpError as exc:
        assert exc.status_code == 403


@pytest.mark.asyncio
async def test_rate_limited_falls_back_to_reader_proxy(user_with_env, monkeypatch):
    """A 429 direct fetch recovers via the reader proxy."""
    from src.consumer.processor import OriginHttpError

    user, env, db = user_with_env
    env.QUEUE = None
    calls = []

    async def fake_fetch(url: str, headers=None) -> str:
        calls.append(url)
        raise OriginHttpError(429, url)

    async def fake_jina(url: str) -> str:
        calls.append("jina:" + url)
        return JINA_SAMPLE

    monkeypatch.setattr("src.consumer.processor.fetch_page_html", fake_fetch)
    monkeypatch.setattr("src.consumer.processor.fetch_jina_reader", fake_jina)
    item, _ = await save_item(
        db, env, user["id"], "https://ratelimited.example.com/a", []
    )
    fetched = await get_item(db, user["id"], item["id"])
    assert fetched["status"] == "ok"
    assert fetched["title"] == "Proxied Article"
    assert calls == [
        "https://ratelimited.example.com/a",
        "jina:https://ratelimited.example.com/a",
    ]


@pytest.mark.asyncio
async def test_cloudflare_530_falls_back_to_reader_proxy(user_with_env, monkeypatch):
    """A 530 Cloudflare origin error recovers via the reader proxy."""
    from src.consumer.processor import OriginHttpError

    user, env, db = user_with_env
    env.QUEUE = None

    async def fake_fetch(url: str, headers=None) -> str:
        raise OriginHttpError(530, url)

    async def fake_jina(url: str) -> str:
        return JINA_SAMPLE

    monkeypatch.setattr("src.consumer.processor.fetch_page_html", fake_fetch)
    monkeypatch.setattr("src.consumer.processor.fetch_jina_reader", fake_jina)
    item, _ = await save_item(
        db, env, user["id"], "https://cf-blocked.example.com/a", []
    )
    fetched = await get_item(db, user["id"], item["id"])
    assert fetched["status"] == "ok"
    assert fetched["title"] == "Proxied Article"


@pytest.mark.asyncio
async def test_cloudflare_522_falls_back_to_reader_proxy(user_with_env, monkeypatch):
    """A 522 Cloudflare timeout recovers via the reader proxy."""
    from src.consumer.processor import OriginHttpError

    user, env, db = user_with_env
    env.QUEUE = None

    async def fake_fetch(url: str, headers=None) -> str:
        raise OriginHttpError(522, url)

    async def fake_jina(url: str) -> str:
        return JINA_SAMPLE

    monkeypatch.setattr("src.consumer.processor.fetch_page_html", fake_fetch)
    monkeypatch.setattr("src.consumer.processor.fetch_jina_reader", fake_jina)
    item, _ = await save_item(
        db, env, user["id"], "https://cf-timeout.example.com/a", []
    )
    fetched = await get_item(db, user["id"], item["id"])
    assert fetched["status"] == "ok"
    assert fetched["title"] == "Proxied Article"


@pytest.mark.asyncio
async def test_search_vectorize_logs_warning_on_failure(caplog):
    mock_env = MagicMock()
    mock_env.AI.run = AsyncMock(side_effect=RuntimeError("AI binding timeout"))
    mock_env.VECTORIZE = MagicMock()

    with caplog.at_level(logging.WARNING):
        res = await search_vectorize(mock_env, "user-123", "test query")
        assert res == []
        assert any("Vector search query failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_delete_item_logs_warning_on_vectorize_and_r2_failure(
    user_with_env, caplog
):
    user, env, db = user_with_env
    item, _ = await save_item(db, env, user["id"], "https://example.com/delete-fail")

    # Add chunk so vectorize delete is triggered
    await db.execute(
        "INSERT INTO chunks (id, item_id, user_id, chunk_index, token_count) "
        "VALUES (?, ?, ?, ?, ?);",
        ("chunk-1", item["id"], user["id"], 0, 100),
    )

    env.VECTORIZE = MagicMock()
    env.VECTORIZE.deleteByIds = AsyncMock(side_effect=RuntimeError("Vectorize error"))
    env.BUCKET = MagicMock()
    env.BUCKET.delete = AsyncMock(side_effect=RuntimeError("R2 error"))

    with caplog.at_level(logging.WARNING):
        deleted = await delete_item(db, env, user["id"], item["id"])
        assert deleted is True
        messages = [r.message for r in caplog.records]
        assert any("Failed to delete vector embeddings" in m for m in messages)
        assert any("Failed to delete R2 snapshots" in m for m in messages)
