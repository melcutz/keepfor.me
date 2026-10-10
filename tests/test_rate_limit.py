# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Rate limiting for the auth endpoints.

Design constraints worth remembering: counting lives in D1 (isolates are
short-lived, so in-memory counters are bypassable), only *failures* count, and
a successful login clears the account counter so a real user who mistypes a
password is never locked out.
"""

import pytest
from fastapi.testclient import TestClient

from keepfor.utils import rate_limit as rl


@pytest.fixture
def client(db, monkeypatch):
    """TestClient with the sqlite DB injected, plus a queue-bearing env."""
    from keepfor.app import app

    class FakeQueue:
        def __init__(self):
            self.sent = []

        async def send(self, message):
            self.sent.append(message)

    class Env:
        QUEUE = FakeQueue()
        AI = None
        VECTORIZE = None
        BUCKET = None
        DB = None

    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: Env())
    return TestClient(app)


def test_client_ip_prefers_cf_connecting_ip():
    """The edge-set header is the key; it cannot be spoofed by the client."""
    assert rl.client_ip({"cf-connecting-ip": "203.0.113.9"}) == "203.0.113.9"


def test_client_ip_falls_back_when_absent():
    """No header must not crash, and must not trust X-Forwarded-For."""
    headers = {"x-forwarded-for": "1.2.3.4"}
    assert rl.client_ip(headers) == rl._UNKNOWN_IP
    assert rl.client_ip({}) == rl._UNKNOWN_IP


@pytest.mark.asyncio
async def test_failures_are_recorded_per_ip_and_account(db):
    user, env, session = await _setup_user(db)
    headers = {"cf-connecting-ip": "203.0.113.5"}

    for _ in range(3):
        await rl.record_failure(db, headers, "target@example.com")

    window = rl._window_start(rl.time.time())
    ip_hits = await rl._current_failures(db, "ip:203.0.113.5", window)
    acct_hits = await rl._current_failures(db, "acct:target@example.com", window)
    assert ip_hits == 3
    assert acct_hits == 3


@pytest.mark.asyncio
async def test_account_key_is_case_insensitive(db):
    """Email casing must not hand an attacker a second bucket."""
    headers = {"cf-connecting-ip": "203.0.113.6"}
    await rl.record_failure(db, headers, "Mixed@Example.COM")
    await rl.record_failure(db, headers, "mixed@example.com")

    window = rl._window_start(rl.time.time())
    assert await rl._current_failures(db, "acct:mixed@example.com", window) == 2


@pytest.mark.asyncio
async def test_login_is_blocked_after_the_account_limit(db):
    """Repeated failures on one account eventually lock it out."""
    headers = {"cf-connecting-ip": "203.0.113.7"}
    for _ in range(rl.MAX_FAILURES_PER_ACCOUNT):
        await rl.record_failure(db, headers, "victim@example.com")

    verdict = await rl.check_allowed(db, headers, "victim@example.com")
    assert not verdict.allowed
    assert verdict.retry_after > 0
    assert verdict.scope == "acct"


@pytest.mark.asyncio
async def test_login_is_blocked_after_the_ip_limit(db):
    """One host hammering many accounts is throttled by IP."""
    headers = {"cf-connecting-ip": "203.0.113.8"}
    for _ in range(rl.MAX_FAILURES_PER_IP):
        await rl.record_failure(db, headers, f"someone{_}@example.com")

    verdict = await rl.check_allowed(db, headers, "fresh@example.com")
    assert not verdict.allowed
    assert verdict.scope == "ip"


@pytest.mark.asyncio
async def test_below_the_account_cap_is_still_allowed(db):
    """Real typos must not be punished: one under the cap still signs in."""
    headers = {"cf-connecting-ip": "203.0.113.10"}
    for _ in range(rl.MAX_FAILURES_PER_ACCOUNT - 1):
        await rl.record_failure(db, headers, "user@example.com")

    verdict = await rl.check_allowed(db, headers, "user@example.com")
    assert verdict.allowed


@pytest.mark.asyncio
async def test_account_cap_trips_before_the_ip_cap(db):
    """The account key is the tighter of the two, so it binds first."""
    headers = {"cf-connecting-ip": "203.0.113.14"}
    for _ in range(rl.MAX_FAILURES_PER_ACCOUNT):
        await rl.record_failure(db, headers, "user@example.com")

    verdict = await rl.check_allowed(db, headers, "user@example.com")
    assert not verdict.allowed
    assert verdict.scope == "acct"
    # Below the IP cap, so the IP key alone would still let it through.
    assert rl.MAX_FAILURES_PER_ACCOUNT < rl.MAX_FAILURES_PER_IP


@pytest.mark.asyncio
async def test_successful_login_clears_the_account_counter(db):
    headers = {"cf-connecting-ip": "203.0.113.11"}
    for _ in range(rl.MAX_FAILURES_PER_ACCOUNT - 1):
        await rl.record_failure(db, headers, "me@example.com")

    await rl.clear_account(db, "me@example.com")

    verdict = await rl.check_allowed(db, headers, "me@example.com")
    assert verdict.allowed


@pytest.mark.asyncio
async def test_ip_counter_survives_a_successful_login(db):
    """Clearing on success is per-account only; the IP throttle must remain."""
    headers = {"cf-connecting-ip": "203.0.113.12"}
    for _ in range(rl.MAX_FAILURES_PER_IP):
        await rl.record_failure(db, headers, "me@example.com")

    await rl.clear_account(db, "me@example.com")

    verdict = await rl.check_allowed(db, headers, "me@example.com")
    assert not verdict.allowed


@pytest.mark.asyncio
async def test_limiter_fails_open_on_error(db):
    """A limiter defect must never lock the owner out of their own instance."""

    class BrokenDB:
        async def query_first(self, *args, **kwargs):
            raise RuntimeError("no such table: rate_limits")

    verdict = await rl.check_allowed(BrokenDB(), {}, "me@example.com")
    assert verdict.allowed


@pytest.mark.asyncio
async def test_old_windows_are_pruned(db):
    """Expired windows are deleted so the table stays bounded."""
    stale = rl._window_start(rl.time.time()) - (rl.WINDOW_SECONDS * 3)
    await db.execute(
        "INSERT INTO rate_limits (key, window_start, hits) VALUES (?, ?, 5);",
        ("ip:203.0.113.13", stale),
    )

    await rl.record_failure(db, {"cf-connecting-ip": "203.0.113.13"}, "x@example.com")

    row = await db.query_first(
        "SELECT COUNT(*) AS c FROM rate_limits WHERE window_start = ?;", (stale,)
    )
    assert row["c"] == 0


@pytest.mark.asyncio
async def test_rate_limited_html_reports_minutes():
    html = rl.rate_limited_html(600)
    assert "Too many attempts" in html
    assert "10 minutes" in html


# ---------------------------------------------------------------- endpoints


@pytest.mark.asyncio
async def test_endpoint_returns_429_after_repeated_failures(client, db):
    """End-to-end: the login form starts refusing with 429 + Retry-After."""
    for _ in range(rl.MAX_FAILURES_PER_ACCOUNT):
        await rl.record_failure(
            db, {"cf-connecting-ip": "203.0.113.20"}, "nobody@example.com"
        )

    response = client.post(
        "/auth/login",
        data={"email": "nobody@example.com", "password": "wrongpassword123"},
    )
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0
    assert "Too many attempts" in response.text


@pytest.mark.asyncio
async def test_blocked_login_does_not_reach_password_hashing(client, db, monkeypatch):
    """The throttle runs BEFORE PBKDF2, so a blocked client costs no CPU."""
    called = []

    async def spy(*args, **kwargs):
        called.append(1)
        raise AssertionError("password hashing must not run when blocked")

    monkeypatch.setattr("keepfor.auth.service.verify_password", spy)

    for _ in range(rl.MAX_FAILURES_PER_ACCOUNT):
        await rl.record_failure(
            db, {"cf-connecting-ip": "203.0.113.21"}, "nobody@example.com"
        )

    response = client.post(
        "/auth/login",
        data={"email": "nobody@example.com", "password": "wrongpassword123"},
    )
    assert response.status_code == 429
    assert called == []


@pytest.mark.asyncio
async def test_real_login_still_works_and_resets_limits(client, db):
    """The happy path is unaffected, and it clears the account counter."""
    from keepfor.auth.service import register_user

    await register_user(db, "owner@test.local", "password12345")
    headers = {"cf-connecting-ip": "203.0.113.22"}
    for _ in range(rl.MAX_FAILURES_PER_ACCOUNT - 1):
        await rl.record_failure(db, headers, "owner@test.local")

    response = client.post(
        "/auth/login",
        data={"email": "owner@test.local", "password": "password12345"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "kfm_session" in response.cookies

    window = rl._window_start(rl.time.time())
    assert await rl._current_failures(db, "acct:owner@test.local", window) == 0


@pytest.mark.asyncio
async def test_failed_login_is_counted(client, db):
    from keepfor.auth.service import register_user

    await register_user(db, "owner2@test.local", "password12345")
    response = client.post(
        "/auth/login",
        data={"email": "owner2@test.local", "password": "wrongpassword123"},
    )
    assert response.status_code == 400

    window = rl._window_start(rl.time.time())
    hits = await rl._current_failures(db, "acct:owner2@test.local", window)
    assert hits == 1


async def _setup_user(db):
    """Register a throwaway user; returns (user, None, session)."""
    from keepfor.auth.service import login_user, register_user

    user = await register_user(db, "ratelimit@test.local", "password12345")
    _, session = await login_user(db, "ratelimit@test.local", "password12345")
    return user, None, session
