# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for keepfor/models/tags.py tag management functions."""

import uuid

import pytest

from keepfor.models import tags as tags_model
from keepfor.models.items import (
    add_tags_to_item,
    create_tag,
    get_item_tags,
)


async def _seed_item(db, user_id, url="https://a.example/", title="Item"):
    item_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title, status)"
        " VALUES (?, ?, ?, ?, ?, 'ok');",
        (item_id, user_id, url, url, title),
    )
    return item_id


@pytest.mark.asyncio
async def test_create_tag_with_metadata(db):
    user_id = str(uuid.uuid4())
    name = await create_tag(
        db, user_id, "Research", description="Research stuff", color="#ff0000"
    )
    assert name == "research"
    tag = await tags_model.get_tag(db, user_id, "research")
    assert tag["description"] == "Research stuff"
    assert tag["color"] == "#ff0000"
    assert tag["parent_id"] is None


@pytest.mark.asyncio
async def test_create_tag_with_parent_hierarchy(db):
    user_id = str(uuid.uuid4())
    await create_tag(db, user_id, "work")
    child = await create_tag(db, user_id, "research", parent="work")
    assert child == "research"
    tag = await tags_model.get_tag(db, user_id, "research")
    parent = await db.query_first(
        "SELECT name FROM tags WHERE id = ?;", (tag["parent_id"],)
    )
    assert parent["name"] == "work"


@pytest.mark.asyncio
async def test_create_tag_creates_missing_parent(db):
    user_id = str(uuid.uuid4())
    await create_tag(db, user_id, "research", parent="work")
    parent = await tags_model.get_tag(db, user_id, "work")
    assert parent is not None


@pytest.mark.asyncio
async def test_create_tag_idempotent(db):
    user_id = str(uuid.uuid4())
    assert await create_tag(db, user_id, "ml") == "ml"
    assert await create_tag(db, user_id, "ml") == "ml"
    row = await db.query_first(
        "SELECT COUNT(*) as c FROM tags WHERE user_id = ? AND name = ?;",
        (user_id, "ml"),
    )
    assert row["c"] == 1


@pytest.mark.asyncio
async def test_update_tag_edits_metadata_and_parent(db):
    user_id = str(uuid.uuid4())
    await create_tag(db, user_id, "work")
    await create_tag(db, user_id, "research")
    assert await tags_model.update_tag(
        db, user_id, "research", description="d", parent="work"
    )
    tag = await tags_model.get_tag(db, user_id, "research")
    assert tag["description"] == "d"
    assert tag["parent"] == "work"


@pytest.mark.asyncio
async def test_update_tag_only_touches_given_fields(db):
    user_id = str(uuid.uuid4())
    await create_tag(db, user_id, "work", description="keep", color="#111111")
    await tags_model.update_tag(db, user_id, "work", color="#222222")
    tag = await tags_model.get_tag(db, user_id, "work")
    assert tag["description"] == "keep"
    assert tag["color"] == "#222222"
    # Empty string clears the description
    await tags_model.update_tag(db, user_id, "work", description="")
    assert (await tags_model.get_tag(db, user_id, "work"))["description"] == ""


@pytest.mark.asyncio
async def test_update_tag_unknown_parent_keeps_hierarchy(db):
    user_id = str(uuid.uuid4())
    await create_tag(db, user_id, "work")
    await create_tag(db, user_id, "research", parent="work")
    await tags_model.update_tag(db, user_id, "research", parent="nope")
    tag = await tags_model.get_tag(db, user_id, "research")
    assert tag["parent"] == "work"


@pytest.mark.asyncio
async def test_list_tags_with_counts_and_sort(db):
    user_id = str(uuid.uuid4())
    item1 = await _seed_item(db, user_id, "https://a.example/", "A")
    item2 = await _seed_item(db, user_id, "https://b.example/", "B")
    await add_tags_to_item(db, user_id, item1, ["ml", "ai"])
    await add_tags_to_item(db, user_id, item2, ["ml"])

    by_count = await tags_model.list_tags(db, user_id, sort="count")
    names = [t["name"] for t in by_count]
    assert names[0] == "ml"  # count 2
    assert by_count[0]["count"] == 2
    assert by_count[0]["last_used"] is not None

    by_name = await tags_model.list_tags(db, user_id, sort="name")
    assert [t["name"] for t in by_name] == ["ai", "ml"]

    filtered = await tags_model.list_tags(db, user_id, prefix="a")
    assert [t["name"] for t in filtered] == ["ai"]

    min_count = await tags_model.list_tags(db, user_id, min_count=2)
    assert [t["name"] for t in min_count] == ["ml"]


