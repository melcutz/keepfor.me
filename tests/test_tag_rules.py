# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Rules tables exist and enforce uniqueness (migration 0004)."""

import uuid

import pytest

from src.auth.service import register_user


@pytest.mark.asyncio
async def test_tag_rules_tables_exist(db):
    user = await register_user(db, "rules@keepfor.me", "password123")
    rid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO tag_rules (id, user_id, field, substr, tag)"
        " VALUES (?, ?, ?, ?, ?);",
        (rid, user["id"], "domain", "arxiv.org", "research"),
    )
    row = await db.query_first(
        "SELECT field, substr, tag FROM tag_rules WHERE id = ?;", (rid,)
    )
    assert (row["field"], row["substr"], row["tag"]) == (
        "domain",
        "arxiv.org",
        "research",
    )


@pytest.mark.asyncio
async def test_rule_crud_roundtrip(db):
    from src.models.items import create_rule, delete_rule, list_rules

    user = await register_user(db, "rc@keepfor.me", "password123")
    assert await create_rule(db, user["id"], "domain", "ArXiv.ORG ", "research")
    assert await create_rule(db, user["id"], "bogus", "x", "y") is None
    assert await create_rule(db, user["id"], "domain", "x", "bad/name") is None
    rules = await list_rules(db, user["id"])
    assert [(r["field"], r["substr"], r["tag"]) for r in rules] == [
        ("domain", "arxiv.org", "research")
    ]
    assert await delete_rule(db, user["id"], rules[0]["id"]) is True
    assert await list_rules(db, user["id"]) == []


def test_match_rules_fields():
    from src.models.items import match_rules

    rules = [
        {"field": "domain", "substr": "arxiv.org", "tag": "research"},
        {"field": "title", "substr": "postgres", "tag": "db"},
        {"field": "url", "substr": "utm_source", "tag": "promo"},
    ]
    got = match_rules(
        rules, "https://arxiv.org/abs/123?utm_source=x", "Postgres 16 notes"
    )
    assert got == ["research", "db", "promo"]
    assert match_rules(rules, "https://example.com/", "Nothing here") == []


@pytest.mark.asyncio
async def test_miner_proposes_high_precision_domain(db):
    from src.models.items import add_tags_to_item, suggest_rules

    user = await register_user(db, "mn@keepfor.me", "password123")
    for i in range(5):
        url = f"https://arxiv.org/abs/{i}"
        await db.execute(
            "INSERT INTO items (id, user_id, url, canonical_url, status)"
            " VALUES (?, ?, ?, ?, 'ok');",
            (f"mn{i}", user["id"], url, url),
        )
        await add_tags_to_item(db, user["id"], f"mn{i}", ["research"])
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status)"
        " VALUES (?, ?, ?, ?, 'ok');",
        ("mn5", user["id"], "https://example.com/x", "https://example.com/x"),
    )
    got = await suggest_rules(db, user["id"])
    assert {"domain": "arxiv.org", "tag": "research"} == {
        k: got[0][k] for k in ("domain", "tag")
    }


@pytest.mark.asyncio
async def test_miner_ignores_untagged_same_host_items(db):
    from src.models.items import add_tags_to_item, suggest_rules

    user = await register_user(db, "mn2@keepfor.me", "password123")
    for i in range(5):
        url = f"https://arxiv.org/abs/{i}"
        await db.execute(
            "INSERT INTO items (id, user_id, url, canonical_url, status)"
            " VALUES (?, ?, ?, ?, 'ok');",
            (f"mx{i}", user["id"], url, url),
        )
        await add_tags_to_item(db, user["id"], f"mx{i}", ["research"])
    for i in range(95):
        url = f"https://arxiv.org/other/{i}"
        await db.execute(
            "INSERT INTO items (id, user_id, url, canonical_url, status)"
            " VALUES (?, ?, ?, ?, 'ok');",
            (f"mu{i}", user["id"], url, url),
        )
    got = await suggest_rules(db, user["id"])
    assert got == []


@pytest.mark.asyncio
async def test_dismiss_roundtrip_hides_suggestion(db):
    from src.models.items import add_tags_to_item, suggest_rules

    user = await register_user(db, "mn3@keepfor.me", "password123")
    for i in range(5):
        url = f"https://example.org/p/{i}"
        await db.execute(
            "INSERT INTO items (id, user_id, url, canonical_url, status)"
            " VALUES (?, ?, ?, ?, 'ok');",
            (f"md{i}", user["id"], url, url),
        )
        await add_tags_to_item(db, user["id"], f"md{i}", ["reads"])
    before = await suggest_rules(db, user["id"])
    assert len(before) == 1
    await db.execute(
        "INSERT INTO rule_suggestion_dismissals (user_id, key) VALUES (?, ?);",
        (user["id"], before[0]["key"]),
    )
    assert await suggest_rules(db, user["id"]) == []


@pytest.mark.asyncio
async def test_duplicate_create_returns_existing_id(db):
    from src.models.items import create_rule

    user = await register_user(db, "mn4@keepfor.me", "password123")
    first = await create_rule(db, user["id"], "domain", "arxiv.org", "research")
    second = await create_rule(db, user["id"], "domain", "arxiv.org", "research")
    assert first is not None and first == second
