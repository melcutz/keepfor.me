import base64
import datetime
import hashlib
import os
import re
import time
from typing import Any
from urllib.parse import urlencode, urlparse

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader
from starlette.middleware.base import BaseHTTPMiddleware

from src.auth.service import (
    InvalidCredentialsError,
    RegistrationClosedError,
    create_pat,
    delete_pat,
    list_pats,
    login_user,
    logout_session,
    register_user,
    validate_pat,
    validate_session,
)
from src.mcp.server import process_mcp_request
from src.models.db import Database
from src.models.items import (
    accept_suggestion,
    create_tag,
    delete_item,
    delete_tag,
    dismiss_suggestion,
    get_item,
    get_item_clean_html,
    list_pending_suggestions,
    list_user_tags,
    rename_tag,
    save_item,
)
from src.schemas import (
    CreatePATRequest,
    LoginRequest,
    RegisterRequest,
    SaveItemRequest,
    SearchRequest,
)
from src.search.engine import get_recent_items, get_status_counts, hybrid_search
from src.utils.importer import (
    export_library_html,
    export_library_json,
    import_bookmarks,
    parse_csv_bookmarks,
    parse_netscape_bookmarks,
)
from src.utils.logging import get_request_id, logger
from src.utils.rate_limit import (
    check_allowed,
    clear_account,
    rate_limited_html,
    record_failure,
)

# Initialize Jinja2 templates
templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
jinja_env = Environment(loader=FileSystemLoader(templates_dir), autoescape=True)

app = FastAPI(title="Keepfor.me API & UI", version="0.1.0")


# Logging middleware
class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        get_request_id()
        start_time = time.time()

        try:
            response = await call_next(request)
            time.time() - start_time

            logger.info(
                f"{request.method} {request.url.path} -> {response.status_code}"
            )
            return response
        except Exception as exc:
            time.time() - start_time
            logger.error(
                f"Exception in {request.method} {request.url.path}", exc_info=exc
            )
            raise


app.add_middleware(LoggingMiddleware)


# Security headers middleware
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # Prevent MIME type sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Disable framing
        response.headers["X-Frame-Options"] = "DENY"
        # Referrer policy
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Permissions policy (disable unnecessary APIs)
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=()"
        )
        # Content Security Policy
        # Note: base.html loads Tailwind via https://cdn.tailwindcss.com
        # and htmx via https://unpkg.com, plus inline <style>/<script>
        # blocks and onclick handlers. 'unsafe-inline' is required for
        # the Tailwind Play CDN (it injects generated styles at runtime).
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' "
            "https://cdn.tailwindcss.com https://unpkg.com "
            "https://static.cloudflareinsights.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
            "img-src 'self' https: data:; "
            "font-src 'self' https: data:; "
            "connect-src 'self' https:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)

# NO CORS middleware on purpose. The app is same-origin only, so the browser's
# own same-origin policy is exactly the policy we want and needs no help.
#
# This used to run CORSMiddleware with allow_origins=["*"] *and*
# allow_credentials=True, which made Starlette echo back whatever Origin the
# caller sent, with credentials allowed — the classic reflected-origin CORS
# hole. Session-cookie reads were blocked only by SameSite=Lax, i.e. one config
# change away from any site reading the whole library.
#
# The browser extension is unaffected: Manifest V3 grants cross-origin fetch via
# host_permissions in browser-extension/manifest.json, not via server CORS.


def get_env_from_request(request: Request) -> Any:
    """Extracts Cloudflare env bindings from ASGI scope or fallback."""
    return request.scope.get("env", None)


def get_db(request: Request) -> Database:
    env = get_env_from_request(request)
    d1 = None
    if env:
        d1 = (
            getattr(env, "DB", None)
            or getattr(env, "keepfor_me_db", None)
            or getattr(env, "D1", None)
        )
    return Database(d1_binding=d1)


async def get_current_user(request: Request) -> dict[str, Any] | None:
    db = get_db(request)

    # 1. Check Bearer PAT
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        user = await validate_pat(db, token)
        if user:
            return user

    # 2. Check Session Cookie
    session_id = request.cookies.get("kfm_session") or request.cookies.get("rk_session")
    if session_id:
        user = await validate_session(db, session_id)
        if user:
            return user

    return None


async def require_user(request: Request) -> dict[str, Any]:
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


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


