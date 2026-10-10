# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import base64
import hashlib
from typing import Any
from urllib.parse import urlparse

from fastapi import Response

from keepfor.models.items import list_user_tags
from keepfor.templating import jinja_env

PER_PAGE_OPTIONS = [10, 20, 30, 50]


def _pager_context(page: int, per_page: int, total: int) -> dict[str, int | list[int]]:
    """Clamp page/per_page and build pager template context."""
    per_page = per_page if per_page in PER_PAGE_OPTIONS else 20
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(page, 1), total_pages)
    start = max(1, min(page - 2, max(1, total_pages - 4)))
    pages = list(range(start, min(total_pages, start + 4) + 1))
    return {
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "pages": pages,
        "shown_from": (page - 1) * per_page + 1 if total else 0,
        "shown_to": min(page * per_page, total),
    }


def _is_safe_path(value: str | None) -> bool:
    r"""Check if a path is safe for redirection (same-origin relative path only)."""
    if not value or not isinstance(value, str):
        return False
    # Backslashes: never legitimate in a path we generate, and browsers treat
    # them as slashes, which can create a protocol-relative (cross-origin) URL.
    if "\\" in value:
        return False
    # Control characters and whitespace are stripped by browsers before the URL
    # is parsed, which can also smuggle a `//` past the checks below.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch == " " for ch in value):
        return False
    if not value.startswith("/") or value.startswith("//"):
        return False
    # Structural check: must have no scheme and no authority component.
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return False
    return True


def _safe_next(value: str | None, default: str = "/") -> str:
    r"""Return a safe post-login redirect: only same-origin paths, else default.

    Getting this wrong is a real open redirect: browsers normalise '\' to '/'
    inside a `Location` header, so a naive `startswith('/')` check lets
    `?next=/\evil.com` through and it lands on `https://evil.com/` -- after
    the victim has just typed their password on the genuine site.

    Verified in Chromium: `Location: /\evil.example` produced a request to
    `http://evil.example/`. A literal tab before `//` is a second bypass.
    Hence the explicit backslash and control-character rejections below, plus a
    structural parse so the rule does not depend on prefix guessing alone.
    """
    return value if _is_safe_path(value) else default


TAG_PILL_CLASSES = [
    "bg-blue-50 text-blue-700",
    "bg-emerald-50 text-emerald-700",
    "bg-amber-50 text-amber-700",
    "bg-rose-50 text-rose-700",
    "bg-violet-50 text-violet-700",
    "bg-cyan-50 text-cyan-700",
    "bg-orange-50 text-orange-700",
    "bg-slate-100 text-slate-600",
]

TAG_DOT_CLASSES = [
    "bg-blue-600",
    "bg-emerald-600",
    "bg-amber-500",
    "bg-rose-500",
    "bg-violet-500",
    "bg-cyan-500",
    "bg-orange-500",
    "bg-slate-400",
]


def tag_palette_index(tag: str) -> int:
    """Deterministic 0-7 palette slot for a tag name (md5, stable across processes)."""
    return hashlib.md5(tag.encode("utf-8")).digest()[0] % 8


def tag_styles_for(tags: list[str]) -> dict[str, tuple[str, str]]:
    """Map each tag name to (pill classes, dot class)."""
    styles = {}
    for t in dict.fromkeys(tags):
        i = tag_palette_index(t)
        styles[t] = (TAG_PILL_CLASSES[i], TAG_DOT_CLASSES[i])
    return styles


def build_ai_markdown(item: dict[str, Any]) -> str:
    """Prompt-ready Markdown payload for the Copy-for-AI action."""
    title = item.get("title") or item.get("url") or "Untitled"
    url = item.get("url") or ""
    source = url if not str(url).startswith("urn:note:") else "Personal Knowledge Note"
    created = str(item.get("created_at") or "")[:10]
    tags = item.get("tags") or []
    tag_str = " ".join(f"#{t}" for t in tags)
    notes = (item.get("user_notes") or "").strip()
    body = (item.get("content_text") or item.get("excerpt") or "").strip()
    lines = [f"# {title}", f"Source: {source}"]
    if created:
        lines.append(f"Date: {created}")
    if tag_str:
        lines.append(f"Tags: {tag_str}")
    if notes:
        lines.append(f"Notes: {notes}")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


def _tags_list_html(db_tags: list[dict[str, Any]]) -> str:
    tag_styles = tag_styles_for([t["name"] for t in db_tags])
    template = jinja_env.get_template("partials/tag_list.html")
    return template.render(tags=db_tags, tag_styles=tag_styles)


async def _render_tags_list(db: Any, user_id: str) -> str:
    tags = await list_user_tags(db, user_id)
    return _tags_list_html(tags)


def _pwa_icon_response(b64: str) -> Response:
    return Response(
        content=base64.b64decode(b64),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )
