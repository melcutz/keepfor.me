# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import asyncio
import datetime
import sys
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlparse

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Form,
    HTTPException,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from keepfor import deps
from keepfor.models.items import (
    accept_suggestion,
    add_tags_to_item,
    archive_item,
    bulk_update_tags,
    create_rule,
    create_tag,
    delete_rule,
    delete_tag,
    dismiss_suggestion,
    get_item,
    get_item_clean_html,
    get_pinned_items,
    list_pending_suggestions,
    list_rules,
    list_user_tags,
    merge_tags,
    prune_unused_tags,
    record_open,
    remove_tags_from_item,
    rename_tag,
    save_item,
    save_note,
    suggest_rules,
    suggest_tags,
    toggle_pin_item,
    unarchive_item,
    update_user_notes,
)
from keepfor.models.stats import get_user_stats, intensity_bucket
from keepfor.routes._shared import (
    PER_PAGE_OPTIONS,
    _is_safe_path,
    _pager_context,
    _pwa_icon_response,
    _render_tags_list,
    _safe_next,
    build_ai_markdown,
    tag_styles_for,
)
from keepfor.search.engine import (
    get_status_counts,
    hybrid_search,
    parse_tag_filter,
)
from keepfor.templating import jinja_env
from keepfor.utils.logging import logger
from keepfor.utils.url import extract_url

if TYPE_CHECKING:
    from keepfor.models.db import Database

router = APIRouter()

# Brand mark served as a file so browsers persist it onto bookmarks
# (inline data: favicons are not). Dynamic route: zero bundle impact.
FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 44 44'>"
    "<defs><linearGradient id='kfm' x1='0' y1='0' x2='1' y2='1'>"
    "<stop offset='0' stop-color='#2563eb'/>"
    "<stop offset='1' stop-color='#0ea5e9'/></linearGradient></defs>"
    "<rect x='2' y='2' width='40' height='40' rx='11' fill='url(#kfm)'/>"
    "<path d='M17 11h10v16l-5-3.8-5 3.8z' fill='#fff'/></svg>"
).encode("utf-8")


