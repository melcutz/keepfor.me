# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from keepfor import deps
from keepfor.mcp.server import process_mcp_request
from keepfor.models.items import (
    delete_item,
    get_item,
    get_item_clean_html,
    save_item,
)
from keepfor.schemas import SaveItemRequest, SearchRequest
from keepfor.search.engine import get_recent_items, hybrid_search
from keepfor.utils.importer import export_library_html, export_library_json
from keepfor.utils.logging import logger
from keepfor.utils.url import extract_url

router = APIRouter()


@router.post("/api/save")
@router.post("/api/items")
async def api_save_item(request: Request, body: SaveItemRequest):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    env = deps.get_env_from_request(request)
    clean_url = extract_url(str(body.url))
    if not clean_url:
        logger.warning(f"API save validation failed: invalid URL '{body.url}'")
        raise HTTPException(status_code=400, detail="Invalid URL")
    item, is_new = await save_item(
        db, env, user["id"], clean_url, body.tags, scope=deps.get_scope(request)
    )
    status_code = 202 if is_new else 200
    return JSONResponse(content={**item, "is_new": is_new}, status_code=status_code)


@router.get("/api/items")
async def api_list_items(
    request: Request, tag: str | None = None, limit: int = 20, offset: int = 0
):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    items = await get_recent_items(
        db, user["id"], tag=tag, limit=min(max(limit, 1), 100), offset=max(offset, 0)
    )
    return JSONResponse(content=items)


@router.get("/api/items/{item_id}")
async def api_get_item(request: Request, item_id: str):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    item = await get_item(db, user["id"], item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return JSONResponse(content=item)


@router.get("/api/items/{item_id}/content")
async def api_get_item_content(request: Request, item_id: str):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    env = deps.get_env_from_request(request)
    html = await get_item_clean_html(
        db, env, user["id"], item_id, scope=deps.get_scope(request)
    )
    if html is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return HTMLResponse(content=html)


@router.delete("/api/items/{item_id}")
async def api_delete_item(request: Request, item_id: str):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    env = deps.get_env_from_request(request)
    success = await delete_item(
        db, env, user["id"], item_id, scope=deps.get_scope(request)
    )
    if not success:
        raise HTTPException(status_code=404, detail="Item not found")
    # Empty HTML: the library card deletes via htmx outerHTML swap, which
    # renders a JSON body as visible text if we return one.
    return HTMLResponse(content="")


@router.post("/api/search")
async def api_search(request: Request, body: SearchRequest):
    user = await deps.require_user(request)
    db = deps.get_db(request)
    env = deps.get_env_from_request(request)
    scope = deps.get_scope(request)
    # body is already validated by Pydantic
    results, _ = await hybrid_search(
        db,
        env,
        user["id"],
        query=body.query,
        mode=body.mode,
        tag=body.tag,
        limit=body.limit,
        scope=scope,
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


@router.post("/api/mcp")
async def mcp_endpoint(request: Request):
    # Stateless Streamable HTTP endpoint: no sessions, no SSE. Requests get
    # 200 + JSON, notifications/responses get 202 + empty body.
    if not _is_mcp_origin_allowed(request):
        raise HTTPException(status_code=403, detail="Invalid Origin")
    user = await deps.require_user(request)
    db = deps.get_db(request)
    env = deps.get_env_from_request(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    scope = deps.get_scope(request)
    providers = deps.get_providers(request)
    decision = await providers.entitlements.check(scope.tenant_id, "mcp_call", qty=1)
    if not decision.allowed:
        req_id = body.get("id") if isinstance(body, dict) else None
        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32001, "message": "limit"},
            }
        )

    response_payload = await process_mcp_request(body, db, env, user, scope=scope)
    if response_payload is None:
        return Response(status_code=202)

    try:
        await providers.entitlements.record(scope.tenant_id, "mcp_call", qty=1)
    except Exception as exc:
        logger.warning("Failed to record entitlement for mcp_call: %s", exc)

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


@router.get("/api/mcp")
async def mcp_no_stream(request: Request):
    """Stateless server: there is no SSE stream to resume."""
    await deps.require_user(request)
    return Response(status_code=405, headers={"Allow": "GET, POST, DELETE"})


@router.delete("/api/mcp")
async def mcp_no_session(request: Request):
    """Stateless server: there are no sessions to terminate."""
    await deps.require_user(request)
    return Response(status_code=405, headers={"Allow": "GET, POST, DELETE"})


@router.get("/api/export")
async def api_export(request: Request, format: str = "json"):
    user = await deps.require_user(request)
    db = deps.get_db(request)
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
