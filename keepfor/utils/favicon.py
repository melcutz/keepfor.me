# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Same-origin favicon proxy.

The library cards used to point their <img> straight at a third-party favicon
service. Those answer 404 for every domain without an icon, so each library
page filled the browser console with failed requests. The page now asks us
instead: /favicons/<domain> always answers 200 -- with the site's icon when it
has one, otherwise with a transparent fallback that leaves the card's letter
tile visible.

The route is public, so only a signed-in request may read the cache or make
us fetch a site's icon. An anonymous request gets the transparent fallback,
always: the cache is global, so serving a cached icon would tell an anonymous
visitor whether some user's library holds that domain.

A cache miss is rate limited per user and per IP: a miss makes us fetch an
arbitrary domain and write an R2 object for it. Over the limit the fallback is
served, with no upstream fetch and no cache write.

A site whose CDN refuses our own network (python.org answers 403 to every
request that leaves Cloudflare) is asked for its icon through a public icon
service instead -- the icon is public, the service just reaches it from a
network the site does not block.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from keepfor.utils.egress import EgressPolicy, fetch_limited
from keepfor.utils.logging import logger
from keepfor.utils.rate_limit import check_limit, client_ip_key, record_hits

# A 1x1 transparent PNG: the browser loads it without a console error and the
# card's letter tile shows through it.
FALLBACK_ICON = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

# Hostname shape: dot-separated labels of letters/digits/hyphens ending in an
# alphabetic TLD. The alphabetic TLD is what keeps IP literals out ("127.0.0.1"
# ends in a digit).
_HOST_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

# Names that never point at a public site: mDNS, split-horizon DNS and the
# loopback names a saved link can carry.
_PRIVATE_TLDS = frozenset(
    {
        "local",
        "localhost",
        "internal",
        "intranet",
        "lan",
        "corp",
        "home",
        "home.arpa",
        "test",
        "invalid",
        "example",
    }
)

# Raster formats only. SVG is left out on purpose: a script inside an SVG
# served from our own origin runs when that URL is opened as a document.
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
)

FAVICON_TIMEOUT_S = 3.0
FAVICON_MAX_BYTES = 256 * 1024
FAVICON_MAX_AGE = 604800  # a real icon: keep it for a week
ICON_MAX_AGE = 30 * 86400  # a cached icon is refreshed after a month
NEGATIVE_CACHE_MAX_AGE = 86400  # negative/fallback outcomes: 24 hours
# Browser cache lifetime for fallback responses.
FALLBACK_MAX_AGE = 3600

# Bounded in-memory isolate cache to avoid repeated R2 reads within an isolate.
FAVICON_MEM_CACHE_MAX = 512
FAVICON_MEM_CACHE_TTL = 3600.0  # 1 hour
_FAVICON_MEM_CACHE: dict[str, tuple[bytes, str, float]] = {}
_FAVICON_INFLIGHT: dict[str, asyncio.Future[tuple[bytes, str]]] = {}

# Bounded short-TTL session cache for /favicons/{domain} so a library page
# burst of 20-50 icons reuses the validated session instead of hitting D1
# per icon. Invalidated on logout_session() and clear_favicon_mem_cache().
FAVICON_SESSION_CACHE_MAX = 256
FAVICON_SESSION_CACHE_TTL = 60.0
_FAVICON_SESSION_CACHE: dict[str, tuple[dict[str, Any] | None, float]] = {}
_FAVICON_SESSION_INFLIGHT: dict[str, asyncio.Future[dict[str, Any] | None]] = {}
_SESSION_CACHE_MISS = object()


def clear_favicon_mem_cache() -> None:
    """Clear the in-memory isolate cache and in-flight coalescing state."""
    _FAVICON_MEM_CACHE.clear()
    _FAVICON_INFLIGHT.clear()
    _FAVICON_SESSION_CACHE.clear()
    _FAVICON_SESSION_INFLIGHT.clear()


def invalidate_favicon_session(session_id: str | None) -> None:
    """Invalidate a cached session entry on logout."""
    if not session_id:
        return
    _FAVICON_SESSION_CACHE.pop(session_id, None)
    _FAVICON_SESSION_INFLIGHT.pop(session_id, None)


def _session_cache_get(session_id: str) -> Any:
    entry = _FAVICON_SESSION_CACHE.get(session_id)
    if entry is None:
        return _SESSION_CACHE_MISS
    user, exp = entry
    if time.time() >= exp:
        _FAVICON_SESSION_CACHE.pop(session_id, None)
        return _SESSION_CACHE_MISS
    _FAVICON_SESSION_CACHE[session_id] = _FAVICON_SESSION_CACHE.pop(session_id)
    return user


