"""Stats: Keep Score scoring and reader open log."""

import datetime

import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.auth.service import login_user, register_user
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


@pytest.fixture
def client(db, monkeypatch):
    """FastAPI test client with database dependency injection."""

    # Monkeypatch get_db to return the test database directly
    def mock_get_db(request):
        return db

    monkeypatch.setattr("src.app.get_db", mock_get_db)
    # Pin a queue-bearing env so request handlers enqueue instead of
    # extracting inline over the real network (TestClient scope has no env).
    monkeypatch.setattr("src.app.get_env_from_request", lambda request: MockEnv())

    return TestClient(app)


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


async def test_reader_logs_opens(client, db, test_user_data, test_item_data):
    """GET /items/{id} logs one item_opens row per visit."""
    # Same register/login pattern as tests/test_endpoints.py (setup_users +
    # auth_headers): service-level register, then login and set the session
    # cookie manually. (A Secure cookie set by POST /auth/register over
    # TestClient's plain-http testserver is never sent back, so following
    # the 303 alone leaves later requests unauthenticated.)
    await register_user(db, test_user_data["email"], test_user_data["password"])
    _, session_id = await login_user(
        db, test_user_data["email"], test_user_data["password"]
    )
    client.cookies["kfm_session"] = session_id

    saved = client.post(
        "/save",
        data={"url": test_item_data["url"], "tags": ""},
        follow_redirects=False,
    )
    assert saved.status_code in [303, 307, 308]

    row = await db.query_first(
        "SELECT id, user_id FROM items WHERE url = ?;",
        (test_item_data["url"],),
    )
    assert row is not None
    item_id = row["id"]
    user_id = row["user_id"]

    first = client.get(f"/items/{item_id}")
    assert first.status_code == 200
    second = client.get(f"/items/{item_id}")
    assert second.status_code == 200

    rows = await db.query_all(
        "SELECT user_id, item_id FROM item_opens WHERE user_id = ?;",
        (user_id,),
    )
    assert len(rows) == 2
    assert all(r["user_id"] == user_id and r["item_id"] == item_id for r in rows)


async def test_reader_survives_open_log_failure(
    client, db, test_user_data, test_item_data, monkeypatch
):
    """A logging failure must never break the reader (still 200 + renders)."""
    await register_user(db, test_user_data["email"], test_user_data["password"])
    _, session_id = await login_user(
        db, test_user_data["email"], test_user_data["password"]
    )
    client.cookies["kfm_session"] = session_id

    saved = client.post(
        "/save",
        data={"url": test_item_data["url"], "tags": ""},
        follow_redirects=False,
    )
    assert saved.status_code in [303, 307, 308]

    row = await db.query_first(
        "SELECT id FROM items WHERE url = ?;",
        (test_item_data["url"],),
    )
    assert row is not None

    async def boom(db, user_id, item_id):
        raise RuntimeError("D1 down")

    monkeypatch.setattr("src.app.record_open", boom)

    response = client.get(f"/items/{row['id']}")
    assert response.status_code == 200
    assert test_item_data["url"] in response.text