@app.get("/favicon.ico")
async def favicon():
    return Response(
        content=FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/manifest.webmanifest")
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


def _pwa_icon_response(b64: str):
    return Response(
        content=base64.b64decode(b64),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/icon-192.png")
async def pwa_icon_192():
    from src import pwa_icons

    return _pwa_icon_response(pwa_icons.ICON_192_B64)


@app.get("/icon-512.png")
async def pwa_icon_512():
    from src import pwa_icons

    return _pwa_icon_response(pwa_icons.ICON_512_B64)


@app.get("/icon-maskable.png")
async def pwa_icon_maskable():
    from src import pwa_icons

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


@app.get("/sw.js")
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


@app.get("/", response_class=HTMLResponse)
async def library_page(
    request: Request,
    tag: str | None = None,
    q: str | None = None,
    status: str | None = None,
):
    db = get_db(request)
    user = await get_current_user(request)
    if not user:
        # Check if any users exist to direct to register vs login
        count_row = await db.query_first("SELECT COUNT(*) as count FROM users;")
        if not count_row or count_row["count"] == 0:
            return RedirectResponse(url="/auth/register", status_code=303)
        return RedirectResponse(url="/auth/login", status_code=303)

    env = get_env_from_request(request)
    clean_status = status.strip() if status and status.strip() else None
    items = await hybrid_search(
        db, env, user["id"], query=q or "", tag=tag, limit=30, status=clean_status
    )
    tags = await list_user_tags(db, user["id"])
    tag_styles = tag_styles_for([t["name"] for t in tags])
    # True library size: the feed is capped at 30, so len(items) lies.
    total_row = await db.query_first(
        "SELECT COUNT(*) as count FROM items WHERE user_id = ?;", (user["id"],)
    )
    total_count = total_row["count"] if total_row else 0
    status_counts = await get_status_counts(db, user["id"])

    template = jinja_env.get_template("library.html")
    html = template.render(
        current_user=user,
        items=items,
        tags=tags,
        active_tag=tag,
        query=q or "",
        tag_styles=tag_styles,
        total_count=total_count,
        active_status=clean_status,
        status_counts=status_counts,
        active_nav="library",
    )
    return HTMLResponse(content=html)


@app.post("/search", response_class=HTMLResponse)
async def search_htmx(
    request: Request,
    query: str = Form(""),
    mode: str = Form("hybrid"),
    tag: str = Form(""),
    status: str = Form(""),
):
    user = await get_current_user(request)
    if not user:
        return HTMLResponse("<p>Please log in</p>", status_code=401)

    db = get_db(request)
    env = get_env_from_request(request)
    clean_tag = tag.strip() if tag.strip() else None
    clean_status = status.strip() if status.strip() else None
    # The mode selector was removed from the UI: always run hybrid. The
    # param stays accepted so old clients and /api/search keep working.
    items = await hybrid_search(
        db,
        env,
        user["id"],
        query=query,
        mode="hybrid",
        tag=clean_tag,
        limit=30,
        status=clean_status,
    )
    tag_styles = tag_styles_for([t for it in items for t in (it.get("tags") or [])])

    template = jinja_env.get_template("partials/item_card.html")
    if not items:
        return HTMLResponse(
            '<div class="text-center py-12 text-slate-400 text-xs">'
            "No matching articles found.</div>"
        )

    cards = [template.render(item=it, tag_styles=tag_styles) for it in items]
    return HTMLResponse(content="".join(cards))


@app.get("/items/{item_id}", response_class=HTMLResponse)
async def reader_page(request: Request, item_id: str):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)

    db = get_db(request)
    env = get_env_from_request(request)
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

    clean_html = await get_item_clean_html(db, env, user["id"], item_id)
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


@app.get("/items/{item_id}/card", response_class=HTMLResponse)
async def item_card(request: Request, item_id: str):
    """Single card fragment for htmx polling of extracting items."""
    user = await get_current_user(request)
    if not user:
        return HTMLResponse("<p>Please log in</p>", status_code=401)
    db = get_db(request)
    item = await get_item(db, user["id"], item_id)
    if not item:
        return HTMLResponse(content="", status_code=404)
    tag_styles = tag_styles_for(item.get("tags") or [])
    template = jinja_env.get_template("partials/item_card.html")
    return HTMLResponse(content=template.render(item=item, tag_styles=tag_styles))


@app.post("/save")
async def save_form(request: Request, url: str = Form(...), tags: str = Form("")):
    user = await require_user(request)
    db = get_db(request)
    env = get_env_from_request(request)

    # Validate URL is not empty
    if not url or not url.strip():
        logger.warning("Save validation failed: empty URL")
        raise HTTPException(status_code=400, detail="URL cannot be empty")

    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    await save_item(db, env, user["id"], url, tag_list)
    return RedirectResponse(url="/", status_code=303)


@app.get("/save-popup", response_class=HTMLResponse)
async def save_popup_get(request: Request, url: str = "", title: str = ""):
    user = await get_current_user(request)
    if not user:
        dest = "/save-popup?" + urlencode({"url": url, "title": title})
        return RedirectResponse(
            url="/auth/login?" + urlencode({"next": dest}), status_code=303
        )
    template = jinja_env.get_template("save_popup.html")
    html = template.render(url=url, title=title, success=False, source="")
    return HTMLResponse(content=html)


@app.post("/save-popup", response_class=HTMLResponse)
async def save_popup_post(
    request: Request,
    url: str = Form(...),
    title: str = Form(""),
    tags: str = Form(""),
    source: str = Form(""),
):
    user = await require_user(request)
    db = get_db(request)
    env = get_env_from_request(request)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    await save_item(db, env, user["id"], url, tag_list)
    template = jinja_env.get_template("save_popup.html")
    html = template.render(url=url, title=title, success=True, source=source)
    return HTMLResponse(content=html)


# ==========================================
# Tags management + suggestion review
# ==========================================


@app.get("/tags", response_class=HTMLResponse)
async def tags_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)
    db = get_db(request)
    tags = await list_user_tags(db, user["id"])
    tag_styles = tag_styles_for([t["name"] for t in tags])
    suggestions = await list_pending_suggestions(db, user["id"])
    template = jinja_env.get_template("tags.html")
    html = template.render(
        current_user=user,
        tags=tags,
        tag_styles=tag_styles,
        suggestions=suggestions,
        active_nav="tags",
    )
    return HTMLResponse(content=html)


