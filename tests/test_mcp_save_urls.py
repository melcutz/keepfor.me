# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for the MCP save_urls tool (GitHub issue #92).

The tool saves many URLs in one call and reports a status for each of them:
queued, exists, invalid or over_limit.
"""

import json

import pytest

from keepfor.auth.service import register_user
from keepfor.mcp.server import MCP_TOOLS, process_mcp_request
from keepfor.runtime import set_providers
from keepfor.spi import Decision, Providers


class BatchQueue:
    """Queue double that records the batches it is given.

    The Cloudflare binding spells the batch call camelCase, so the double
    answers to that name as well.
    """

    def __init__(self):
        self.batches: list[list[dict]] = []

    async def send(self, message: dict) -> None:
        self.batches.append([message])

    async def send_batch(self, messages: list[dict]) -> None:
        self.batches.append(list(messages))

    def __getattr__(self, name: str):
        if name == "sendBatch":
            return self.send_batch
        raise AttributeError(name)


class PlainQueue:
    """Queue double without the batch call: the messages go one by one."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, message: dict) -> None:
        self.sent.append(message)


class MockEnv:
    """Mock Cloudflare environment with a queue binding."""

    def __init__(self, queue):
        self.QUEUE = queue
        self.DB = None
        self.AI = None
        self.VECTORIZE = None
        self.BUCKET = None


