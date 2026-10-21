# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Same-origin favicon proxy.

The library cards used to point their <img> straight at a third-party favicon
service. Those answer 404 for every domain without an icon, so each library
page filled the browser console with failed requests. The page now asks us
instead: /favicons/<domain> always answers 200 -- with the site's icon when it
has one, otherwise with a transparent fallback that leaves the card's letter
tile visible.
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
FALLBACK_MAX_AGE = 3600  # no icon yet: look again sooner
ICON_MAX_AGE = 30 * 86400  # a cached icon is refreshed after a month

# Same browser agent the article fetcher uses: plenty of sites answer 403 to
# an unknown agent.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


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


async def _fetch_upstream(host: str) -> tuple[bytes, str] | None:
    """Fetch https://<host>/favicon.ico through the hardened egress fetcher."""
    policy = EgressPolicy(
        max_bytes=FAVICON_MAX_BYTES,
        timeout_s=FAVICON_TIMEOUT_S,
        max_redirects=2,
    )
    try:
        result = await fetch_limited(
            f"https://{host}/favicon.ico",
            {
                "Accept": "image/*,*/*;q=0.8",
                "User-Agent": USER_AGENT,
            },
            policy,
        )
    except Exception:
        logger.warning("favicon fetch failed for %s", host, exc_info=True)
        return None
    if result.status != 200 or not result.body:
        return None
    content_type = _image_type(result.body)
    if content_type is None:
        return None
    return result.body, content_type


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
        is_fallback = bool(payload.get("fallback"))
        age = time.time() - float(payload.get("ts", 0))
        if age > (FALLBACK_MAX_AGE if is_fallback else ICON_MAX_AGE):
            return None  # stale: look again
        if is_fallback:
            return FALLBACK_ICON, "image/png"
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
                "fallback": body == FALLBACK_ICON,
                "ts": time.time(),
            }
        )
        await bucket.put(
            key, payload, {"httpMetadata": {"contentType": "application/json"}}
        )
    except Exception:
        logger.warning("favicon cache write failed for %s", key, exc_info=True)


async def resolve_favicon(domain: str, env: Any = None) -> tuple[bytes, str]:
    """Return (bytes, content_type) for /favicons/<domain>. Never raises.

    An unknown, private or unreachable domain yields the transparent fallback,
    so the browser never sees a 404 for an icon.
    """
    host = normalize_host(domain)
    if host is None:
        return FALLBACK_ICON, "image/png"
    bucket = _bucket(env)
    key = f"favicons/{host}"
    cached = await _cache_get(bucket, key)
    if cached is not None:
        return cached
    fetched = await _fetch_upstream(host)
    if fetched is None:
        body, content_type = FALLBACK_ICON, "image/png"
    else:
        body, content_type = fetched
    await _cache_put(bucket, key, body, content_type)
    return body, content_type
