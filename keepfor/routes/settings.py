# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse

from keepfor import deps
from keepfor.auth.service import create_pat, delete_pat, list_pats
from keepfor.models.items import delete_item
from keepfor.schemas import CreatePATRequest
from keepfor.search.engine import get_status_counts
from keepfor.templating import jinja_env
from keepfor.utils.importer import (
    import_bookmarks,
    parse_csv_bookmarks,
    parse_netscape_bookmarks,
)
from keepfor.utils.logging import logger

if TYPE_CHECKING:
    from keepfor.models.db import Database

router = APIRouter()

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

# Max bookmarks per queue message for bulk imports (keeps messages small).
IMPORT_QUEUE_CHUNK = 25

# Max stuck items requeued per call (keeps the request fast).
REQUEUE_LIMIT = 500
# Only rows older than this are considered orphaned; freshly saved items
# may still have live queue messages in flight.
REQUEUE_STUCK_MINUTES = 10


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, new_token: str | None = None):
    user = await deps.get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)

    db = deps.get_db(request)
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


@router.post("/settings/tokens", response_class=HTMLResponse)
async def create_token_route(request: Request, name: str = Form(...)):
    user = await deps.require_user(request)
    db = deps.get_db(request)
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


@router.post("/settings/tokens/{pat_id}/delete")
async def delete_token_route(request: Request, pat_id: str):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    await delete_pat(db, user["id"], pat_id)
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/cleanup-failed", response_class=HTMLResponse)
async def cleanup_failed_route(request: Request, pattern: str = Form("all")):
    """Deletes failed items matching a whitelisted failure group."""
    user = await deps.require_user(request)
    if pattern not in FAIL_CLEANUP_CHOICES:
        raise HTTPException(status_code=400, detail="Unknown failure group")
    db = deps.get_db(request)
    env = deps.get_env_from_request(request)

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
            if await delete_item(
                db, env, user["id"], r["id"], scope=deps.get_scope(request)
            ):
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


@router.post("/admin/requeue", response_class=HTMLResponse)
async def requeue_stuck_items(request: Request):
    """Re-sends queue messages for items orphaned in 'queued'.

    Needed when queue messages were dropped without processing (e.g. the
    2026-10-03 consumer TypeError): backlog returns to 0 while rows stay
    queued forever. Skips fresh rows so live messages are not duplicated.
    """
    user = await deps.require_user(request)
    db = deps.get_db(request)
    env = deps.get_env_from_request(request)
    queue = getattr(env, "QUEUE", None) if env is not None else None
    if queue is None:
        return HTMLResponse(
            content="<p>No QUEUE binding available.</p>"
            "<p><a href='/settings'>Back to Settings</a></p>",
            status_code=503,
        )
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(minutes=REQUEUE_STUCK_MINUTES)
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


@router.post("/import")
async def import_route(
    request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...)
):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    env = deps.get_env_from_request(request)

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