@router.get("/favicon.ico")
async def favicon():
    return Response(
        content=FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/manifest.webmanifest")
async def pwa_manifest():
    return JSONResponse(
        content={
            "name": "Keepfor.me",
            "short_name": "Keepfor.me",
            "description": "Read-it-later personal library.",
            "id": "/",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#f8fafc",
            "theme_color": "#0f172a",
            "icons": [
                {
                    "src": "/icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                },
                {
                    "src": "/icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                },
                {
                    "src": "/icon-maskable.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
            "share_target": {
                "action": "/share",
                "method": "GET",
                "enctype": "application/x-www-form-urlencoded",
                "params": {"title": "title", "text": "text", "url": "url"},
            },
        },
        media_type="application/manifest+json",
    )


@router.get("/icon-192.png")
async def pwa_icon_192():
    from keepfor import pwa_icons

    return _pwa_icon_response(pwa_icons.ICON_192_B64)


@router.get("/icon-512.png")
async def pwa_icon_512():
    from keepfor import pwa_icons

    return _pwa_icon_response(pwa_icons.ICON_512_B64)


@router.get("/icon-maskable.png")
async def pwa_icon_maskable():
    from keepfor import pwa_icons

    return _pwa_icon_response(pwa_icons.ICON_MASKABLE_B64)


# Lightweight service worker: satisfies PWA installability without caching
# anything. Network-passthrough keeps auth redirects and library content
# fresh; served from Python so there is zero static-asset bundle impact.
SW_JS = (
    "const CACHE = 'kfm-v1';\n"
    "self.addEventListener('install', (e) => { self.skipWaiting(); });\n"
    "self.addEventListener('activate', (e) => { self.clients.claim(); });\n"
    "self.addEventListener('fetch', (e) => {});\n"
).encode("utf-8")


@router.get("/sw.js")
async def service_worker():
    return Response(
        content=SW_JS,
        media_type="application/javascript",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Service-Worker-Allowed": "/",
        },
    )


# ==========================================
# Web UI Pages
# ==========================================


@router.get("/", response_class=HTMLResponse)
async def library_page(
    request: Request,
    tag: str | None = None,
    q: str | None = None,
    status: str | None = None,
    page: int = 1,
    per_page: int = 20,
    category: str | None = None,
    quick: bool = False,
):
    user = await deps.get_current_user(request)
    db = deps.get_db(request)
    if not user:
        # Check if any users exist to direct to register vs login
        count_row = await db.query_first("SELECT COUNT(*) as count FROM users;")
        if not count_row or count_row["count"] == 0:
            return RedirectResponse(url="/auth/register", status_code=303)
        return RedirectResponse(url="/auth/login", status_code=303)

    env = deps.get_env_from_request(request)
    scope = deps.get_scope(request)
    clean_status = status.strip() if status and status.strip() else None
    clean_category = category.strip() if category and category.strip() else None
    limit = per_page if per_page in PER_PAGE_OPTIONS else 20
    tag_list, untagged_only = parse_tag_filter(tag)
    items, total = await hybrid_search(
        db,
        env,
        user["id"],
        query=q or "",
        tags=tag_list,
        untagged=untagged_only,
        limit=limit,
        offset=(max(page, 1) - 1) * limit,
        status=clean_status,
        category=clean_category,
        quick=bool(quick),
        scope=scope,
    )
    pager = _pager_context(page, per_page, total)
    if pager["page"] != page:
        items, total = await hybrid_search(
            db,
            env,
            user["id"],
            query=q or "",
            tags=tag_list,
            untagged=untagged_only,
            limit=pager["per_page"],
            offset=(pager["page"] - 1) * pager["per_page"],
            status=clean_status,
            category=clean_category,
            quick=bool(quick),
            scope=scope,
        )
        pager = _pager_context(pager["page"], pager["per_page"], total)
    tags, status_counts, pinned_items = await asyncio.gather(
        list_user_tags(db, user["id"]),
        get_status_counts(
            db,
            user["id"],
            category=clean_category,
            tags=tag_list,
            untagged=untagged_only,
            quick=bool(quick),
        ),
        get_pinned_items(db, user["id"]),
    )
    tag_styles = tag_styles_for([t["name"] for t in tags])
    total_count = status_counts.get("all", 0)

    pager_qs = urlencode(
        {
            k: v
            for k, v in {
                "q": q or "",
                "tag": tag or "",
                "status": clean_status or "",
                "category": clean_category or "",
            }.items()
            if v
        }
    )
    push_base = ("/?" + pager_qs + "&") if pager_qs else "/?"
    template = jinja_env.get_template("library.html")
    html = template.render(
        current_user=user,
        items=items,
        tags=tags,
        active_tag=tag,
        active_tags=tag_list,
        active_untagged=untagged_only,
        query=q or "",
        tag_styles=tag_styles,
        total_count=total_count,
        active_status=clean_status,
        status_counts=status_counts,
        active_nav="library",
        total=total,
        per_page_options=PER_PAGE_OPTIONS,
        push_base=push_base,
        pinned_items=pinned_items,
        active_category=clean_category or "all",
        active_quick=bool(quick),
        **pager,
    )
    return HTMLResponse(content=html)


@router.post("/search", response_class=HTMLResponse)
async def search_htmx(
    request: Request,
    query: str = Form(""),
    mode: str = Form("hybrid"),
    tag: str = Form(""),
    status: str = Form(""),
    page: int = Form(1),
    per_page: int = Form(20),
    category: str = Form(""),
    quick: str = Form(""),
):
    user = await deps.get_current_user(request)
    if not user:
        return HTMLResponse("<p>Please log in</p>", status_code=401)

    db = deps.get_db(request)
    env = deps.get_env_from_request(request)
    scope = deps.get_scope(request)
    clean_tag = tag.strip() if tag.strip() else None
    clean_status = status.strip() if status.strip() else None
    clean_category = category.strip() if category.strip() else None
    quick_flag = str(quick).strip().lower() in ("1", "true", "on", "yes")
    # The mode selector was removed from the UI: always run hybrid. The
    # param stays accepted so old clients and /api/search keep working.
    tag_list, untagged_only = parse_tag_filter(clean_tag)
    items, total = await hybrid_search(
        db,
        env,
        user["id"],
        query=query,
        mode="hybrid",
        tags=tag_list,
        untagged=untagged_only,
        limit=per_page if per_page in PER_PAGE_OPTIONS else 20,
        offset=(max(page, 1) - 1) * (per_page if per_page in PER_PAGE_OPTIONS else 20),
        status=clean_status,
        category=clean_category,
        quick=quick_flag,
        scope=scope,
    )
    pager = _pager_context(page, per_page, total)
    if pager["page"] != page:
        items, total = await hybrid_search(
            db,
            env,
            user["id"],
            query=query,
            mode="hybrid",
            tags=tag_list,
            untagged=untagged_only,
            limit=pager["per_page"],
            offset=(pager["page"] - 1) * pager["per_page"],
            status=clean_status,
            category=clean_category,
            quick=quick_flag,
            scope=scope,
        )
        pager = _pager_context(pager["page"], pager["per_page"], total)
    tag_styles = tag_styles_for([t for it in items for t in (it.get("tags") or [])])

    pager_qs = urlencode(
        {
            k: v
            for k, v in {
                "q": query or "",
                "tag": clean_tag or "",
                "status": clean_status or "",
                "category": clean_category or "",
            }.items()
            if v
        }
    )
    push_base = ("/?" + pager_qs + "&") if pager_qs else "/?"
    pager_html = jinja_env.get_template("partials/pager.html").render(
        total=total, per_page_options=PER_PAGE_OPTIONS, push_base=push_base, **pager
    )
    # Status pills re-count within the active category/tags scope (not the
    # text query: that would multiply FTS/vector work per group). OOB swap
    # keeps them live across htmx interactions without a full reload.
    scoped_counts = await get_status_counts(
        db,
        user["id"],
        category=clean_category,
        tags=tag_list,
        untagged=untagged_only,
        quick=quick_flag,
    )
    pills_html = (
        '<div id="status-filters" hx-swap-oob="true">'
        + jinja_env.get_template("partials/status_pills.html").render(
            status_counts=scoped_counts, active_status=clean_status
        )
        + "</div>"
        + f'<span id="library-total" hx-swap-oob="true">{scoped_counts["all"]}</span>'
    )
    template = jinja_env.get_template("partials/item_card.html")
    if not items:
        msg = (
            "No saves match these tags."
            if (tag_list or untagged_only)
            else "No matching articles found."
        )
        clear = (
            ' <a href="/" class="underline">Clear filters</a>'
            if (tag_list or untagged_only)
            else ""
        )
        return HTMLResponse(
            '<div class="col-span-all text-center py-12 text-slate-400 text-xs">'
            + msg
            + clear
            + "</div>"
            + pager_html
            + pills_html
        )

    cards = [template.render(item=it, tag_styles=tag_styles) for it in items]
    return HTMLResponse(content="".join(cards) + pager_html + pills_html)


def _get_record_open():
    app_mod = sys.modules.get("keepfor.app")
    if app_mod and hasattr(app_mod, "record_open"):
        return getattr(app_mod, "record_open")
    return record_open


async def _log_open_safely(db: Database, user_id: str, item_id: str) -> None:
    """Record a reader visit; a logging failure must never break the reader."""
    try:
        fn = _get_record_open()
        await fn(db, user_id, item_id)
    except Exception:
        logger.warning("item_opens insert failed", exc_info=True)


@router.get("/items/{item_id}", response_class=HTMLResponse)
async def reader_page(
    request: Request, item_id: str, background_tasks: BackgroundTasks
):
    user = await deps.get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)

    db = deps.get_db(request)
    env = deps.get_env_from_request(request)
    item = await get_item(db, user["id"], item_id)
    if not item:
        return HTMLResponse(
            content="<div style='font-family:sans-serif;max-width:40rem;"
            "margin:4rem auto;text-align:center;'>"
            "<h2>Article not found</h2>"
            "<p>It may have been deleted.</p>"
            "<p><a href='/'>Back to Library</a></p></div>",
            status_code=404,
        )

    background_tasks.add_task(_log_open_safely, db, user["id"], item_id)

    clean_html = await get_item_clean_html(
        db, env, user["id"], item_id, scope=deps.get_scope(request)
    )
    tag_styles = tag_styles_for(item.get("tags") or [])
    suggestions = await list_pending_suggestions(db, user["id"], item_id=item_id)
    template = jinja_env.get_template("reader.html")
    html = template.render(
        current_user=user,
        item=item,
        clean_html=clean_html,
        tag_styles=tag_styles,
        suggestions=suggestions,
        hide_mobile_nav=True,
    )
    return HTMLResponse(content=html)


@router.get("/items/{item_id}/card", response_class=HTMLResponse)
async def item_card(request: Request, item_id: str):
    """Single card fragment for htmx polling of extracting items."""
    user = await deps.get_current_user(request)
    if not user:
        return HTMLResponse("<p>Please log in</p>", status_code=401)
    db = deps.get_db(request)
    item = await get_item(db, user["id"], item_id)
    if not item:
        return HTMLResponse(content="", status_code=404)
    tag_styles = tag_styles_for(item.get("tags") or [])
    template = jinja_env.get_template("partials/item_card.html")
    return HTMLResponse(content=template.render(item=item, tag_styles=tag_styles))


@router.post("/notes")
async def create_note_form(
    request: Request,
    title: str = Form(""),
    content: str = Form(...),
    tags: str = Form(""),
):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    env = deps.get_env_from_request(request)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    note = await save_note(
        db, env, user["id"], title, content, tag_list, scope=deps.get_scope(request)
    )
    return RedirectResponse(url=f"/items/{note['id']}", status_code=303)


@router.post("/items/{item_id}/pin", response_class=HTMLResponse)
async def toggle_pin_route(request: Request, item_id: str):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    new_state = await toggle_pin_item(db, user["id"], item_id)
    if new_state is None:
        raise HTTPException(status_code=404, detail="Item not found")
    if request.headers.get("hx-request"):
        item = await get_item(db, user["id"], item_id)
        tag_styles = tag_styles_for(item.get("tags") or [])
        template = jinja_env.get_template("partials/item_card.html")
        return HTMLResponse(content=template.render(item=item, tag_styles=tag_styles))
    referer = request.headers.get("referer")
    fallback = f"/items/{item_id}"
    dest = fallback
    if referer:
        parsed_ref = urlparse(referer)
        req_host = (request.url.netloc or "").lower()
        ref_host = (parsed_ref.netloc or "").lower()
        if not ref_host or ref_host == req_host:
            candidate = parsed_ref.path + (
                "?" + parsed_ref.query if parsed_ref.query else ""
            )
            if _is_safe_path(candidate):
                dest = candidate
    return RedirectResponse(url=dest, status_code=303)


@router.post("/items/{item_id}/notes", response_class=HTMLResponse)
async def update_notes_route(
    request: Request, item_id: str, user_notes: str = Form(""), next: str = Form("")
):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    ok = await update_user_notes(db, user["id"], item_id, user_notes)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    if request.headers.get("hx-request"):
        item = await get_item(db, user["id"], item_id)
        tag_styles = tag_styles_for(item.get("tags") or [])
        template = jinja_env.get_template("partials/item_card.html")
        return HTMLResponse(content=template.render(item=item, tag_styles=tag_styles))
    fallback = f"/items/{item_id}"
    dest = next if _is_safe_path(next) else fallback
    return RedirectResponse(url=dest, status_code=303)


@router.post("/items/{item_id}/archive", response_class=HTMLResponse)
async def archive_route(request: Request, item_id: str):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    ok = await archive_item(db, user["id"], item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    if request.headers.get("hx-request"):
        return HTMLResponse(content="")
    return RedirectResponse(url="/", status_code=303)


@router.post("/items/{item_id}/unarchive", response_class=HTMLResponse)
async def unarchive_route(request: Request, item_id: str):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    ok = await unarchive_item(db, user["id"], item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    if request.headers.get("hx-request"):
        item = await get_item(db, user["id"], item_id)
        tag_styles = tag_styles_for(item.get("tags") or [])
        template = jinja_env.get_template("partials/item_card.html")
        return HTMLResponse(content=template.render(item=item, tag_styles=tag_styles))
    return RedirectResponse(url="/", status_code=303)


@router.get("/items/{item_id}/markdown")
async def item_markdown(request: Request, item_id: str):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    item = await get_item(db, user["id"], item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return Response(content=build_ai_markdown(item), media_type="text/markdown")


@router.post("/save")
async def save_form(request: Request, url: str = Form(...), tags: str = Form("")):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    env = deps.get_env_from_request(request)

    clean_url = extract_url(url)
    if not clean_url:
        logger.warning(f"Save validation failed: invalid URL '{url}'")
        raise HTTPException(status_code=400, detail="Invalid URL")

    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    await save_item(
        db, env, user["id"], clean_url, tag_list, scope=deps.get_scope(request)
    )
    return RedirectResponse(url="/", status_code=303)


@router.get("/save-popup", response_class=HTMLResponse)
async def save_popup_get(request: Request, url: str = "", title: str = ""):
    clean_url = extract_url(url) or url.strip()
    user = await deps.get_current_user(request)
    if not user:
        dest = "/save-popup?" + urlencode({"url": clean_url, "title": title})
        return RedirectResponse(
            url="/auth/login?" + urlencode({"next": dest}), status_code=303
        )
    template = jinja_env.get_template("save_popup.html")
    db = deps.get_db(request)
    recent_tags = [r["name"] for r in await suggest_tags(db, user["id"], "", limit=5)]
    html = template.render(
        url=clean_url, title=title, success=False, source="", recent_tags=recent_tags
    )
    return HTMLResponse(content=html)


@router.post("/save-popup", response_class=HTMLResponse)
async def save_popup_post(
    request: Request,
    url: str = Form(...),
    title: str = Form(""),
    tags: str = Form(""),
    source: str = Form(""),
):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    env = deps.get_env_from_request(request)
    clean_url = extract_url(url)
    if not clean_url:
        logger.warning(f"Save popup validation failed: invalid URL '{url}'")
        raise HTTPException(status_code=400, detail="Invalid URL")
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    await save_item(
        db, env, user["id"], clean_url, tag_list, scope=deps.get_scope(request)
    )
    template = jinja_env.get_template("save_popup.html")
    html = template.render(url=clean_url, title=title, success=True, source=source)
    return HTMLResponse(content=html)


# ==========================================
# Tags management + suggestion review
# ==========================================


@router.get("/tags", response_class=HTMLResponse)
async def tags_page(request: Request):
    user = await deps.get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)
    db = deps.get_db(request)
    tags = await list_user_tags(db, user["id"])
    tag_styles = tag_styles_for([t["name"] for t in tags])
    suggestions = await list_pending_suggestions(db, user["id"])
    template = jinja_env.get_template("tags.html")
    html = template.render(
        current_user=user,
        tags=tags,
        tag_styles=tag_styles,
        suggestions=suggestions,
        rules=await list_rules(db, user["id"]),
        suggested_rules=await suggest_rules(db, user["id"]),
        active_nav="tags",
    )
    return HTMLResponse(content=html)


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    user = await deps.get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)
    db = deps.get_db(request)
    stats = await get_user_stats(db, user["id"])
    day_scores: dict = stats.get("day_scores", {})
    breakdown: dict = stats.get("day_breakdown", {})
    median = stats.get("median_active_day", 0.0)

    def _day_title(iso: str, score: int) -> str:
        parts = []
        counts = breakdown.get(iso, {})
        if counts.get("save"):
            parts.append(f"saved {counts['save']}")
        if counts.get("open"):
            parts.append(f"read {counts['open']}")
        if counts.get("archive"):
            parts.append(f"triaged {counts['archive']}")
        suffix = f" ({', '.join(parts)})" if parts else ""
        return f"{iso}: {score} pts{suffix}"

    today = datetime.datetime.now(datetime.timezone.utc).date()
    # Year grid: trailing 365 days, columns are Mon-Sun weeks (~53x7).
    start = today - datetime.timedelta(days=364)
    grid_start = start - datetime.timedelta(days=start.weekday())
    weeks: list[list[dict]] = []
    cursor = grid_start
    while cursor <= today:
        week = []
        for _ in range(7):
            iso = cursor.isoformat()
            in_future = cursor > today
            in_range = cursor >= start and not in_future
            score = day_scores.get(iso, 0) if in_range else 0
            week.append(
                {
                    "date": iso,
                    "score": score,
                    "cls": intensity_bucket(score, median),
                    "in_range": in_range,
                    "title": _day_title(iso, score),
                }
            )
            cursor += datetime.timedelta(days=1)
        weeks.append(week)
        if len(weeks) > 60:
            break
    # Current-week strip: last 7 days ending today (UTC).
    week_strip = []
    for n in range(6, -1, -1):
        day = today - datetime.timedelta(days=n)
        iso = day.isoformat()
        score = day_scores.get(iso, 0)
        week_strip.append(
            {
                "date": iso,
                "label": day.strftime("%a"),
                "score": score,
                "cls": intensity_bucket(score, median),
                "is_today": n == 0,
                "title": _day_title(iso, score),
            }
        )
    week_rhythm = stats.get("week_rhythm", [])
    max_rhythm_action = 0
    for w in week_rhythm:
        for action in ("save", "open", "archive"):
            if w.get(action, 0) > max_rhythm_action:
                max_rhythm_action = w[action]
    template = jinja_env.get_template("stats.html")
    return HTMLResponse(
        content=template.render(
            current_user=user,
            stats=stats,
            active_nav="stats",
            weeks=weeks,
            week_strip=week_strip,
            max_rhythm_action=max_rhythm_action,
        )
    )


@router.post("/tags/create")
async def tags_create(request: Request, name: str = Form("")):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    await create_tag(db, user["id"], name)
    return RedirectResponse(url="/tags", status_code=303)


@router.post("/tags/rename")
async def tags_rename(
    request: Request, old_name: str = Form(""), new_name: str = Form("")
):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    await rename_tag(db, user["id"], old_name, new_name)
    if request.headers.get("hx-request"):
        return HTMLResponse(content=await _render_tags_list(db, user["id"]))
    return RedirectResponse(url="/tags", status_code=303)


@router.post("/tags/delete")
async def tags_delete(request: Request, name: str = Form("")):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    await delete_tag(db, user["id"], name)
    if request.headers.get("hx-request"):
        return HTMLResponse(content=await _render_tags_list(db, user["id"]))
    return RedirectResponse(url="/tags", status_code=303)


@router.get("/tags/suggest")
async def tags_suggest(request: Request, q: str = "", exclude: str = ""):
    user = await deps.get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    db = deps.get_db(request)
    return await suggest_tags(
        db, user["id"], q, [e for e in exclude.split(",") if e.strip()]
    )


@router.post("/tags/merge")
async def tags_merge(
    request: Request, old_names: str = Form(""), new_name: str = Form("")
):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    names = [n.strip() for n in old_names.split(",") if n.strip()]
    await merge_tags(db, user["id"], names, new_name)
    if request.headers.get("hx-request"):
        return HTMLResponse(content=await _render_tags_list(db, user["id"]))
    return RedirectResponse(url="/tags", status_code=303)


@router.post("/tags/prune")
async def tags_prune(request: Request):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    await prune_unused_tags(db, user["id"])
    if request.headers.get("hx-request"):
        return HTMLResponse(content=await _render_tags_list(db, user["id"]))
    return RedirectResponse(url="/tags", status_code=303)


@router.post("/tags/rules/create")
async def tag_rule_create(
    request: Request,
    field: str = Form(""),
    substr: str = Form(""),
    tag: str = Form(""),
):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    await create_rule(db, user["id"], field, substr, tag)
    return RedirectResponse(url="/tags", status_code=303)


@router.post("/tags/rules/delete")
async def tag_rule_delete(request: Request, rule_id: str = Form("")):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    await delete_rule(db, user["id"], rule_id)
    return RedirectResponse(url="/tags", status_code=303)


@router.post("/tags/rules/suggestions/dismiss")
async def tag_rule_suggestion_dismiss(request: Request, key: str = Form("")):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    clean = (key or "").strip()
    if clean:
        await db.execute(
            "INSERT OR IGNORE INTO rule_suggestion_dismissals (user_id, key)"
            " VALUES (?, ?);",
            (user["id"], clean),
        )
    return RedirectResponse(url="/tags", status_code=303)


@router.post("/items/bulk-tags")
async def items_bulk_tags(
    request: Request,
    item_ids: list[str] = Form([]),
    add: str = Form(""),
    remove: str = Form(""),
    next: str = Form("/"),
):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    await bulk_update_tags(
        db,
        user["id"],
        item_ids,
        [t for t in add.split(",") if t.strip()],
        [t for t in remove.split(",") if t.strip()],
    )
    return RedirectResponse(url=_safe_next(next), status_code=303)


@router.post("/items/{item_id}/tags", response_class=HTMLResponse)
async def item_tags_update(
    request: Request,
    item_id: str,
    add: str = Form(""),
    remove: str = Form(""),
):
    # htmx tag assignment: returns the re-rendered card (HTML, not JSON).
    user = await deps.require_user(request)
    db = deps.get_db(request)
    if add.strip():
        await add_tags_to_item(
            db, user["id"], item_id, [t for t in add.split(",") if t.strip()]
        )
    if remove.strip():
        await remove_tags_from_item(
            db, user["id"], item_id, [t for t in remove.split(",") if t.strip()]
        )
    item = await get_item(db, user["id"], item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    tag_styles = tag_styles_for(item.get("tags") or [])
    template = jinja_env.get_template("partials/item_card.html")
    return HTMLResponse(content=template.render(item=item, tag_styles=tag_styles))


@router.post("/items/{item_id}/suggestions/{sugg_id}/accept")
async def suggestion_accept(
    request: Request, item_id: str, sugg_id: str, next: str = Form("/tags")
):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    ok = await accept_suggestion(db, user["id"], sugg_id)
    if not ok:
        return HTMLResponse(content="<p>Suggestion not found.</p>", status_code=404)
    back = _safe_next(next)
    return RedirectResponse(url=back if back != "/" else "/tags", status_code=303)


@router.post("/items/{item_id}/suggestions/{sugg_id}/dismiss")
async def suggestion_dismiss(
    request: Request, item_id: str, sugg_id: str, next: str = Form("/tags")
):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    ok = await dismiss_suggestion(db, user["id"], sugg_id)
    if not ok:
        return HTMLResponse(content="<p>Suggestion not found.</p>", status_code=404)
    back = _safe_next(next)
    return RedirectResponse(url=back if back != "/" else "/tags", status_code=303)


@router.get("/share", response_class=HTMLResponse)
async def share_target(
    request: Request, url: str = "", title: str = "", text: str = ""
):
    """Web Share Target (PWA): pre-filled save sheet for shared links."""
    dest = "/share?" + urlencode({"url": url, "title": title, "text": text})
    user = await deps.get_current_user(request)
    if not user:
        return RedirectResponse(
            url="/auth/login?" + urlencode({"next": dest}), status_code=303
        )
    # Android/iOS often puts the link in text instead of url or shares mixed text.
    target = extract_url(url) or extract_url(text)
    if not target:
        return RedirectResponse(url="/", status_code=303)
    template = jinja_env.get_template("save_popup.html")
    db = deps.get_db(request)
    recent_tags = [r["name"] for r in await suggest_tags(db, user["id"], "", limit=5)]
    html = template.render(
        url=target, title=title, success=False, source="share", recent_tags=recent_tags
    )
    return HTMLResponse(content=html)


@router.get("/share/saved", response_class=HTMLResponse)
async def share_saved(request: Request, url: str = ""):
    """Share-target success panel (GET so the form can location.replace() here).

    Replacing the form history entry means swipe-back exits the PWA instead
    of resurrecting the just-submitted form.
    """
    user = await deps.get_current_user(request)
    if not user:
        dest = "/share/saved?" + urlencode({"url": url})
        return RedirectResponse(
            url="/auth/login?" + urlencode({"next": dest}), status_code=303
        )
    template = jinja_env.get_template("save_popup.html")
    html = template.render(url=url, title="", success=True, source="share")
    return HTMLResponse(content=html)
