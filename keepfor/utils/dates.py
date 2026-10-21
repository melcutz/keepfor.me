# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Date helpers shared by the extractors and the templates."""

import re
from datetime import date

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")


def normalize_published_date(value: object) -> str | None:
    """Normalize an ISO-ish date or datetime to YYYY-MM-DD.

    "2024-03-01", "2024-03-01T12:00:00+00:00" and "2024-03-01 12:00:00" all
    become "2024-03-01". Anything else, including an impossible date such as
    "2024-13-45", becomes None, so the caller renders no date at all.
    """
    if not value:
        return None
    match = _ISO_DATE_RE.match(str(value).strip())
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None
