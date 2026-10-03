"""Tests for items management and hybrid search."""

import pytest
from src.auth.service import register_user
from src.models.items import save_item, get_item, delete_item
from src.search.engine import hybrid_search

class MockEnv:
    """Mock Cloudflare environment for testing."""
    def __init__(self):
        self.DB = None
        self.QUEUE = None
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
    item2, is_new2 = await save_item(db, env, user["id"], "https://example.com/post", ["reading"])
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
        SET title = 'Modern Edge Architecture', content_text = 'Cloudflare Python Workers enable scalable distributed computing with low latency.', status = 'ok'
        WHERE id = ?;
        """,
        (item["id"],)
    )
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text) VALUES (?, ?, 'Modern Edge Architecture', 'Cloudflare Python Workers enable scalable distributed computing with low latency.');",
        (item["id"], user["id"])
    )

    # Keyword search
    results = await hybrid_search(db, env, user["id"], query="Python Workers", mode="keyword")
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
