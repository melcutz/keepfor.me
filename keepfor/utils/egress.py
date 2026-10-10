# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Egress policy and hardened HTTP fetcher with lexical SSRF defenses.

LIMITATION:
Cloudflare Workers run in a sandboxed V8 environment without arbitrary socket
access or synchronous DNS resolution APIs. Consequently, the SSRF protections
implemented here operate as a LEXICAL policy on URL strings and IP literals.
Hostnames that resolve via external DNS to private IP addresses (DNS rebinding
or private DNS records) cannot be inspected prior to connection in Workers.
To defend against DNS rebinding in environments where egress must be strictly
isolated, configure Cloudflare egress gateways or perimeter firewall rules.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from keepfor.spi import FetchBudget


@dataclass(frozen=True)
class EgressPolicy:
    """Configuration for outbound HTTP requests."""

    max_bytes: int = 5 * 1024 * 1024  # 5 MB
    timeout_s: float = 20.0
    max_redirects: int = 5
    allowed_ports: frozenset[int] = frozenset({80, 443})
    deny_hosts: frozenset[str] = frozenset()  # exact or suffix (leading dot) matches
    # Test hook only; default False strictly blocks private/local
    allow_private_ips: bool = False


class EgressBlocked(RuntimeError):  # noqa: N818
    """Raised when an egress request is blocked by policy or budget."""


class ResponseTooLarge(RuntimeError):  # noqa: N818
    """Raised when an egress response exceeds the maximum allowed byte size."""

    def __init__(self, message: str, bytes_read: int = 0) -> None:
        super().__init__(message)
        self.bytes_read = bytes_read


@dataclass
class FetchResult:
    """Result of a limited outbound fetch."""

    text: str
    status: int
    url: str
    bytes_read: int = 0
    headers: dict[str, str] = field(default_factory=dict)


def _get_env_val(env: Any, key: str) -> Any:
    """Retrieve config value from Worker env, dict, or os.environ."""
    if env is not None:
        if hasattr(env, key):
            val = getattr(env, key)
            if val is not None:
                return val
        if isinstance(env, dict) and key in env:
            val = env[key]
            if val is not None:
                return val
    return os.environ.get(key)


def policy_from_env(env: Any = None) -> EgressPolicy:
    """Build an EgressPolicy from environment variables.

    Reads:
    - EGRESS_MAX_BYTES: maximum response body in bytes
    - EGRESS_TIMEOUT_S: request timeout in seconds
    - EGRESS_DENY_HOSTS: comma-separated list of denied hostnames or domain suffixes
    """
    max_bytes_val = _get_env_val(env, "EGRESS_MAX_BYTES")
    max_bytes = int(max_bytes_val) if max_bytes_val is not None else 5 * 1024 * 1024

    timeout_val = _get_env_val(env, "EGRESS_TIMEOUT_S")
    timeout_s = float(timeout_val) if timeout_val is not None else 20.0

    deny_hosts_val = _get_env_val(env, "EGRESS_DENY_HOSTS") or ""
    if isinstance(deny_hosts_val, str):
        deny_hosts = frozenset(
            h.strip().lower() for h in deny_hosts_val.split(",") if h.strip()
        )
    elif isinstance(deny_hosts_val, (set, frozenset, list, tuple)):
        deny_hosts = frozenset(
            str(h).strip().lower() for h in deny_hosts_val if str(h).strip()
        )
    else:
        deny_hosts = frozenset()

    return EgressPolicy(
        max_bytes=max_bytes,
        timeout_s=timeout_s,
        deny_hosts=deny_hosts,
    )