@app.post("/tags/create")
async def tags_create(request: Request, name: str = Form("")):
    user = await require_user(request)
    db = get_db(request)
    await create_tag(db, user["id"], name)
    return RedirectResponse(url="/tags", status_code=303)


@app.post("/tags/rename")
async def tags_rename(
    request: Request, old_name: str = Form(""), new_name: str = Form("")
):
    user = await require_user(request)
    db = get_db(request)
    await rename_tag(db, user["id"], old_name, new_name)
    return RedirectResponse(url="/tags", status_code=303)


@app.post("/tags/delete")
async def tags_delete(request: Request, name: str = Form("")):
    user = await require_user(request)
    db = get_db(request)
    await delete_tag(db, user["id"], name)
    return RedirectResponse(url="/tags", status_code=303)


@app.post("/items/{item_id}/suggestions/{sugg_id}/accept")
async def suggestion_accept(
    request: Request, item_id: str, sugg_id: str, next: str = Form("/tags")
):
    user = await require_user(request)
    db = get_db(request)
    ok = await accept_suggestion(db, user["id"], sugg_id)
    if not ok:
        return HTMLResponse(content="<p>Suggestion not found.</p>", status_code=404)
    back = _safe_next(next)
    return RedirectResponse(url=back if back != "/" else "/tags", status_code=303)


@app.post("/items/{item_id}/suggestions/{sugg_id}/dismiss")
async def suggestion_dismiss(
    request: Request, item_id: str, sugg_id: str, next: str = Form("/tags")
):
    user = await require_user(request)
    db = get_db(request)
    ok = await dismiss_suggestion(db, user["id"], sugg_id)
    if not ok:
        return HTMLResponse(content="<p>Suggestion not found.</p>", status_code=404)
    back = _safe_next(next)
    return RedirectResponse(url=back if back != "/" else "/tags", status_code=303)


@app.get("/share", response_class=HTMLResponse)
async def share_target(
    request: Request, url: str = "", title: str = "", text: str = ""
):
    """Web Share Target (PWA): pre-filled save sheet for shared links."""
    dest = "/share?" + urlencode({"url": url, "title": title, "text": text})
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(
            url="/auth/login?" + urlencode({"next": dest}), status_code=303
        )
    # Android often puts the link in text instead of url.
    target = url.strip()
    if not target:
        match = re.search(r"https?://\S+", text)
        target = match.group(0).rstrip(").,!?") if match else ""
    if not target:
        return RedirectResponse(url="/", status_code=303)
    template = jinja_env.get_template("save_popup.html")
    html = template.render(url=target, title=title, success=False, source="share")
    return HTMLResponse(content=html)


