# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

import http.server
import socket
import threading
import urllib.parse
from typing import Any

import pytest

from keepfor.consumer.processor import _fetch_and_extract
from keepfor.spi import FetchBudget
from keepfor.utils.egress import (
    EgressBlocked,
    EgressPolicy,
    ResponseTooLarge,
    fetch_limited,
    policy_from_env,
    validate_url,
)

# ---------------------------------------------------------------------------
# 1. Table-driven validate_url test cases (>= 40 cases)
# ---------------------------------------------------------------------------

BLOCKED_URL_CASES: list[tuple[str, str]] = [
    # Explicit requirements from plan:
    ("http://127.0.0.1/", "IPv4 loopback literal"),
    ("http://2130706433/", "C-style integer IPv4 loopback (127.0.0.1)"),
    ("http://0x7f.1/", "C-style hex/multi-part IPv4 loopback"),
    ("http://[::1]/", "IPv6 loopback literal"),
    ("http://[::ffff:10.0.0.1]/", "IPv6-mapped IPv4 private 10.0.0.1"),
    ("http://169.254.169.254/", "IPv4 link-local / cloud metadata"),
    ("http://user:pw@example.com/", "Userinfo in URL"),
    ("http://example.com:8080/", "Non-allowed port (8080)"),
    ("ftp://x/", "Unsupported scheme ftp"),
    ("http://localhost./", "Localhost with trailing dot"),
    # Additional scheme checks:
    ("file:///etc/passwd", "file scheme"),
    ("javascript:alert(1)", "javascript scheme"),
    ("data:text/html,hello", "data scheme"),
    ("gopher://example.com/", "gopher scheme"),
    ("ssh://git@github.com/", "ssh scheme"),
    # Missing host:
    ("http:///path", "empty host"),
    ("https://:80/path", "missing hostname"),
    # Userinfo variations:
    ("http://user@example.com/", "userinfo without password"),
    ("https://admin:pass@example.com:443/", "userinfo on https"),
    # Disallowed ports:
    ("http://example.com:22/", "SSH port 22"),
    ("http://example.com:25/", "SMTP port 25"),
    ("https://example.com:8443/", "Alternative https port 8443"),
    ("http://example.com:3000/", "Dev port 3000"),
    # Denied hostnames and domain patterns:
    ("http://localhost/", "exact localhost"),
    ("http://sub.localhost/", "subdomain of localhost"),
    ("http://myhost.local/", "mDNS .local domain"),
    ("http://service.internal/", "internal domain"),
    ("http://api.workers.dev/", "Cloudflare workers.dev default deny"),
    ("https://my-app.workers.dev/ping", "https workers.dev default deny"),
    # IPv4 loopback, private, link-local, multicast, reserved:
    ("http://127.0.0.2:80/", "IPv4 loopback range"),
    ("http://10.0.0.1/", "IPv4 10.0.0.0/8 private"),
    ("http://10.255.255.254/", "IPv4 10.0.0.0/8 boundary"),
    ("http://172.16.0.1/", "IPv4 172.16.0.0/12 private start"),
    ("http://172.31.255.254/", "IPv4 172.16.0.0/12 private end"),
    ("http://192.168.0.1/", "IPv4 192.168.0.0/16 private"),
    ("http://192.168.1.254/", "IPv4 192.168.0.0/16 private"),
    ("http://169.254.1.1/", "IPv4 169.254.0.0/16 link-local"),
    ("http://0.0.0.0/", "IPv4 unspecified"),
    ("http://224.0.0.1/", "IPv4 multicast"),
    ("http://240.0.0.1/", "IPv4 reserved class E"),
    ("http://255.255.255.255/", "IPv4 broadcast"),
    # C-style integer, octal, hex IPv4 variations:
    ("http://0177.0.0.1/", "Octal 0177.0.0.1 (127.0.0.1)"),
    ("http://0x7f000001/", "Hex integer 0x7f000001 (127.0.0.1)"),
    ("http://2886729729/", "Integer 2886729729 (172.16.0.1)"),
    ("http://3232235777/", "Integer 3232235777 (192.168.1.1)"),
    ("http://2852039166/", "Integer 2852039166 (169.254.169.254)"),
    ("http://0xa9fea9fe/", "Hex integer 0xa9fea9fe (169.254.169.254)"),
    ("http://012.0.0.1/", "Octal leading 012 (10.0.0.1)"),
    # IPv6 literals:
    ("http://[::]/", "IPv6 unspecified"),
    ("http://[fe80::1]/", "IPv6 link-local"),
    ("http://[fc00::1]/", "IPv6 unique local (ULA)"),
    ("http://[fd12:3456:789a::1]/", "IPv6 unique local ULA fd00::/8"),
    ("http://[ff02::1]/", "IPv6 multicast"),
    ("http://[::ffff:127.0.0.1]/", "IPv6-mapped loopback"),
    ("http://[::ffff:169.254.169.254]/", "IPv6-mapped metadata"),
    ("http://[::ffff:192.168.1.1]/", "IPv6-mapped private 192.168"),
]

