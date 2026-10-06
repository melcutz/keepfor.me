"""Keep Score: pure scoring functions (no DB). UTC calendar days."""

import datetime

WEIGHTS = {"save": 1, "tag": 1, "note": 1, "pin": 1, "archive": 2, "open": 3}
DAILY_CAP = 10


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
