"""Stats: reader open log."""

from src.auth.service import register_user
from src.models.items import record_open, save_item


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


async def test_item_opens_table_exists(db):
    rows = await db.query_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='item_opens'"
    )
    assert [dict(r) for r in rows] == [{"name": "item_opens"}]


async def test_record_open_inserts_row(db, test_user_data, test_item_data):
    user = await register_user(db, test_user_data["email"], test_user_data["password"])
    env = MockEnv()
    item, is_new = await save_item(db, env, user["id"], test_item_data["url"])
    assert is_new is True
    await record_open(db, user["id"], item["id"])
    rows = await db.query_all(
        "SELECT user_id, item_id, opened_at FROM item_opens WHERE user_id = ?",
        (user["id"],),
    )
    assert len(rows) == 1
    assert rows[0]["user_id"] == user["id"]
    assert rows[0]["item_id"] == item["id"]
    assert rows[0]["opened_at"] is not None
