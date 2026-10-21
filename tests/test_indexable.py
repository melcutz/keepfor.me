# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for the indexer hooks (keepfor-cloud#120).

An item that holds no text must be skipped by the indexer up front instead
of being written to the index and deleted after extraction.
"""

import pytest

from keepfor.auth.service import register_user
from keepfor.search.indexable import (
    NON_TEXT_EXTENSIONS,
    should_index_text,
    should_index_title,
)


class MockEnv:
    DB = None
    BUCKET = None
    AI = None
    VECTORIZE = None
    QUEUE = None


PHOTO_URL = "https://example.com/photo.jpg"


def _item(**over):
    item = {
        "url": "https://example.com/post",
        "title": "A real post about something",
        "content_text": "Readable article body with a few words in it.",
        "status": "ok",
    }
    item.update(over)
    return item


# ==========================================
# should_index_text: the indexer hook
# ==========================================


def test_text_item_is_indexed():
    assert should_index_text(_item()) is True


def test_failed_status_is_skipped():
    assert should_index_text(_item(status="failed")) is False
    assert should_index_text(_item(status="Error")) is False


def test_non_text_url_extensions_are_skipped():
    for extension in ("jpg", "png", "mp4", "zip", "exe"):
        assert extension in NON_TEXT_EXTENSIONS
        item = _item(url=f"https://example.com/photo.{extension}")
        assert should_index_text(item) is False


def test_query_string_does_not_defeat_the_extension_rule():
    item = _item(url="https://example.com/photo.jpg?size=full", content_text="...x")
    assert should_index_text(item) is False


def test_empty_content_is_skipped():
    assert should_index_text(_item(content_text="   ")) is False


def test_bare_url_item_is_skipped():
    url = "https://example.com/a/only"
    assert should_index_text(_item(url=url, title=url, content_text="")) is False


def test_url_only_body_is_skipped():
    item = _item(content_text="https://example.com/redirected-here")
    assert should_index_text(item) is False


def test_binary_payload_is_skipped():
    binary = "".join(chr(byte) for byte in range(1, 32)) * 10
    assert should_index_text(_item(content_text=binary)) is False


def test_missing_keys_tolerated_and_skipped_by_default():
    none_item = {"url": None, "title": None, "content_text": None}
    assert should_index_text({}) is False
    assert should_index_text(none_item) is False


# ==========================================
# should_index_title: title-only indexing (keepfor-cloud#119)
# ==========================================


def test_title_hook_stays_open_for_non_text_items():
    item = _item(url="https://example.com/photo.jpg", content_text="")
    assert should_index_text(item) is False
    # The title-only row stays possible even where the text index skips.
    assert should_index_title(item) is True


def test_title_hook_follows_the_title():
    assert should_index_title(_item()) is True
    assert should_index_title(_item(title="   ")) is False


# ==========================================
# Processor wiring: the extraction indexer uses the hook
# ==========================================


class FakeVectorize:
    """A Vectorize binding that records the vectors it is given."""

    def __init__(self):
        self.upserts: list[dict] = []

    async def upsert(self, vectors):
        self.upserts.extend(vectors)


def _env_with_vectors():
    """An env whose Vectorize binding records the vectors it receives."""
    env = MockEnv()
    env.VECTORIZE = FakeVectorize()
    return env


async def _insert_photo(db, user_id, item_id):
    """Saves a .jpg item (no text to extract)."""
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status, created_at) "
        "VALUES (?,?,?,?, 'queued', '2026-01-01 00:00:00');",
        (item_id, user_id, PHOTO_URL, PHOTO_URL),
    )


async def _extract_photo(db, monkeypatch, env, item_id, title="The Photograph"):
    """Extracts the item as a title with no text; returns the chunker calls."""
    from keepfor.consumer import processor

    async def fake_fetch_and_extract(url, item_id):
        return {
            "title": title,
            "byline": "",
            "site_name": "example.com",
            "published_date": "",
            "excerpt": "",
            "content_text": "",
            "word_count": 0,
            "is_fallback": 1,
            "clean_html": "<article><p></p></article>",
        }, "<html>raw</html>"

    monkeypatch.setattr(
        "keepfor.consumer.processor._fetch_and_extract", fake_fetch_and_extract
    )
    chunk_calls: list[tuple[str, tuple]] = []
    real_split = processor.recursive_character_split

    def spy(text, **kwargs):
        chunk_calls.append((text, tuple(kwargs)))
        return real_split(text, **kwargs)

    monkeypatch.setattr(processor, "recursive_character_split", spy)

    await processor.extract_and_store(db, env, item_id, PHOTO_URL)
    return chunk_calls


@pytest.mark.asyncio
async def test_processor_keeps_a_title_only_row_for_a_non_text_item(db, monkeypatch):
    """An image save keeps a title-only FTS row: no text, but findable."""
    user = await register_user(db, "u1@test.local", "password123")
    item_id = "55555555-5555-5555-5555-555555555555"
    env = _env_with_vectors()

    await _insert_photo(db, user["id"], item_id)
    await db.execute(
        "UPDATE items SET why_saved = ? WHERE id = ?;",
        ("A note about the exhibition", item_id),
    )
    chunk_calls = await _extract_photo(db, monkeypatch, env, item_id)

    row = await db.query_first("SELECT status, title FROM items WHERE id=?", (item_id,))
    assert row["status"] == "ok"
    fts = await db.query_first(
        "SELECT title, content_text FROM items_fts WHERE item_id = ? AND user_id = ?;",
        (item_id, user["id"]),
    )
    assert fts is not None
    assert fts["title"] == "The Photograph"
    # The title and the user's own note, no extracted text.
    assert fts["content_text"] == "A note about the exhibition"
    # No chunks means no embeddings and no vectors.
    chunks = await db.query_all("SELECT * FROM chunks WHERE item_id = ?;", (item_id,))
    assert chunks == []
    assert chunk_calls == []
    assert env.VECTORIZE.upserts == []


@pytest.mark.asyncio
async def test_a_textless_item_is_found_by_a_title_search(db, monkeypatch):
    """A saved .jpg is found by its title in the library search."""
    from keepfor.search.engine import hybrid_search

    user = await register_user(db, "u3@test.local", "password123")
    item_id = "77777777-7777-7777-7777-777777777777"
    env = _env_with_vectors()

    await _insert_photo(db, user["id"], item_id)
    await _extract_photo(db, monkeypatch, env, item_id)

    results, total = await hybrid_search(db, env, user["id"], "Photograph")
    assert [r["id"] for r in results] == [item_id]
    assert total == 1


@pytest.mark.asyncio
async def test_a_re_extraction_keeps_the_title_only_row(db, monkeypatch):
    """Extracting the same textless item again keeps its title-only row."""
    user = await register_user(db, "u4@test.local", "password123")
    item_id = "88888888-8888-8888-8888-888888888888"
    env = _env_with_vectors()

    await _insert_photo(db, user["id"], item_id)
    await _extract_photo(db, monkeypatch, env, item_id)
    await _extract_photo(db, monkeypatch, env, item_id)

    rows = await db.query_all(
        "SELECT title, content_text FROM items_fts WHERE item_id = ? AND user_id = ?;",
        (item_id, user["id"]),
    )
    assert len(rows) == 1
    assert rows[0]["title"] == "The Photograph"
    assert not (rows[0]["content_text"] or "").strip()


@pytest.mark.asyncio
async def test_processor_still_indexes_a_text_item(db, monkeypatch):
    """A real article keeps its full text FTS row and gets chunked."""
    from keepfor.consumer import processor

    user = await register_user(db, "u2@test.local", "password123")
    item_id = "66666666-6666-6666-6666-666666666666"
    url = "https://example.com/article"
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status, created_at) "
        "VALUES (?,?,?,?, 'queued', '2026-01-01 00:00:00');",
        (item_id, user["id"], url, url),
    )

    long_text = "This is a meaningful article body that has plenty of text. " * 20

    async def fake_fetch_and_extract(url, item_id):
        return {
            "title": "Real Article",
            "byline": "Author",
            "site_name": "example.com",
            "published_date": "2026-01-01",
            "excerpt": "Short excerpt",
            "content_text": long_text,
            "word_count": len(long_text.split()),
            "is_fallback": 0,
            "clean_html": f"<article><p>{long_text}</p></article>",
        }, "<html>raw</html>"

    monkeypatch.setattr(
        "keepfor.consumer.processor._fetch_and_extract", fake_fetch_and_extract
    )
    await processor.extract_and_store(db, MockEnv(), item_id, url)

    row = await db.query_first("SELECT status FROM items WHERE id=?", (item_id,))
    assert row["status"] == "ok"
    fts = await db.query_first(
        "SELECT title, content_text FROM items_fts WHERE item_id = ? AND user_id = ?;",
        (item_id, user["id"]),
    )
    assert fts is not None
    assert fts["title"] == "Real Article"
    assert "meaningful article body" in fts["content_text"]
