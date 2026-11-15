# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Favicons are served from our own origin, so the console never sees a 404.

The route stays public, but only a signed-in request may read the cache or
make us fetch a site's icon. An anonymous request always gets the fallback:
the cache is global, so a cached icon would reveal that some user's library
holds that domain.
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
import zlib

import pytest
from fastapi.testclient import TestClient

from keepfor.app import app
from keepfor.auth.service import login_user, logout_session, register_user
from keepfor.models.items import save_item
from keepfor.utils import rate_limit as rl
from keepfor.utils.egress import FetchResult
from keepfor.utils.favicon import (
    _FAVICON_INFLIGHT,
    _FAVICON_MEM_CACHE,
    FALLBACK_ICON,
    FAVICON_MAX_MISSES_PER_IP,
    FAVICON_MAX_MISSES_PER_USER,
    FAVICON_MISS_WINDOW_SECONDS,
    NEGATIVE_CACHE_MAX_AGE,
    clear_favicon_mem_cache,
    favicon_url,
    normalize_host,
    resolve_favicon,
)
from tests.fakes import FakeR2Bucket


@pytest.fixture(autouse=True)
def _clear_favicon_cache():
    """Keep the isolate favicon memory cache isolated between tests."""
    clear_favicon_mem_cache()
    yield
    clear_favicon_mem_cache()


def _png() -> bytes:
    """A valid 1x1 PNG, distinct from the fallback icon."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


PNG = _png()

# A minimal valid ICO: the 4-byte magic \x00\x00\x01\x00 that _image_type
# detects as image/x-icon, like python.org's publicly served favicon.ico.
ICO = b"\x00\x00\x01\x00" + b"\x00" * 12


class FakeQueue:
    """In-test queue: records sends without delivery."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, message: dict) -> None:
        self.sent.append(message)


class MockEnv:
    """Mock Cloudflare environment for endpoint testing."""

    def __init__(self, bucket=None):
        self.DB = None
        self.AI = None
        self.VECTORIZE = None
        self.QUEUE = FakeQueue()
        self.BUCKET = bucket


@pytest.fixture
def client(db, monkeypatch):
    """FastAPI test client with database dependency injection."""
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: MockEnv())
    return TestClient(app)


@pytest.fixture
def r2_client(db, monkeypatch):
    """Test client whose env carries an R2 bucket, to watch the cache writes."""
    bucket = FakeR2Bucket()
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr(
        "keepfor.deps.get_env_from_request", lambda request: MockEnv(bucket)
    )
    return TestClient(app), bucket


async def _sign_in(client, db) -> str:
    """Give the client a valid session cookie; returns the session id."""
    await register_user(
        db, "admin@test.local", "password123", allow_public_signups=False
    )
    _, session_id = await login_user(db, "admin@test.local", "password123")
    client.cookies["kfm_session"] = session_id
    return session_id


async def _miss_hits(db, user_id: str) -> int:
    """The cache misses counted for this user in the current window."""
    window = rl._window_start(time.time(), FAVICON_MISS_WINDOW_SECONDS)
    return await rl._current_failures(db, f"fav-user:{user_id}", window)


@pytest.fixture
def calls(monkeypatch):
    """Record the upstream fetches the proxy makes."""

    def factory(status: int = 200, body: bytes = PNG):
        recorded: list[str] = []

        async def fake_fetch(url, headers, policy, **kwargs):
            recorded.append(url)
            return FetchResult(
                text="",
                status=status,
                url=url,
                headers={"Content-Type": "image/png"},
                body=body,
            )

        monkeypatch.setattr("keepfor.utils.favicon.fetch_limited", fake_fetch)
        return recorded

    return factory


def test_normalize_host_keeps_public_names_only():
    """Only public hostnames survive; anything else has no favicon."""
    assert normalize_host("https://example.com/a?b=c") == "example.com"
    assert normalize_host("example.com") == "example.com"
    assert normalize_host("https://Example.COM:8443/x") == "example.com"
    assert normalize_host("http://127.0.0.1:8000/x") is None
    assert normalize_host("http://[::1]/x") is None
    assert normalize_host("http://localhost:3000") is None
    assert normalize_host("http://192.168.0.5/x") is None
    assert normalize_host("http://169.254.169.254/latest/meta-data") is None
    assert normalize_host("http://box.local/x") is None
    assert normalize_host("http://svc.internal/x") is None
    assert normalize_host("not a url") is None
    assert normalize_host(None) is None
    assert favicon_url("https://example.com/a") == "/favicons/example.com"
    assert favicon_url("http://127.0.0.1/a") is None


