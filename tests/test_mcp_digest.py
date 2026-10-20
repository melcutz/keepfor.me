# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for the MCP library_digest tool (GitHub issue #22)."""

import json

import pytest

from keepfor.auth.service import register_user
from keepfor.mcp.server import MCP_TOOLS, process_mcp_request
from keepfor.models.items import save_item


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
async def digest_user(db):
    user = await register_user(db, "digest@example.com", "password123")
    return user, MockEnv(), db


async def _save(db, env, user_id, url, tags, *, title=None, saved_at=None):
    """Saves an item and pins its title/save date when asked."""
    item, _ = await save_item(db, env, user_id, url, tags=tags)
    if title or saved_at:
        await db.execute(
            "UPDATE items SET title = ?, created_at = ? WHERE id = ? AND user_id = ?;",
            (title, saved_at, item["id"], user_id),
        )
    return item


async def _call(db, env, user, arguments, tool="library_digest"):
    """Calls an MCP tool and returns the parsed payload (or the error)."""
    res = await process_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        db,
        env,
        user,
    )
    if "error" in res:
        return res["error"]
    return json.loads(res["result"]["content"][0]["text"])


@pytest.mark.asyncio
async def test_library_digest_reports_saves_opens_and_tags(digest_user):
    """The digest counts saves and opens and lists the top tags."""
    user, env, db = digest_user

    await _save(
        db,
        env,
        user["id"],
        "https://example.com/a",
        ["tech", "ai"],
        title="A",
        saved_at="2026-06-01 09:00:00",
    )
    await _save(
        db,
        env,
        user["id"],
        "https://example.com/b",
        ["tech"],
        title="B",
        saved_at="2026-06-02 09:00:00",
    )
    await _save(
        db,
        env,
        user["id"],
        "https://example.com/c",
        ["science"],
        title="C",
        saved_at="2026-06-03 09:00:00",
    )
    await db.execute(
        "INSERT INTO item_opens (user_id, item_id, opened_at) VALUES (?, ?, ?);",
        (
            user["id"],
            (
                await db.query_first(
                    "SELECT id FROM items WHERE url = ?;", ("https://example.com/a",)
                )
            )["id"],
            "2026-06-04 09:00:00",
        ),
    )

    payload = await _call(db, env, user, {})

    assert payload["saves"] == 3
    assert payload["opens"] == 1
    assert payload["unread"] == 3
    assert payload["top_tags"][0] == {"name": "tech", "count": 2}
    assert {t["name"] for t in payload["top_tags"]} == {"tech", "ai", "science"}
    assert [h["title"] for h in payload["unread_highlights"]] == ["C", "B", "A"]
    assert payload["unread_highlights"][0]["tags"] == ["science"]
    assert payload["truncated"] is False


@pytest.mark.asyncio
async def test_library_digest_filters_by_period(digest_user):
    """since/until bound the saves and the opens to the period."""
    user, env, db = digest_user

    await _save(
        db,
        env,
        user["id"],
        "https://example.com/old",
        ["tech"],
        title="Old",
        saved_at="2026-01-10 09:00:00",
    )
    await _save(
        db,
        env,
        user["id"],
        "https://example.com/new",
        ["tech"],
        title="New",
        saved_at="2026-06-10 09:00:00",
    )
    old_id = (
        await db.query_first(
            "SELECT id FROM items WHERE url = ?;", ("https://example.com/old",)
        )
    )["id"]
    await db.execute(
        "INSERT INTO item_opens (user_id, item_id, opened_at) VALUES (?, ?, ?);",
        (user["id"], old_id, "2026-01-11 09:00:00"),
    )

    payload = await _call(db, env, user, {"since": "2026-06-01", "until": "2026-06-30"})
    assert payload["saves"] == 1
    assert payload["opens"] == 0
    assert [h["title"] for h in payload["unread_highlights"]] == ["New"]
    assert payload["period"] == {
        "since": "2026-06-01 00:00:00",
        "until": "2026-06-30 00:00:00",
    }

    payload = await _call(db, env, user, {"since": "2026-01-01"})
    assert payload["saves"] == 2
    assert payload["opens"] == 1
    assert payload["unread"] == 2


@pytest.mark.asyncio
async def test_library_digest_filters_by_tag(digest_user):
    """A tag restricts the digest to the items carrying it."""
    user, env, db = digest_user

    await _save(
        db,
        env,
        user["id"],
        "https://example.com/a",
        ["tech"],
        title="A",
        saved_at="2026-06-01 09:00:00",
    )
    await _save(
        db,
        env,
        user["id"],
        "https://example.com/b",
        ["science"],
        title="B",
        saved_at="2026-06-02 09:00:00",
    )

    payload = await _call(db, env, user, {"tag": "tech"})

    assert payload["tag"] == "tech"
    assert payload["saves"] == 1
    assert [h["title"] for h in payload["unread_highlights"]] == ["A"]
    assert {t["name"] for t in payload["top_tags"]} == {"tech"}


