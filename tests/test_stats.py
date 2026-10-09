"""Stats: Keep Score scoring and reader open log."""

import datetime

import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.auth.service import login_user, register_user
from src.models.items import archive_item, create_tag, record_open, save_item
from src.models.stats import current_streak, get_user_stats, intensity_bucket, score_day


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


def _utc_today_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


async def test_get_user_stats_aggregates(db, test_user_data):
    """Seeded library: totals, deduped reads, day score, streak."""
    user = await register_user(db, test_user_data["email"], test_user_data["password"])
    env = MockEnv()
    uid = user["id"]
    item1, _ = await save_item(
        db, env, uid, "https://example.com/stats-one", tags=["python"]
    )
    item2, _ = await save_item(db, env, uid, "https://example.com/stats-two")
    await record_open(db, uid, item1["id"])
    await record_open(db, uid, item1["id"])
    assert await archive_item(db, uid, item2["id"]) is True

    stats = await get_user_stats(db, uid)
    today = _utc_today_iso()
    # saves=2, tags=1, opens deduped to 1, archives=1:
    # 2*1 + 1*1 + 1*2 + 1*3 = 8
    assert stats["total_saves"] == 2
    assert stats["total_reads"] == 1
    assert stats["total_archived"] == 1
    assert stats["day_scores"][today] == 8
    assert stats["streak"] >= 1
    assert stats["total_score"] == 8
    assert stats["best_day"] == today
    assert stats["median_active_day"] == 8.0
    assert stats["longest_streak"] >= 1
    assert stats["unread_count"] == 1
    assert stats["oldest_unread"]["id"] == item1["id"]
    assert stats["oldest_unread"]["days"] == 0
    assert stats["archive_rate"] == 0.5
    assert ("python", 1) in stats["top_tags"]
    assert ("example.com", 2) in stats["top_domains"]
    assert len(stats["week_rhythm"]) == 12
    this_week = stats["week_rhythm"][-1]
    assert (this_week["save"], this_week["open"], this_week["archive"]) == (2, 1, 1)


async def test_get_user_stats_open_dedup(db, test_user_data):
    """Two opens seconds apart score as one; an hour-old open counts too."""
    user = await register_user(db, test_user_data["email"], test_user_data["password"])
    uid = user["id"]
    item, _ = await save_item(db, MockEnv(), uid, "https://example.com/dedup")
    await record_open(db, uid, item["id"])
    await record_open(db, uid, item["id"])

    stats = await get_user_stats(db, uid)
    assert stats["total_reads"] == 1
    # save=1 + deduped open=1 -> 1*1 + 1*3 = 4
    assert stats["day_scores"][_utc_today_iso()] == 4

    hour_ago = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    ).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO item_opens (user_id, item_id, opened_at) VALUES (?, ?, ?);",
        (uid, item["id"], hour_ago),
    )
    stats = await get_user_stats(db, uid)
    assert stats["total_reads"] == 2


async def test_get_user_stats_empty_library(db, test_user_data):
    """Fresh user: zeros/None/empty, no crash."""
    user = await register_user(db, test_user_data["email"], test_user_data["password"])
    stats = await get_user_stats(db, user["id"])
    assert stats["total_saves"] == 0
    assert stats["total_reads"] == 0
    assert stats["total_archived"] == 0
    assert stats["streak"] == 0
    assert stats["longest_streak"] == 0
    assert stats["total_score"] == 0
    assert stats["best_day"] is None
    assert stats["day_scores"] == {}
    assert stats["unread_count"] == 0
    assert stats["oldest_unread"] is None
    assert stats["archive_rate"] == 0.0
    assert stats["top_tags"] == []
    assert stats["top_domains"] == []
    assert stats["median_active_day"] == 0.0
    assert len(stats["week_rhythm"]) == 12
    assert all(
        w["save"] == 0 and w["open"] == 0 and w["archive"] == 0
        for w in stats["week_rhythm"]
    )


async def test_get_user_stats_top_tags_excludes_unused(db, test_user_data):
    """Detached/never-used tags (count 0) never occupy top-10 slots."""
    user = await register_user(db, test_user_data["email"], test_user_data["password"])
    uid = user["id"]
    await save_item(db, MockEnv(), uid, "https://example.com/tagged", tags=["used"])
    assert await create_tag(db, uid, "unused") == "unused"

    stats = await get_user_stats(db, uid)
    assert ("used", 1) in stats["top_tags"]
    assert all(name != "unused" for name, _ in stats["top_tags"])


async def test_get_user_stats_isolates_users(db, test_user_data):
    """User B's saves/opens/tags must not leak into user A's stats."""
    user_a = await register_user(
        db, test_user_data["email"], test_user_data["password"]
    )
    user_b = await register_user(
        db, "other@example.com", "other_password_123", allow_public_signups=True
    )
    b_item, _ = await save_item(
        db, MockEnv(), user_b["id"], "https://example.com/other", tags=["btag"]
    )
    await record_open(db, user_b["id"], b_item["id"])
    assert await archive_item(db, user_b["id"], b_item["id"]) is True

    stats = await get_user_stats(db, user_a["id"])
    assert stats["total_saves"] == 0
    assert stats["total_reads"] == 0
    assert stats["total_archived"] == 0
    assert stats["total_score"] == 0
    assert stats["best_day"] is None
    assert stats["day_scores"] == {}
    assert stats["streak"] == 0
    assert stats["unread_count"] == 0
    assert stats["oldest_unread"] is None
    assert stats["archive_rate"] == 0.0
    assert stats["top_tags"] == []
    assert stats["top_domains"] == []
    assert stats["median_active_day"] == 0.0
    assert all(
        w["save"] == 0 and w["open"] == 0 and w["archive"] == 0
        for w in stats["week_rhythm"]
    )


