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

import base64
import json
import re
import time
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
# The fallback is never stored: this is only how long the browser keeps it
# before asking again (the next ask may come from a signed-in visitor).
FALLBACK_MAX_AGE = 3600

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
        if result.status not in REFUSED_STATUSES:
            return None
        # The site refused us: its icon is public, so a public icon service
        # can still fetch it -- from a network the site does not block.
        base = _icon_service(env)
        if not base:
            return None
        logger.info(
            "favicon %s refused us (%s): asking the icon service",
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


async def _cache_get(bucket: Any, key: str) -> tuple[bytes, str] | None:
    if bucket is None:
        return None
    try:
        obj = await bucket.get(key)
        if obj is None:
            return None
        payload = json.loads(await obj.text())
        if payload.get("fallback"):
            # A fallback entry from an older release: never served, refetched.
            return None
        age = time.time() - float(payload.get("ts", 0))
        if age > ICON_MAX_AGE:
            return None  # stale: look again
        return base64.b64decode(payload["b64"]), str(payload["ct"])
    except Exception:
        return None


async def _cache_put(bucket: Any, key: str, body: bytes, content_type: str) -> None:
    if bucket is None:
        return
    try:
        # Base64 in a text blob: R2's binary put differs between runtimes,
        # its text put does not.
        payload = json.dumps(
            {
                "ct": content_type,
                "b64": base64.b64encode(body).decode("ascii"),
                "ts": time.time(),
            }
        )
        await bucket.put(
            key, payload, {"httpMetadata": {"contentType": "application/json"}}
        )
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
    if not await _consume_miss(db, headers, user_id):
        return FALLBACK_ICON, "image/png"
    fetched = await _fetch_upstream(host, env)
    if fetched is None:
        # No icon upstream: the fallback is never cached, so the site gets
        # another chance on the next request.
        return FALLBACK_ICON, "image/png"
    body, content_type = fetched
    await _cache_put(bucket, key, body, content_type)
    return body, content_type