@pytest.mark.asyncio
async def test_list_tags_last_used_tracks_opens(db):
    user_id = str(uuid.uuid4())
    item1 = await _seed_item(db, user_id, "https://a.example/", "A")
    await add_tags_to_item(db, user_id, item1, ["read"])
    await db.execute(
        "INSERT INTO item_opens (user_id, item_id, opened_at) VALUES (?, ?, ?);",
        (user_id, item1, "2030-01-02 03:04:05"),
    )
    tags = await tags_model.list_tags(db, user_id, sort="recent")
    assert tags[0]["name"] == "read"
    assert str(tags[0]["last_used"]).startswith("2030-01-02")


@pytest.mark.asyncio
async def test_search_tags_fuzzy(db):
    user_id = str(uuid.uuid4())
    await create_tag(db, user_id, "machine-learning", description="ML")
    await create_tag(db, user_id, "vacation")

    hits = await tags_model.search_tags(db, user_id, "machine")
    assert any(t["name"] == "machine-learning" for t in hits)
    # separator-folded variant
    hits2 = await tags_model.search_tags(db, user_id, "machinelearning")
    assert any(t["name"] == "machine-learning" for t in hits2)
    # description match
    hits3 = await tags_model.search_tags(db, user_id, "ml")
    assert any(t["name"] == "machine-learning" for t in hits3)


@pytest.mark.asyncio
async def test_related_tags_cooccurrence(db):
    user_id = str(uuid.uuid4())
    item1 = await _seed_item(db, user_id, "https://a.example/", "A")
    item2 = await _seed_item(db, user_id, "https://b.example/", "B")
    await add_tags_to_item(db, user_id, item1, ["ml", "python"])
    await add_tags_to_item(db, user_id, item2, ["ml", "python"])
    await add_tags_to_item(db, user_id, item2, ["ml", "ai"])

    related = await tags_model.related_tags(db, user_id, "ml")
    by_name = {t["name"]: t["cooccurrence"] for t in related}
    assert by_name["python"] == 2
    assert by_name["ai"] == 1


@pytest.mark.asyncio
async def test_tag_stats(db):
    user_id = str(uuid.uuid4())
    item1 = await _seed_item(db, user_id, "https://a.example/", "A")
    await _seed_item(db, user_id, "https://b.example/", "B")
    await add_tags_to_item(db, user_id, item1, ["ml"])

    stats = await tags_model.tag_stats(db, user_id)
    assert stats["total_tags"] == 1
    assert stats["total_items"] == 2
    assert stats["tagged_items"] == 1
    assert stats["untagged_items"] == 1
    assert stats["top_tags"][0] == {"name": "ml", "count": 1}


@pytest.mark.asyncio
async def test_find_duplicate_tags_variants(db):
    user_id = str(uuid.uuid4())
    await create_tag(db, user_id, "machine-learning")
    await create_tag(db, user_id, "machine_learning")
    await create_tag(db, user_id, "machinelearning")
    await create_tag(db, user_id, "tags")
    await create_tag(db, user_id, "tag")
    await create_tag(db, user_id, "vacation")

    dups = await tags_model.find_duplicate_tags(db, user_id)
    groups = [set(g["tags"]) for g in dups]
    assert any(
        {"machine-learning", "machine_learning", "machinelearning"} <= g for g in groups
    )
    assert any({"tags", "tag"} <= g for g in groups)
    # A unique tag is not reported
    assert not any("vacation" in g for g in groups)


@pytest.mark.asyncio
async def test_find_duplicate_tags_acronym(db):
    user_id = str(uuid.uuid4())
    await create_tag(db, user_id, "ml")
    await create_tag(db, user_id, "machine-learning")
    await create_tag(db, user_id, "vacation")

    dups = await tags_model.find_duplicate_tags(db, user_id)
    assert any({"ml", "machine-learning"} <= set(g["tags"]) for g in dups), dups


@pytest.mark.asyncio
async def test_find_unused_tags_and_untagged_items(db):
    user_id = str(uuid.uuid4())
    item1 = await _seed_item(db, user_id, "https://a.example/", "A")
    await add_tags_to_item(db, user_id, item1, ["used"])
    await create_tag(db, user_id, "unused")

    unused = await tags_model.find_unused_tags(db, user_id)
    assert [t["name"] for t in unused] == ["unused"]

    untagged = await tags_model.find_untagged_items(db, user_id)
    assert len(untagged) == 0  # item1 has a tag

    item2 = await _seed_item(db, user_id, "https://b.example/", "B")
    untagged = await tags_model.find_untagged_items(db, user_id)
    assert [i["id"] for i in untagged] == [item2]


