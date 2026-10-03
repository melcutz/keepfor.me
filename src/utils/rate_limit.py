"""Rate limiting for the authentication endpoints.

Why this exists: `/auth/login` had no attempt counter, lockout, or delay, so a
publicly reachable deployment gave an attacker unlimited password guesses.

Storage is D1 (the SQLite binding this app already uses) rather than an
in-memory counter. Isolates are short-lived and numerous, so per-isolate
counters would be trivially bypassed by spreading requests across them. D1 gives
a shared, durable counter for every request.

Design notes:

- Only *failed* attempts are counted. A legitimate user who fat-fingers a
  password and then succeeds is not penalised, and an attacker still cannot make
  progress without failing.
- Two keys are tracked, because either alone is bypassable:
  - per-IP  -> stops one host hammering every account
  - per-account -> stops many IPs spraying a single account
- Fixed time windows (not a sliding log). Cheap: one indexed row per key per
  window. The known trade-off is that a burst can straddle a window boundary;
  the limits are set conservatively to absorb that.
- On a limiter error we **fail open** and log. Login already requires D1, so a
  limiter that hard-fails on, say, a missing migration would lock the owner out
  of their own instance over a defect in this file.
"""

import time
from dataclasses import dataclass
from typing import Any

from src.models.db import Database
from src.utils.logging import logger

# Window length and caps. Tuned for a single-user instance on a phone: enough
# room for real typos and re-tries, far too few for guessing.
WINDOW_SECONDS = 900
MAX_FAILURES_PER_IP = 20
MAX_FAILURES_PER_ACCOUNT = 10

_UNKNOWN_IP = "unknown"


def client_ip(headers: Any) -> str:
    """Best-effort client IP from Cloudflare-provided headers.

    `CF-Connecting-IP` is set by the edge and cannot be spoofed by the client.
    We deliberately do NOT trust `X-Forwarded-For` (client-controllable) as a
    limiter key -- it would let an attacker mint a fresh limit per request.
    """
    value = headers.get("cf-connecting-ip") or ""
    value = (value or "").strip()
    return value or _UNKNOWN_IP


def _window_start(now: float) -> int:
    return int(now - (now % WINDOW_SECONDS))


def _keys(ip: str, account: str) -> list[str]:
    acct = (account or "").strip().lower()
    keys = [f"ip:{ip}"]
    if acct:
        keys.append(f"acct:{acct}")
    return keys


@dataclass
class RateLimitVerdict:
    allowed: bool
    retry_after: int
    scope: str = ""


# Per-key caps, keyed by the prefix used in _keys().
_KEY_LIMITS = {"ip": MAX_FAILURES_PER_IP, "acct": MAX_FAILURES_PER_ACCOUNT}


def _split(key: str) -> tuple[str, str]:
    prefix, _, value = key.partition(":")
    return prefix, value


async def _current_failures(db: Database, key: str, window: int) -> int:
    row = await db.query_first(
        "SELECT hits FROM rate_limits WHERE key = ? AND window_start = ?;",
        (key, window),
    )
    if not row:
        return 0
    try:
        return int(row["hits"])
    except (TypeError, ValueError):
        return 0


async def check_allowed(db: Database, headers: Any, account: str) -> RateLimitVerdict:
    """Return whether an auth attempt may proceed, without mutating state.

    Called *before* the expensive password hashing so a blocked client cannot
    burn CPU on PBKDF2.
    """
    now = time.time()
    window = _window_start(now)
    try:
        for key in _keys(client_ip(headers), account):
            scope, value = _split(key)
            limit = _KEY_LIMITS.get(scope)
            if limit is None or not value:
                continue
            hits = await _current_failures(db, key, window)
            if hits >= limit:
                logger.warning(
                    f"Auth rate limit hit: scope={scope} failures={hits}/{limit}"
                )
                return RateLimitVerdict(
                    allowed=False,
                    retry_after=max(1, int(window + WINDOW_SECONDS - now)),
                    scope=scope,
                )
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"Rate limit check failed, failing open: {exc}")
        return RateLimitVerdict(allowed=True, retry_after=0)

    return RateLimitVerdict(allowed=True, retry_after=0)


async def record_failure(db: Database, headers: Any, account: str) -> None:
    """Increment the IP and account failure counters for the current window."""
    now = time.time()
    window = _window_start(now)
    try:
        for key in _keys(client_ip(headers), account):
            await db.execute(
                "INSERT INTO rate_limits (key, window_start, hits) VALUES (?, ?, 1) "
                "ON CONFLICT(key, window_start) DO UPDATE SET hits = hits + 1;",
                (key, window),
            )
        # Keep the table bounded: drop windows older than the current one.
        await db.execute("DELETE FROM rate_limits WHERE window_start < ?;", (window,))
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"Rate limit record failed: {exc}")


async def clear_account(db: Database, account: str) -> None:
    """Reset the per-account counter after a successful sign-in."""
    acct = (account or "").strip().lower()
    if not acct:
        return
    try:
        await db.execute("DELETE FROM rate_limits WHERE key = ?;", (f"acct:{acct}",))
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"Rate limit reset failed: {exc}")


def rate_limited_html(retry_after: int) -> str:
    """User-facing page for a blocked sign-in attempt."""
    minutes = max(1, round(retry_after / 60))
    return (
        '<div style="font-family:sans-serif;max-width:26rem;margin:4rem auto;'
        'text-align:center;">'
        '<h1 style="font-size:1.25rem;margin-bottom:0.5rem;">'
        "Too many attempts</h1>"
        '<p style="color:#475569;font-size:0.875rem;">'
        "For your security, sign-in is temporarily paused after several failed "
        f"attempts. Try again in about {minutes} minute"
        f"{'s' if minutes != 1 else ''}.</p>"
        "</div>"
    )
