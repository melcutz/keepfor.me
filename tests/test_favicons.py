# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Favicons are served from our own origin, so the console never sees a 404.

The route stays public, but only a signed-in request may make us fetch a
site's icon or write it to the cache.
"""

from __future__ import annotations

import json
import struct
import zlib

import pytest
from fastapi.testclient import TestClient

from keepfor.app import app
from keepfor.auth.service import login_user, logout_session, register_user
from keepfor.models.items import save_item
from keepfor.utils.egress import FetchResult
from keepfor.utils.favicon import FALLBACK_ICON, favicon_url, normalize_host
from tests.fakes import FakeR2Bucket


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
    calls(status=404, body=b"")
    await _sign_in(client, db)
    response = client.get("/favicons/example.com")
    assert response.status_code == 200
    assert response.content == FALLBACK_ICON
    assert response.headers["content-type"].startswith("image/png")


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


async def test_anonymous_hit_serves_the_cached_icon(r2_client, db, calls):
    """The cached icon is served to an anonymous visitor, without a fetch."""
    client, bucket = r2_client
    recorded = calls()
    await _sign_in(client, db)
    assert client.get("/favicons/example.com").content == PNG
    assert len(recorded) == 1
    client.cookies.clear()  # the session expired: the next visit is anonymous
    recorded.clear()
    response = client.get("/favicons/example.com")
    assert response.status_code == 200
    assert response.content == PNG
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


async def test_signed_in_miss_with_a_failing_fetch_never_writes(
    r2_client, db, monkeypatch
):
    """A failed fetch yields the fallback and writes nothing to the cache."""
    client, bucket = r2_client

    async def boom(url, headers, policy, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr("keepfor.utils.favicon.fetch_limited", boom)
    await _sign_in(client, db)
    response = client.get("/favicons/example.com")
    assert response.status_code == 200
    assert response.content == FALLBACK_ICON
    assert bucket.puts == []


async def test_a_fallback_is_never_cached(r2_client, db, calls):
    """A site without an icon is asked again, but nothing is written to R2."""
    client, bucket = r2_client
    recorded = calls(status=404, body=b"")
    await _sign_in(client, db)
    first = client.get("/favicons/example.com")
    second = client.get("/favicons/example.com")
    assert first.content == FALLBACK_ICON
    assert second.content == FALLBACK_ICON
    assert len(recorded) == 2  # no cached fallback stops the retry
    assert bucket.puts == []
    assert bucket.store == {}


async def test_a_stale_cached_icon_is_refetched(r2_client, db, calls):
    """A cached icon is fetched again once it is older than its lifetime."""
    client, bucket = r2_client
    recorded = calls()
    await _sign_in(client, db)
    assert client.get("/favicons/example.com").content == PNG
    payload = json.loads(bucket.store["favicons/example.com"])
    payload["ts"] -= 31 * 86400
    bucket.store["favicons/example.com"] = json.dumps(payload).encode()
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