@pytest.mark.asyncio
async def test_delete_tag_dry_run_then_apply(db):
    user_id = str(uuid.uuid4())
    item1 = await _seed_item(db, user_id, "https://a.example/", "A")
    await add_tags_to_item(db, user_id, item1, ["obsolete", "keep"])

    # dry run: nothing deleted
    res = await tags_model.delete_tag(db, user_id, "obsolete", dry_run=True)
    assert res["found"] is True
    assert res["deleted"] is False
    assert res["match_count"] == 1
    assert item1 in res["sample"]
    # tag still present
    assert await tags_model.get_tag(db, user_id, "obsolete") is not None

    # apply
    res2 = await tags_model.delete_tag(db, user_id, "obsolete", dry_run=False)
    assert res2["deleted"] is True
    assert await tags_model.get_tag(db, user_id, "obsolete") is None
    # item survives, other tag intact
    assert await get_item_tags(db, user_id, item1) == ["keep"]


@pytest.mark.asyncio
async def test_tag_items_bulk_with_dry_run(db):
    user_id = str(uuid.uuid4())
    item1 = await _seed_item(db, user_id, "https://a.example/", "A")
    item2 = await _seed_item(db, user_id, "https://b.example/", "B")

    res = await tags_model.tag_items(
        db, user_id, [item1, item2], add=["x"], dry_run=True
    )
    assert res["applied"] is False
    assert res["match_count"] == 2
    assert set(res["sample"]) == {item1, item2}
    assert await get_item_tags(db, user_id, item1) == []

    res2 = await tags_model.tag_items(
        db, user_id, [item1, item2], add=["x"], remove=["y"], dry_run=False
    )
    assert res2["applied"] is True
    assert await get_item_tags(db, user_id, item1) == ["x"]


@pytest.mark.asyncio
async def test_tag_items_skips_foreign_items(db):
    user_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    mine = await _seed_item(db, user_id, "https://a.example/", "A")
    theirs = await _seed_item(db, other_id, "https://b.example/", "B")

    res = await tags_model.tag_items(
        db, user_id, [mine, theirs], add=["x"], dry_run=False
    )
    assert res["match_count"] == 1
    assert await get_item_tags(db, user_id, theirs) == []


@pytest.mark.asyncio
async def test_set_item_tags_replaces(db):
    user_id = str(uuid.uuid4())
    item1 = await _seed_item(db, user_id, "https://a.example/", "A")
    await add_tags_to_item(db, user_id, item1, ["old", "stale"])

    res = await tags_model.set_item_tags(db, user_id, item1, ["new", "fresh"])
    assert res["found"] is True
    assert await get_item_tags(db, user_id, item1) == ["fresh", "new"]


@pytest.mark.asyncio
async def test_retag_by_query_dry_run_and_apply(db):
    user_id = str(uuid.uuid4())
    item1 = await _seed_item(db, user_id, "https://a.example/", "Python guide")
    item2 = await _seed_item(db, user_id, "https://b.example/", "Rust guide")

    res = await tags_model.retag_by_query(
        db, user_id, query="python", add=["lang"], dry_run=True
    )
    assert res["applied"] is False
    assert res["match_count"] == 1
    assert item1 in res["sample"]
    assert await get_item_tags(db, user_id, item1) == []

    res2 = await tags_model.retag_by_query(
        db, user_id, query="python", add=["lang"], dry_run=False
    )
    assert res2["applied"] is True
    assert await get_item_tags(db, user_id, item1) == ["lang"]
    assert await get_item_tags(db, user_id, item2) == []


@pytest.mark.asyncio
async def test_suggest_tags_for_item_uses_vocabulary(db):
    user_id = str(uuid.uuid4())
    await create_tag(db, user_id, "python")
    item1 = await _seed_item(db, user_id, "https://a.example/", "Python tips")
    suggestions = await tags_model.suggest_tags_for_item(db, user_id, item1)
    names = [s["name"] for s in suggestions]
    assert "python" in names


@pytest.mark.asyncio
async def test_suggest_tags_for_text(db):
    user_id = str(uuid.uuid4())
    await create_tag(db, user_id, "python")
    suggestions = await tags_model.suggest_tags_for_text(
        db, user_id, "I love python programming"
    )
    names = [s["name"] for s in suggestions]
    assert "python" in names