@app.get("/share/saved", response_class=HTMLResponse)
async def share_saved(request: Request, url: str = ""):
    """Share-target success panel (GET so the form can location.replace() here).

    Replacing the form history entry means swipe-back exits the PWA instead
    of resurrecting the just-submitted form.
    """
    user = await get_current_user(request)
    if not user:
        dest = "/share/saved?" + urlencode({"url": url})
        return RedirectResponse(
            url="/auth/login?" + urlencode({"next": dest}), status_code=303
        )
    template = jinja_env.get_template("save_popup.html")
    html = template.render(url=url, title="", success=True, source="share")
    return HTMLResponse(content=html)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, new_token: str | None = None):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)

    db = get_db(request)
    pats = await list_pats(db, user["id"])
    base_url = str(request.base_url).rstrip("/")
    failed_count = (await get_status_counts(db, user["id"])).get("failed", 0)
    fail_counts = await get_fail_group_counts(db, user["id"])

    template = jinja_env.get_template("settings.html")
    html = template.render(
        current_user=user,
        pats=pats,
        new_token=new_token,
        base_url=base_url,
        failed_count=failed_count,
        fail_counts=fail_counts,
        deleted_count=None,
        active_nav="settings",
    )
    return HTMLResponse(content=html)


@app.post("/settings/tokens", response_class=HTMLResponse)
async def create_token_route(request: Request, name: str = Form(...)):
    user = await require_user(request)
    db = get_db(request)
    # Validate input with model
    try:
        validated = CreatePATRequest(name=name)
    except Exception as e:
        logger.warning(f"PAT creation validation failed: {e}")
        return RedirectResponse(url="/settings", status_code=303)

    res = await create_pat(db, user["id"], validated.name)
    # Re-render settings with the new token visible (raw token is shown once).
    pats = await list_pats(db, user["id"])
    base_url = str(request.base_url).rstrip("/")
    failed_count = (await get_status_counts(db, user["id"])).get("failed", 0)
    fail_counts = await get_fail_group_counts(db, user["id"])
    template = jinja_env.get_template("settings.html")
    html = template.render(
        current_user=user,
        pats=pats,
        new_token=res["token"],
        base_url=base_url,
        failed_count=failed_count,
        fail_counts=fail_counts,
        deleted_count=None,
        active_nav="settings",
    )
    return HTMLResponse(content=html)


@app.post("/settings/tokens/{pat_id}/delete")
async def delete_token_route(request: Request, pat_id: str):
    user = await require_user(request)
    db = get_db(request)
    await delete_pat(db, user["id"], pat_id)
    return RedirectResponse(url="/settings", status_code=303)


# Whitelisted failure groups for the settings cleanup picker. fail_reason is
# free text (str(exc)[:500]), so groups match case-insensitive LIKE patterns.
# Keys are the only accepted form values; patterns never interpolate input.
FAIL_CLEANUP_PATTERNS: dict[str, list[str]] = {
    "all": [],
    "blocked": ["%403%", "%401%", "%forbidden%"],
    "notfound": ["%404%", "%not found%"],
    "client": ["%HTTP 4%"],
    "server": ["%HTTP 5%", "%500%", "%502%", "%503%", "%server error%"],
    "connection": [
        "%timeout%",
        "%timed out%",
        "%connection%",
        "%refused%",
        "%reset%",
        "%unreachable%",
        "%dns%",
        "%resolve%",
    ],
}

# Priority order for exclusive failure-group classification: a 403 matches
# both "blocked" and "client", so the first matching group wins and the
# per-group counts in the cleanup picker always sum to the total.
FAIL_GROUP_ORDER = ["blocked", "notfound", "client", "server", "connection"]
# Every value the cleanup picker may submit: all + classified groups + other.
FAIL_CLEANUP_CHOICES = ["all", *FAIL_GROUP_ORDER, "other"]


def _fail_reason_matches(reason: str | None, likes: list[str]) -> bool:
    """Python mirror of the SQL LOWER(fail_reason) LIKE LOWER(?) matching."""
    text = (reason or "").lower()
    return any(p.strip("%").lower() in text for p in likes)


