# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for MCP tools v3: direct lookup (find_item, compact list_items)
and full tag management."""

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


async def call(db, env, user, tool, **arguments):
    """Invokes an MCP tool and returns the parsed JSON payload."""
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
        return res
    return json.loads(res["result"]["content"][0]["text"])


@pytest.fixture
async def mcp_user(db):
    user = await register_user(db, "mcptags@example.com", "password123")
    return user, MockEnv(), db


@pytest.fixture
async def library(db):
    """A user with a small, tagged library."""
    from keepfor.models.items import save_item

    user = await register_user(db, "library@example.com", "password123")
    env = MockEnv()

    item1, _ = await save_item(
        db, env, user["id"], "https://example.com/alpha", tags=["work", "python"]
    )
    item2, _ = await save_item(
        db, env, user["id"], "https://other.org/beta", tags=["work"]
    )
    item3, _ = await save_item(db, env, user["id"], "https://example.com/gamma")

    await db.execute(
        "UPDATE items SET title = ?, content_text = ?, excerpt = ?, status = 'ok'"
        " WHERE id = ?;",
        (
            "Alpha Guide",
            "Alpha content about python. " * 200,
            "Alpha excerpt",
            item1["id"],
        ),
    )
    await db.execute(
        "UPDATE items SET title = ?, content_text = ?, excerpt = ?, status = 'ok'"
        " WHERE id = ?;",
        ("Beta Notes", "Beta content. " * 200, "Beta excerpt", item2["id"]),
    )
    await db.execute(
        "UPDATE items SET title = ?, content_text = ?, excerpt = ?, status = 'ok'"
        " WHERE id = ?;",
        ("Gamma Thing", "Gamma content. " * 200, "Gamma excerpt", item3["id"]),
    )
    await db.execute("UPDATE items SET is_pinned = 1 WHERE id = ?;", (item1["id"],))
    await db.execute(
        "INSERT INTO item_opens (user_id, item_id, opened_at) VALUES (?, ?, ?);",
        (user["id"], item2["id"], "2031-05-06 07:08:09"),
    )
    return (
        user,
        env,
        db,
        {"alpha": item1["id"], "beta": item2["id"], "gamma": item3["id"]},
    )


# --- A: direct lookup ---------------------------------------------------


@pytest.mark.asyncio
async def test_list_items_is_compact_by_default(library):
    user, env, db, ids = library
    payload = await call(db, env, user, "list_items")
    assert "items" in payload
    first = payload["items"][0]
    assert set(first) == {"id", "title", "url", "saved_at", "tags"}
    assert "excerpt" not in first
    assert "content_text" not in first


@pytest.mark.asyncio
async def test_list_items_default_response_under_4kb(library):
    """The default page (limit 20) must stay under 4 KB."""
    user, env, db, ids = library
    for i in range(30):
        await db.execute(
            "INSERT INTO items (id, user_id, url, canonical_url, title, excerpt,"
            " content_text, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'ok');",
            (
                f"bulk-{i}",
                user["id"],
                f"https://example.com/bulk-{i}",
                f"https://example.com/bulk-{i}",
                f"Bulk item {i} with a fairly long title to make it heavier",
                "x" * 500,
                "y" * 5000,
            ),
        )
    res = await process_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_items", "arguments": {}},
        },
        db,
        env,
        user,
    )
    text = res["result"]["content"][0]["text"]
    assert len(text.encode("utf-8")) < 4096


@pytest.mark.asyncio
async def test_list_items_sort_and_filters(library):
    user, env, db, ids = library

    by_title = await call(db, env, user, "list_items", sort="title")
    titles = [i["title"] for i in by_title["items"]]
    assert titles == sorted(titles, key=str.lower)

    by_opened = await call(db, env, user, "list_items", sort="opened")
    assert by_opened["items"][0]["id"] == ids["beta"]

    by_domain = await call(db, env, user, "list_items", domain="example.com")
    assert {i["id"] for i in by_domain["items"]} == {ids["alpha"], ids["gamma"]}

    pinned = await call(db, env, user, "list_items", pinned=True)
    assert [i["id"] for i in pinned["items"]] == [ids["alpha"]]

    unpinned = await call(db, env, user, "list_items", pinned=False)
    assert ids["alpha"] not in {i["id"] for i in unpinned["items"]}

    notes = await call(db, env, user, "list_items", type="note")
    assert notes["items"] == []
    urls = await call(db, env, user, "list_items", type="url")
    assert len(urls["items"]) == 3

    future = await call(db, env, user, "list_items", since="2999-01-01")
    assert future["items"] == []
    past = await call(db, env, user, "list_items", until="2000-01-01")
    assert past["items"] == []


@pytest.mark.asyncio
async def test_list_items_pagination_and_projection(library):
    user, env, db, ids = library

    page1 = await call(db, env, user, "list_items", limit=2)
    assert len(page1["items"]) == 2
    assert page1["next_cursor"]

    page2 = await call(
        db, env, user, "list_items", limit=2, cursor=page1["next_cursor"]
    )
    assert page2["offset"] == 2
    assert {i["id"] for i in page1["items"]}.isdisjoint(
        {i["id"] for i in page2["items"]}
    )

    by_offset = await call(db, env, user, "list_items", limit=2, offset=2)
    assert {i["id"] for i in by_offset["items"]} == {i["id"] for i in page2["items"]}

    projected = await call(db, env, user, "list_items", fields=["id", "title"])
    assert set(projected["items"][0]) == {"id", "title"}


@pytest.mark.asyncio
async def test_find_item_by_title_exact_and_prefix(library):
    user, env, db, ids = library

    exact = await call(db, env, user, "find_item", title="Alpha Guide")
    assert exact["ids"] == [ids["alpha"]]

    prefix = await call(db, env, user, "find_item", title="Alpha")
    assert ids["alpha"] in prefix["ids"]

    case_insensitive = await call(db, env, user, "find_item", title="alpha guide")
    assert case_insensitive["ids"] == [ids["alpha"]]

    missing = await call(db, env, user, "find_item", title="Nope Nothing")
    assert missing["ids"] == []


@pytest.mark.asyncio
async def test_find_item_by_url_exact_and_normalized(library):
    user, env, db, ids = library

    exact = await call(db, env, user, "find_item", url="https://example.com/alpha")
    assert exact["ids"] == [ids["alpha"]]

    normalized = await call(
        db, env, user, "find_item", url="https://example.com/alpha?utm_source=x"
    )
    assert normalized["ids"] == [ids["alpha"]]

    missing = await call(db, env, user, "find_item", url="https://nope.test/x")
    assert missing["ids"] == []


@pytest.mark.asyncio
async def test_find_item_requires_title_or_url(library):
    user, env, db, ids = library
    res = await call(db, env, user, "find_item")
    assert "error" in res
    assert res["error"]["data"]["type"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_get_item_by_id_url_and_title(library):
    user, env, db, ids = library

    by_id = await call(db, env, user, "get_item", item_id=ids["alpha"])
    assert by_id["id"] == ids["alpha"]

    by_url = await call(db, env, user, "get_item", url="https://example.com/alpha")
    assert by_url["id"] == ids["alpha"]

    by_title = await call(db, env, user, "get_item", title="Beta Notes")
    assert by_title["id"] == ids["beta"]


@pytest.mark.asyncio
async def test_get_item_caps_content_by_default(library):
    user, env, db, ids = library

    capped = await call(db, env, user, "get_item", item_id=ids["alpha"])
    assert capped["content_text"] == "Alpha excerpt"

    full = await call(
        db, env, user, "get_item", item_id=ids["alpha"], include_content=True
    )
    assert len(full["content_text"]) > 1000


@pytest.mark.asyncio
async def test_get_item_reports_truncation_and_content_length(library):
    """A capped result says so and tells the client the full content size."""
    user, env, db, ids = library

    capped = await call(db, env, user, "get_item", item_id=ids["alpha"])
    assert capped["truncated"] is True
    assert capped["content_length"] == len("Alpha content about python. " * 200)

    full = await call(
        db, env, user, "get_item", item_id=ids["alpha"], include_content=True
    )
    assert full["truncated"] is False
    assert full["content_length"] == len(full["content_text"])


@pytest.mark.asyncio
async def test_get_item_missing_returns_structured_error(library):
    user, env, db, ids = library
    res = await call(db, env, user, "get_item", title="Does Not Exist")
    assert "error" in res
    assert res["error"]["data"]["type"] == "not_found"


@pytest.mark.asyncio
async def test_title_to_full_text_in_two_calls(library):
    """Acceptance: title -> id -> full text, no semantic search."""
    user, env, db, ids = library

    found = await call(db, env, user, "find_item", title="Alpha Guide")
    assert found["ids"]

    from keepfor.search.engine import read_item_chunks

    read = await read_item_chunks(
        db, env, user["id"], found["ids"][0], cursor=0, max_tokens=500
    )
    assert "Alpha content about python." in read["text"]


# --- B: tag management --------------------------------------------------


@pytest.mark.asyncio
async def test_list_tags_with_counts(library):
    user, env, db, ids = library
    payload = await call(db, env, user, "list_tags")
    by_name = {t["name"]: t for t in payload["tags"]}
    assert by_name["work"]["count"] == 2
    assert by_name["work"]["last_used"] is not None

    by_name_sort = await call(db, env, user, "list_tags", sort="name")
    assert [t["name"] for t in by_name_sort["tags"]] == sorted(
        t["name"] for t in by_name_sort["tags"]
    )

    prefixed = await call(db, env, user, "list_tags", prefix="wo")
    assert [t["name"] for t in prefixed["tags"]] == ["work"]

    min_count = await call(db, env, user, "list_tags", min_count=2)
    assert [t["name"] for t in min_count["tags"]] == ["work"]


@pytest.mark.asyncio
async def test_create_tag_with_metadata_and_parent(library):
    user, env, db, ids = library
    payload = await call(
        db,
        env,
        user,
        "create_tag",
        name="research",
        description="Deep work",
        color="#ff0000",
        parent="work",
    )
    assert payload["name"] == "research"
    assert payload["created"] is True
    assert payload["tag"]["description"] == "Deep work"
    assert payload["tag"]["color"] == "#ff0000"
    assert payload["tag"]["parent"] == "work"

    described = await call(db, env, user, "describe_tag", name="research")
    assert described["parent"] == "work"
    assert described["count"] == 0

    again = await call(db, env, user, "create_tag", name="research")
    assert again["created"] is False


@pytest.mark.asyncio
async def test_update_tag_changes_only_given_fields(library):
    user, env, db, ids = library
    await call(
        db, env, user, "create_tag", name="research", description="d", color="#111"
    )

    updated = await call(db, env, user, "update_tag", name="research", color="#222")
    assert updated["tag"]["color"] == "#222"
    assert updated["tag"]["description"] == "d"

    detached = await call(db, env, user, "update_tag", name="research", parent="")
    assert detached["tag"]["parent_id"] is None


@pytest.mark.asyncio
async def test_rename_and_merge_tags(library):
    user, env, db, ids = library
    renamed = await call(db, env, user, "rename_tag", old="python", new="python3")
    assert renamed["result"] == "merged"
    listed = await call(db, env, user, "list_tags")
    names = {t["name"] for t in listed["tags"]}
    assert "python3" in names
    assert "python" not in names

    merged = await call(db, env, user, "merge_tags", sources=["work"], target="job")
    assert merged["result"] == "merged"
    listed = await call(db, env, user, "list_tags")
    names = {t["name"] for t in listed["tags"]}
    assert "job" in names
    assert "work" not in names


@pytest.mark.asyncio
async def test_mcp_create_rename_and_merge_unicode_tags(library):
    user, env, db, ids = library
    for name, expected in (
        ("Konsumgüterbranche", "konsumgüterbranche"),
        ("Straße", "straße"),
        ("Café", "café"),
    ):
        created = await call(db, env, user, "create_tag", name=name)
        assert created["name"] == expected
        assert created["created"] is True

    # Decomposed u + combining diaeresis hits the existing NFC tag.
    duplicate = await call(db, env, user, "create_tag", name="konsumgu\u0308terbranche")
    assert duplicate["name"] == "konsumgüterbranche"
    assert duplicate["created"] is False

    renamed = await call(db, env, user, "rename_tag", old="straße", new="großstraße")
    assert renamed["result"] == "merged"

    merged = await call(
        db,
        env,
        user,
        "merge_tags",
        sources=["café", "großstraße"],
        target="konsumgu\u0308terbranche",
    )
    assert merged["result"] == "merged"
    listed = await call(db, env, user, "list_tags")
    names = {t["name"] for t in listed["tags"]}
    assert "konsumgüterbranche" in names
    assert "café" not in names
    assert "straße" not in names
    assert "großstraße" not in names


@pytest.mark.asyncio
async def test_delete_tag_dry_run_then_apply(library):
    user, env, db, ids = library
    preview = await call(db, env, user, "delete_tag", name="work")
    assert preview["dry_run"] is True
    assert preview["deleted"] is False
    assert preview["match_count"] == 2
    assert preview["sample"]

    listed = await call(db, env, user, "list_tags")
    assert "work" in {t["name"] for t in listed["tags"]}

    applied = await call(db, env, user, "delete_tag", name="work", dry_run=False)
    assert applied["deleted"] is True

    listed = await call(db, env, user, "list_tags")
    assert "work" not in {t["name"] for t in listed["tags"]}

    # Items survive
    items = await call(db, env, user, "list_items")
    assert len(items["items"]) == 3


@pytest.mark.asyncio
async def test_delete_tag_missing_is_structured_error(library):
    user, env, db, ids = library
    res = await call(db, env, user, "delete_tag", name="nope")
    assert "error" in res
    assert res["error"]["data"]["type"] == "not_found"


@pytest.mark.asyncio
async def test_tag_items_dry_run_then_apply(library):
    user, env, db, ids = library
    preview = await call(
        db, env, user, "tag_items", item_ids=[ids["gamma"]], add=["new"]
    )
    assert preview["applied"] is False
    assert preview["match_count"] == 1

    listed = await call(db, env, user, "list_items")
    assert "new" not in listed["items"][0]["tags"]

    applied = await call(
        db, env, user, "tag_items", item_ids=[ids["gamma"]], add=["new"], dry_run=False
    )
    assert applied["applied"] is True
    listed = await call(db, env, user, "list_items")
    gamma = next(i for i in listed["items"] if i["id"] == ids["gamma"])
    assert gamma["tags"] == ["new"]


@pytest.mark.asyncio
async def test_retag_by_query_dry_run_then_apply(library):
    user, env, db, ids = library
    preview = await call(db, env, user, "retag_by_query", query="Alpha", add=["topic"])
    assert preview["applied"] is False
    assert preview["match_count"] == 1

    applied = await call(
        db, env, user, "retag_by_query", query="Alpha", add=["topic"], dry_run=False
    )
    assert applied["applied"] is True
    listed = await call(db, env, user, "list_items")
    alpha = next(i for i in listed["items"] if i["id"] == ids["alpha"])
    assert "topic" in alpha["tags"]


@pytest.mark.asyncio
async def test_set_item_tags_replaces(library):
    user, env, db, ids = library
    payload = await call(
        db, env, user, "set_item_tags", item_id=ids["alpha"], tags=["only"]
    )
    assert payload["tags"] == ["only"]
    listed = await call(db, env, user, "list_items")
    alpha = next(i for i in listed["items"] if i["id"] == ids["alpha"])
    assert alpha["tags"] == ["only"]


# --- bulk caps ----------------------------------------------------------


async def _seed_items(db, user_id, count, prefix="bulk"):
    """Inserts `count` owned items and returns their ids."""
    ids = [f"{prefix}-{i}" for i in range(count)]
    for item_id in ids:
        await db.execute(
            "INSERT INTO items (id, user_id, url, canonical_url, title, status)"
            " VALUES (?, ?, ?, ?, ?, 'ok');",
            (
                item_id,
                user_id,
                f"https://example.com/{item_id}",
                f"https://example.com/{item_id}",
                f"Bulk item {item_id}",
            ),
        )
    return ids


@pytest.mark.asyncio
async def test_tag_items_caps_at_100_and_reports_truncation(library):
    """More than 100 ids: only 100 are processed, the rest is reported."""
    user, env, db, ids = library
    seeded = await _seed_items(db, user["id"], 150)

    payload = await call(db, env, user, "tag_items", item_ids=seeded, add=["bulk"])
    assert payload["truncated"] is True
    assert payload["total_matched"] == 150
    assert payload["match_count"] == 100
    assert payload["applied"] is False


@pytest.mark.asyncio
async def test_tag_items_under_cap_is_not_truncated(library):
    user, env, db, ids = library
    seeded = await _seed_items(db, user["id"], 3)

    payload = await call(db, env, user, "tag_items", item_ids=seeded, add=["bulk"])
    assert payload["truncated"] is False
    assert payload["total_matched"] == 3
    assert payload["match_count"] == 3


@pytest.mark.asyncio
async def test_tag_items_truncates_a_huge_id_list(library):
    """More ids than are scanned per call: reported, not scanned forever."""
    user, env, db, ids = library

    payload = await call(
        db,
        env,
        user,
        "tag_items",
        item_ids=[f"nope-{i}" for i in range(1001)],
        add=["bulk"],
    )
    assert payload["truncated"] is True
    assert payload["total_matched"] == 0
    assert payload["match_count"] == 0


@pytest.mark.asyncio
async def test_retag_by_query_caps_at_100_and_reports_total_matched(library):
    user, env, db, ids = library
    await _seed_items(db, user["id"], 150, prefix="bulk")

    payload = await call(db, env, user, "retag_by_query", query="bulk", add=["topic"])
    assert payload["truncated"] is True
    assert payload["total_matched"] == 150
    assert payload["match_count"] == 100


@pytest.mark.asyncio
async def test_retag_by_query_under_cap_is_not_truncated(library):
    user, env, db, ids = library
    await _seed_items(db, user["id"], 3, prefix="bulk")

    payload = await call(db, env, user, "retag_by_query", query="bulk", add=["topic"])
    assert payload["truncated"] is False
    assert payload["total_matched"] == 3
    assert payload["match_count"] == 3


@pytest.mark.asyncio
async def test_merge_tags_rejects_more_than_20_sources(library):
    """Above the cap the call fails with a structured error, silently."""
    user, env, db, ids = library
    sources = [f"tag-{i}" for i in range(21)]

    payload = await call(db, env, user, "merge_tags", sources=sources, target="job")
    assert "error" in payload
    assert payload["error"]["data"]["type"] == "invalid_arguments"
    assert payload["error"]["data"]["max_sources"] == 20
    assert payload["error"]["data"]["received"] == 21

    listed = await call(db, env, user, "list_tags")
    assert "job" not in {t["name"] for t in listed["tags"]}


@pytest.mark.asyncio
async def test_merge_tags_accepts_20_sources(library):
    user, env, db, ids = library
    sources = [f"tag-{i}" for i in range(20)]
    for name in sources:
        await call(db, env, user, "create_tag", name=name)

    payload = await call(db, env, user, "merge_tags", sources=sources, target="job")
    assert payload["result"] == "merged"


@pytest.mark.asyncio
async def test_set_item_tags_missing_is_error(library):
    user, env, db, ids = library
    res = await call(
        db, env, user, "set_item_tags", item_id="does-not-exist", tags=["x"]
    )
    assert "error" in res
    assert res["error"]["data"]["type"] == "not_found"


@pytest.mark.asyncio
async def test_suggest_tags_uses_existing_vocabulary(library):
    user, env, db, ids = library
    payload = await call(db, env, user, "suggest_tags", item_id=ids["alpha"])
    names = [s["name"] for s in payload["suggestions"]]
    assert "python" in names

    text = await call(db, env, user, "suggest_tags", text="some python code")
    assert "python" in [s["name"] for s in text["suggestions"]]


@pytest.mark.asyncio
async def test_search_tags_fuzzy(library):
    user, env, db, ids = library
    payload = await call(db, env, user, "search_tags", query="pyth")
    assert any(t["name"] == "python" for t in payload["tags"])


@pytest.mark.asyncio
async def test_tag_stats_and_related_tags(library):
    user, env, db, ids = library
    stats = await call(db, env, user, "tag_stats")
    assert stats["total_tags"] == 2
    assert stats["total_items"] == 3
    assert stats["tagged_items"] == 2
    assert stats["untagged_items"] == 1

    related = await call(db, env, user, "related_tags", tag="work")
    assert {t["name"] for t in related["tags"]} == {"python"}


@pytest.mark.asyncio
async def test_tag_cleanup_reports_duplicates_and_unused(library):
    user, env, db, ids = library
    await call(db, env, user, "create_tag", name="python-3")
    await call(db, env, user, "create_tag", name="python3")
    await call(db, env, user, "create_tag", name="unused-tag")

    payload = await call(db, env, user, "tag_cleanup")
    assert any(
        {"python-3", "python3"} <= set(group["tags"])
        for group in payload["near_duplicates"]
    ), payload["near_duplicates"]
    assert "unused-tag" in {t["name"] for t in payload["unused_tags"]}
    assert [i["id"] for i in payload["untagged_items"]] == [ids["gamma"]]


# --- tag filters --------------------------------------------------------


@pytest.mark.asyncio
async def test_tag_filters_on_list_items(library):
    user, env, db, ids = library
    await call(db, env, user, "create_tag", name="child", parent="work")

    all_work = await call(db, env, user, "list_items", tags_all=["work"])
    assert {i["id"] for i in all_work["items"]} == {ids["alpha"], ids["beta"]}

    any_work = await call(db, env, user, "list_items", tags_any=["work", "python"])
    assert {i["id"] for i in any_work["items"]} == {ids["alpha"], ids["beta"]}

    none_work = await call(db, env, user, "list_items", tags_none=["work"])
    assert {i["id"] for i in none_work["items"]} == {ids["gamma"]}

    untagged = await call(db, env, user, "list_items", untagged=True)
    assert {i["id"] for i in untagged["items"]} == {ids["gamma"]}


@pytest.mark.asyncio
async def test_tag_filters_include_child_tags(library):
    user, env, db, ids = library
    await call(db, env, user, "create_tag", name="research", parent="work")
    await call(
        db,
        env,
        user,
        "tag_items",
        item_ids=[ids["gamma"]],
        add=["research"],
        dry_run=False,
    )

    matched = await call(db, env, user, "list_items", tags_all=["work"])
    assert ids["gamma"] in {i["id"] for i in matched["items"]}


@pytest.mark.asyncio
async def test_tag_filters_on_find_item(library):
    user, env, db, ids = library
    await call(db, env, user, "create_tag", name="child", parent="work")

    payload = await call(db, env, user, "find_item", title="Alpha", tags_all=["work"])
    assert payload["ids"] == [ids["alpha"]]

    payload = await call(db, env, user, "find_item", title="Alpha", tags_none=["work"])
    assert payload["ids"] == []


@pytest.mark.asyncio
async def test_tag_filters_on_reading_history(library):
    user, env, db, ids = library
    payload = await call(db, env, user, "reading_history", tags_all=["work"])
    assert {i["id"] for i in payload["items"]} == {ids["alpha"], ids["beta"]}

    payload = await call(db, env, user, "reading_history", untagged=True)
    assert {i["id"] for i in payload["items"]} == {ids["gamma"]}


@pytest.mark.asyncio
async def test_reading_history_opened_with_tag_filter(library):
    """Regression: the opened branch used to bind the tag filter wrongly."""
    user, env, db, ids = library
    payload = await call(
        db, env, user, "reading_history", event="opened", tags_all=["work"]
    )
    assert {i["id"] for i in payload["items"]} == {ids["beta"]}


@pytest.mark.asyncio
async def test_reading_history_opened_with_query(library):
    user, env, db, ids = library
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?,?,?,?);",
        (ids["beta"], user["id"], "Beta Notes", "Beta content."),
    )
    payload = await call(db, env, user, "reading_history", event="opened", query="beta")
    assert {i["id"] for i in payload["items"]} == {ids["beta"]}


@pytest.mark.asyncio
async def test_tag_filters_on_search(library):
    user, env, db, ids = library
    await db.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text)"
        " VALUES (?,?,?,?);",
        (ids["alpha"], user["id"], "Alpha Guide", "Alpha content about python."),
    )
    payload = await call(db, env, user, "search_library", query="alpha")
    assert payload["items"]

    filtered = await call(
        db, env, user, "search_library", query="alpha", tags_none=["work"]
    )
    assert filtered["items"] == []