@pytest.mark.asyncio
async def test_library_digest_caps_output(digest_user):
    """Top tags and highlights are capped, and the digest says so."""
    user, env, db = digest_user

    for i in range(12):
        await _save(db, env, user["id"], f"https://example.com/{i}", [f"tag{i:02d}"])
    for i in range(8):
        await _save(db, env, user["id"], f"https://example.com/x{i}", ["bulk"])

    payload = await _call(db, env, user, {})

    assert len(payload["top_tags"]) == 10
    assert len(payload["unread_highlights"]) == 5
    assert payload["truncated"] is True


@pytest.mark.asyncio
async def test_library_digest_caps_tags_per_highlight(digest_user):
    """A highlight carries at most 5 tags."""
    user, env, db = digest_user

    await _save(
        db,
        env,
        user["id"],
        "https://example.com/many-tags",
        [f"tag{i}" for i in range(8)],
        title="Many tags",
        saved_at="2026-06-01 09:00:00",
    )

    payload = await _call(db, env, user, {})

    assert len(payload["unread_highlights"]) == 1
    assert len(payload["unread_highlights"][0]["tags"]) == 5
    assert payload["truncated"] is True


@pytest.mark.asyncio
async def test_library_digest_rejects_invalid_arguments(digest_user):
    """Bad dates, reversed ranges and oversized inputs are rejected."""
    user, env, db = digest_user

    error = await _call(db, env, user, {"since": "not-a-date"})
    assert error["data"]["type"] == "invalid_arguments"

    error = await _call(db, env, user, {"until": "2026-13-45"})
    assert error["data"]["type"] == "invalid_arguments"

    error = await _call(db, env, user, {"since": "2026-06-01", "until": "2026-01-01"})
    assert error["data"]["type"] == "invalid_arguments"

    error = await _call(db, env, user, {"since": "x" * 100})
    assert error["data"]["type"] == "invalid_arguments"

    error = await _call(db, env, user, {"tag": "t" * 100})
    assert error["data"]["type"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_library_digest_counts_distinct_opened_items(digest_user):
    """Opening the same item twice still counts as one opened item."""
    user, env, db = digest_user

    item, _ = await save_item(db, env, user["id"], "https://example.com/a")
    for _ in range(3):
        await db.execute(
            "INSERT INTO item_opens (user_id, item_id, opened_at) VALUES (?, ?, ?);",
            (user["id"], item["id"], "2026-06-04 09:00:00"),
        )

    payload = await _call(db, env, user, {})
    assert payload["opens"] == 1
    assert payload["saves"] == 1


@pytest.mark.asyncio
async def test_library_digest_response_is_compact(digest_user):
    """The digest stays under 4 KB even with long urls, titles and tags."""
    user, env, db = digest_user

    for i in range(20):
        await _save(
            db,
            env,
            user["id"],
            f"https://example.com/long-{i}/" + "segment/" * 60,
            [f"tag{i:02d}" + "x" * 60],
            title=f"{i} " + "a very long article title " * 20,
        )

    res = await process_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "library_digest", "arguments": {}},
        },
        db,
        env,
        user,
    )
    response_text = res["result"]["content"][0]["text"]
    assert len(response_text.encode("utf-8")) < 4096

    payload = json.loads(response_text)
    for highlight in payload["unread_highlights"]:
        assert len(highlight["url"]) <= 200
        assert len(highlight["title"]) <= 120
        assert all(len(t) <= 64 for t in highlight["tags"])
    assert all(len(t["name"]) <= 64 for t in payload["top_tags"])


@pytest.mark.asyncio
async def test_library_digest_stays_compact_with_fat_highlights(digest_user):
    """Worst case (5 highlights, each with 5 long tags) still fits the budget."""
    user, env, db = digest_user

    for i in range(5):
        await _save(
            db,
            env,
            user["id"],
            f"https://example.com/fat-{i}/" + "segment/" * 60,
            [f"tag{j}{i}" + "x" * 60 for j in range(5)],
            title=f"{i} " + "a very long article title " * 20,
        )

    res = await process_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "library_digest", "arguments": {}},
        },
        db,
        env,
        user,
    )
    response_text = res["result"]["content"][0]["text"]
    assert len(response_text.encode("utf-8")) < 4096

    payload = json.loads(response_text)
    assert payload["saves"] == 5
    assert payload["truncated"] is True
    # Highlights were dropped to fit the budget: the counts stay complete.
    assert len(payload["unread_highlights"]) < 5


@pytest.mark.asyncio
async def test_library_digest_is_read_only(digest_user):
    """The tool is annotated read-only and changes nothing."""
    user, env, db = digest_user

    await _save(db, env, user["id"], "https://example.com/a", ["tech"], title="A")
    before = await db.query_first(
        "SELECT COUNT(*) AS n FROM items WHERE user_id = ?;", (user["id"],)
    )

    payload = await _call(db, env, user, {})

    after = await db.query_first(
        "SELECT COUNT(*) AS n FROM items WHERE user_id = ?;", (user["id"],)
    )
    assert after["n"] == before["n"]
    assert payload["saves"] == 1

    tool = next(t for t in MCP_TOOLS if t["name"] == "library_digest")
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["annotations"]["destructiveHint"] is False
    assert tool["annotations"]["idempotentHint"] is True
