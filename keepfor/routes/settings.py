# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import datetime
import json
from typing import TYPE_CHECKING, Any

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
from keepfor.models.imports import create_import, fail_import, set_import_counts
from keepfor.models.items import delete_item
from keepfor.routes._shared import user_import_status
from keepfor.schemas import CreatePATRequest
from keepfor.search.engine import get_status_counts
from keepfor.templating import jinja_env
from keepfor.utils.importer import (
    ENEX_MAX_BYTES,
    HTML_CSV_MAX_BYTES,
    MAX_URLS_PER_IMPORT,
    ZIP_MAX_BYTES,
    ZipBombError,
    detect_file_type,
    import_bookmarks,
    import_notes,
    parse_csv_bookmarks,
    parse_enex,
    parse_keep_takeout,
    parse_markdown_zip,
    parse_netscape_bookmarks,
    plan_quota,
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


async def _create_import(
    db: Database, user_id: str, source: str, filename: str
) -> str | None:
    """Creates the persisted import record; None when the table is missing.

    A missing migration must never break the import itself, so every write
    to the status table fails soft.
    """
    try:
        return await create_import(db, user_id, source=source, filename=filename)
    except Exception as exc:
        logger.warning(f"import status not created: {exc}")
        return None


async def _update_import(
    db: Database, user_id: str, import_id: str | None, **counts: Any
) -> None:
    if not import_id:
        return
    try:
        await set_import_counts(db, user_id, import_id, **counts)
    except Exception as exc:
        logger.warning(f"import status not updated: {exc}")


async def _fail_import(
    db: Database, user_id: str, import_id: str | None, error: str
) -> None:
    if not import_id:
        return
    try:
        await fail_import(db, user_id, import_id, error)
    except Exception as exc:
        logger.warning(f"import status not failed: {exc}")


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

    # One-shot import summary: a failed upload redirects here, so the error
    # has to be shown on this page (the cookie is dropped after rendering).
    import_summary = None
    import_summary_cookie = request.cookies.get("import_summary")
    if import_summary_cookie:
        try:
            import_summary = json.loads(import_summary_cookie)
        except Exception:
            pass

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
        import_summary=import_summary,
        import_status=await user_import_status(db, user["id"], limit=5),
    )
    response = HTMLResponse(content=html)
    if import_summary_cookie:
        response.delete_cookie("import_summary", path="/")
    return response


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
        import_status=await user_import_status(db, user["id"], limit=5),
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
        import_status=await user_import_status(db, user["id"], limit=5),
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
    # Parse-stage summary shown after the redirect: counts only, never note
    # content (privacy invariant).
    summary: dict[str, Any] = {
        "notes_queued": 0,
        "trashed_skipped": 0,
        "attachments_not_imported": 0,
        "urls_found": 0,
        "parse_errors": 0,
        "plan_skipped": 0,
        "error": None,
    }

    def _redirect_with_summary(url: str = "/") -> RedirectResponse:
        # Cookie display works without SessionMiddleware.
        response = RedirectResponse(url=url, status_code=303)
        response.set_cookie(
            "import_summary",
            json.dumps(summary),
            max_age=60,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    raw = await file.read()
    if not raw:
        summary["error"] = "The uploaded file is empty."
        return _redirect_with_summary("/settings")

    # Detect file type by extension and magic bytes
    filename = file.filename or ""
    file_type = detect_file_type(filename, raw)

    # One persisted record per notes upload: the counters and the error
    # survive a refresh (the toast cookie above is one-shot).
    import_id: str | None = None
    if file_type in ("zip", "enex"):
        import_id = await _create_import(db, user["id"], file_type, filename)

    # Enforce size limits per type (documented in README + settings help)
    size_limits = {
        "zip": ZIP_MAX_BYTES,
        "enex": ENEX_MAX_BYTES,
        "html": HTML_CSV_MAX_BYTES,
        "csv": HTML_CSV_MAX_BYTES,
    }
    limit = size_limits.get(file_type)
    if limit is not None and len(raw) > limit:
        summary["error"] = (
            f"File too large ({len(raw) // (1024 * 1024)} MB). The limit for "
            f"{file_type} files is {limit // (1024 * 1024)} MB."
        )
        await _fail_import(db, user["id"], import_id, summary["error"])
        return _redirect_with_summary("/settings")

    scope = deps.get_scope(request)
    providers = deps.get_providers(request)

    parse_stats: dict[str, Any] = {}
    notes: list[dict] = []
    bookmarks: list[dict] = []

    if file_type == "zip":
        # Keep Takeout first (Keep/*.json), then a Markdown/text zip.
        zip_errors = 0
        try:
            notes = parse_keep_takeout(raw, stats=parse_stats)
        except ZipBombError as exc:
            # The zip breaks the decompression caps: nothing is imported and
            # the user is told why.
            logger.warning(f"Zip rejected: {exc}")
            summary["error"] = str(exc)
            await _fail_import(db, user["id"], import_id, summary["error"])
            return _redirect_with_summary("/settings")
        except Exception as exc:
            zip_errors += 1
            logger.warning(f"Keep parse failed: {type(exc).__name__}")
        if not notes:
            try:
                notes = parse_markdown_zip(raw, stats=parse_stats)
            except ZipBombError as exc:
                logger.warning(f"Zip rejected: {exc}")
                summary["error"] = str(exc)
                await _fail_import(db, user["id"], import_id, summary["error"])
                return _redirect_with_summary("/settings")
            except Exception as exc:
                zip_errors += 1
                logger.warning(f"Markdown zip parse failed: {type(exc).__name__}")
        if not notes and zip_errors:
            summary["error"] = "The zip file could not be parsed; nothing was imported."
            await _fail_import(db, user["id"], import_id, summary["error"])

    elif file_type == "enex":
        try:
            notes = parse_enex(raw)
        except ValueError as exc:
            # Malformed or rejected (DTD/entity expansion): the message is
            # fixed text, never note content.
            logger.warning(f"ENEX parse failed: {exc}")
            summary["error"] = str(exc)
        except Exception as exc:
            logger.warning(f"ENEX parse failed: {type(exc).__name__}")
            summary["error"] = (
                "The ENEX file could not be parsed; nothing was imported."
            )
        if summary["error"]:
            await _fail_import(db, user["id"], import_id, summary["error"])

    elif file_type == "csv":
        bookmarks = parse_csv_bookmarks(raw.decode("utf-8", errors="replace"))

    elif file_type == "html":
        bookmarks = parse_netscape_bookmarks(raw.decode("utf-8", errors="replace"))

    else:
        summary["error"] = (
            "Unrecognized file type. Upload a Google Keep Takeout zip, an "
            "Evernote .enex file, a Markdown/text zip, Netscape HTML or CSV."
        )
        return _redirect_with_summary("/settings")

    summary["trashed_skipped"] = parse_stats.get("trashed_skipped", 0)
    summary["parse_errors"] = parse_stats.get("parse_errors", 0)

    # Notes (Keep, ENEX, Markdown zip): fan out through the queue; the
    # consumer runs import_notes per chunk.
    if notes and not summary["error"]:
        # Pre-check entitlements for the whole import (spec item 10): every
        # note and every queued URL costs 1 `import` unit.
        total_urls = sum(len(n.get("urls", [])) for n in notes)
        capped_urls = min(total_urls, MAX_URLS_PER_IMPORT)
        total_items = len(notes) + capped_urls

        # Attachments are listed in the note body, never stored (spec item 8).
        summary["notes_queued"] = len(notes)
        summary["urls_found"] = capped_urls
        summary["attachments_not_imported"] = sum(
            n.get("attachments_count", 0) for n in notes
        )
        source = notes[0].get("source", "markdown")

        # The persisted record carries the parse-stage counts; the consumer
        # adds the per-chunk counts while the import runs.
        await _update_import(
            db,
            user["id"],
            import_id,
            source=source,
            notes_total=len(notes),
            trashed_skipped=summary["trashed_skipped"],
            urls_found=capped_urls,
            attachments_not_imported=summary["attachments_not_imported"],
            parse_errors=summary["parse_errors"],
            errors=parse_stats.get("errors") or [],
        )

        decision = await providers.entitlements.check(
            scope.tenant_id, "import", qty=total_items
        )
        if not decision.allowed:
            # The plan covers only part of the upload: import what fits and
            # report the rest as skipped instead of refusing the whole file.
            allowed = await plan_quota(
                providers.entitlements, scope.tenant_id, total_items
            )
            if allowed <= 0:
                from keepfor.spi import EntitlementDenied

                await _fail_import(
                    db,
                    user["id"],
                    import_id,
                    "Your plan does not allow importing this file.",
                )
                raise EntitlementDenied(decision)
            summary["plan_skipped"] = total_items - allowed

        # Never bulk-insert inside the request: 1,000 notes x per-note D1
        # writes outlast both the browser's patience and the Worker's request
        # limit. Fan out through the queue in small messages; without a queue
        # binding fall back to a background task so the redirect returns
        # immediately either way.
        queue = getattr(env, "QUEUE", None) if env is not None else None
        if queue is not None:
            for i in range(0, len(notes), IMPORT_QUEUE_CHUNK):
                await queue.send(
                    {
                        "user_id": user["id"],
                        "source": source,
                        "import_id": import_id,
                        "import_notes_batch": notes[i : i + IMPORT_QUEUE_CHUNK],
                    }
                )
        else:
            background_tasks.add_task(
                import_notes,
                db,
                env,
                user["id"],
                notes,
                providers,
                scope,
                source,
                import_id,
            )

    # Import bookmarks (CSV, HTML)
    if bookmarks:
        # Check entitlements for bookmarks
        decision = await providers.entitlements.check(
            scope.tenant_id, "import", qty=len(bookmarks)
        )
        if not decision.allowed:
            from keepfor.spi import EntitlementDenied

            raise EntitlementDenied(decision)

        # Fan out through queue or background task
        queue = getattr(env, "QUEUE", None) if env is not None else None
        if queue is not None:
            for i in range(0, len(bookmarks), IMPORT_QUEUE_CHUNK):
                await queue.send(
                    {
                        "user_id": user["id"],
                        "import_batch": bookmarks[i : i + IMPORT_QUEUE_CHUNK],
                    }
                )
        else:
            background_tasks.add_task(import_bookmarks, db, env, user["id"], bookmarks)

        try:
            await providers.entitlements.record(
                scope.tenant_id, "import", qty=len(bookmarks)
            )
        except Exception as exc:
            logger.warning("Failed to record entitlement for import: %s", exc)

    # A zip/enex that parsed cleanly but yielded nothing is almost certainly
    # the wrong file (e.g. a generic photo zip); say so instead of silently
    # importing zero items.
    if (
        file_type in ("zip", "enex")
        and not notes
        and not bookmarks
        and not summary["error"]
    ):
        summary["error"] = "No notes found in the file."
        await _fail_import(db, user["id"], import_id, summary["error"])

    return _redirect_with_summary("/settings" if summary["error"] else "/")