async def get_fail_group_counts(db: Database, user_id: str) -> dict[str, int]:
    """Exclusive per-group failed counts for the cleanup picker (one query)."""
    rows = await db.query_all(
        "SELECT fail_reason FROM items WHERE user_id = ? AND status = 'failed';",
        (user_id,),
    )
    counts = {"all": len(rows), "other": 0}
    for key in FAIL_GROUP_ORDER:
        counts[key] = 0
    for r in rows:
        for key in FAIL_GROUP_ORDER:
            if _fail_reason_matches(r["fail_reason"], FAIL_CLEANUP_PATTERNS[key]):
                counts[key] += 1
                break
        else:
            counts["other"] += 1
    return counts


# Max failed items deleted per cleanup request (keeps R2/Vectorize fan-out fast).
CLEANUP_LIMIT = 500


@app.post("/settings/cleanup-failed", response_class=HTMLResponse)
async def cleanup_failed_route(request: Request, pattern: str = Form("all")):
    """Deletes failed items matching a whitelisted failure group."""
    user = await require_user(request)
    if pattern not in FAIL_CLEANUP_CHOICES:
        raise HTTPException(status_code=400, detail="Unknown failure group")
    db = get_db(request)
    env = get_env_from_request(request)

    likes = FAIL_CLEANUP_PATTERNS.get(pattern, [])
    if pattern == "all":
        like_clause = ""
        params: tuple = (user["id"], CLEANUP_LIMIT)
    elif pattern == "other":
        # Failures matching none of the known groups (NULL counts as other:
        # NOT (NULL LIKE ...) is NULL, which would otherwise exclude the row).
        all_likes = [p for key in FAIL_GROUP_ORDER for p in FAIL_CLEANUP_PATTERNS[key]]
        like_clause = (
            " AND (fail_reason IS NULL OR NOT ("
            + " OR ".join("LOWER(fail_reason) LIKE LOWER(?)" for _ in all_likes)
            + "))"
        )
        params = (user["id"], *all_likes, CLEANUP_LIMIT)
    elif likes:
        like_clause = (
            " AND ("
            + " OR ".join("LOWER(fail_reason) LIKE LOWER(?)" for _ in likes)
            + ")"
        )
        params = (user["id"], *likes, CLEANUP_LIMIT)
    rows = await db.query_all(
        "SELECT id FROM items WHERE user_id = ? AND status = 'failed'"
        f"{like_clause} ORDER BY created_at ASC LIMIT ?;",
        params,
    )
    deleted = 0
    for r in rows:
        try:
            if await delete_item(db, env, user["id"], r["id"]):
                deleted += 1
        except Exception as exc:
            logger.warning(f"Cleanup delete failed: item={r['id']}: {exc}")

    # Re-render settings with the result (same pattern as token creation).
    pats = await list_pats(db, user["id"])
    base_url = str(request.base_url).rstrip("/")
    failed_count = (await get_status_counts(db, user["id"])).get("failed", 0)
    fail_counts = await get_fail_group_counts(db, user["id"])
    template = jinja_env.get_template("settings.html")
    html = template.render(
        current_user=user,
        pats=pats,
        new_token=None,
        base_url=base_url,
        failed_count=failed_count,
        fail_counts=fail_counts,
        deleted_count=deleted,
        active_nav="settings",
    )
    return HTMLResponse(content=html)


# Max bookmarks per queue message for bulk imports (keeps messages small).
IMPORT_QUEUE_CHUNK = 25


# Max stuck items requeued per call (keeps the request fast).
REQUEUE_LIMIT = 500
# Only rows older than this are considered orphaned; freshly saved items
# may still have live queue messages in flight.
REQUEUE_STUCK_MINUTES = 10