@pytest.mark.asyncio
async def test_suggest_tags_for_item_caps_total_and_prefers_vocabulary(db):
    user_id = str(uuid.uuid4())
    await create_tag(db, user_id, "python")
    item_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title,"
        " content_text, status)"
        " VALUES (?, ?, ?, ?, ?, ?, 'ok');",
        (
            item_id,
            user_id,
            "https://a.example/java",
            "https://a.example/java",
            "Java versus Rust versus Kotlin versus Scala versus Perl versus Ruby",
            "I love python programming. Python is fun.",
        ),
    )
    suggestions = await tags_model.suggest_tags_for_item(db, user_id, item_id)
    # The user's own vocabulary is listed before novel keyphrases, and the
    # combined output never exceeds the cap.
    assert suggestions[0]["name"] == "python"
    assert 1 <= len(suggestions) <= 5


@pytest.mark.asyncio
async def test_suggest_tags_for_item_filters_junk_existing_tags(db):
    user_id = str(uuid.uuid4())
    hard_blocked = [
        "www",
        "http",
        "https",
        "com",
        "2024",
        "123",
    ]
    for tag in hard_blocked:
        await create_tag(db, user_id, tag)
    await create_tag(db, user_id, "databases")

    item_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title,"
        " content_text, status)"
        " VALUES (?, ?, ?, ?, ?, ?, 'ok');",
        (
            item_id,
            user_id,
            "https://example.com/databases-guide",
            "https://example.com/databases-guide",
            "Databases Guide",
            (
                "Published in 2024. Retrieved from http https www com with 123. "
                "Go explore databases today. Databases provide persistence."
            ),
        ),
    )
    suggestions = await tags_model.suggest_tags_for_item(db, user_id, item_id)
    names = [s["name"] for s in suggestions]

    # Verify hard-blocked tags are never offered, even though they exist in user's tags
    for j in hard_blocked:
        assert j not in names, f"hard-blocked tag '{j}' should not be suggested"

    # Verify valid existing tag is suggested with reason 'existing'
    assert "databases" in names
    db_sugg = next(s for s in suggestions if s["name"] == "databases")
    assert db_sugg["reason"] == "existing"

    # Verify cap of 5 holds
    assert 1 <= len(suggestions) <= 5


@pytest.mark.asyncio
async def test_suggest_tags_for_text_filters_junk_existing_tags(db):
    user_id = str(uuid.uuid4())
    for tag in ["www", "2024", "https", "com", "python"]:
        await create_tag(db, user_id, tag)

    suggestions = await tags_model.suggest_tags_for_text(
        db, user_id, "Python programming tutorial from https www com in 2024"
    )
    names = [s["name"] for s in suggestions]

    assert "python" in names
    assert "www" not in names
    assert "2024" not in names
    assert "https" not in names
    assert "com" not in names
    assert 1 <= len(suggestions) <= 5


@pytest.mark.asyncio
async def test_suggest_tags_exempts_existing_short_and_generic_tags(db):
    user_id = str(uuid.uuid4())
    existing = ["ai", "ux", "css", "may", "archive"]
    for t in existing:
        await create_tag(db, user_id, t)

    item_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title, excerpt,"
        " content_text, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'ok');",
        (
            item_id,
            user_id,
            "https://example.com/styling-ai",
            "https://example.com/styling-ai",
            "Modern AI and UX Design",
            "CSS layout techniques.",
            "May archive of web patterns. CSS and UX are key in AI apps.",
        ),
    )
    suggestions = await tags_model.suggest_tags_for_item(db, user_id, item_id)
    names = [s["name"] for s in suggestions]

    for t in existing:
        assert t in names, f"exempt tag '{t}' should be suggested"
        sugg = next(s for s in suggestions if s["name"] == t)
        assert sugg["reason"] == "existing"

    # Also verify text suggestions exempt them
    text_sugg = await tags_model.suggest_tags_for_text(
        db, user_id, "Exploring ai, ux, and css tools in may archive"
    )
    text_names = [s["name"] for s in text_sugg]
    for t in ["ai", "ux", "css"]:
        assert t in text_names, f"exempt tag '{t}' should be suggested for text"


