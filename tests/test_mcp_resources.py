# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for MCP resources and prompts (GitHub issue #23).

Resources expose the caller's own library (`keepfor://item/{id}`,
`keepfor://tag/{name}`); prompts expose reusable templates that read the
caller's own data (`research_brief`, `weekly_review`).
"""

import json

import pytest

from keepfor.auth.service import register_user
from keepfor.mcp.server import process_mcp_request
from keepfor.models.items import create_tag, save_item, save_note


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


async def _call(db, env, user, method, params=None, req_id=1):
    body: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return await process_mcp_request(body, db, env, user)


async def _seed_item(db, env, user, url, title, tags, content=""):
    item, _ = await save_item(db, env, user["id"], url, tags=tags)
    await db.execute(
        "UPDATE items SET title = ?, content_text = ?, excerpt = ?, status = 'ok'"
        " WHERE id = ? AND user_id = ?;",
        (title, content, content[:200], item["id"], user["id"]),
    )
    return item


# ==========================================
# initialize
# ==========================================


@pytest.mark.asyncio
async def test_initialize_declares_resources_and_prompts(mcp_user):
    """The server advertises the resources and prompts capabilities."""
    user, env, db = mcp_user
    res = await _call(db, env, user, "initialize")
    caps = res["result"]["capabilities"]
    assert "resources" in caps
    assert caps["resources"]["subscribe"] is False
    assert caps["resources"]["listChanged"] is False
    assert "prompts" in caps
    assert caps["prompts"]["listChanged"] is False


# ==========================================
# resources
# ==========================================


@pytest.mark.asyncio
async def test_resource_templates_list(mcp_user):
    """resources/templates/list exposes the item and tag templates."""
    user, env, db = mcp_user
    res = await _call(db, env, user, "resources/templates/list")
    templates = res["result"]["resourceTemplates"]
    uris = [t["uriTemplate"] for t in templates]
    assert "keepfor://item/{item_id}" in uris
    assert "keepfor://tag/{name}" in uris
    for template in templates:
        assert template["name"]
        assert template["description"]
        assert template["mimeType"]
        assert template["annotations"]["audience"]
        assert 0 <= template["annotations"]["priority"] <= 1


@pytest.mark.asyncio
async def test_resources_list_returns_items_and_tags(mcp_user):
    """resources/list lists the caller's own items and tags."""
    user, env, db = mcp_user
    item = await _seed_item(
        db,
        env,
        user,
        "https://example.com/a",
        "Article A",
        ["alpha"],
        content="Body of article A",
    )

    res = await _call(db, env, user, "resources/list")
    resources = res["result"]["resources"]
    uris = [r["uri"] for r in resources]

    assert f"keepfor://item/{item['id']}" in uris
    assert "keepfor://tag/alpha" in uris
    for resource in resources:
        assert resource["name"]
        assert resource["mimeType"] == "application/json"
        assert resource["annotations"]["audience"]
        assert 0 <= resource["annotations"]["priority"] <= 1


@pytest.mark.asyncio
async def test_resources_read_item(mcp_user):
    """resources/read returns the item payload as JSON text."""
    user, env, db = mcp_user
    item = await _seed_item(
        db,
        env,
        user,
        "https://example.com/b",
        "Article B",
        ["beta"],
        content="Body of article B",
    )

    res = await _call(
        db, env, user, "resources/read", {"uri": f"keepfor://item/{item['id']}"}
    )
    contents = res["result"]["contents"]
    assert len(contents) == 1
    assert contents[0]["uri"] == f"keepfor://item/{item['id']}"
    assert contents[0]["mimeType"] == "application/json"
    payload = json.loads(contents[0]["text"])
    assert payload["id"] == item["id"]
    assert payload["title"] == "Article B"
    assert payload["url"] == "https://example.com/b"
    assert payload["tags"] == ["beta"]
    assert payload["content"] == "Body of article B"
    assert payload["truncated"] is False


