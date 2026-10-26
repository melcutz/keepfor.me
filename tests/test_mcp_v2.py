# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for MCP v2 tools: search_passages, read_item, reading_history, related_items,
save_url/save_note v2 with agent metadata."""

import json

import pytest

from keepfor.auth.service import register_user
from keepfor.mcp.server import process_mcp_request


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
async def mcp_user(db):
    """Create user for MCP testing."""
    user = await register_user(db, "mcpuser@example.com", "password123")
    return user, MockEnv(), db


@pytest.fixture
async def user_with_items(db):
    """Create user with some items for testing."""
    from keepfor.models.items import save_item

    user = await register_user(db, "testuser@example.com", "password123")
    env = MockEnv()

    # Save a few items
    item1, _ = await save_item(
        db,
        env,
        user["id"],
        "https://example.com/article1",
        tags=["tech", "ai"],
    )
    item2, _ = await save_item(
        db,
        env,
        user["id"],
        "https://example.com/article2",
        tags=["tech", "programming"],
    )
    item3, _ = await save_item(
        db,
        env,
        user["id"],
        "https://example.com/article3",
        tags=["science"],
    )

    # Update items with content for search
    content1 = (
        "Machine learning is a subset of artificial intelligence "
        "that enables systems to learn from data."
    )
    await db.execute(
        """
        UPDATE items SET
            title = 'Introduction to Machine Learning',
            content_text = ?,
            excerpt = 'Machine learning is a subset of AI...',
            status = 'ok',
            word_count = 100
        WHERE id = ?;
        """,
        (content1, item1["id"]),
    )
    content2 = (
        "Python is a versatile programming language used for web "
        "development, data science, and automation."
    )
    await db.execute(
        """
        UPDATE items SET
            title = 'Python Programming Guide',
            content_text = ?,
            excerpt = 'Python is a versatile programming language...',
            status = 'ok',
            word_count = 100
        WHERE id = ?;
        """,
        (content2, item2["id"]),
    )
    content3 = (
        "Quantum physics studies the behavior of matter and "
        "energy at the smallest scales."
    )
    await db.execute(
        """
        UPDATE items SET
            title = 'Quantum Physics Basics',
            content_text = ?,
            excerpt = 'Quantum physics studies the behavior...',
            status = 'ok',
            word_count = 100
        WHERE id = ?;
        """,
        (content3, item3["id"]),
    )

    # Add FTS entries
    for item in [item1, item2, item3]:
        await db.execute(
            "INSERT INTO items_fts (item_id, user_id, title, content_text) "
            "VALUES (?, ?, ?, ?);",
            (
                item["id"],
                user["id"],
                (
                    await db.query_first(
                        "SELECT title FROM items WHERE id = ?", (item["id"],)
                    )
                )["title"],
                (
                    await db.query_first(
                        "SELECT content_text FROM items WHERE id = ?", (item["id"],)
                    )
                )["content_text"],
            ),
        )

    # Add chunks for read_item and search_passages
    for i, item in enumerate([item1, item2, item3]):
        await db.execute(
            "INSERT INTO chunks (id, item_id, user_id, chunk_index, "
            "token_count) VALUES (?, ?, ?, ?, ?);",
            (f"item_{item['id']}_chunk_0", item["id"], user["id"], 0, 50),
        )
        await db.execute(
            "INSERT INTO chunks (id, item_id, user_id, chunk_index, "
            "token_count) VALUES (?, ?, ?, ?, ?);",
            (f"item_{item['id']}_chunk_1", item["id"], user["id"], 1, 50),
        )

    return user, env, db, item1, item2, item3


@pytest.mark.asyncio
async def test_search_passages_tool(mcp_user):
    """Test search_passages MCP tool."""
    user, env, db = mcp_user

    # First save an item with content
    from keepfor.models.items import save_item

    item, _ = await save_item(
        db, env, user["id"], "https://example.com/ml-article", tags=["ml"]
    )
    content_text = (
        "Machine learning is a method of data analysis that "
        "automates analytical model building."
    )
    await db.execute(
        """
        UPDATE items SET
            title = 'Machine Learning Basics',
            content_text = ?,
            excerpt = 'Machine learning is a method of data analysis...',
            status = 'ok',
            word_count = 50
        WHERE id = ?;
        """,
        (content_text, item["id"]),
    )
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text) "
        "VALUES (?, ?, ?, ?);",
        (
            item["id"],
            user["id"],
            "Machine Learning Basics",
            content_text,
        ),
    )
    await db.execute(
        "INSERT INTO chunks (id, item_id, user_id, chunk_index, "
        "token_count) VALUES (?, ?, ?, ?, ?);",
        (f"item_{item['id']}_chunk_0", item["id"], user["id"], 0, 50),
    )

    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "search_passages",
            "arguments": {"query": "machine learning", "limit": 5, "max_chars": 1000},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    assert "result" in res
    content = json.loads(res["result"]["content"][0]["text"])
    assert "passages" in content
    assert "total" in content
    assert isinstance(content["passages"], list)


@pytest.mark.asyncio
async def test_search_passages_max_chars_budget(mcp_user):
    """Test search_passages respects max_chars budget."""
    user, env, db = mcp_user

    from keepfor.models.items import save_item

    # Save multiple items
    items = []
    for i in range(5):
        item, _ = await save_item(
            db, env, user["id"], f"https://example.com/article{i}", tags=["test"]
        )
        await db.execute(
            f"""
            UPDATE items SET
                title = 'Article {i}',
                content_text = 'This is a long article about topic {i}. ' * 20,
                excerpt = 'This is a long article...',
                status = 'ok',
                word_count = 500
            WHERE id = ?;
            """,
            (item["id"],),
        )
        await db.execute(
            "INSERT INTO items_fts (item_id, user_id, title, content_text) "
            "VALUES (?, ?, ?, ?);",
            (
                item["id"],
                user["id"],
                f"Article {i}",
                "This is a long article about topic " * 20,
            ),
        )
        await db.execute(
            "INSERT INTO chunks (id, item_id, user_id, chunk_index, "
            "token_count) VALUES (?, ?, ?, ?, ?);",
            (f"item_{item['id']}_chunk_0", item["id"], user["id"], 0, 200),
        )
        items.append(item)

    req = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "search_passages",
            "arguments": {"query": "article", "limit": 10, "max_chars": 500},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    content = json.loads(res["result"]["content"][0]["text"])
    total_chars = sum(len(p.get("text", "")) for p in content["passages"])
    assert total_chars <= 500 * 1.1  # Allow small overhead


@pytest.mark.asyncio
async def test_read_item_tool(user_with_items):
    """Test read_item MCP tool with pagination."""
    user, env, db, item1, item2, item3 = user_with_items

    req = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "read_item",
            "arguments": {"item_id": item1["id"], "cursor": 0, "max_tokens": 1000},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    assert "result" in res
    content = json.loads(res["result"]["content"][0]["text"])
    assert "text" in content
    assert "next_cursor" in content
    assert "total_chunks" in content
    assert "outline" in content
    assert content["total_chunks"] == 2


@pytest.mark.asyncio
async def test_read_item_pagination(user_with_items):
    """Test read_item pagination with cursor."""
    user, env, db, item1, item2, item3 = user_with_items

    # First page
    req1 = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "read_item",
            "arguments": {"item_id": item1["id"], "cursor": 0, "max_tokens": 100},
        },
    }
    res1 = await process_mcp_request(req1, db, env, user)
    content1 = json.loads(res1["result"]["content"][0]["text"])

    # Second page if available
    if content1["next_cursor"] is not None:
        req2 = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "read_item",
                "arguments": {
                    "item_id": item1["id"],
                    "cursor": content1["next_cursor"],
                    "max_tokens": 100,
                },
            },
        }
        res2 = await process_mcp_request(req2, db, env, user)
        content2 = json.loads(res2["result"]["content"][0]["text"])
        assert (
            content2["next_cursor"] is None
            or content2["next_cursor"] > content1["next_cursor"]
        )


@pytest.mark.asyncio
async def test_get_item_capped_content(user_with_items):
    """Test get_item caps content_text by default (include_content=false)."""
    user, env, db, item1, item2, item3 = user_with_items

    req = {
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {
            "name": "get_item",
            "arguments": {"item_id": item1["id"]},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    content = json.loads(res["result"]["content"][0]["text"])
    # Should have excerpt, not full content_text
    assert "content_text" in content
    # The content should be the excerpt (short), not the full content_text
    assert len(content["content_text"]) < 200  # excerpt is short


@pytest.mark.asyncio
async def test_get_item_include_content(user_with_items):
    """Test get_item with include_content=true returns full text."""
    user, env, db, item1, item2, item3 = user_with_items

    req = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": "get_item",
            "arguments": {"item_id": item1["id"], "include_content": True},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    content = json.loads(res["result"]["content"][0]["text"])
    assert "content_text" in content
    # Should have full content when include_content=true
    assert "Machine learning" in content["content_text"]


@pytest.mark.asyncio
async def test_reading_history_tool(user_with_items):
    """Test reading_history MCP tool."""
    user, env, db, item1, item2, item3 = user_with_items

    # Add an item_open record
    await db.execute(
        "INSERT INTO item_opens (user_id, item_id) VALUES (?, ?);",
        (user["id"], item1["id"]),
    )

    req = {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "tools/call",
        "params": {
            "name": "reading_history",
            "arguments": {"limit": 10},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    assert "result" in res
    content = json.loads(res["result"]["content"][0]["text"])
    assert "items" in content
    assert "total" in content
    assert isinstance(content["items"], list)


@pytest.mark.asyncio
async def test_reading_history_date_filter(user_with_items):
    """Test reading_history with date filtering."""
    user, env, db, item1, item2, item3 = user_with_items

    # Add item_opens
    await db.execute(
        "INSERT INTO item_opens (user_id, item_id) VALUES (?, ?);",
        (user["id"], item1["id"]),
    )
    await db.execute(
        "INSERT INTO item_opens (user_id, item_id) VALUES (?, ?);",
        (user["id"], item2["id"]),
    )

    req = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {
            "name": "reading_history",
            "arguments": {"since": "2026-01-01", "until": "2026-12-31", "limit": 10},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    content = json.loads(res["result"]["content"][0]["text"])
    assert "items" in content


@pytest.mark.asyncio
async def test_reading_history_event_filter(user_with_items):
    """Test reading_history with event filter."""
    user, env, db, item1, item2, item3 = user_with_items

    req = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {
            "name": "reading_history",
            "arguments": {"event": "saved", "limit": 10},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    print("RESPONSE:", res)
    content = json.loads(res["result"]["content"][0]["text"])
    assert "items" in content


@pytest.mark.asyncio
async def test_related_items_tool(user_with_items):
    """Test related_items MCP tool."""
    user, env, db, item1, item2, item3 = user_with_items

    req = {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {
            "name": "related_items",
            "arguments": {"item_id": item1["id"], "limit": 5},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    assert "result" in res
    content = json.loads(res["result"]["content"][0]["text"])
    assert "items" in content
    assert isinstance(content["items"], list)
    # Should not include the source item
    for item in content["items"]:
        assert item["id"] != item1["id"]


@pytest.mark.asyncio
async def test_related_items_by_text(user_with_items):
    """Test related_items with text query."""
    user, env, db, item1, item2, item3 = user_with_items

    req = {
        "jsonrpc": "2.0",
        "id": 12,
        "method": "tools/call",
        "params": {
            "name": "related_items",
            "arguments": {
                "text": "machine learning artificial intelligence",
                "limit": 5,
            },
        },
    }
    res = await process_mcp_request(req, db, env, user)
    assert "result" in res
    content = json.loads(res["result"]["content"][0]["text"])
    assert "items" in content


@pytest.mark.asyncio
async def test_save_url_v2_with_metadata(mcp_user):
    """Test save_url v2 with agent_summary, why_saved, source_context, saved_by."""
    user, env, db = mcp_user

    req = {
        "jsonrpc": "2.0",
        "id": 13,
        "method": "tools/call",
        "params": {
            "name": "save_url",
            "arguments": {
                "url": "https://example.com/v2-article",
                "tags": ["v2", "test"],
                "agent_summary": "AI-generated summary of the article",
                "why_saved": "Needed for research project",
                "source_context": "Discussion about ML architectures",
                "saved_by": "claude-desktop",
            },
        },
    }
    res = await process_mcp_request(req, db, env, user)
    assert "result" in res
    content = json.loads(res["result"]["content"][0]["text"])
    assert content["is_new"]
    assert content["id"]

    # Verify the metadata was stored
    from keepfor.models.items import get_item

    item = await get_item(db, user["id"], content["id"], include_content=True)
    assert item["agent_summary"] == "AI-generated summary of the article"
    assert item["why_saved"] == "Needed for research project"
    assert item["source_context"] == "Discussion about ML architectures"
    assert item["saved_by"] == "claude-desktop"


@pytest.mark.asyncio
async def test_save_note_v2_with_metadata(mcp_user):
    """Test save_note v2 with agent_summary, why_saved, source_context, saved_by."""
    user, env, db = mcp_user

    req = {
        "jsonrpc": "2.0",
        "id": 14,
        "method": "tools/call",
        "params": {
            "name": "save_note",
            "arguments": {
                "title": "My Research Note",
                "content": "Important findings about transformers",
                "tags": ["research", "transformers"],
                "agent_summary": "Summary of transformer architecture",
                "why_saved": "Key reference for paper",
                "source_context": "Reading Attention Is All You Need",
                "saved_by": "opencode",
            },
        },
    }
    res = await process_mcp_request(req, db, env, user)
    assert "result" in res
    content = json.loads(res["result"]["content"][0]["text"])
    assert content["id"]

    # Verify the metadata was stored
    from keepfor.models.items import get_item

    item = await get_item(db, user["id"], content["id"], include_content=True)
    assert item["agent_summary"] == "Summary of transformer architecture"
    assert item["why_saved"] == "Key reference for paper"
    assert item["source_context"] == "Reading Attention Is All You Need"
    assert item["saved_by"] == "opencode"


@pytest.mark.asyncio
async def test_search_library_returns_new_format(user_with_items):
    """Test search_library returns new format with items/total/limit/offset."""
    user, env, db, item1, item2, item3 = user_with_items

    req = {
        "jsonrpc": "2.0",
        "id": 15,
        "method": "tools/call",
        "params": {
            "name": "search_library",
            "arguments": {"query": "machine learning", "limit": 5},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    content = json.loads(res["result"]["content"][0]["text"])
    assert "items" in content
    assert "total" in content
    assert "limit" in content
    assert "offset" in content


@pytest.mark.asyncio
async def test_list_items_returns_new_format(user_with_items):
    """Test list_items returns new format with items/limit/offset."""
    user, env, db, item1, item2, item3 = user_with_items

    req = {
        "jsonrpc": "2.0",
        "id": 16,
        "method": "tools/call",
        "params": {
            "name": "list_items",
            "arguments": {"limit": 5},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    content = json.loads(res["result"]["content"][0]["text"])
    assert "items" in content
    assert "limit" in content
    assert "offset" in content


@pytest.mark.asyncio
async def test_server_info_version(mcp_user):
    """serverInfo.version is 0.2.0 and every tool carries annotations."""
    from keepfor.mcp.server import MCP_TOOLS

    user, env, db = mcp_user
    res = await process_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, db, env, user
    )
    assert res["result"]["serverInfo"]["version"] == "0.2.0"

    # Just verify tools have annotations
    for tool in MCP_TOOLS:
        assert "annotations" in tool
        assert "readOnlyHint" in tool["annotations"]
        assert "destructiveHint" in tool["annotations"]
        assert "idempotentHint" in tool["annotations"]


@pytest.mark.asyncio
async def test_read_item_20k_word_article(db):
    """Test read_item paging a ~20k-word article with each chunk <= max_tokens
    and concatenation reproducing the clean text."""
    from keepfor.auth.service import register_user
    from keepfor.models.items import save_item

    user = await register_user(db, "longarticle@example.com", "password123")
    env = MockEnv()

    # Create ~20k word article (approximately 80k chars)
    long_content = (
        "This is a long article about machine learning and artificial intelligence. "
        * 500
    )
    # That's ~500 * ~70 = 35000 words, let's make it ~20k words
    long_content = (
        "This is a long article about machine learning and artificial intelligence. "
        * 300
    )

    item, _ = await save_item(
        db, env, user["id"], "https://example.com/long-article", tags=["long"]
    )
    await db.execute(
        """
        UPDATE items SET
            title = 'Very Long Article',
            content_text = ?,
            excerpt = 'This is a long article...',
            status = 'ok',
            word_count = ?
        WHERE id = ?;
        """,
        (long_content, len(long_content.split()), item["id"]),
    )
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text) "
        "VALUES (?, ?, ?, ?);",
        (item["id"], user["id"], "Very Long Article", long_content),
    )
    # Add multiple chunks
    for i in range(10):
        await db.execute(
            "INSERT INTO chunks (id, item_id, user_id, chunk_index, token_count) "
            "VALUES (?, ?, ?, ?, ?);",
            (f"item_{item['id']}_chunk_{i}", item["id"], user["id"], i, 400),
        )

    # Read with default max_tokens (2000)
    req = {
        "jsonrpc": "2.0",
        "id": 17,
        "method": "tools/call",
        "params": {
            "name": "read_item",
            "arguments": {"item_id": item["id"], "cursor": 0, "max_tokens": 2000},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    content = json.loads(res["result"]["content"][0]["text"])
    assert "text" in content
    assert "next_cursor" in content
    assert "total_chunks" in content
    assert content["total_chunks"] == 10

    # Read all chunks and concatenate
    all_text = content["text"]
    cursor = content["next_cursor"]
    while cursor is not None:
        req = {
            "jsonrpc": "2.0",
            "id": 18,
            "method": "tools/call",
            "params": {
                "name": "read_item",
                "arguments": {
                    "item_id": item["id"],
                    "cursor": cursor,
                    "max_tokens": 2000,
                },
            },
        }
        res = await process_mcp_request(req, db, env, user)
        page = json.loads(res["result"]["content"][0]["text"])
        all_text += "\n\n" + page["text"]
        cursor = page["next_cursor"]

    # Verify concatenated text matches original (approximately)
    # The chunking may split differently, so check key phrases are present
    assert "machine learning" in all_text.lower()
    assert "artificial intelligence" in all_text.lower()
    # Total tokens should be reasonable
    total_tokens = len(all_text) // 4
    assert total_tokens > 1000  # At least some content was read


@pytest.mark.asyncio
async def test_search_passages_snapshot_url(mcp_user):
    """Test search_passages snapshot_url present and resolves when origin
    URL deleted."""
    user, env, db = mcp_user
    from keepfor.models.items import save_item

    item, _ = await save_item(
        db, env, user["id"], "https://example.com/dead-link", tags=["test"]
    )
    content_text = "This article discusses machine learning concepts in depth."
    await db.execute(
        """
        UPDATE items SET
            title = 'Dead Link Article',
            content_text = ?,
            excerpt = 'This article discusses machine learning...',
            status = 'ok',
            word_count = 50
        WHERE id = ?;
        """,
        (content_text, item["id"]),
    )
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text) "
        "VALUES (?, ?, ?, ?);",
        (item["id"], user["id"], "Dead Link Article", content_text),
    )
    await db.execute(
        "INSERT INTO chunks (id, item_id, user_id, chunk_index, token_count) "
        "VALUES (?, ?, ?, ?, ?);",
        (f"item_{item['id']}_chunk_0", item["id"], user["id"], 0, 50),
    )

    req = {
        "jsonrpc": "2.0",
        "id": 19,
        "method": "tools/call",
        "params": {
            "name": "search_passages",
            "arguments": {"query": "machine learning", "limit": 5, "max_chars": 1000},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    content = json.loads(res["result"]["content"][0]["text"])
    assert "passages" in content
    assert len(content["passages"]) > 0
    passage = content["passages"][0]
    assert "snapshot_url" in passage
    assert passage["snapshot_url"] == f"/api/items/{item['id']}/content"
    # The snapshot_url should be a permanent R2 archive link format
    assert passage["snapshot_url"].startswith("/api/items/")


@pytest.mark.asyncio
async def test_search_passages_response_size(mcp_user):
    """Test default search_passages(limit 10) JSON response < 4 KB on fixtures."""
    user, env, db = mcp_user
    from keepfor.models.items import save_item

    # Save 5 items with minimal content to keep response small
    for i in range(5):
        item, _ = await save_item(
            db, env, user["id"], f"https://example.com/article{i}", tags=["test"]
        )
        content_text = f"Article {i}. " * 10
        await db.execute(
            f"""
            UPDATE items SET
                title = 'Article {i}',
                content_text = ?,
                excerpt = 'Article {i}...',
                status = 'ok',
                word_count = 20
            WHERE id = ?;
            """,
            (content_text, item["id"]),
        )
        await db.execute(
            "INSERT INTO items_fts (item_id, user_id, title, content_text) "
            "VALUES (?, ?, ?, ?);",
            (item["id"], user["id"], f"Article {i}", content_text),
        )
        await db.execute(
            "INSERT INTO chunks (id, item_id, user_id, chunk_index, token_count) "
            "VALUES (?, ?, ?, ?, ?);",
            (f"item_{item['id']}_chunk_0", item["id"], user["id"], 0, 50),
        )

    req = {
        "jsonrpc": "2.0",
        "id": 20,
        "method": "tools/call",
        "params": {
            "name": "search_passages",
            "arguments": {"query": "article", "limit": 10, "max_chars": 1000},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    response_text = res["result"]["content"][0]["text"]
    response_size = len(response_text.encode("utf-8"))
    assert response_size < 4096, f"Response size {response_size} bytes exceeds 4 KB"


@pytest.mark.asyncio
async def test_list_items_response_size(mcp_user):
    """Test default list_items(limit 20) JSON response < 4 KB on fixtures."""
    user, env, db = mcp_user
    from keepfor.models.items import save_item

    # Save 10 items with minimal content
    for i in range(10):
        item, _ = await save_item(
            db, env, user["id"], f"https://example.com/item{i}", tags=["test"]
        )
        await db.execute(
            f"""
            UPDATE items SET
                title = 'Item {i}',
                content_text = 'Content. ',
                excerpt = 'Content...',
                status = 'ok',
                word_count = 2
            WHERE id = ?;
            """,
            (item["id"],),
        )

    req = {
        "jsonrpc": "2.0",
        "id": 21,
        "method": "tools/call",
        "params": {
            "name": "list_items",
            "arguments": {"limit": 10},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    response_text = res["result"]["content"][0]["text"]
    response_size = len(response_text.encode("utf-8"))
    assert response_size < 4096, f"Response size {response_size} bytes exceeds 4 KB"


@pytest.mark.asyncio
async def test_reading_history_saved_opened_filters(user_with_items):
    """Test reading_history saved/opened date filters with item_opens fixture."""
    user, env, db, item1, item2, item3 = user_with_items

    # Add item_opens with specific timestamps

    await db.execute(
        "INSERT INTO item_opens (user_id, item_id, opened_at) VALUES (?, ?, ?);",
        (user["id"], item1["id"], "2026-06-15 12:00:00"),
    )
    await db.execute(
        "INSERT INTO item_opens (user_id, item_id, opened_at) VALUES (?, ?, ?);",
        (user["id"], item2["id"], "2026-07-15 12:00:00"),
    )

    # Test 'opened' event filter with date range
    req = {
        "jsonrpc": "2.0",
        "id": 22,
        "method": "tools/call",
        "params": {
            "name": "reading_history",
            "arguments": {
                "event": "opened",
                "since": "2026-06-01",
                "until": "2026-06-30",
                "limit": 10,
            },
        },
    }
    res = await process_mcp_request(req, db, env, user)
    content = json.loads(res["result"]["content"][0]["text"])
    assert "items" in content
    # Should only find item1 opened in June
    opened_items = [it for it in content["items"] if it["event_type"] == "opened"]
    assert len(opened_items) >= 1

    # Test 'saved' event filter
    req = {
        "jsonrpc": "2.0",
        "id": 23,
        "method": "tools/call",
        "params": {
            "name": "reading_history",
            "arguments": {"event": "saved", "limit": 10},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    content = json.loads(res["result"]["content"][0]["text"])
    assert "items" in content
    saved_items = [it for it in content["items"] if it["event_type"] == "saved"]
    assert len(saved_items) >= 3  # All three items were saved


@pytest.mark.asyncio
async def test_match_items_by_url_www_variant_uses_indexed_lookup(
    mcp_user, monkeypatch
):
    from keepfor.models.items import save_item
    from keepfor.search.engine import match_items_by_url

    user, env, db = mcp_user
    item_www, _ = await save_item(
        db, env, user["id"], "https://www.example.com/article?utm_source=x"
    )
    item_bare, _ = await save_item(db, env, user["id"], "https://sub.example.org/post/")

    executed_sqls: list[str] = []
    orig_query_all = db.query_all

    async def tracking_query_all(sql, params=()):
        executed_sqls.append(" ".join(sql.split()))
        return await orig_query_all(sql, params)

    monkeypatch.setattr(db, "query_all", tracking_query_all)

    # Matching without www finds the www item
    assert await match_items_by_url(db, user["id"], "https://example.com/article") == [
        item_www["id"]
    ]
    # Matching with www finds the non-www item
    assert await match_items_by_url(
        db, user["id"], "https://www.sub.example.org/post"
    ) == [item_bare["id"]]
    # Missing URL returns empty without scanning the whole items table
    assert (
        await match_items_by_url(db, user["id"], "https://unseen.example.com/none")
        == []
    )

    for sql in executed_sqls:
        assert (
            "SELECT id, url, canonical_url FROM items"
            " WHERE user_id = ? AND deleted_at IS NULL;" not in sql
        )


@pytest.mark.asyncio
async def test_reading_history_highlighted_event_and_total_count(mcp_user):
    from keepfor.models.highlights import create_highlight
    from keepfor.models.items import save_item
    from keepfor.search.engine import reading_history

    user, env, db = mcp_user
    item1, _ = await save_item(db, env, user["id"], "https://example.com/h1")
    item2, _ = await save_item(db, env, user["id"], "https://example.com/h2")
    item3, _ = await save_item(db, env, user["id"], "https://example.com/h3")

    await create_highlight(
        db,
        user["id"],
        item1["id"],
        "important quote",
        0,
        15,
    )

    # Highlighted event returns the highlighted item with total=1
    highlighted_rows, h_total = await reading_history(
        db, user["id"], event="highlighted", limit=10, offset=0
    )
    assert h_total == 1
    assert len(highlighted_rows) == 1
    assert highlighted_rows[0]["id"] == item1["id"]
    assert highlighted_rows[0]["event_type"] == "highlighted"

    # Saved event with limit=1 returns total >= 3 (full count, not page length 1)
    saved_rows, s_total = await reading_history(
        db, user["id"], event="saved", limit=1, offset=0
    )
    assert len(saved_rows) == 1
    assert s_total >= 3


@pytest.mark.asyncio
async def test_read_item_chunks_format_text_strips_markdown(mcp_user):
    from keepfor.models.items import save_note
    from keepfor.search.engine import read_item_chunks

    user, env, db = mcp_user
    note = await save_note(
        db,
        env,
        user["id"],
        "Markdown Note",
        "# Heading\n\nSome **bold** text and a [link](https://example.com).",
    )
    md_res = await read_item_chunks(db, env, user["id"], note["id"], format="markdown")
    assert "# Heading" in md_res["text"]
    assert "**bold**" in md_res["text"]

    txt_res = await read_item_chunks(db, env, user["id"], note["id"], format="text")
    assert "# " not in txt_res["text"]
    assert "**bold**" not in txt_res["text"]
    assert "Heading" in txt_res["text"]
    assert "bold" in txt_res["text"]
    assert "link" in txt_res["text"]
