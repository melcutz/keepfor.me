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