@pytest.mark.asyncio
async def test_resources_read_item_caps_content(mcp_user):
    """The resource text is capped, and the cap is reported."""
    user, env, db = mcp_user
    long_text = "x" * 50_000
    item = await _seed_item(
        db, env, user, "https://example.com/long", "Long", ["alpha"], content=long_text
    )

    res = await _call(
        db, env, user, "resources/read", {"uri": f"keepfor://item/{item['id']}"}
    )
    payload = json.loads(res["result"]["contents"][0]["text"])
    assert payload["truncated"] is True
    assert len(payload["content"]) < len(long_text)
    assert payload["content_length"] == len(long_text)


@pytest.mark.asyncio
async def test_resources_read_note(mcp_user):
    """A saved note is a resource too."""
    user, env, db = mcp_user
    note = await save_note(
        db, env, user["id"], "Shopping list", "milk\nbread", tags=["kitchen"]
    )

    res = await _call(
        db, env, user, "resources/read", {"uri": f"keepfor://item/{note['id']}"}
    )
    payload = json.loads(res["result"]["contents"][0]["text"])
    assert payload["id"] == note["id"]
    assert payload["title"] == "Shopping list"
    assert payload["item_type"] == "note"
    assert payload["content"] == "milk\nbread"
    assert payload["tags"] == ["kitchen"]


@pytest.mark.asyncio
async def test_resources_read_tag(mcp_user):
    """resources/read returns a tag with the items carrying it."""
    user, env, db = mcp_user
    item = await _seed_item(
        db, env, user, "https://example.com/c", "Article C", ["gamma"], content="Body"
    )

    res = await _call(db, env, user, "resources/read", {"uri": "keepfor://tag/gamma"})
    payload = json.loads(res["result"]["contents"][0]["text"])
    assert payload["name"] == "gamma"
    assert [i["id"] for i in payload["items"]] == [item["id"]]


@pytest.mark.asyncio
async def test_resources_read_tag_name_is_url_decoded(mcp_user):
    """A tag with a space is addressed percent-encoded."""
    user, env, db = mcp_user
    await _seed_item(
        db, env, user, "https://example.com/d", "Article D", ["machine learning"]
    )

    res = await _call(
        db, env, user, "resources/read", {"uri": "keepfor://tag/machine%20learning"}
    )
    payload = json.loads(res["result"]["contents"][0]["text"])
    assert payload["name"] == "machine learning"


@pytest.mark.asyncio
async def test_resources_list_caps_items_and_tags(mcp_user):
    """Listing stops at 50 items and 50 tags."""
    user, env, db = mcp_user
    for i in range(51):
        await _seed_item(
            db, env, user, f"https://example.com/bulk{i}", f"Bulk {i}", ["bulk"]
        )
        await create_tag(db, user["id"], f"bulk-tag-{i}")

    res = await _call(db, env, user, "resources/list")
    uris = [r["uri"] for r in res["result"]["resources"]]
    items = [u for u in uris if u.startswith("keepfor://item/")]
    tags = [u for u in uris if u.startswith("keepfor://tag/")]
    assert len(items) == 50
    assert len(tags) == 50


@pytest.mark.asyncio
async def test_resources_read_tag_caps_items(mcp_user):
    """A tag carries at most 50 items and says when it dropped the rest."""
    user, env, db = mcp_user
    for i in range(51):
        await _seed_item(
            db, env, user, f"https://example.com/many{i}", f"Many {i}", ["many"]
        )

    res = await _call(db, env, user, "resources/read", {"uri": "keepfor://tag/many"})
    payload = json.loads(res["result"]["contents"][0]["text"])
    assert len(payload["items"]) == 50
    assert payload["items_truncated"] is True


