# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Favicons are served from our own origin, so the console never sees a 404."""

from __future__ import annotations

import json
import struct
import zlib

import pytest
from fastapi.testclient import TestClient

from keepfor.app import app
from keepfor.auth.service import login_user, register_user
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


def test_favicon_route_serves_the_site_icon(client, calls):
    """A site with an icon gets it, fetched server-side."""
    recorded = calls()
    response = client.get("/favicons/example.com")
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"].startswith("image/png")
    assert "max-age=" in response.headers["cache-control"]
    assert recorded == ["https://example.com/favicon.ico"]


def test_favicon_route_falls_back_when_the_site_has_no_icon(client, calls):
    """No icon upstream is a 200 with the fallback, never a 404."""
    calls(status=404, body=b"")
    response = client.get("/favicons/example.com")
    assert response.status_code == 200
    assert response.content == FALLBACK_ICON
    assert response.headers["content-type"].startswith("image/png")


def test_favicon_route_falls_back_when_the_upstream_body_is_not_an_image(client, calls):
    """An HTML error page must never be served as an icon."""
    calls(status=200, body=b"<html>not found</html>")
    response = client.get("/favicons/example.com")
    assert response.status_code == 200
    assert response.content == FALLBACK_ICON


def test_favicon_route_refuses_private_and_invalid_domains(client, calls):
    """Private hosts and junk never reach the network; they get the fallback."""
    recorded = calls()
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


def test_favicon_route_caches_the_icon_in_r2(db, monkeypatch, calls):
    """The second request for a domain is served from the cache."""
    recorded = calls()
    bucket = FakeR2Bucket()
    monkeypatch.setattr(
        "keepfor.deps.get_env_from_request", lambda request: MockEnv(bucket)
    )
    client = TestClient(app)
    first = client.get("/favicons/example.com")
    second = client.get("/favicons/example.com")
    assert first.content == PNG
    assert second.content == PNG
    assert len(recorded) == 1
    assert any(key.startswith("favicons/") for key in bucket.store)


def test_favicon_route_recaches_a_missing_icon_but_retries_when_stale(
    db, monkeypatch, calls
):
    """A domain without an icon is not refetched on every page load."""
    recorded = calls(status=404, body=b"")
    bucket = FakeR2Bucket()
    monkeypatch.setattr(
        "keepfor.deps.get_env_from_request", lambda request: MockEnv(bucket)
    )
    client = TestClient(app)
    first = client.get("/favicons/example.com")
    second = client.get("/favicons/example.com")
    assert first.content == FALLBACK_ICON
    assert second.content == FALLBACK_ICON
    assert len(recorded) == 1

    # Age the entry past its lifetime: the site may have grown an icon.
    payload = json.loads(bucket.store["favicons/example.com"])
    payload["ts"] -= 7200
    bucket.store["favicons/example.com"] = json.dumps(payload).encode()
    client.get("/favicons/example.com")
    assert len(recorded) == 2


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
