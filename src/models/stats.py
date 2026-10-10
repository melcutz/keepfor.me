# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Keep Score: pure scoring functions (no DB) + aggregate query layer. UTC days."""

import asyncio
import datetime
import statistics
from collections import Counter
from urllib.parse import urlparse

WEIGHTS = {"save": 1, "tag": 1, "note": 1, "pin": 1, "archive": 2, "open": 3}
DAILY_CAP = 10
OPEN_DEDUP_SECONDS = 300
STATS_WINDOW_DAYS = 365
RHYTHM_WEEKS = 12


def score_day(events: dict[str, int]) -> int:
    """events maps action -> count. Caps each action at DAILY_CAP.

    Callers must pass already-deduped open counts
    (5-minute rule applied upstream).
    """
    return sum(
        max(0, min(int(events.get(a, 0)), DAILY_CAP)) * w for a, w in WEIGHTS.items()
    )


def intensity_bucket(score: int, median: float) -> int:
    """0-4 bucket relative to the user's median active day."""
    if score <= 0:
        return 0
    if median <= 0:
        return 1
    ratio = score / median
    if ratio < 0.5:
        return 1
    if ratio <= 1.0:
        return 2
    if ratio < 2.0:
        return 3
    return 4


def current_streak(
    active_days: set[datetime.date], today: datetime.date | None = None
) -> int:
    """Consecutive active days ending today or yesterday."""
    if today is None:
        today = datetime.date.today()
    cursor = today if today in active_days else today - datetime.timedelta(days=1)
    streak = 0
    while cursor in active_days:
        streak += 1
        cursor -= datetime.timedelta(days=1)
    return streak