def _parse_c_style_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Parse integer, octal, hex, or dotted-part IPv4 strings."""
    try:
        packed = socket.inet_aton(host)
        parts = host.split(".")
        if 1 <= len(parts) <= 4:
            return ipaddress.IPv4Address(packed)
    except (socket.error, OSError, ValueError):
        pass

    # Pure Python fallback
    parts = host.split(".")
    if not (1 <= len(parts) <= 4):
        return None
    vals: list[int] = []
    for p in parts:
        if not p:
            return None
        try:
            if p.startswith(("0x", "0X")):
                v = int(p, 16)
            elif p.startswith("0") and len(p) > 1:
                v = int(p, 8)
            else:
                v = int(p, 10)
            if v < 0:
                return None
            vals.append(v)
        except ValueError:
            return None

    if len(vals) == 1:
        if vals[0] > 0xFFFFFFFF:
            return None
        num = vals[0]
    elif len(vals) == 2:
        if vals[0] > 0xFF or vals[1] > 0xFFFFFF:
            return None
        num = (vals[0] << 24) | vals[1]
    elif len(vals) == 3:
        if vals[0] > 0xFF or vals[1] > 0xFF or vals[2] > 0xFFFF:
            return None
        num = (vals[0] << 24) | (vals[1] << 16) | vals[2]
    elif len(vals) == 4:
        if any(v > 0xFF for v in vals):
            return None
        num = (vals[0] << 24) | (vals[1] << 16) | (vals[2] << 8) | vals[3]
    else:
        return None
    return ipaddress.IPv4Address(num)


def _is_ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if an IP literal represents a private/local/reserved network."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def _is_host_denied(host: str, deny_hosts: frozenset[str]) -> bool:
    """Check if host matches built-in or custom denied host patterns."""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    if host == "local" or host.endswith(".local"):
        return True
    if host == "internal" or host.endswith(".internal"):
        return True
    if host == "workers.dev" or host.endswith(".workers.dev"):
        return True

    for pattern in deny_hosts:
        p = pattern.lower().rstrip(".")
        if not p:
            continue
        if p.startswith("."):
            root = p[1:]
            if host == root or host.endswith(p):
                return True
        else:
            if host == p or host.endswith("." + p):
                return True
    return False


def validate_url(url: str, policy: EgressPolicy) -> str:
    """Validate and normalize a URL against SSRF policy rules.

    Raises EgressBlocked if the URL violates scheme, userinfo, port,
    host deny-list, or private/local IP literal rules.
    Returns the normalized URL string.
    """
    clean_url = url.strip()
    try:
        parsed = urllib.parse.urlsplit(clean_url)
    except Exception as exc:
        raise EgressBlocked(f"Invalid URL: {exc}") from exc

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise EgressBlocked(f"Unsupported scheme: {parsed.scheme}")

    # Reject userinfo
    if (
        "@" in parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise EgressBlocked("Userinfo is not permitted")

    raw_host = parsed.hostname
    if not raw_host:
        raise EgressBlocked("Missing host")

    clean_host = raw_host.rstrip(".").lower()
    if not clean_host:
        raise EgressBlocked("Missing host")

    # Validate port
    effective_port = parsed.port or (443 if scheme == "https" else 80)
    if effective_port not in policy.allowed_ports:
        raise EgressBlocked(f"Port {effective_port} is not allowed")

    # Validate host against deny lists
    if not policy.allow_private_ips:
        if _is_host_denied(clean_host, policy.deny_hosts):
            raise EgressBlocked(f"Denied host: {clean_host}")
    else:
        for pattern in policy.deny_hosts:
            p = pattern.lower().rstrip(".")
            if p and (clean_host == p or clean_host.endswith("." + p)):
                raise EgressBlocked(f"Denied host: {clean_host}")

    # Validate IP literals
    ip_candidate = clean_host.strip("[]")
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        ip_obj = ipaddress.ip_address(ip_candidate)
    except ValueError:
        ip_obj = _parse_c_style_ipv4(ip_candidate)

    if not policy.allow_private_ips and ip_obj is not None:
        if _is_ip_blocked(ip_obj):
            raise EgressBlocked(f"Prohibited IP address: {ip_obj}")

    is_ipv6 = isinstance(ip_obj, ipaddress.IPv6Address) or ":" in clean_host
    if parsed.port and (
        (scheme == "http" and parsed.port != 80)
        or (scheme == "https" and parsed.port != 443)
    ):
        host_repr = f"[{clean_host}]" if is_ipv6 else clean_host
        netloc = f"{host_repr}:{parsed.port}"
    else:
        netloc = f"[{clean_host}]" if is_ipv6 else clean_host

    return urllib.parse.urlunsplit(
        (scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Handler that prevents automatic redirection so each hop can be validated."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_ORIGINAL_URLOPEN = urllib.request.urlopen


def _open_request(req: urllib.request.Request, timeout: float) -> Any:
    """Execute HTTP request with manual redirect handling or test monkeypatch."""
    if urllib.request.urlopen is not _ORIGINAL_URLOPEN:
        # Honor test monkeypatch
        return urllib.request.urlopen(req, timeout=timeout)
    opener = urllib.request.build_opener(_NoRedirectHandler)
    return opener.open(req, timeout=timeout)


def _sync_fetch_hop(
    url: str,
    headers: dict[str, str],
    policy: EgressPolicy,
) -> tuple[int, dict[str, str], bytes, str | None]:
    """Execute a single HTTP hop via urllib, enforcing streaming size limits."""
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = _open_request(req, timeout=policy.timeout_s)
        code = resp.status if hasattr(resp, "status") else resp.code
        headers_dict = dict(resp.headers)
    except urllib.error.HTTPError as exc:
        code = exc.code
        headers_dict = dict(exc.headers)
        resp = exc

    # Check for redirect (301, 302, 303, 307, 308)
    if code in (301, 302, 303, 307, 308):
        loc = headers_dict.get("Location") or headers_dict.get("location")
        if loc:
            return code, headers_dict, b"", loc

    try:
        # Check Content-Length header early
        cl_header = headers_dict.get("Content-Length") or headers_dict.get(
            "content-length"
        )
        if cl_header:
            try:
                cl = int(cl_header)
                if cl > policy.max_bytes:
                    raise ResponseTooLarge(
                        f"Content-Length {cl} exceeds maximum {policy.max_bytes}",
                        bytes_read=0,
                    )
            except ValueError:
                pass

        # Read body in 64 KiB chunks, stopping immediately if max_bytes is exceeded
        chunk_size = 64 * 1024
        chunks: list[bytes] = []
        total_read = 0
        if hasattr(resp, "read"):
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                total_read += len(chunk)
                chunks.append(chunk)
                if total_read > policy.max_bytes:
                    raise ResponseTooLarge(
                        f"Response exceeded maximum {policy.max_bytes} bytes",
                        bytes_read=total_read,
                    )
        raw = b"".join(chunks)
        return code, headers_dict, raw, None
    finally:
        if hasattr(resp, "close"):
            try:
                resp.close()
            except Exception:
                pass


async def _fetch_limited_urllib(
    url: str,
    headers: dict[str, str],
    policy: EgressPolicy,
    *,
    budget: FetchBudget | None = None,
    tenant_id: str | None = None,
) -> FetchResult:
    """CPython fallback implementation using urllib."""
    current_url = validate_url(url, policy)
    redirect_count = 0

    while True:
        if budget:
            allowed = await budget.allow_fetch(tenant_id, current_url)
            if not allowed:
                raise EgressBlocked("budget")

        loop = asyncio.get_running_loop()
        code, headers_dict, body_bytes, redirect_to = await loop.run_in_executor(
            None, _sync_fetch_hop, current_url, headers, policy
        )

        if redirect_to is not None:
            redirect_count += 1
            if redirect_count > policy.max_redirects:
                raise EgressBlocked("Too many redirects")
            next_url = urllib.parse.urljoin(current_url, redirect_to)
            current_url = validate_url(next_url, policy)
            continue

        if budget:
            await budget.record_fetch(tenant_id, len(body_bytes))

        text = body_bytes.decode("utf-8", errors="replace")
        return FetchResult(
            text=text,
            status=code,
            url=current_url,
            bytes_read=len(body_bytes),
            headers=headers_dict,
        )


async def _fetch_limited_pyodide(
    url: str,
    headers: dict[str, str],
    policy: EgressPolicy,
    *,
    budget: FetchBudget | None = None,
    tenant_id: str | None = None,
) -> FetchResult:
    """Cloudflare Workers Pyodide implementation using pyfetch and ReadableStream."""
    import pyodide.http

    current_url = validate_url(url, policy)
    redirect_count = 0
    manual_redirects_supported = True

    while True:
        if budget:
            allowed = await budget.allow_fetch(tenant_id, current_url)
            if not allowed:
                raise EgressBlocked("budget")

        redirect_mode = "manual" if manual_redirects_supported else "follow"
        try:
            resp = await pyodide.http.pyfetch(
                current_url,
                headers=headers,
                redirect=redirect_mode,
                timeout=policy.timeout_s,
            )
        except Exception as exc:
            if manual_redirects_supported and "manual" in str(exc).lower():
                manual_redirects_supported = False
                resp = await pyodide.http.pyfetch(
                    current_url,
                    headers=headers,
                    redirect="follow",
                    timeout=policy.timeout_s,
                )
            else:
                raise

        status = resp.status
        resp_headers = dict(resp.headers)

        if not manual_redirects_supported:
            final_url = getattr(resp, "url", current_url)
            try:
                validate_url(final_url, policy)
            except EgressBlocked:
                if hasattr(resp, "js_response") and resp.js_response.body:
                    try:
                        reader = resp.js_response.body.getReader()
                        await reader.cancel()
                    except Exception:
                        pass
                raise
            current_url = final_url

        # Check for manual redirect
        if status in (301, 302, 303, 307, 308):
            loc = resp_headers.get("Location") or resp_headers.get("location")
            if loc:
                redirect_count += 1
                if redirect_count > policy.max_redirects:
                    raise EgressBlocked("Too many redirects")
                next_url = urllib.parse.urljoin(current_url, loc)
                current_url = validate_url(next_url, policy)
                continue

        # Check Content-Length header
        cl_header = resp_headers.get("Content-Length") or resp_headers.get(
            "content-length"
        )
        if cl_header:
            try:
                cl = int(cl_header)
                if cl > policy.max_bytes:
                    raise ResponseTooLarge(
                        f"Content-Length {cl} exceeds maximum {policy.max_bytes}",
                        bytes_read=0,
                    )
            except ValueError:
                pass

        # Stream body via ReadableStreamDefaultReader
        body_bytes = b""
        if hasattr(resp, "js_response") and resp.js_response.body:
            reader = resp.js_response.body.getReader()
            chunks: list[bytes] = []
            total_read = 0
            try:
                while True:
                    res = await reader.read()
                    if res.done:
                        break
                    val = res.value
                    chunk_len = val.length if hasattr(val, "length") else len(val)
                    total_read += chunk_len
                    if total_read > policy.max_bytes:
                        await reader.cancel()
                        raise ResponseTooLarge(
                            f"Response exceeded maximum {policy.max_bytes} bytes",
                            bytes_read=total_read,
                        )
                    if hasattr(val, "to_py"):
                        chunks.append(bytes(val.to_py()))
                    else:
                        chunks.append(bytes(val))
            finally:
                try:
                    reader.releaseLock()
                except Exception:
                    pass
            body_bytes = b"".join(chunks)
        else:
            raw_text = await resp.text()
            body_bytes = raw_text.encode("utf-8")
            if len(body_bytes) > policy.max_bytes:
                raise ResponseTooLarge(
                    f"Response exceeded maximum {policy.max_bytes} bytes",
                    bytes_read=len(body_bytes),
                )

        if budget:
            await budget.record_fetch(tenant_id, len(body_bytes))

        text = body_bytes.decode("utf-8", errors="replace")
        return FetchResult(
            text=text,
            status=status,
            url=current_url,
            bytes_read=len(body_bytes),
            headers=resp_headers,
        )


async def fetch_limited(
    url: str,
    headers: dict[str, str],
    policy: EgressPolicy,
    *,
    budget: FetchBudget | None = None,
    tenant_id: str | None = None,
) -> FetchResult:
    """Fetch URL with SSRF validation, size capping, and budget tracking.

    Delegates to Pyodide pyfetch in Worker runtime, or urllib in CPython.
    """
    if budget is None:
        try:
            from keepfor.runtime import get_providers

            budget = get_providers().fetch_budget
        except Exception:
            budget = None

    try:
        import pyodide.http  # noqa: F401

        is_pyodide = True
    except ImportError:
        is_pyodide = False

    if is_pyodide:
        return await _fetch_limited_pyodide(
            url, headers, policy, budget=budget, tenant_id=tenant_id
        )
    return await _fetch_limited_urllib(
        url, headers, policy, budget=budget, tenant_id=tenant_id
    )