ALLOWED_URL_CASES: list[tuple[str, str]] = [
    # Explicit requirement from plan:
    ("https://example.com/a?b=1", "valid https query string"),
    # Other valid internet targets:
    ("http://example.com/", "standard http port 80 implicit"),
    ("http://example.com:80/", "standard http port 80 explicit"),
    ("https://example.com:443/docs", "standard https port 443 explicit"),
    ("https://news.ycombinator.com/item?id=12345", "news website url"),
    ("https://sub.domain.example.org/path/to/page#heading", "fragment & subdomain"),
    ("http://93.184.216.34/", "Public IPv4 literal (example.com)"),
    ("http://[2606:2800:220:1:248:1893:25c8:1946]/", "Public IPv6 literal"),
    ("https://blog.cloudflare.com/workers", "public cloudflare blog"),
]


@pytest.mark.parametrize("url,reason", BLOCKED_URL_CASES)
def test_validate_url_rejects_dangerous_urls(url: str, reason: str):
    policy = EgressPolicy()
    with pytest.raises(EgressBlocked, match=r".+"):
        validate_url(url, policy)


@pytest.mark.parametrize("url,reason", ALLOWED_URL_CASES)
def test_validate_url_allows_valid_urls(url: str, reason: str):
    policy = EgressPolicy()
    normalized = validate_url(url, policy)
    assert normalized.startswith("http://") or normalized.startswith("https://")


def test_validate_url_custom_deny_hosts():
    policy = EgressPolicy(deny_hosts=frozenset({"forbidden.com", ".secret.org"}))
    with pytest.raises(EgressBlocked, match="Denied host"):
        validate_url("https://forbidden.com/login", policy)

    with pytest.raises(EgressBlocked, match="Denied host"):
        validate_url("https://sub.secret.org/data", policy)

    with pytest.raises(EgressBlocked, match="Denied host"):
        validate_url("https://secret.org/", policy)

    # Unrelated hosts should pass
    valid = validate_url("https://allowed.org/data", policy)
    assert valid == "https://allowed.org/data"


# ---------------------------------------------------------------------------
# 2. policy_from_env tests
# ---------------------------------------------------------------------------


def test_policy_from_env_defaults():
    policy = policy_from_env(None)
    assert policy.max_bytes == 5 * 1024 * 1024
    assert policy.timeout_s == 20.0
    assert policy.max_redirects == 5
    assert policy.allowed_ports == frozenset({80, 443})
    assert policy.deny_hosts == frozenset()