@app.post("/admin/requeue", response_class=HTMLResponse)
async def requeue_stuck_items(request: Request):
    """Re-sends queue messages for items orphaned in 'queued'.

    Needed when queue messages were dropped without processing (e.g. the
    2026-10-03 consumer TypeError): backlog returns to 0 while rows stay
    queued forever. Skips fresh rows so live messages are not duplicated.
    """
    user = await require_user(request)
    db = get_db(request)
    env = get_env_from_request(request)
    queue = getattr(env, "QUEUE", None) if env is not None else None
    if queue is None:
        return HTMLResponse(
            content="<p>No QUEUE binding available.</p>"
            "<p><a href='/settings'>Back to Settings</a></p>",
            status_code=503,
        )
    cutoff = (
        datetime.datetime.utcnow() - datetime.timedelta(minutes=REQUEUE_STUCK_MINUTES)
    ).strftime("%Y-%m-%d %H:%M:%S")
    rows = await db.query_all(
        "SELECT id, canonical_url FROM items WHERE user_id = ? AND status = 'queued' "
        "AND created_at < ? ORDER BY created_at ASC LIMIT ?;",
        (user["id"], cutoff, REQUEUE_LIMIT),
    )
    sent = 0
    for r in rows:
        try:
            await queue.send({"item_id": r["id"], "url": r["canonical_url"]})
            sent += 1
        except Exception as exc:
            logger.warning(f"Requeue send failed: item={r['id']}: {exc}")
            break
    html = (
        "<div style='font-family:sans-serif;max-width:40rem;margin:4rem auto;'>"
        f"<h2>Re-queued {sent} item(s) for extraction.</h2>"
        "<p>Watch them flip from Extracting to content over the next minutes.</p>"
        "<p><a href='/'>Back to Library</a> · "
        "<a href='/settings'>Back to Settings</a></p></div>"
    )
    return HTMLResponse(content=html)


@app.post("/import")
async def import_route(
    request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...)
):
    user = await require_user(request)
    db = get_db(request)
    env = get_env_from_request(request)

    # The settings form uploads the export file directly (multipart).
    raw = await file.read()
    if not raw or len(raw) > 10 * 1024 * 1024:
        return RedirectResponse(url="/settings", status_code=303)
    content = raw.decode("utf-8", errors="replace")

    # CSV by file extension, Netscape HTML otherwise (Chrome/Pocket/Omnivore).
    filename = (file.filename or "").lower()
    if filename.endswith(".csv"):
        bookmarks = parse_csv_bookmarks(content)
    else:
        bookmarks = parse_netscape_bookmarks(content)

    # Never bulk-insert inside the request: 972 bookmarks x per-item D1
    # writes (+inline fetches without a queue) outlasts both the browser's
    # patience and the Worker's request limit. Fan out through the queue in
    # small messages; without a queue binding fall back to a background
    # task so the redirect returns immediately either way.
    queue = getattr(env, "QUEUE", None) if env is not None else None
    if queue is not None:
        for i in range(0, len(bookmarks), IMPORT_QUEUE_CHUNK):
            await queue.send(
                {
                    "user_id": user["id"],
                    "import_batch": bookmarks[i : i + IMPORT_QUEUE_CHUNK],
                }
            )
    elif bookmarks:
        background_tasks.add_task(import_bookmarks, db, env, user["id"], bookmarks)
    return RedirectResponse(url="/", status_code=303)


# ==========================================
# Auth Handlers
# ==========================================


def _safe_next(value: str | None) -> str:
    r"""Return a safe post-login redirect: only same-origin paths, else '/'.

    Getting this wrong is a real open redirect: browsers normalise '\' to '/'
    inside a `Location` header, so a naive `startswith('/')` check lets
    `?next=/\evil.com` through and it lands on `https://evil.com/` -- after
    the victim has just typed their password on the genuine site.

    Verified in Chromium: `Location: /\evil.example` produced a request to
    `http://evil.example/`. A literal tab before `//` is a second bypass.
    Hence the explicit backslash and control-character rejections below, plus a
    structural parse so the rule does not depend on prefix guessing alone.
    """
    if not value or not isinstance(value, str):
        return "/"
    # Backslashes: never legitimate in a path we generate, and browsers treat
    # them as slashes, which can create a protocol-relative (cross-origin) URL.
    if "\\" in value:
        return "/"
    # Control characters and whitespace are stripped by browsers before the URL
    # is parsed, which can also smuggle a `//` past the checks below.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch == " " for ch in value):
        return "/"
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    # Structural check: must have no scheme and no authority component.
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return "/"
    return value


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


@app.get("/auth/login", response_class=HTMLResponse)
async def login_get(
    request: Request, error: str | None = None, next: str | None = None
):
    # Signed-in visitors should never see the login form.
    if await get_current_user(request):
        return RedirectResponse(url=_safe_next(next), status_code=303)
    template = jinja_env.get_template("login.html")
    return HTMLResponse(content=template.render(error=error, next=next or ""))