def _session_cache_put(session_id: str, user: dict[str, Any] | None) -> None:
    if session_id in _FAVICON_SESSION_CACHE:
        _FAVICON_SESSION_CACHE.pop(session_id)
    elif len(_FAVICON_SESSION_CACHE) >= FAVICON_SESSION_CACHE_MAX:
        oldest = next(iter(_FAVICON_SESSION_CACHE))
        _FAVICON_SESSION_CACHE.pop(oldest, None)
    _FAVICON_SESSION_CACHE[session_id] = (
        user,
        time.time() + FAVICON_SESSION_CACHE_TTL,
    )


async def resolve_favicon_user(
    request: Any,
    auth_fn: Callable[[], Awaitable[dict[str, Any] | None]],
) -> dict[str, Any] | None:
    """Resolve the current user for /favicons/{domain} with short-TTL caching.

    Bearer Authorization requests and cookie-less requests delegate directly
    to `auth_fn`. Session-cookie requests (`kfm_session` / `rk_session` /
    `kfc_session`) reuse `_FAVICON_SESSION_CACHE` (60s TTL, max 256 entries)
    and coalesce concurrent validations for the same session cookie via
    `_FAVICON_SESSION_INFLIGHT`.
    """
    headers = getattr(request, "headers", None)
    if headers is not None and headers.get("Authorization"):
        return await auth_fn()

    cookies = getattr(request, "cookies", None)
    session_id = (
        (
            cookies.get("kfm_session")
            or cookies.get("rk_session")
            or cookies.get("kfc_session")
        )
        if cookies is not None
        else None
    )
    if not session_id:
        return await auth_fn()

    cached = _session_cache_get(session_id)
    if cached is not _SESSION_CACHE_MISS:
        return cached

    inflight = _FAVICON_SESSION_INFLIGHT.get(session_id)
    if inflight is not None and not inflight.done():
        return await asyncio.shield(inflight)

    fut = asyncio.get_running_loop().create_future()
    _FAVICON_SESSION_INFLIGHT[session_id] = fut
    try:
        user = await auth_fn()
        if _FAVICON_SESSION_INFLIGHT.get(session_id) is fut:
            _session_cache_put(session_id, user)
        if not fut.done():
            fut.set_result(user)
        return user
    except BaseException:
        if not fut.done():
            fut.cancel()
        raise
    finally:
        if _FAVICON_SESSION_INFLIGHT.get(session_id) is fut:
            _FAVICON_SESSION_INFLIGHT.pop(session_id, None)


# Cache-miss rate limit. Every miss makes the Worker fetch an arbitrary
# domain and write an R2 object for it, so the misses are capped per user and
# per IP. 60 misses per 10 minutes is far more than a human browsing a
# library needs.
FAVICON_MISS_WINDOW_SECONDS = 600
FAVICON_MAX_MISSES_PER_USER = 60
FAVICON_MAX_MISSES_PER_IP = 60

# Same browser agent the article fetcher uses: plenty of sites answer 403 to
# an unknown agent.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_ICON_HEADERS = {
    "Accept": "image/*,*/*;q=0.8",
    "User-Agent": USER_AGENT,
}

# A site's CDN can refuse our egress outright: python.org answers 403 with an
# empty body to every request that leaves Cloudflare's network, even though
# the icon is public (keepfor-cloud#168). Such a site is not broken, it just
# does not serve us -- so the icon is fetched from a public icon service,
# which reaches the site from a network it does not refuse.
# Set FAVICON_ICON_SERVICE to "" to turn the fallback off.
ICON_SERVICE_DEFAULT = "https://icons.duckduckgo.com/ip3/"

# Statuses that mean "the site refused us", not "the site has no icon": a 404
# (or any other answer) keeps the transparent fallback. The same set the
# article fetcher proxies around (PROXY_FALLBACK_STATUSES), plus the 419
# Cloudflare answers to a bot-fight challenge.
REFUSED_STATUSES = frozenset({403, 419, 429, 520, 522, 530})


def normalize_host(value: str | None) -> str | None:
    """Return the public hostname of a URL (or of a bare host), else None.

    None means "not a public site": an IP literal, a private/reserved name or
    anything that is not a hostname at all. Callers use that to fall back to
    the placeholder instead of fetching.
    """
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw if "://" in raw else "//" + raw)
        host = parsed.hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.rstrip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None
    if not _HOST_RE.match(host):
        return None
    if host.rsplit(".", 1)[1] in _PRIVATE_TLDS:
        return None
    return host