def test_policy_from_env_dict():
    env = {
        "EGRESS_MAX_BYTES": "2097152",
        "EGRESS_TIMEOUT_S": "10.5",
        "EGRESS_DENY_HOSTS": "corp.internal, .private.lan, blocked.com",
    }
    policy = policy_from_env(env)
    assert policy.max_bytes == 2 * 1024 * 1024
    assert policy.timeout_s == 10.5
    assert "corp.internal" in policy.deny_hosts
    assert ".private.lan" in policy.deny_hosts
    assert "blocked.com" in policy.deny_hosts


def test_policy_from_env_object():
    class FakeEnv:
        EGRESS_MAX_BYTES = 1048576
        EGRESS_TIMEOUT_S = 5.0
        EGRESS_DENY_HOSTS = "custom.internal"

    policy = policy_from_env(FakeEnv())
    assert policy.max_bytes == 1048576
    assert policy.timeout_s == 5.0
    assert "custom.internal" in policy.deny_hosts


# ---------------------------------------------------------------------------
# 3. CPython streaming size cap test using local http.server thread
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _StreamServerHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass  # Suppress server stderr logs during test

    def do_GET(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/stream-20mb":
                # Stream 20 MB of data without Content-Length
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                chunk = b"X" * 65536  # 64 KiB chunk
                for _ in range(320):  # 320 * 64 KiB = 20 MB
                    self.wfile.write(chunk)
                    self.wfile.flush()
            elif parsed.path == "/oversized-content-length":
                # Return Content-Length header greater than max_bytes
                self.send_response(200)
                self.send_header("Content-Length", str(20 * 1024 * 1024))
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"should not be read")
            elif parsed.path == "/redirect-loop":
                # Redirect to itself
                self.send_response(302)
                self.send_header("Location", "/redirect-loop")
                self.end_headers()
            elif parsed.path == "/redirect-to-blocked":
                # Redirect to loopback IP (blocked when allow_private_ips=False)
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1/admin")
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture(scope="module")
def streaming_server():
    port = _find_free_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _StreamServerHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    yield port
    server.shutdown()
    server.server_close()


@pytest.mark.asyncio
async def test_cpython_streaming_size_cap(streaming_server):
    """CPython fallback caps at max_bytes streaming 20 MB.

    Bytes read must be <= max_bytes + 64 KiB.
    """
    port = streaming_server
    # Allow 128 KiB max body; allow_private_ips enabled for local test server
    max_bytes = 128 * 1024
    policy = EgressPolicy(
        max_bytes=max_bytes,
        allowed_ports=frozenset({port}),
        allow_private_ips=True,
    )

    url = f"http://127.0.0.1:{port}/stream-20mb"
    with pytest.raises(ResponseTooLarge) as exc_info:
        await fetch_limited(url, {}, policy)

    # Must have stopped reading as soon as max_bytes was exceeded
    assert exc_info.value.bytes_read <= max_bytes + 64 * 1024
    assert exc_info.value.bytes_read > max_bytes


@pytest.mark.asyncio
async def test_cpython_content_length_early_rejection(streaming_server):
    """Oversized Content-Length header aborts before reading body."""
    port = streaming_server
    max_bytes = 1024 * 1024  # 1 MB
    policy = EgressPolicy(
        max_bytes=max_bytes,
        allowed_ports=frozenset({port}),
        allow_private_ips=True,
    )

    url = f"http://127.0.0.1:{port}/oversized-content-length"
    with pytest.raises(ResponseTooLarge) as exc_info:
        await fetch_limited(url, {}, policy)

    assert exc_info.value.bytes_read == 0
    assert "Content-Length" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 4. Meta-refresh SSRF rejection test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meta_refresh_to_loopback_refused(monkeypatch):
    """A meta-refresh stub pointing to 127.0.0.1 is refused with EgressBlocked."""
    # Mock initial fetch_page_html returning a meta-refresh stub to 127.0.0.1
    meta_html = (
        '<html><head><meta http-equiv="refresh" content="0; url=http://127.0.0.1/secret">'
        "</head><body>Redirecting...</body></html>"
    )

    async def fake_fetch(url, headers=None, **kwargs):
        return meta_html

    monkeypatch.setattr("keepfor.consumer.processor.fetch_page_html", fake_fetch)

    with pytest.raises(EgressBlocked, match=r"Prohibited IP address|Denied host"):
        await _fetch_and_extract("https://example.com/start", "item_123")