async def test_favicon_route_serves_the_site_icon(client, db, calls):
    """A signed-in visitor gets the site's icon, fetched server-side."""
    recorded = calls()
    await _sign_in(client, db)
    response = client.get("/favicons/example.com")
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"].startswith("image/png")
    assert "max-age=" in response.headers["cache-control"]
    assert recorded == ["https://example.com/favicon.ico"]


async def test_favicon_route_falls_back_when_the_site_has_no_icon(client, db, calls):
    """No icon upstream is a 200 with the fallback, never a 404."""
    recorded = calls(status=404, body=b"")
    await _sign_in(client, db)
    response = client.get("/favicons/example.com")
    assert response.status_code == 200
    assert response.content == FALLBACK_ICON
    assert response.headers["content-type"].startswith("image/png")
    assert recorded == [
        "https://example.com/favicon.ico",
        "https://icons.duckduckgo.com/ip3/example.com.ico",
    ]


async def test_404_and_non_raster_consult_icon_service(client, db, monkeypatch):
    """404 or HTML/SVG on /favicon.ico falls back to the configured icon service."""
    recorded: list[str] = []

    async def fake_fetch(url, headers, policy, **kwargs):
        recorded.append(url)
        if url == "https://svgonly.com/favicon.ico":
            return FetchResult(
                text="<svg/>", status=200, url=url, headers={}, body=b"<svg/>"
            )
        if url.endswith("/favicon.ico"):
            return FetchResult(text="", status=404, url=url, headers={}, body=b"")
        return FetchResult(text="", status=200, url=url, headers={}, body=ICO)

    monkeypatch.setattr("keepfor.utils.favicon.fetch_limited", fake_fetch)
    await _sign_in(client, db)

    res_404 = client.get("/favicons/notfound.com")
    assert res_404.status_code == 200
    assert res_404.content == ICO
    assert res_404.headers["content-type"].startswith("image/x-icon")

    res_svg = client.get("/favicons/svgonly.com")
    assert res_svg.status_code == 200
    assert res_svg.content == ICO
    assert res_svg.headers["content-type"].startswith("image/x-icon")

    assert recorded == [
        "https://notfound.com/favicon.ico",
        "https://icons.duckduckgo.com/ip3/notfound.com.ico",
        "https://svgonly.com/favicon.ico",
        "https://icons.duckduckgo.com/ip3/svgonly.com.ico",
    ]


def _refusing_site(monkeypatch, *, service_body=ICO):
    """Mock the upstream: the site refuses us, the icon service answers."""
    recorded: list[str] = []

    async def fake_fetch(url, headers, policy, **kwargs):
        recorded.append(url)
        if url.endswith("/favicon.ico"):
            return FetchResult(text="", status=403, url=url, headers={}, body=b"")
        return FetchResult(text="", status=200, url=url, headers={}, body=service_body)

    monkeypatch.setattr("keepfor.utils.favicon.fetch_limited", fake_fetch)
    return recorded


async def test_a_site_that_refuses_us_is_asked_through_the_icon_service(
    client, db, monkeypatch
):
    """python.org's CDN answers 403 to every request from Cloudflare's network.

    The icon is public, so the icon service still serves it: the card shows
    the site's real favicon instead of the transparent fallback.
    """
    recorded = _refusing_site(monkeypatch)
    await _sign_in(client, db)
    response = client.get("/favicons/www.python.org")
    assert response.status_code == 200
    assert response.content == ICO
    assert response.headers["content-type"].startswith("image/x-icon")
    assert recorded == [
        "https://www.python.org/favicon.ico",
        "https://icons.duckduckgo.com/ip3/www.python.org.ico",
    ]


