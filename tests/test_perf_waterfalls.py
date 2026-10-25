# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import pytest
from keepfor.models.db import Database
from keepfor.models.items import (
    get_item,
    get_item_clean_html,
    get_items_by_ids,
    save_note,
)


class QueryCounterDB(Database):
    def __init__(self, base_db: Database) -> None:
        super().__init__(base_db.d1, base_db.sqlite)
        self.queries: list[str] = []

    async def query_first(self, sql: str, params: tuple | list = ()):
        self.queries.append(" ".join(sql.split()))
        return await super().query_first(sql, params)

    async def query_all(self, sql: str, params: tuple | list = ()):
        self.queries.append(" ".join(sql.split()))
        return await super().query_all(sql, params)


@pytest.mark.asyncio
async def test_get_items_by_ids_batches_in_two_queries_and_preserves_order(
    db: Database,
):
    await db.execute(
        "INSERT INTO users (id, email, password_hash) "
        "VALUES ('u1', 'u1@example.com', 'h');"
    )
    n1 = await save_note(db, None, "u1", "Note 1", "Body 1", tags=["alpha", "beta"])
    n2 = await save_note(db, None, "u1", "Note 2", "Body 2", tags=["gamma"])
    n3 = await save_note(db, None, "u1", "Note 3", "Body 3", tags=[])

    counting_db = QueryCounterDB(db)
    wanted_order = [n3["id"], n1["id"], n2["id"]]
    items = await get_items_by_ids(counting_db, "u1", wanted_order)

    assert [it["id"] for it in items] == wanted_order
    assert items[0]["tags"] == []
    assert items[1]["tags"] == ["alpha", "beta"]
    assert items[2]["tags"] == ["gamma"]
    # Exactly 2 queries: 1 for items IN (...), 1 for item_tags IN (...)
    assert len(counting_db.queries) == 2


@pytest.mark.asyncio
async def test_get_item_clean_html_reuses_preloaded_item(db: Database):
    await db.execute(
        "INSERT INTO users (id, email, password_hash) "
        "VALUES ('u1', 'u1@example.com', 'h');"
    )
    n1 = await save_note(db, None, "u1", "My Title", "Paragraph one.\n\nParagraph two.")
    preloaded = await get_item(db, "u1", n1["id"])
    assert preloaded is not None

    counting_db = QueryCounterDB(db)
    html = await get_item_clean_html(counting_db, None, "u1", n1["id"], item=preloaded)
    assert html is not None
    assert "Paragraph one." in html
    assert len(counting_db.queries) == 0