def test_stats_anonymous_redirects_to_login(client):
    response = client.get("/stats", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


async def test_stats_page_renders_for_user(client, db, test_user_data):
    await register_user(db, test_user_data["email"], test_user_data["password"])
    _, session_id = await login_user(
        db, test_user_data["email"], test_user_data["password"]
    )
    client.cookies["kfm_session"] = session_id

    response = client.get("/stats")
    assert response.status_code == 200
    assert "Streak" in response.text


async def test_stats_empty_library_day_one_nudge(client, db, test_user_data):
    await register_user(db, test_user_data["email"], test_user_data["password"])
    _, session_id = await login_user(
        db, test_user_data["email"], test_user_data["password"]
    )
    client.cookies["kfm_session"] = session_id

    response = client.get("/stats")
    assert response.status_code == 200
    assert "Day 1 starts with one save" in response.text


async def test_stats_top_tag_links_use_singular_tag_param(client, db, test_user_data):
    """Top-tag links must use ?tag= (what the library reads), URL-encoded."""
    await register_user(db, test_user_data["email"], test_user_data["password"])
    _, session_id = await login_user(
        db, test_user_data["email"], test_user_data["password"]
    )
    client.cookies["kfm_session"] = session_id
    user_row = await db.query_first("SELECT id FROM users LIMIT 1;")
    await save_item(
        db, MockEnv(), user_row["id"], "https://example.com/tagged", tags=["python"]
    )

    response = client.get("/stats")
    assert response.status_code == 200
    assert "/?tag=python" in response.text
    assert "/?tags=" not in response.text


async def test_stats_rhythm_renders_grouped_bars(client, db, test_user_data):
    """Each of the 12 weeks renders save/open/archive grouped bars."""
    await register_user(db, test_user_data["email"], test_user_data["password"])
    _, session_id = await login_user(
        db, test_user_data["email"], test_user_data["password"]
    )
    client.cookies["kfm_session"] = session_id
    user_row = await db.query_first("SELECT id FROM users LIMIT 1;")
    item, _ = await save_item(
        db, MockEnv(), user_row["id"], "https://example.com/rhythm"
    )
    await record_open(db, user_row["id"], item["id"])

    response = client.get("/stats")
    assert response.status_code == 200
    # One hover title per week group, all 12 weeks present.
    assert response.text.count(' archived"') == 12
    # Three distinct per-action bars in every group.
    assert response.text.count('rounded-t bg-blue-600" style=') == 12
    assert response.text.count('rounded-t bg-emerald-600" style=') == 12
    assert response.text.count('rounded-t bg-amber-600" style=') == 12


async def test_get_user_stats_day_breakdown(db, test_user_data):
    """Per-day per-action counts behind each day score (hover breakdown)."""
    user = await register_user(db, test_user_data["email"], test_user_data["password"])
    env = MockEnv()
    uid = user["id"]
    item1, _ = await save_item(
        db, env, uid, "https://example.com/stats-one", tags=["python"]
    )
    item2, _ = await save_item(db, env, uid, "https://example.com/stats-two")
    await record_open(db, uid, item1["id"])
    await record_open(db, uid, item1["id"])
    assert await archive_item(db, uid, item2["id"]) is True

    stats = await get_user_stats(db, uid)
    today = _utc_today_iso()
    assert stats["day_breakdown"][today] == {
        "save": 2,
        "tag": 1,
        "note": 0,
        "pin": 0,
        "archive": 1,
        "open": 1,
    }


async def test_stats_heatmap_uses_theme_css_variables(client, db, test_user_data):
    """Heatmap cells must reference CSS custom properties for dark/sepia support."""
    await register_user(db, test_user_data["email"], test_user_data["password"])
    _, session_id = await login_user(
        db, test_user_data["email"], test_user_data["password"]
    )
    client.cookies["kfm_session"] = session_id

    response = client.get("/stats")
    assert response.status_code == 200
    # Templates must use CSS variables rather than hardcoded light cream colors
    assert "var(--contrib-s0)" in response.text
    assert "var(--contrib-s4)" in response.text
    # base.html must define the tokens for light, sepia, and dark
    assert "--contrib-s0:" in response.text


async def test_get_user_stats_oldest_unread_fallback_title(db, test_user_data):
    """When title is NULL, fallback to host or Untitled article, not UUID."""
    user = await register_user(db, test_user_data["email"], test_user_data["password"])
    uid = user["id"]
    # Insert an unread item with title=None
    await db.execute(
        "INSERT INTO items"
        " (id, user_id, url, canonical_url, title, read_state, created_at)"
        " VALUES ('uuid-1234', ?, 'https://news.ycombinator.com/item?id=1',"
        " 'https://news.ycombinator.com/item?id=1', NULL, 'unread',"
        " '2026-01-01 00:00:00');",
        (uid,),
    )

    stats = await get_user_stats(db, uid)
    assert stats["oldest_unread"] is not None
    assert stats["oldest_unread"]["id"] == "uuid-1234"
    assert stats["oldest_unread"]["title"] == "news.ycombinator.com"