def _parse_opened_at(value: str | None) -> datetime.datetime | None:
    """Parse an item_opens timestamp; None when unparseable (fail-open)."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(value, fmt).replace(
                tzinfo=datetime.timezone.utc
            )
        except ValueError:
            continue
    return None


def _longest_streak(active_days: set[datetime.date]) -> int:
    """Longest run of consecutive active days."""
    best = run = 0
    prev: datetime.date | None = None
    for day in sorted(active_days):
        contiguous = prev is not None and day == prev + datetime.timedelta(days=1)
        run = run + 1 if contiguous else 1
        best = max(best, run)
        prev = day
    return best


def _week_range_str(year: int, week: int) -> str:
    mon = datetime.date.fromisocalendar(year, week, 1)
    sun = mon + datetime.timedelta(days=6)
    if mon.month == sun.month:
        return f"{mon.strftime('%b')} {mon.day}–{sun.day}"
    return f"{mon.strftime('%b')} {mon.day} – {sun.strftime('%b')} {sun.day}"


def format_reading_metrics(words: int) -> tuple[str, str]:
    """Format words read and estimated reading time (200 wpm standard)."""
    if words <= 0:
        return "0 words", "0 mins"
    if words < 1000:
        words_str = f"{words} words"
    else:
        words_str = f"{words / 1000:.1f}k words"

    minutes = max(1, round(words / 200))
    if minutes < 60:
        time_str = f"{minutes} min{'s' if minutes != 1 else ''}"
    else:
        time_str = f"{minutes / 60:.1f} hrs"
    return words_str, time_str


async def get_user_stats(db, user_id: str) -> dict:
    """Everything the stats page needs; every query scoped to the user."""
    from src.models.items import list_user_tags

    today = datetime.datetime.now(datetime.timezone.utc).date()
    cutoff = today - datetime.timedelta(days=STATS_WINDOW_DAYS - 1)
    cutoff_iso = cutoff.isoformat()

    # All reads are independent (db + user_id only), so they run concurrently:
    # one D1 roundtrip instead of ~10 sequential ones.
    (
        saves,
        tag_rows,
        note_rows,
        pin_rows,
        archive_rows,
        open_rows,
        count_row,
        oldest,
        tag_list,
        url_rows,
        words_row,
    ) = await asyncio.gather(
        db.query_all(
            "SELECT DATE(created_at) AS day, COUNT(*) AS n FROM items"
            " WHERE user_id = ? GROUP BY day;",
            (user_id,),
        ),
        db.query_all(
            "SELECT DATE(it.created_at) AS day, COUNT(*) AS n FROM item_tags it"
            " JOIN tags t ON t.id = it.tag_id"
            " JOIN items i ON i.id = it.item_id AND i.user_id = ?"
            " WHERE t.user_id = ? GROUP BY day;",
            (user_id, user_id),
        ),
        db.query_all(
            "SELECT DATE(updated_at) AS day, COUNT(*) AS n FROM items"
            " WHERE user_id = ? AND user_notes IS NOT NULL GROUP BY day;",
            (user_id,),
        ),
        db.query_all(
            "SELECT DATE(updated_at) AS day, COUNT(*) AS n FROM items"
            " WHERE user_id = ? AND is_pinned = 1 GROUP BY day;",
            (user_id,),
        ),
        # Approximation: archive transitions are not timestamped anywhere,
        # so archived rows are attributed to DATE(updated_at).
        db.query_all(
            "SELECT DATE(updated_at) AS day, COUNT(*) AS n FROM items"
            " WHERE user_id = ? AND read_state = 'archived' GROUP BY day;",
            (user_id,),
        ),
        db.query_all(
            "SELECT item_id, opened_at, DATE(opened_at) AS day FROM item_opens"
            " WHERE user_id = ? ORDER BY item_id, opened_at;",
            (user_id,),
        ),
        db.query_first(
            "SELECT COUNT(*) AS total,"
            " COALESCE(SUM(read_state = 'archived'), 0) AS archived,"
            " COALESCE(SUM(read_state = 'unread'), 0) AS unread"
            " FROM items WHERE user_id = ?;",
            (user_id,),
        ),
        db.query_first(
            "SELECT id, title, canonical_url, url, created_at FROM items"
            " WHERE user_id = ? AND read_state = 'unread'"
            " ORDER BY created_at ASC LIMIT 1;",
            (user_id,),
        ),
        list_user_tags(db, user_id),
        db.query_all("SELECT canonical_url FROM items WHERE user_id = ?;", (user_id,)),
        db.query_first(
            "SELECT COALESCE(SUM(word_count), 0) AS total_words FROM items"
            " WHERE user_id = ? AND (read_state = 'archived' OR id IN ("
            "   SELECT DISTINCT item_id FROM item_opens WHERE user_id = ?"
            " ));",
            (user_id, user_id),
        ),
    )

    # Collapse opens within 5 minutes of the previous kept open of the same
    # item (mirrors score_day's already-deduped contract).
    open_days: Counter[str] = Counter()
    total_reads = 0
    last_kept: dict[str, datetime.datetime | None] = {}
    for row in open_rows:
        # Skip NULL-day rows (unparseable/hand-inserted opened_at): a None
        # day key must never reach the date comparisons below.
        if row["day"] is None:
            continue
        item_id = row["item_id"]
        ts = _parse_opened_at(row["opened_at"])
        prev = last_kept.get(item_id)
        if (
            ts is not None
            and prev is not None
            and (ts - prev).total_seconds() < OPEN_DEDUP_SECONDS
        ):
            continue
        total_reads += 1
        open_days[row["day"]] += 1
        if ts is not None:
            last_kept[item_id] = ts

    day_events: dict[str, dict[str, int]] = {}
    for rows, action in (
        (saves, "save"),
        (tag_rows, "tag"),
        (note_rows, "note"),
        (pin_rows, "pin"),
        (archive_rows, "archive"),
    ):
        for row in rows:
            if row["day"] is None:
                continue
            day_events.setdefault(row["day"], {}).setdefault(action, 0)
            day_events[row["day"]][action] += row["n"]
    for day, n in open_days.items():
        day_events.setdefault(day, {}).setdefault("open", 0)
        day_events[day]["open"] += n

    # Sparse day scores: only nonzero days inside the trailing 365-day window.
    day_scores = {
        day: score_day(events)
        for day, events in day_events.items()
        if day >= cutoff_iso and score_day(events) > 0
    }
    # Per-day per-action counts behind each score (grid hover breakdown).
    # Same window/sparsity as day_scores; every action key always present.
    day_breakdown = {
        day: {action: events.get(action, 0) for action in WEIGHTS}
        for day, events in day_events.items()
        if day >= cutoff_iso and score_day(events) > 0
    }
    active_days = {
        datetime.date.fromisoformat(day) for day in day_scores if day_scores[day] > 0
    }
    nonzero = sorted(day_scores.values())
    median_active_day = float(statistics.median(nonzero)) if nonzero else 0.0
    total_score = sum(nonzero)
    best_day = (
        max(day_scores.items(), key=lambda kv: (kv[1], kv[0]))[0]
        if day_scores
        else None
    )

    # Weekly rhythm: trailing 12 ISO weeks, chronological.
    monday = today - datetime.timedelta(days=today.weekday())
    week_keys = [
        (monday - datetime.timedelta(weeks=RHYTHM_WEEKS - 1 - i)).isocalendar()[:2]
        for i in range(RHYTHM_WEEKS)
    ]
    rhythm: dict[tuple[int, int], dict[str, int]] = {
        key: {"save": 0, "open": 0, "archive": 0} for key in week_keys
    }
    for day, events in day_events.items():
        key = datetime.date.fromisoformat(day).isocalendar()[:2]
        if key in rhythm:
            for action in ("save", "open", "archive"):
                rhythm[key][action] += events.get(action, 0)
    week_rhythm = []
    for year, week in week_keys:
        mon = datetime.date.fromisocalendar(year, week, 1)
        week_rhythm.append(
            {
                "week": f"{year}-W{week:02d}",
                "date_range": _week_range_str(year, week),
                "date_label": f"{mon.strftime('%b')} {mon.day}",
                "save": rhythm[(year, week)]["save"],
                "open": rhythm[(year, week)]["open"],
                "archive": rhythm[(year, week)]["archive"],
            }
        )

    total_saves = count_row["total"] if count_row else 0
    total_archived = count_row["archived"] if count_row else 0
    unread_count = count_row["unread"] if count_row else 0

    oldest_unread = None
    if oldest:
        raw_title = (oldest.get("title") or "").strip()
        if not raw_title:
            target_url = oldest.get("canonical_url") or oldest.get("url") or ""
            try:
                raw_title = urlparse(target_url).netloc
            except Exception:
                raw_title = ""
        display_title = raw_title if raw_title else "Untitled article"
        oldest_unread = {
            "id": oldest["id"],
            "title": display_title,
            "days": (
                today - datetime.date.fromisoformat(oldest["created_at"][:10])
            ).days,
        }

    top_tags = [(r["name"], r["count"]) for r in tag_list if r["count"] > 0][:10]

    hosts: Counter[str] = Counter()
    for row in url_rows:
        try:
            host = urlparse(row["canonical_url"] or "").netloc.lower()
        except Exception:
            continue
        if host:
            hosts[host] += 1
    top_domains = [
        (host, count)
        for host, count in sorted(hosts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    ]

    words_read = words_row["total_words"] if words_row else 0
    words_display, time_display = format_reading_metrics(words_read)

    return {
        "total_saves": total_saves,
        "total_reads": total_reads,
        "total_archived": total_archived,
        "words_read": words_read,
        "words_read_display": words_display,
        "reading_time_display": time_display,
        "streak": current_streak(active_days, today),
        "longest_streak": _longest_streak(active_days),
        "total_score": total_score,
        "best_day": best_day,
        "day_scores": day_scores,
        "day_breakdown": day_breakdown,
        "week_rhythm": week_rhythm,
        "unread_count": unread_count,
        "oldest_unread": oldest_unread,
        "archive_rate": (total_archived / total_saves) if total_saves else 0.0,
        "top_tags": top_tags,
        "top_domains": top_domains,
        "median_active_day": median_active_day,
    }