async def test_the_icon_service_can_be_turned_off(client, db, monkeypatch):
    """FAVICON_ICON_SERVICE = "" keeps a refusing site on the fallback."""
    recorded = _refusing_site(monkeypatch)
    env = MockEnv()
    env.FAVICON_ICON_SERVICE = ""
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    await _sign_in(client, db)
    response = client.get("/favicons/www.python.org")
    assert response.status_code == 200
    assert response.content == FALLBACK_ICON
    assert recorded == ["https://www.python.org/favicon.ico"]


async def test_the_icon_service_never_serves_a_non_image(client, db, monkeypatch):
    """A third party answers with bytes we do not recognise: the fallback."""
    _refusing_site(monkeypatch, service_body=b"<html>not an icon</html>")
    await _sign_in(client, db)
    response = client.get("/favicons/www.python.org")
    assert response.status_code == 200
    assert response.content == FALLBACK_ICON


async def test_favicon_route_falls_back_when_the_upstream_body_is_not_an_image(
    client, db, calls
):
    """An HTML error page must never be served as an icon."""
    calls(status=200, body=b"<html>not found</html>")
    await _sign_in(client, db)
    response = client.get("/favicons/example.com")
    assert response.status_code == 200
    assert response.content == FALLBACK_ICON


async def test_favicon_route_refuses_private_and_invalid_domains(client, db, calls):
    """Private hosts and junk never reach the network; they get the fallback."""
    recorded = calls()
    await _sign_in(client, db)
    for domain in (
        "127.0.0.1",
        "localhost",
        "10.0.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "example.local",
        "not-a-domain",
    ):
        response = client.get(f"/favicons/{domain}")
        assert response.status_code == 200, domain
        assert response.content == FALLBACK_ICON, domain
    assert recorded == []


async def test_anonymous_miss_never_fetches_nor_writes(r2_client, calls):
    """An anonymous visitor gets the fallback: no outbound fetch, no cache write."""
    client, bucket = r2_client
    recorded = calls()
    response = client.get("/favicons/example.com")
    assert response.status_code == 200
    assert response.content == FALLBACK_ICON
    assert recorded == []
    assert bucket.puts == []


async def test_anonymous_hit_does_not_reveal_the_cached_icon(r2_client, db, calls):
    """An anonymous visitor gets the fallback even for a cached domain."""
    client, bucket = r2_client
    recorded = calls()
    await _sign_in(client, db)
    assert client.get("/favicons/example.com").content == PNG
    assert len(recorded) == 1
    client.cookies.clear()  # the session expired: the next visit is anonymous
    recorded.clear()
    response = client.get("/favicons/example.com")
    assert response.status_code == 200
    assert response.content == FALLBACK_ICON
    assert recorded == []
    assert bucket.puts == ["favicons/example.com"]


async def test_expired_session_is_treated_as_anonymous(r2_client, db, calls):
    """An expired session gets the fallback, exactly like no session at all."""
    client, bucket = r2_client
    recorded = calls()
    session_id = await _sign_in(client, db)
    await logout_session(db, session_id)
    response = client.get("/favicons/example.com")
    assert response.status_code == 200
    assert response.content == FALLBACK_ICON
    assert recorded == []
    assert bucket.puts == []


async def test_signed_in_miss_fetches_and_caches_the_real_icon(r2_client, db, calls):
    """A signed-in visitor may fetch the icon, and the real icon is cached."""
    client, bucket = r2_client
    recorded = calls()
    await _sign_in(client, db)
    first = client.get("/favicons/example.com")
    assert first.status_code == 200
    assert first.content == PNG
    assert recorded == ["https://example.com/favicon.ico"]
    assert bucket.puts == ["favicons/example.com"]
    # The second request is served from the cache: no fetch, no new write.
    recorded.clear()
    second = client.get("/favicons/example.com")
    assert second.content == PNG
    assert recorded == []
    assert bucket.puts == ["favicons/example.com"]