@app.post("/auth/login")
async def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
):
    db = get_db(request)
    # Throttle BEFORE hashing: PBKDF2 is deliberately expensive, so a blocked
    # client must not be allowed to make us do the work.
    limit = await check_allowed(db, request.headers, email)
    if not limit.allowed:
        return HTMLResponse(
            content=rate_limited_html(limit.retry_after),
            status_code=429,
            headers={"Retry-After": str(limit.retry_after)},
        )
    # Validate input with model
    try:
        validated = LoginRequest(email=email, password=password)
    except Exception as e:
        logger.warning(f"Login validation failed: {e}")
        template = jinja_env.get_template("login.html")
        return HTMLResponse(
            content=template.render(error="Invalid input", next=next),
            status_code=400,
        )

    try:
        user, session_id = await login_user(db, validated.email, validated.password)
        # A successful sign-in clears the account counter so a legitimate user
        # who mistyped a few times is never locked out.
        await clear_account(db, validated.email)
        resp = RedirectResponse(url=_safe_next(next), status_code=303)
        resp.set_cookie(
            key="kfm_session",
            value=session_id,
            max_age=30 * 86400,
            httponly=True,
            samesite="lax",
            secure=True,
        )
        return resp
    except InvalidCredentialsError as err:
        await record_failure(db, request.headers, email)
        template = jinja_env.get_template("login.html")
        return HTMLResponse(
            content=template.render(error=str(err), next=next), status_code=400
        )


@app.get("/auth/register", response_class=HTMLResponse)
async def register_get(
    request: Request, error: str | None = None, next: str | None = None
):
    # Registration is closed after the first user anyway; don't show the
    # form to someone already signed in.
    if await get_current_user(request):
        return RedirectResponse(url=_safe_next(next), status_code=303)
    template = jinja_env.get_template("register.html")
    return HTMLResponse(content=template.render(error=error, next=next or ""))


@app.post("/auth/register")
async def register_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
):
    db = get_db(request)
    env = get_env_from_request(request)
    allow_signups = (
        getattr(env, "ALLOW_PUBLIC_SIGNUPS", "false") == "true" if env else False
    )

    # Same throttle as login: registration also hashes, and when public
    # signups are enabled it is an account-creation endpoint worth limiting.
    limit = await check_allowed(db, request.headers, email)
    if not limit.allowed:
        return HTMLResponse(
            content=rate_limited_html(limit.retry_after),
            status_code=429,
            headers={"Retry-After": str(limit.retry_after)},
        )

    # Validate input with model
    try:
        validated = RegisterRequest(email=email, password=password)
    except Exception as e:
        logger.warning(f"Register validation failed: {e}")
        template = jinja_env.get_template("register.html")
        return HTMLResponse(
            content=template.render(error="Invalid input", next=next),
            status_code=400,
        )

    try:
        await register_user(
            db, validated.email, validated.password, allow_public_signups=allow_signups
        )
        # Automatically log in after registration
        _, session_id = await login_user(db, validated.email, validated.password)
        resp = RedirectResponse(url=_safe_next(next), status_code=303)
        resp.set_cookie(
            key="kfm_session",
            value=session_id,
            max_age=30 * 86400,
            httponly=True,
            samesite="lax",
            secure=True,
        )
        return resp
    except RegistrationClosedError as err:
        template = jinja_env.get_template("register.html")
        return HTMLResponse(
            content=template.render(error=str(err), next=next), status_code=403
        )
    except Exception as err:
        # Includes InvalidCredentialsError from the auto-login below, but the
        # account now exists, so it is not a credential-guessing failure.
        template = jinja_env.get_template("register.html")
        return HTMLResponse(
            content=template.render(error=str(err), next=next), status_code=400
        )


@app.get("/auth/logout")
async def logout_route(request: Request):
    db = get_db(request)
    session_id = request.cookies.get("kfm_session") or request.cookies.get("rk_session")
    if session_id:
        await logout_session(db, session_id)
    resp = RedirectResponse(url="/auth/login", status_code=303)
    resp.delete_cookie("kfm_session")
    resp.delete_cookie("rk_session")
    return resp


# ==========================================
# REST API & MCP Endpoints
# ==========================================


