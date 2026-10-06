"""Stats: Keep Score scoring and reader open log."""

import datetime

from src.auth.service import register_user
from src.models.items import record_open, save_item
from src.models.stats import current_streak, intensity_bucket, score_day


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


def test_score_day_weights_and_caps():
    events = {"save": 12, "tag": 3, "note": 0, "pin": 1, "archive": 4, "open": 2}
    # saves capped at 10: 10*1 + 3*1 + 0 + 1*1 + 4*2 + 2*3 = 28
    assert score_day(events) == 28


def test_intensity_bucket_self_scales():
    assert intensity_bucket(0, median=5) == 0
    assert intensity_bucket(5, median=5) == 2
    assert intensity_bucket(50, median=5) == 4
    assert intensity_bucket(3, median=0) == 1  # no history yet: any activity > 0


def test_intensity_bucket_boundaries():
    assert intensity_bucket(2.45, median=5) == 1  # ratio 0.49
    assert intensity_bucket(2.5, median=5) == 2  # ratio 0.5
    assert intensity_bucket(5, median=5) == 2  # ratio 1.0
    assert intensity_bucket(7.5, median=5) == 3  # ratio 1.5
    assert intensity_bucket(10, median=5) == 4  # ratio 2.0


def test_streak_ending_yesterday_stays_alive():
    today = datetime.date.today()
    active = {today - datetime.timedelta(days=n) for n in (1, 2, 3)}
    assert current_streak(active, today) == 3


def test_streak_empty_set_is_zero():
    assert current_streak(set(), datetime.date.today()) == 0


def test_streak_gap_breaks_streak():
    today = datetime.date.today()
    active = {today, today - datetime.timedelta(days=2)}
    assert current_streak(active, today) == 1
