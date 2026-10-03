"""Tests for items management and hybrid search."""

import pytest

from src.auth.service import register_user
from src.models.items import delete_item, get_item, save_item
from src.search.engine import hybrid_search


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
    results = await hybrid_search(
        db, env, user["id"], query="Python Workers", mode="keyword"
    )
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