@pytest.mark.asyncio
async def test_suggest_tags_new_suggestions_still_filter_short_and_junk_tokens(db):
    user_id = str(uuid.uuid4())
    # User has no existing tags matching the article
    await create_tag(db, user_id, "unrelated")

    item_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title,"
        " content_text, status)"
        " VALUES (?, ?, ?, ?, ?, ?, 'ok');",
        (
            item_id,
            user_id,
            "https://example.com/post",
            "https://example.com/post",
            "AI and UX in 2024",
            (
                "Published on Monday in April 2024. Retrieved from http www com. "
                "AI systems need good UX and CSS styling. Visit 42 times."
            ),
        ),
    )
    suggestions = await tags_model.suggest_tags_for_item(db, user_id, item_id)
    names = [s["name"] for s in suggestions]

    for blocked in [
        "ai",
        "ux",
        "css",
        "www",
        "http",
        "com",
        "2024",
        "42",
        "monday",
        "april",
        "retrieved",
        "published",
    ]:
        assert blocked not in names, f"new suggestion '{blocked}' should be filtered"


@pytest.mark.asyncio
async def test_suggest_tags_ordering_and_cap_with_exempt_tags(db):
    user_id = str(uuid.uuid4())
    existing = ["ai", "ux", "css", "databases", "python", "design"]
    for t in existing:
        await create_tag(db, user_id, t)
    await create_tag(db, user_id, "www")

    item_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title,"
        " content_text, status)"
        " VALUES (?, ?, ?, ?, ?, ?, 'ok');",
        (
            item_id,
            user_id,
            "https://example.com/tech-stack",
            "https://example.com/tech-stack",
            "AI UX CSS Databases Python Design",
            (
                "AI UX CSS databases python design www. "
                "Novel keyphrase one. Novel keyphrase two."
            ),
        ),
    )
    suggestions = await tags_model.suggest_tags_for_item(db, user_id, item_id)

    assert len(suggestions) == 5
    assert all(s["reason"] == "existing" for s in suggestions)
    assert "www" not in [s["name"] for s in suggestions]


@pytest.mark.asyncio
async def test_suggest_tags_existing_tags_first_and_cap_of_five(db):
    user_id = str(uuid.uuid4())
    existing_tags = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
    for t in existing_tags:
        await create_tag(db, user_id, t)
    # Also add a junk tag to verify it is omitted
    await create_tag(db, user_id, "www")

    item_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title,"
        " content_text, status)"
        " VALUES (?, ?, ?, ?, ?, ?, 'ok');",
        (
            item_id,
            user_id,
            "https://example.com/greek",
            "https://example.com/greek",
            "Alpha Beta Gamma Delta Epsilon Zeta",
            (
                "Alpha beta gamma delta epsilon zeta www. "
                "Novel keyphrase one. Novel keyphrase two. Novel keyphrase three."
            ),
        ),
    )
    suggestions = await tags_model.suggest_tags_for_item(db, user_id, item_id)

    assert len(suggestions) == 5
    assert all(s["reason"] == "existing" for s in suggestions)
    assert "www" not in [s["name"] for s in suggestions]


@pytest.mark.asyncio
async def test_expand_tag_names_includes_children(db):
    user_id = str(uuid.uuid4())
    await create_tag(db, user_id, "work")
    await create_tag(db, user_id, "research", parent="work")
    await create_tag(db, user_id, "deep", parent="research")
    await create_tag(db, user_id, "other")

    expanded = await tags_model.expand_tag_names(db, user_id, ["work", "other"])
    assert set(expanded["work"]) == {"work", "research", "deep"}
    assert expanded["other"] == ["other"]
    assert await tags_model.expand_tag_names(db, user_id, []) == {}


@pytest.mark.asyncio
async def test_tag_filter_clause_filters_items(db):
    user_id = str(uuid.uuid4())
    item1 = await _seed_item(db, user_id, "https://a.example/", "A")
    item2 = await _seed_item(db, user_id, "https://b.example/", "B")
    item3 = await _seed_item(db, user_id, "https://c.example/", "C")
    await create_tag(db, user_id, "work")
    await create_tag(db, user_id, "research", parent="work")
    await add_tags_to_item(db, user_id, item1, ["research"])
    await add_tags_to_item(db, user_id, item2, ["other"])
    # item3 stays untagged

    async def _ids(**kwargs):
        clause, params = await tags_model.tag_filter_clause(db, user_id, **kwargs)
        rows = await db.query_all(
            f"SELECT i.id FROM items i WHERE i.user_id = ?{clause} ORDER BY i.id;",
            (user_id, *params),
        )
        return {r["id"] for r in rows}

    # A child tag satisfies the parent filter
    assert await _ids(tags_all=["work"]) == {item1}
    assert await _ids(tags_any=["work", "other"]) == {item1, item2}
    assert await _ids(tags_none=["work"]) == {item2, item3}
    assert await _ids(untagged=True) == {item3}
    assert await _ids() == {item1, item2, item3}