async def test_signed_in_miss_with_a_failing_fetch_caches_negative_fallback(
    r2_client, db, monkeypatch
):
    """A failed fetch yields the fallback and caches a negative entry in mem + R2."""
    client, bucket = r2_client
    fetch_calls = 0

    async def boom(url, headers, policy, **kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        raise RuntimeError("network down")

    monkeypatch.setattr("keepfor.utils.favicon.fetch_limited", boom)
    await _sign_in(client, db)
    row = await db.query_first("SELECT id FROM users;")
    user_id = row["id"]

    first = client.get("/favicons/example.com")
    assert first.status_code == 200
    assert first.content == FALLBACK_ICON
    assert fetch_calls == 1
    assert await _miss_hits(db, user_id) == 1
    assert bucket.puts == ["favicons/example.com"]
    assert bucket.metadata["favicons/example.com"].get("customMetadata") == {
        "fallback": "true"
    }
    payload = json.loads(bucket.store["favicons/example.com"])
    assert payload["fallback"] is True
    assert isinstance(payload["ts"], (int, float))

    # Second signed-in request within TTL: no upstream fetches or rate_limits writes
    second = client.get("/favicons/example.com")
    assert second.status_code == 200
    assert second.content == FALLBACK_ICON
    assert fetch_calls == 1
    assert await _miss_hits(db, user_id) == 1


async def test_negative_fallback_is_cached_in_mem_and_r2(r2_client, db, calls):
    """A 404 domain is negatively cached in both _FAVICON_MEM_CACHE and R2."""
    client, bucket = r2_client
    recorded = calls(status=404, body=b"")
    await _sign_in(client, db)
    row = await db.query_first("SELECT id FROM users;")
    user_id = row["id"]

    first = client.get("/favicons/example.com")
    assert first.status_code == 200
    assert first.content == FALLBACK_ICON
    assert len(recorded) == 2  # /favicon.ico + icon service
    assert await _miss_hits(db, user_id) == 1
    assert bucket.puts == ["favicons/example.com"]
    assert "favicons/example.com" in _FAVICON_MEM_CACHE

    # Second request hits _FAVICON_MEM_CACHE: zero upstream fetches, zero miss hits
    recorded.clear()
    second = client.get("/favicons/example.com")
    assert second.content == FALLBACK_ICON
    assert recorded == []
    assert await _miss_hits(db, user_id) == 1

    # New isolate (mem cache cleared) still hits R2 negative cache: no fetches
    clear_favicon_mem_cache()
    third = client.get("/favicons/example.com")
    assert third.content == FALLBACK_ICON
    assert recorded == []
    assert await _miss_hits(db, user_id) == 1


async def test_expired_negative_r2_entry_is_refetched(r2_client, db, calls):
    """An expired negative R2 entry is refetched when its TTL lapses."""
    client, bucket = r2_client
    recorded = calls(status=404, body=b"")
    await _sign_in(client, db)
    assert client.get("/favicons/example.com").content == FALLBACK_ICON
    assert len(recorded) == 2

    payload = json.loads(bucket.store["favicons/example.com"])
    payload["ts"] -= NEGATIVE_CACHE_MAX_AGE + 1
    bucket.store["favicons/example.com"] = json.dumps(payload).encode()
    clear_favicon_mem_cache()
    recorded.clear()

    # Now the site has an icon
    recorded = calls(status=200, body=PNG)
    res = client.get("/favicons/example.com")
    assert res.content == PNG
    assert recorded == ["https://example.com/favicon.ico"]


async def test_a_stale_cached_icon_is_refetched(r2_client, db, calls):
    """A cached icon is fetched again once it is older than its lifetime."""
    client, bucket = r2_client
    recorded = calls()
    await _sign_in(client, db)
    assert client.get("/favicons/example.com").content == PNG
    payload = json.loads(bucket.store["favicons/example.com"])
    payload["ts"] -= 31 * 86400
    bucket.store["favicons/example.com"] = json.dumps(payload).encode()
    clear_favicon_mem_cache()
    recorded.clear()
    assert client.get("/favicons/example.com").content == PNG
    assert len(recorded) == 1


@pytest.mark.asyncio
async def test_library_views_render_no_third_party_favicon_urls(client, db, calls):
    """Every view asks us for the favicon, no external service."""
    user = await register_user(
        db, "admin@test.local", "password123", allow_public_signups=False
    )
    _, session_id = await login_user(db, "admin@test.local", "password123")
    await save_item(db, MockEnv(), user["id"], "https://example.com/article", ["x"])
    client.cookies["kfm_session"] = session_id

    for view in ("cards", "list", "compact"):
        page = client.get(f"/?view={view}")
        assert page.status_code == 200, view
        assert 'src="/favicons/example.com"' in page.text, view
        assert "s2/favicons" not in page.text, view
        assert "icons.duckduckgo.com" not in page.text, view


# ------------------------------------------------------------ miss limits


async def test_cache_hits_do_not_consume_the_miss_limit(r2_client, db, calls):
    """Only a miss counts against the cap: a cached icon is free."""
    client, bucket = r2_client
    recorded = calls()
    await _sign_in(client, db)
    row = await db.query_first("SELECT id FROM users;")
    user_id = row["id"]

    assert client.get("/favicons/example.com").content == PNG
    assert len(recorded) == 1
    assert await _miss_hits(db, user_id) == 1

    recorded.clear()
    for _ in range(5):
        assert client.get("/favicons/example.com").content == PNG
    assert recorded == []
    assert await _miss_hits(db, user_id) == 1


async def test_a_signed_in_user_over_the_miss_limit_gets_the_fallback(
    r2_client, db, calls
):
    """Over the cap: the fallback, with no upstream fetch and no cache write."""
    client, bucket = r2_client
    recorded = calls()
    await _sign_in(client, db)

    for i in range(FAVICON_MAX_MISSES_PER_USER):
        assert client.get(f"/favicons/site{i}.com").content == PNG
    assert len(recorded) == FAVICON_MAX_MISSES_PER_USER

    recorded.clear()
    bucket.puts.clear()
    response = client.get("/favicons/one-too-many.com")
    assert response.status_code == 200
    assert response.content == FALLBACK_ICON
    assert recorded == []
    assert bucket.puts == []


async def test_the_miss_limit_applies_per_ip(r2_client, db, calls):
    """The IP cap throttles a user whose own counter is still empty."""
    client, bucket = r2_client
    recorded = calls()
    await _sign_in(client, db)
    ip = "203.0.113.30"
    window = rl._window_start(time.time(), FAVICON_MISS_WINDOW_SECONDS)
    await db.execute(
        "INSERT INTO rate_limits (key, window_start, hits) VALUES (?, ?, ?);",
        (f"fav-ip:{ip}", window, FAVICON_MAX_MISSES_PER_IP),
    )

    response = client.get("/favicons/example.com", headers={"cf-connecting-ip": ip})
    assert response.status_code == 200
    assert response.content == FALLBACK_ICON
    assert recorded == []
    assert bucket.puts == []


async def test_anonymous_response_is_identical_for_cached_and_uncached(
    r2_client, db, calls
):
    """An anonymous visitor must not learn whether a domain is cached."""
    client, bucket = r2_client
    recorded = calls()
    await _sign_in(client, db)
    assert client.get("/favicons/cached.com").content == PNG
    recorded_404 = calls(status=404, body=b"")
    assert client.get("/favicons/negcached.com").content == FALLBACK_ICON
    assert len(recorded_404) == 2
    client.cookies.clear()  # anonymous from now on
    recorded.clear()
    recorded_404.clear()

    cached = client.get("/favicons/cached.com")
    negcached = client.get("/favicons/negcached.com")
    uncached = client.get("/favicons/uncached.com")
    assert cached.status_code == negcached.status_code == uncached.status_code == 200
    assert cached.content == negcached.content == uncached.content == FALLBACK_ICON
    assert dict(cached.headers) == dict(negcached.headers) == dict(uncached.headers)
    assert recorded == []
    assert recorded_404 == []
    assert bucket.puts == ["favicons/cached.com", "favicons/negcached.com"]


async def test_concurrent_signed_in_misses_coalesce_via_inflight(monkeypatch):
    """Concurrent signed-in misses for the same host coalesce into one fetch."""
    bucket = FakeR2Bucket()
    env = MockEnv(bucket)
    fetch_count = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_fetch(url, headers, policy, **kwargs):
        nonlocal fetch_count
        fetch_count += 1
        started.set()
        await release.wait()
        return FetchResult(text="", status=200, url=url, headers={}, body=PNG)

    monkeypatch.setattr("keepfor.utils.favicon.fetch_limited", slow_fetch)

    t1 = asyncio.create_task(resolve_favicon("burst.com", env, may_fetch=True))
    await started.wait()
    assert "favicons/burst.com" in _FAVICON_INFLIGHT

    t2 = asyncio.create_task(resolve_favicon("burst.com", env, may_fetch=True))
    t3 = asyncio.create_task(resolve_favicon("burst.com", env, may_fetch=True))
    await asyncio.sleep(0)
    release.set()

    results = await asyncio.gather(t1, t2, t3)
    assert all(r == (PNG, "image/png") for r in results)
    assert fetch_count == 1
    assert bucket.puts == ["favicons/burst.com"]
    assert _FAVICON_INFLIGHT == {}


def test_clear_favicon_mem_cache_clears_cache_and_inflight():
    """clear_favicon_mem_cache clears both _FAVICON_MEM_CACHE and _FAVICON_INFLIGHT."""
    _FAVICON_MEM_CACHE["favicons/a.com"] = (PNG, "image/png", time.time() + 60)
    _FAVICON_INFLIGHT["favicons/b.com"] = None  # type: ignore[assignment]
    clear_favicon_mem_cache()
    assert _FAVICON_MEM_CACHE == {}
    assert _FAVICON_INFLIGHT == {}


async def test_a_favicon_miss_keeps_the_live_auth_counter(r2_client, db, calls):
    """The shared row cleanup must not drop a counter that is still current."""
    client, bucket = r2_client
    calls()
    await _sign_in(client, db)
    await rl.record_failure(
        db, {"cf-connecting-ip": "203.0.113.40"}, "victim@example.com"
    )
    window = rl._window_start(time.time())
    key = "acct:victim@example.com"
    assert await rl._current_failures(db, key, window) == 1

    assert client.get("/favicons/example.com").content == PNG  # a favicon miss

    assert await rl._current_failures(db, key, window) == 1


# ------------------------------------------------------------ session fast-path


async def test_invalid_or_private_domain_short_circuits_before_auth_and_db(
    r2_client, db, monkeypatch
):
    """Invalid or private domains return FALLBACK_ICON without auth or DB work."""
    client, _ = r2_client
    await _sign_in(client, db)

    async def fail_auth(request):
        raise AssertionError("get_current_user must not be called")

    def fail_db(request):
        raise AssertionError("get_db must not be called")

    monkeypatch.setattr("keepfor.deps.get_current_user", fail_auth)
    monkeypatch.setattr("keepfor.deps.get_db", fail_db)

    for domain in ("localhost", "127.0.0.1", "intranet.local", "not_a_host"):
        res = client.get(f"/favicons/{domain}")
        assert res.status_code == 200
        assert res.content == FALLBACK_ICON


async def test_signed_in_mem_cache_hit_executes_zero_d1_queries(db, monkeypatch, calls):
    """Cached session + cached favicon serves hits with zero D1 queries."""
    from keepfor.models.db import Database

    class CountingDB(Database):
        def __init__(self, base: Database) -> None:
            super().__init__(base.d1, base.sqlite)
            self.queries: list[str] = []

        async def query_first(self, sql: str, params=()):
            self.queries.append(sql)
            return await super().query_first(sql, params)

        async def query_all(self, sql: str, params=()):
            self.queries.append(sql)
            return await super().query_all(sql, params)

        async def execute(self, sql: str, params=()):
            self.queries.append(sql)
            return await super().execute(sql, params)

        async def execute_batch(self, statements):
            self.queries.extend(sql for sql, _ in statements)
            return await super().execute_batch(statements)

    calls()
    bucket = FakeR2Bucket()
    counting_db = CountingDB(db)
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: counting_db)
    monkeypatch.setattr(
        "keepfor.deps.get_env_from_request", lambda request: MockEnv(bucket)
    )

    client = TestClient(app)
    await _sign_in(client, db)
    counting_db.queries.clear()

    # Initial miss validates session and caches both icon and session
    assert client.get("/favicons/example.com").content == PNG
    assert len(counting_db.queries) > 0

    # Subsequent hit uses _FAVICON_SESSION_CACHE + _FAVICON_MEM_CACHE: zero D1 queries
    counting_db.queries.clear()
    for _ in range(5):
        res = client.get("/favicons/example.com")
        assert res.status_code == 200
        assert res.content == PNG
    assert counting_db.queries == []