def favicon_url(url: str | None) -> str | None:
    """Same-origin URL of the favicon for a saved link, or None if unusable."""
    host = normalize_host(url)
    return f"/favicons/{host}" if host else None


def _image_type(body: bytes) -> str | None:
    """Detect a raster image type from the magic bytes, or None."""
    for signature, content_type in _IMAGE_SIGNATURES:
        if body.startswith(signature):
            return content_type
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return None


async def _icon_at(url: str, policy: EgressPolicy) -> tuple[bytes, str] | None:
    """(body, content_type) when the URL answers 200 with a raster image."""
    result = await fetch_limited(url, _ICON_HEADERS, policy)
    if result.status != 200 or not result.body:
        return None
    content_type = _image_type(result.body)
    if content_type is None:
        return None
    return result.body, content_type


def _icon_service(env: Any) -> str:
    """The public icon service used when a site refuses our request."""
    base = getattr(env, "FAVICON_ICON_SERVICE", ICON_SERVICE_DEFAULT)
    if not isinstance(base, str):
        base = ICON_SERVICE_DEFAULT
    base = base.strip()
    if base and not base.endswith("/"):
        base += "/"
    return base


async def _fetch_upstream(host: str, env: Any = None) -> tuple[bytes, str] | None:
    """Fetch https://<host>/favicon.ico through the hardened egress fetcher.

    A site that refuses us (a CDN block on our own network) is asked for its
    icon through a public icon service instead.
    """
    policy = EgressPolicy(
        max_bytes=FAVICON_MAX_BYTES,
        timeout_s=FAVICON_TIMEOUT_S,
        max_redirects=2,
    )
    try:
        result = await fetch_limited(
            f"https://{host}/favicon.ico",
            _ICON_HEADERS,
            policy,
        )
        if result.status == 200 and result.body:
            content_type = _image_type(result.body)
            if content_type is not None:
                return result.body, content_type
        # /favicon.ico did not yield a valid raster image (404, HTML/SVG, or
        # CDN refusal): ask the configured public icon service before falling back.
        base = _icon_service(env)
        if not base:
            return None
        logger.info(
            "favicon %s unavailable (%s): asking the icon service",
            host,
            result.status,
        )
        return await _icon_at(f"{base}{host}.ico", policy)
    except Exception:
        logger.warning("favicon fetch failed for %s", host, exc_info=True)
        return None


def _bucket(env: Any) -> Any:
    """The R2 binding, when the deployment has one."""
    if env is None:
        return None
    for name in ("BUCKET", "keepfor_me_bucket"):
        bucket = getattr(env, name, None)
        if bucket is not None:
            return bucket
    return None


def _mem_cache_put(key: str, body: bytes, content_type: str, ttl: float) -> None:
    """Store a favicon or negative fallback entry in the isolate memory cache."""
    _FAVICON_MEM_CACHE.pop(key, None)
    if len(_FAVICON_MEM_CACHE) >= FAVICON_MEM_CACHE_MAX:
        _FAVICON_MEM_CACHE.pop(next(iter(_FAVICON_MEM_CACHE)), None)
    _FAVICON_MEM_CACHE[key] = (body, content_type, time.time() + ttl)


async def _cache_get(bucket: Any, key: str) -> tuple[bytes, str] | None:
    now = time.time()
    cached = _FAVICON_MEM_CACHE.get(key)
    if cached is not None:
        body, ct, exp = cached
        if exp > now:
            # Refresh LRU position on cache hit
            _FAVICON_MEM_CACHE[key] = _FAVICON_MEM_CACHE.pop(key)
            return body, ct
        _FAVICON_MEM_CACHE.pop(key, None)

    if bucket is None:
        return None

    try:
        obj = await bucket.get(key)
        if obj is None:
            return None
        payload = json.loads(await obj.text())
        age = now - float(payload.get("ts", 0))
        if payload.get("fallback"):
            if age > NEGATIVE_CACHE_MAX_AGE:
                return None
            mem_ttl = min(FAVICON_MEM_CACHE_TTL, max(0.0, NEGATIVE_CACHE_MAX_AGE - age))
            _mem_cache_put(key, FALLBACK_ICON, "image/png", mem_ttl)
            return FALLBACK_ICON, "image/png"
        if age > ICON_MAX_AGE:
            return None  # stale: look again
        body = base64.b64decode(payload["b64"])
        content_type = str(payload["ct"])
        _mem_cache_put(key, body, content_type, FAVICON_MEM_CACHE_TTL)
        return body, content_type
    except Exception:
        return None