# ---------------------------------------------------------------------------
# 5. FetchBudget hook test
# ---------------------------------------------------------------------------


class _TrackingBudget(FetchBudget):
    def __init__(self, allow: bool = True):
        self.allow = allow
        self.recorded_bytes: list[tuple[str | None, int]] = []
        self.checked_urls: list[str] = []

    async def allow_fetch(self, tenant_id: str | None, url: str) -> bool:
        self.checked_urls.append(url)
        return self.allow

    async def record_fetch(self, tenant_id: str | None, bytes_read: int) -> None:
        self.recorded_bytes.append((tenant_id, bytes_read))


@pytest.mark.asyncio
async def test_budget_hook_denies_and_records():
    # 1. Budget denial
    denying_budget = _TrackingBudget(allow=False)
    policy = EgressPolicy()
    with pytest.raises(EgressBlocked, match="budget"):
        await fetch_limited(
            "https://example.com/article",
            {},
            policy,
            budget=denying_budget,
            tenant_id="tenant_x",
        )

    assert len(denying_budget.checked_urls) == 1
    assert len(denying_budget.recorded_bytes) == 0

    # 2. Budget recording on success
    allowing_budget = _TrackingBudget(allow=True)

    # Monkeypatch _sync_fetch_hop to return fixed content without real network
    from keepfor.utils import egress

    def mock_hop(url, headers, policy):
        return 200, {"content-type": "text/html"}, b"Hello World", None

    original_hop = egress._sync_fetch_hop
    try:
        egress._sync_fetch_hop = mock_hop
        res = await fetch_limited(
            "https://example.com/article",
            {},
            policy,
            budget=allowing_budget,
            tenant_id="tenant_x",
        )
        assert res.status == 200
        assert res.text == "Hello World"
        assert res.bytes_read == 11
        assert len(allowing_budget.recorded_bytes) == 1
        assert allowing_budget.recorded_bytes[0] == ("tenant_x", 11)
    finally:
        egress._sync_fetch_hop = original_hop


# ---------------------------------------------------------------------------
# 6. Max redirects and redirect security tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redirect_loop_exceeding_max_redirects(streaming_server):
    port = streaming_server
    policy = EgressPolicy(
        max_redirects=3,
        allowed_ports=frozenset({port}),
        allow_private_ips=True,
    )
    url = f"http://127.0.0.1:{port}/redirect-loop"
    with pytest.raises(EgressBlocked, match="Too many redirects"):
        await fetch_limited(url, {}, policy)


@pytest.mark.asyncio
async def test_redirect_to_blocked_host_rejected(streaming_server):
    port = streaming_server
    # allow_private_ips=False for redirect target validation
    policy = EgressPolicy(
        max_redirects=5,
        allowed_ports=frozenset({port, 80, 443}),
        allow_private_ips=False,
    )
    # The server redirects to http://127.0.0.1/admin, which validate_url will block
    from keepfor.utils import egress

    def mock_hop(url, headers, policy):
        if url == "https://example.com/hop1":
            return (
                302,
                {"Location": "http://127.0.0.1/admin"},
                b"",
                "http://127.0.0.1/admin",
            )
        return 200, {}, b"ok", None

    original_hop = egress._sync_fetch_hop
    try:
        egress._sync_fetch_hop = mock_hop
        with pytest.raises(EgressBlocked, match="Prohibited IP address"):
            await fetch_limited("https://example.com/hop1", {}, policy)
    finally:
        egress._sync_fetch_hop = original_hop