@pytest.mark.asyncio
async def test_resources_read_unknown_scheme_is_rejected(mcp_user):
    """A URI outside the keepfor:// scheme is an invalid-arguments error."""
    user, env, db = mcp_user
    res = await _call(db, env, user, "resources/read", {"uri": "https://example.com/x"})
    assert "error" in res
    assert res["error"]["data"]["type"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_resources_read_unknown_type_is_rejected(mcp_user):
    """A keepfor:// URI with an unknown type is an invalid-arguments error."""
    user, env, db = mcp_user
    res = await _call(db, env, user, "resources/read", {"uri": "keepfor://note/1"})
    assert "error" in res
    assert res["error"]["data"]["type"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_resources_read_missing_uri_is_rejected(mcp_user):
    """resources/read without a uri is an invalid-arguments error."""
    user, env, db = mcp_user
    res = await _call(db, env, user, "resources/read", {})
    assert "error" in res
    assert res["error"]["data"]["type"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_resources_read_oversized_uri_is_rejected(mcp_user):
    """The uri input is capped."""
    user, env, db = mcp_user
    long_id = "a" * 5000
    res = await _call(
        db, env, user, "resources/read", {"uri": f"keepfor://item/{long_id}"}
    )
    assert "error" in res
    assert res["error"]["data"]["type"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_resources_read_unknown_item_is_not_found(mcp_user):
    """Reading an item the caller does not own is a not_found error."""
    user, env, db = mcp_user
    res = await _call(
        db,
        env,
        user,
        "resources/read",
        {"uri": "keepfor://item/11111111-2222-3333-4444-555555555555"},
    )
    assert "error" in res
    assert res["error"]["data"]["type"] == "not_found"
    # The spec names -32002 for a resource that does not exist.
    assert res["error"]["code"] == -32002


@pytest.mark.asyncio
async def test_resources_read_unknown_tag_is_not_found(mcp_user):
    """Reading a tag the caller does not own is a not_found error."""
    user, env, db = mcp_user
    res = await _call(db, env, user, "resources/read", {"uri": "keepfor://tag/nope"})
    assert "error" in res
    assert res["error"]["data"]["type"] == "not_found"


# ==========================================
# prompts
# ==========================================


@pytest.mark.asyncio
async def test_prompts_list(mcp_user):
    """prompts/list exposes the research brief and the weekly review."""
    user, env, db = mcp_user
    res = await _call(db, env, user, "prompts/list")
    prompts = res["result"]["prompts"]
    names = [p["name"] for p in prompts]
    assert "research_brief" in names
    assert "weekly_review" in names
    for prompt in prompts:
        assert prompt["description"]
        for arg in prompt.get("arguments", []):
            assert arg["name"]
            assert arg["description"]


@pytest.mark.asyncio
async def test_prompts_get_research_brief(mcp_user):
    """research_brief briefs the assistant on the caller's own items."""
    user, env, db = mcp_user
    item = await _seed_item(
        db,
        env,
        user,
        "https://example.com/ml",
        "Machine Learning Basics",
        ["ai"],
        content="Machine learning is a subset of artificial intelligence.",
    )
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?, ?, ?, ?);",
        (
            item["id"],
            user["id"],
            "Machine Learning Basics",
            "Machine learning is a subset of artificial intelligence.",
        ),
    )

    res = await _call(
        db,
        env,
        user,
        "prompts/get",
        {"name": "research_brief", "arguments": {"topic": "machine learning"}},
    )
    result = res["result"]
    assert result["messages"]
    message = result["messages"][0]
    assert message["role"] == "user"
    assert message["content"]["type"] == "text"
    text = message["content"]["text"]
    assert "machine learning" in text
    assert "Machine Learning Basics" in text
    assert f"keepfor://item/{item['id']}" in text


@pytest.mark.asyncio
async def test_prompts_get_research_brief_requires_topic(mcp_user):
    """research_brief without a topic is an invalid-arguments error."""
    user, env, db = mcp_user
    res = await _call(db, env, user, "prompts/get", {"name": "research_brief"})
    assert "error" in res
    assert res["error"]["data"]["type"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_prompts_get_research_brief_rejects_long_topic(mcp_user):
    """The topic input is capped."""
    user, env, db = mcp_user
    res = await _call(
        db,
        env,
        user,
        "prompts/get",
        {"name": "research_brief", "arguments": {"topic": "t" * 5000}},
    )
    assert "error" in res
    assert res["error"]["data"]["type"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_prompts_get_research_brief_caps_max_items(mcp_user):
    """max_items is capped, so a huge request cannot flood the prompt."""
    user, env, db = mcp_user
    for i in range(3):
        await _seed_item(
            db,
            env,
            user,
            f"https://example.com/item{i}",
            f"Item {i}",
            ["ai"],
            content="machine learning",
        )

    res = await _call(
        db,
        env,
        user,
        "prompts/get",
        {
            "name": "research_brief",
            "arguments": {"topic": "machine learning", "max_items": "9999"},
        },
    )
    text = res["result"]["messages"][0]["content"]["text"]
    assert text.count("keepfor://item/") <= 20


@pytest.mark.asyncio
async def test_prompts_get_weekly_review(mcp_user):
    """weekly_review lists what the caller saved in the period."""
    user, env, db = mcp_user
    item = await _seed_item(
        db, env, user, "https://example.com/week", "Weekly item", ["ai"], content="Body"
    )

    res = await _call(
        db, env, user, "prompts/get", {"name": "weekly_review", "arguments": {}}
    )
    result = res["result"]
    assert result["messages"]
    text = result["messages"][0]["content"]["text"]
    assert "Weekly item" in text
    assert f"keepfor://item/{item['id']}" in text


@pytest.mark.asyncio
async def test_prompts_get_weekly_review_caps_days(mcp_user):
    """The days input is capped to a sane range."""
    user, env, db = mcp_user
    res = await _call(
        db,
        env,
        user,
        "prompts/get",
        {"name": "weekly_review", "arguments": {"days": "99999"}},
    )
    assert "result" in res
    text = res["result"]["messages"][0]["content"]["text"]
    assert "90" in text


@pytest.mark.asyncio
async def test_prompts_get_weekly_review_rejects_bad_days(mcp_user):
    """A non-numeric days input is an invalid-arguments error."""
    user, env, db = mcp_user
    res = await _call(
        db,
        env,
        user,
        "prompts/get",
        {"name": "weekly_review", "arguments": {"days": "not-a-number"}},
    )
    assert "error" in res
    assert res["error"]["data"]["type"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_prompts_get_weekly_review_rejects_long_tag(mcp_user):
    """The tag input is capped."""
    user, env, db = mcp_user
    res = await _call(
        db,
        env,
        user,
        "prompts/get",
        {"name": "weekly_review", "arguments": {"tag": "t" * 5000}},
    )
    assert "error" in res
    assert res["error"]["data"]["type"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_prompts_get_weekly_review_filters_by_tag(mcp_user):
    """The tag argument narrows the review to the items carrying it."""
    user, env, db = mcp_user
    kept = await _seed_item(
        db,
        env,
        user,
        "https://example.com/kept",
        "Kept item",
        ["alpha"],
        content="Body",
    )
    await _seed_item(
        db,
        env,
        user,
        "https://example.com/dropped",
        "Dropped item",
        ["beta"],
        content="Body",
    )

    res = await _call(
        db,
        env,
        user,
        "prompts/get",
        {"name": "weekly_review", "arguments": {"tag": "alpha"}},
    )
    text = res["result"]["messages"][0]["content"]["text"]
    assert "Kept item" in text
    assert "Dropped item" not in text
    assert f"keepfor://item/{kept['id']}" in text


@pytest.mark.asyncio
async def test_prompts_get_unknown_prompt_is_not_found(mcp_user):
    """An unknown prompt name is a not_found error."""
    user, env, db = mcp_user
    res = await _call(db, env, user, "prompts/get", {"name": "nope"})
    assert "error" in res
    assert res["error"]["data"]["type"] == "not_found"
    # The spec names -32602 (invalid params) for an unknown prompt.
    assert res["error"]["code"] == -32602