async def _cache_put(
    bucket: Any,
    key: str,
    body: bytes,
    content_type: str,
    *,
    is_fallback: bool = False,
) -> None:
    mem_ttl = (
        min(FAVICON_MEM_CACHE_TTL, NEGATIVE_CACHE_MAX_AGE)
        if is_fallback
        else FAVICON_MEM_CACHE_TTL
    )
    _mem_cache_put(key, body, content_type, mem_ttl)
    if bucket is None:
        return
    try:
        if is_fallback:
            payload = json.dumps({"fallback": True, "ts": time.time()})
            options: dict[str, Any] = {
                "httpMetadata": {"contentType": "application/json"},
                "customMetadata": {"fallback": "true"},
            }
        else:
            # Base64 in a text blob: R2's binary put differs between runtimes,
            # its text put does not.
            payload = json.dumps(
                {
                    "ct": content_type,
                    "b64": base64.b64encode(body).decode("ascii"),
                    "ts": time.time(),
                }
            )
            options = {"httpMetadata": {"contentType": "application/json"}}
        await bucket.put(key, payload, options)
    except Exception:
        logger.warning("favicon cache write failed for %s", key, exc_info=True)


def _miss_limits(headers: Any, user_id: str | None) -> dict[str, int]:
    """The miss counters of this caller: one per user, one per IP."""
    limits: dict[str, int] = {}
    if user_id:
        limits[f"fav-user:{user_id}"] = FAVICON_MAX_MISSES_PER_USER
    ip = client_ip_key(headers)
    if ip:
        limits[f"fav-ip:{ip}"] = FAVICON_MAX_MISSES_PER_IP
    return limits


async def _consume_miss(db: Any, headers: Any, user_id: str | None) -> bool:
    """Count this cache miss against the per-user and per-IP caps.

    False means the caller is over the limit: serve the fallback, fetch
    nothing and write nothing.
    """
    if db is None:
        return True
    limits = _miss_limits(headers, user_id)
    verdict = await check_limit(db, limits, window_seconds=FAVICON_MISS_WINDOW_SECONDS)
    if not verdict.allowed:
        logger.warning("Favicon miss limit hit: scope=%s", verdict.scope)
        return False
    await record_hits(db, list(limits), window_seconds=FAVICON_MISS_WINDOW_SECONDS)
    return True


async def _resolve_favicon_miss(
    host: str,
    key: str,
    bucket: Any,
    env: Any,
    *,
    db: Any = None,
    headers: Any = None,
    user_id: str | None = None,
) -> tuple[bytes, str]:
    """Handle a favicon cache miss: rate-limit, fetch upstream, and cache."""
    if not await _consume_miss(db, headers, user_id):
        return FALLBACK_ICON, "image/png"
    fetched = await _fetch_upstream(host, env)
    if fetched is None:
        await _cache_put(bucket, key, FALLBACK_ICON, "image/png", is_fallback=True)
        return FALLBACK_ICON, "image/png"
    body, content_type = fetched
    await _cache_put(bucket, key, body, content_type)
    return body, content_type


async def resolve_favicon(
    domain: str,
    env: Any = None,
    *,
    may_fetch: bool = False,
    db: Any = None,
    headers: Any = None,
    user_id: str | None = None,
) -> tuple[bytes, str]:
    """Return (bytes, content_type) for /favicons/<domain>. Never raises.

    An unknown, private or unreachable domain yields the transparent fallback,
    so the browser never sees a 404 for an icon.

    `may_fetch` is the abuse guard: only a signed-in request may read the
    cache or make us fetch the site's icon. An anonymous request gets the
    fallback, always -- the cache is global, so a cached icon would tell the
    visitor whether some user's library holds that domain.

    A cache miss is counted against the per-user and per-IP caps: over the
    limit the fallback is served, with no outbound request and no cache write.
    """
    host = normalize_host(domain)
    if host is None:
        return FALLBACK_ICON, "image/png"
    if not may_fetch:
        return FALLBACK_ICON, "image/png"
    bucket = _bucket(env)
    key = f"favicons/{host}"
    cached = await _cache_get(bucket, key)
    if cached is not None:
        return cached
    inflight = _FAVICON_INFLIGHT.get(key)
    if inflight is not None and not inflight.done():
        return await asyncio.shield(inflight)
    fut = asyncio.get_running_loop().create_future()
    _FAVICON_INFLIGHT[key] = fut
    try:
        result = await _resolve_favicon_miss(
            host, key, bucket, env, db=db, headers=headers, user_id=user_id
        )
        if not fut.done():
            fut.set_result(result)
        return result
    except BaseException:
        if not fut.done():
            fut.cancel()
        raise
    finally:
        if _FAVICON_INFLIGHT.get(key) is fut:
            _FAVICON_INFLIGHT.pop(key, None)
