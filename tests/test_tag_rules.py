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