async def test_session_cache_ttl_and_max_entries(db, monkeypatch, calls):
    """Session cache expires after 60s TTL and evicts LRU beyond 256 entries."""
    from keepfor.utils.favicon import (
        _FAVICON_SESSION_CACHE,
        FAVICON_SESSION_CACHE_MAX,
        FAVICON_SESSION_CACHE_TTL,
    )

    assert FAVICON_SESSION_CACHE_TTL == 60.0
    assert FAVICON_SESSION_CACHE_MAX == 256

    calls()
    bucket = FakeR2Bucket()
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr(
        "keepfor.deps.get_env_from_request", lambda request: MockEnv(bucket)
    )

    client = TestClient(app)
    session_id = await _sign_in(client, db)
    assert client.get("/favicons/example.com").content == PNG
    assert session_id in _FAVICON_SESSION_CACHE

    # Expire entry and confirm auth is re-evaluated on next request
    user, _ = _FAVICON_SESSION_CACHE[session_id]
    _FAVICON_SESSION_CACHE[session_id] = (user, time.time() - 1.0)
    auth_calls = 0
    orig_get_current_user = __import__(
        "keepfor.deps", fromlist=["get_current_user"]
    ).get_current_user

    async def spy_auth(request):
        nonlocal auth_calls
        auth_calls += 1
        return await orig_get_current_user(request)

    monkeypatch.setattr("keepfor.deps.get_current_user", spy_auth)
    assert client.get("/favicons/example.com").content == PNG
    assert auth_calls == 1

    # Fill beyond FAVICON_SESSION_CACHE_MAX to verify LRU bound
    for i in range(FAVICON_SESSION_CACHE_MAX + 5):
        client.cookies["kfm_session"] = f"bogus-session-{i}"
        client.get("/favicons/example.com")
    assert len(_FAVICON_SESSION_CACHE) == FAVICON_SESSION_CACHE_MAX
    assert session_id not in _FAVICON_SESSION_CACHE


