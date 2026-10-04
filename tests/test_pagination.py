"""Engine offset/total tests for library pagination."""

import pytest

from src.auth.service import register_user
from src.models.items import save_item
from src.search.engine import count_recent_items, hybrid_search


class FakeQueue:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


class MockEnv:
    def __init__(self):
        self.DB = None
        self.QUEUE = FakeQueue()
        self.AI = None
        self.VECTORIZE = None
        self.BUCKET = None


@pytest.fixture
async def user_with_items(db):
    user = await register_user(db, "pager@keepfor.me", "password123")
    env = MockEnv()
    for n in range(7):
        await save_item(db, env, user["id"], f"https://example.com/p{n}")
    return user, env, db


async def test_browse_offset_and_total(user_with_items):
    user, env, db = user_with_items
    items, total = await hybrid_search(db, env, user["id"], query="", limit=3, offset=0)
    assert total == 7
    assert len(items) == 3
    items2, total2 = await hybrid_search(
        db, env, user["id"], query="", limit=3, offset=3
    )
    assert total2 == 7
    assert len(items2) == 3
    assert items2[0]["id"] != items[0]["id"]


async def test_count_recent_items_matches(user_with_items):
    from src.models.items import add_tags_to_item

    user, env, db = user_with_items
    assert await count_recent_items(db, user["id"]) == 7
    items, _ = await hybrid_search(db, env, user["id"], query="", limit=7, offset=0)
    await add_tags_to_item(db, user["id"], items[0]["id"], ["triage"])
    assert await count_recent_items(db, user["id"], tag="triage") == 1