@app.post("/api/save")
async def api_save_item(request: Request, body: SaveItemRequest):
    user = await require_user(request)
    db = get_db(request)
    env = get_env_from_request(request)
    # body is already validated by Pydantic
    item, is_new = await save_item(db, env, user["id"], str(body.url), body.tags)
    status_code = 202 if is_new else 200
    return JSONResponse(content={**item, "is_new": is_new}, status_code=status_code)


@app.get("/api/items")
async def api_list_items(
    request: Request, tag: str | None = None, limit: int = 20, offset: int = 0
):
    user = await require_user(request)
    db = get_db(request)
    items = await get_recent_items(db, user["id"], tag=tag, limit=limit, offset=offset)
    return JSONResponse(content=items)


@app.get("/api/items/{item_id}")
async def api_get_item(request: Request, item_id: str):
    user = await require_user(request)
    db = get_db(request)
    item = await get_item(db, user["id"], item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return JSONResponse(content=item)


@app.get("/api/items/{item_id}/content")
async def api_get_item_content(request: Request, item_id: str):
    user = await require_user(request)
    db = get_db(request)
    env = get_env_from_request(request)
    html = await get_item_clean_html(db, env, user["id"], item_id)
    return HTMLResponse(content=html)


@app.delete("/api/items/{item_id}")
async def api_delete_item(request: Request, item_id: str):
    user = await require_user(request)
    db = get_db(request)
    env = get_env_from_request(request)
    success = await delete_item(db, env, user["id"], item_id)
    if not success:
        raise HTTPException(status_code=404, detail="Item not found")
    # Empty HTML: the library card deletes via htmx outerHTML swap, which
    # renders a JSON body as visible text if we return one.
    return HTMLResponse(content="")


@app.post("/api/search")
async def api_search(request: Request, body: SearchRequest):
    user = await require_user(request)
    db = get_db(request)
    env = get_env_from_request(request)
    # body is already validated by Pydantic
    results, _ = await hybrid_search(
        db,
        env,
        user["id"],
        query=body.query,
        mode=body.mode,
        tag=body.tag,
        limit=body.limit,
    )
    return JSONResponse(content=results)


def _is_mcp_origin_allowed(request: Request) -> bool:
    """DNS-rebinding guard for the MCP endpoint.

    Server-to-server MCP clients send no Origin header and always pass.
    Requests carrying one must be same-origin (Origin host == Host header).
    """
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = request.headers.get("host", "")
    return urlparse(origin).netloc.lower() == host.lower()


@app.post("/api/mcp")
async def mcp_endpoint(request: Request):
    # Stateless Streamable HTTP endpoint: no sessions, no SSE. Requests get
    # 200 + JSON, notifications/responses get 202 + empty body.
    if not _is_mcp_origin_allowed(request):
        raise HTTPException(status_code=403, detail="Invalid Origin")
    user = await require_user(request)
    db = get_db(request)
    env = get_env_from_request(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    response_payload = await process_mcp_request(body, db, env, user)
    if response_payload is None:
        return Response(status_code=202)
    if body.get("method") == "initialize":
        if "result" in response_payload:
            return JSONResponse(
                content=response_payload,
                headers={
                    "MCP-Protocol-Version": response_payload["result"][
                        "protocolVersion"
                    ]
                },
            )
        raise HTTPException(
            status_code=400,
            detail=response_payload.get("error", {}).get(
                "message", "Unsupported protocol version"
            ),
        )

    return JSONResponse(content=response_payload)


@app.get("/api/mcp")
async def mcp_no_stream(request: Request):
    """Stateless server: there is no SSE stream to resume."""
    await require_user(request)
    return Response(status_code=405, headers={"Allow": "GET, POST, DELETE"})


@app.delete("/api/mcp")
async def mcp_no_session(request: Request):
    """Stateless server: there are no sessions to terminate."""
    await require_user(request)
    return Response(status_code=405, headers={"Allow": "GET, POST, DELETE"})


@app.get("/api/export")
async def api_export(request: Request, format: str = "json"):
    user = await require_user(request)
    db = get_db(request)
    if format == "html":
        content = await export_library_html(db, user["id"])
        return Response(
            content=content,
            media_type="text/html",
            headers={
                "Content-Disposition": "attachment; filename=keepfor_me_bookmarks.html"
            },
        )
    else:
        items = await export_library_json(db, user["id"])
        return JSONResponse(
            content=items,
            headers={
                "Content-Disposition": "attachment; filename=keepfor_me_library.json"
            },
        )