class LimitedEntitlements:
    """Allows a fixed number of `save_item` units, then refuses."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    async def check(self, tenant_id, action, qty=1):
        if action != "save_item":
            return Decision(allowed=True)
        return Decision(allowed=self.used + qty <= self.limit, reason="limit")

    async def record(self, tenant_id, action, qty=1):
        if action == "save_item":
            self.used += qty


@pytest.fixture
async def batch_user(db):
    """A user with a queue that records the batches."""
    user = await register_user(db, "saveurls@example.com", "password123")
    queue = BatchQueue()
    return user, MockEnv(queue), queue


async def _call(db, env, user, arguments, scopes=None):
    """Calls save_urls and returns the JSON-RPC response."""
    caller = user if scopes is None else {**user, "scopes": scopes}
    return await process_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "save_urls", "arguments": arguments},
        },
        db,
        env,
        caller,
    )


def _payload(res):
    """Unwraps the tool payload; fails loudly on a JSON-RPC error."""
    assert "error" not in res, res["error"]
    return json.loads(res["result"]["content"][0]["text"])


async def _urls_of(db, user_id):
    rows = await db.query_all(
        "SELECT canonical_url FROM items WHERE user_id = ? ORDER BY canonical_url;",
        (user_id,),
    )
    return [r["canonical_url"] for r in rows]


@pytest.mark.asyncio
async def test_save_urls_saves_every_url(db, batch_user):
    """Every URL lands in the library, tagged, with a status of its own."""
    user, env, _ = batch_user
    res = await _call(
        db,
        env,
        user,
        {
            "urls": [
                "https://example.com/one",
                "https://example.com/two",
                "https://example.com/three",
            ],
            "tags": ["batch"],
            "why_saved": "research",
            "saved_by": "agent",
        },
    )
    payload = _payload(res)
    assert len(payload["results"]) == 3
    for result in payload["results"]:
        assert result["status"] == "queued"
        assert result["is_new"] is True
        assert result["id"]
    assert payload["totals"] == {
        "requested": 3,
        "queued": 3,
        "exists": 0,
        "invalid": 0,
        "over_limit": 0,
    }
    assert await _urls_of(db, user["id"]) == [
        "https://example.com/one",
        "https://example.com/three",
        "https://example.com/two",
    ]
    row = await db.query_first(
        "SELECT why_saved, saved_by FROM items WHERE user_id = ? LIMIT 1;",
        (user["id"],),
    )
    assert row["why_saved"] == "research"
    assert row["saved_by"] == "agent"
    tags = await db.query_all(
        "SELECT t.name FROM item_tags it JOIN tags t ON t.id = it.tag_id"
        " JOIN items i ON i.id = it.item_id WHERE i.user_id = ?;",
        (user["id"],),
    )
    assert {t["name"] for t in tags} == {"batch"}


@pytest.mark.asyncio
async def test_save_urls_dedupes_within_the_request(db, batch_user):
    """The same canonical URL twice is saved once."""
    user, env, queue = batch_user
    payload = _payload(
        await _call(
            db,
            env,
            user,
            {
                "urls": [
                    "https://example.com/dup",
                    "https://example.com/dup?utm_source=news",
                    "https://example.com/dup/",
                ]
            },
        )
    )
    assert [r["status"] for r in payload["results"]] == [
        "queued",
        "exists",
        "exists",
    ]
    assert payload["results"][1]["id"] == payload["results"][0]["id"]
    assert payload["results"][1]["is_new"] is False
    assert payload["totals"]["queued"] == 1
    assert payload["totals"]["exists"] == 2
    assert await _urls_of(db, user["id"]) == ["https://example.com/dup"]
    # Only the new item is queued for extraction.
    assert len(queue.batches) == 1
    assert len(queue.batches[0]) == 1


@pytest.mark.asyncio
async def test_save_urls_dedupes_against_the_library(db, batch_user):
    """A URL already in the library comes back as exists, not as a new item."""
    user, env, _ = batch_user
    first = _payload(
        await _call(db, env, user, {"urls": ["https://example.com/known"]})
    )
    payload = _payload(
        await _call(
            db,
            env,
            user,
            {"urls": ["https://example.com/known", "https://example.com/fresh"]},
        )
    )
    assert payload["results"][0]["status"] == "exists"
    assert payload["results"][0]["is_new"] is False
    assert payload["results"][0]["id"] == first["results"][0]["id"]
    assert payload["results"][1]["status"] == "queued"
    assert await _urls_of(db, user["id"]) == [
        "https://example.com/fresh",
        "https://example.com/known",
    ]


@pytest.mark.asyncio
async def test_save_urls_reports_invalid_urls(db, batch_user):
    """A URL that is not a URL is reported, the rest of the call goes on."""
    user, env, _ = batch_user
    payload = _payload(
        await _call(
            db,
            env,
            user,
            {"urls": ["https://example.com/ok", "not a url", "", "example.org/x"]},
        )
    )
    statuses = [r["status"] for r in payload["results"]]
    assert statuses == ["queued", "invalid", "invalid", "queued"]
    assert payload["results"][1]["id"] is None
    assert payload["results"][1]["url"] == "not a url"
    assert payload["totals"]["invalid"] == 2
    assert payload["totals"]["queued"] == 2
    assert await _urls_of(db, user["id"]) == [
        "https://example.com/ok",
        "https://example.org/x",
    ]


@pytest.mark.asyncio
async def test_save_urls_reports_the_urls_the_save_limit_refuses(db, batch_user):
    """The save limit refuses the rest of the call, it does not fail it."""
    user, env, _ = batch_user
    set_providers(Providers(entitlements=LimitedEntitlements(limit=2)))
    payload = _payload(
        await _call(
            db,
            env,
            user,
            {
                "urls": [
                    "https://example.com/1",
                    "https://example.com/2",
                    "https://example.com/3",
                    "https://example.com/4",
                ]
            },
        )
    )
    statuses = [r["status"] for r in payload["results"]]
    assert statuses == ["queued", "queued", "over_limit", "over_limit"]
    assert payload["results"][2]["id"] is None
    assert payload["results"][2]["is_new"] is False
    assert payload["totals"] == {
        "requested": 4,
        "queued": 2,
        "exists": 0,
        "invalid": 0,
        "over_limit": 2,
    }
    assert await _urls_of(db, user["id"]) == [
        "https://example.com/1",
        "https://example.com/2",
    ]


@pytest.mark.asyncio
async def test_save_urls_queues_the_ingestion_as_one_batch(db, batch_user):
    """The extraction of the whole call goes into one queue batch."""
    user, env, queue = batch_user
    payload = _payload(
        await _call(
            db,
            env,
            user,
            {"urls": [f"https://example.com/q{i}" for i in range(5)]},
        )
    )
    assert len(queue.batches) == 1
    messages = queue.batches[0]
    assert len(messages) == 5
    ids = {r["id"] for r in payload["results"]}
    assert {m["body"]["item_id"] for m in messages} == ids
    assert all(m["body"]["user_id"] == user["id"] for m in messages)


@pytest.mark.asyncio
async def test_save_urls_falls_back_to_one_message_per_url(db):
    """A queue without the batch call still gets every message."""
    user = await register_user(db, "saveurls-plain@example.com", "password123")
    queue = PlainQueue()
    env = MockEnv(queue)
    payload = _payload(await _call(db, env, user, {"urls": ["https://example.com/p1"]}))
    assert payload["totals"]["queued"] == 1
    assert len(queue.sent) == 1
    assert queue.sent[0]["item_id"] == payload["results"][0]["id"]


@pytest.mark.asyncio
async def test_save_urls_refuses_more_urls_than_the_cap(db, batch_user):
    """More than 50 URLs is refused before anything is written."""
    user, env, _ = batch_user
    res = await _call(
        db,
        env,
        user,
        {"urls": [f"https://example.com/{i}" for i in range(51)]},
    )
    assert res["error"]["data"]["type"] == "invalid_arguments"
    assert await _urls_of(db, user["id"]) == []


@pytest.mark.asyncio
async def test_save_urls_needs_the_write_scope(db, batch_user):
    """A read-only token is refused, and nothing is written."""
    user, env, _ = batch_user
    res = await _call(
        db,
        env,
        user,
        {"urls": ["https://example.com/denied"]},
        scopes=["read"],
    )
    assert res["error"]["data"]["type"] == "insufficient_scope"
    assert res["error"]["data"]["required_scope"] == "write"
    assert await _urls_of(db, user["id"]) == []


@pytest.mark.asyncio
async def test_save_urls_is_a_write_tool():
    """The tool is labeled as a write and needs the write scope."""
    tool = next(t for t in MCP_TOOLS if t["name"] == "save_urls")
    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["annotations"]["destructiveHint"] is False
    assert tool["inputSchema"]["required"] == ["urls"]