async def test_concurrent_session_requests_coalesce_auth(db, monkeypatch, calls):
    """Concurrent /favicons/{domain} requests with same cookie coalesce auth."""
    import httpx

    calls()
    bucket = FakeR2Bucket()
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr(
        "keepfor.deps.get_env_from_request", lambda request: MockEnv(bucket)
    )

    await register_user(db, "burst@example.com", "password123")
    _, session_id = await login_user(db, "burst@example.com", "password123")

    orig_get_current_user = __import__(
        "keepfor.deps", fromlist=["get_current_user"]
    ).get_current_user
    auth_calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_auth(request):
        nonlocal auth_calls
        auth_calls += 1
        started.set()
        await release.wait()
        return await orig_get_current_user(request)

    monkeypatch.setattr("keepfor.deps.get_current_user", slow_auth)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies={"kfm_session": session_id},
    ) as async_client:
        tasks = [
            asyncio.create_task(async_client.get(f"/favicons/site{i}.com"))
            for i in range(5)
        ]
        await started.wait()
        await asyncio.sleep(0)
        release.set()
        responses = await asyncio.gather(*tasks)

    assert all(r.status_code == 200 and r.content == PNG for r in responses)
    assert auth_calls == 1


async def test_logout_session_invalidates_cached_favicon_session(r2_client, db, calls):
    """logout_session invalidates _FAVICON_SESSION_CACHE so icon falls back."""
    client, _ = r2_client
    calls()
    session_id = await _sign_in(client, db)

    # Warm both _FAVICON_MEM_CACHE and _FAVICON_SESSION_CACHE
    assert client.get("/favicons/example.com").content == PNG

    await logout_session(db, session_id)

    res = client.get("/favicons/example.com")
    assert res.status_code == 200
    assert res.content == FALLBACK_ICON
