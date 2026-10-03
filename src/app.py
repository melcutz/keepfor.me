import os
import time
from typing import Any

from fastapi import (
    FastAPI,
    Form,
    HTTPException,
    Request,
    Response,
)
from fastapi.middleware.cors import CORSMiddleware
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
    delete_item,
    get_item,
    get_item_clean_html,
    list_user_tags,
    save_item,
)
from src.schemas import (
    CreatePATRequest,
    LoginRequest,
    RegisterRequest,
    SaveItemRequest,
    SearchRequest,
)
from src.search.engine import get_recent_items, hybrid_search
from src.utils.importer import (
    export_library_html,
    export_library_json,
    import_bookmarks,
    parse_csv_bookmarks,
    parse_netscape_bookmarks,
)
from src.utils.logging import get_request_id, logger

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
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' https: data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)

# CORS middleware for API access from browser extensions
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: Configure specific origins for production
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS", "PUT"],
    allow_headers=["*"],
    expose_headers=["Content-Type"],
)


def get_env_from_request(request: Request) -> Any:
    """Extracts Cloudflare env bindings from ASGI scope or fallback."""
    return request.scope.get("env", None)


def get_db(request: Request) -> Database:
    env = get_env_from_request(request)
    d1 = getattr(env, "DB", None) if env else None
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


# ==========================================
# Web UI Pages
# ==========================================


@app.get("/", response_class=HTMLResponse)
async def library_page(request: Request, tag: str | None = None, q: str | None = None):
    db = get_db(request)
    user = await get_current_user(request)
    if not user:
        # Check if any users exist to direct to register vs login
        count_row = await db.query_first("SELECT COUNT(*) as count FROM users;")
        if not count_row or count_row["count"] == 0:
            return RedirectResponse(url="/auth/register", status_code=303)
        return RedirectResponse(url="/auth/login", status_code=303)

    env = get_env_from_request(request)
    items = await hybrid_search(db, env, user["id"], query=q or "", tag=tag, limit=30)
    tags = await list_user_tags(db, user["id"])

    template = jinja_env.get_template("library.html")
    html = template.render(
        current_user=user, items=items, tags=tags, active_tag=tag, query=q or ""
    )
    return HTMLResponse(content=html)


@app.post("/search", response_class=HTMLResponse)
async def search_htmx(
    request: Request,
    query: str = Form(""),
    mode: str = Form("hybrid"),
    tag: str = Form(""),
):
    user = await get_current_user(request)
    if not user:
        return HTMLResponse("<p>Please log in</p>", status_code=401)

    db = get_db(request)
    env = get_env_from_request(request)
    clean_tag = tag.strip() if tag.strip() else None
    items = await hybrid_search(
        db, env, user["id"], query=query, mode=mode, tag=clean_tag, limit=30
    )

    template = jinja_env.get_template("partials/item_card.html")
    if not items:
        return HTMLResponse(
            '<div class="text-center py-12 text-slate-400 text-xs">'
            "No matching articles found.</div>"
        )

    cards = [template.render(item=it) for it in items]
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
        raise HTTPException(status_code=404, detail="Item not found")

    clean_html = await get_item_clean_html(db, env, user["id"], item_id)
    template = jinja_env.get_template("reader.html")
    html = template.render(current_user=user, item=item, clean_html=clean_html)
    return HTMLResponse(content=html)


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
        return RedirectResponse(
            url=f"/auth/login?next=/save-popup?url={url}&title={title}", status_code=303
        )
    template = jinja_env.get_template("save_popup.html")
    html = template.render(url=url, title=title, success=False)
    return HTMLResponse(content=html)


@app.post("/save-popup", response_class=HTMLResponse)
async def save_popup_post(
    request: Request, url: str = Form(...), title: str = Form(""), tags: str = Form("")
):
    user = await require_user(request)
    db = get_db(request)
    env = get_env_from_request(request)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    await save_item(db, env, user["id"], url, tag_list)
    template = jinja_env.get_template("save_popup.html")
    html = template.render(url=url, title=title, success=True)
    return HTMLResponse(content=html)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, new_token: str | None = None):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)

    db = get_db(request)
    pats = await list_pats(db, user["id"])
    base_url = str(request.base_url).rstrip("/")

    template = jinja_env.get_template("settings.html")
    html = template.render(
        current_user=user, pats=pats, new_token=new_token, base_url=base_url
    )
    return HTMLResponse(content=html)


@app.post("/settings/tokens")
async def create_token_route(request: Request, name: str = Form(...)):
    user = await require_user(request)
    db = get_db(request)
    # Validate input with model
    try:
        validated = CreatePATRequest(name=name)
    except Exception as e:
        logger.warning(f"PAT creation validation failed: {e}")
        return JSONResponse(content={"error": "Invalid token name"}, status_code=400)

    res = await create_pat(db, user["id"], validated.name)
    return JSONResponse(content=res, status_code=201)


@app.post("/settings/tokens/{pat_id}/delete")
async def delete_token_route(request: Request, pat_id: str):
    user = await require_user(request)
    db = get_db(request)
    await delete_pat(db, user["id"], pat_id)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/import")
async def import_route(
    request: Request, content: str = Form(...), format: str = Form("csv")
):
    user = await require_user(request)
    db = get_db(request)
    env = get_env_from_request(request)

    # Validate format
    if format not in ["csv", "netscape"]:
        raise HTTPException(
            status_code=400, detail="Invalid format. Must be 'csv' or 'netscape'"
        )

    if format == "csv":
        bookmarks = parse_csv_bookmarks(content)
    else:
        bookmarks = parse_netscape_bookmarks(content)

    await import_bookmarks(db, env, user["id"], bookmarks)
    return RedirectResponse(url="/", status_code=303)


# ==========================================
# Auth Handlers
# ==========================================


@app.get("/auth/login", response_class=HTMLResponse)
async def login_get(request: Request, error: str | None = None):
    template = jinja_env.get_template("login.html")
    return HTMLResponse(content=template.render(error=error))


@app.post("/auth/login")
async def login_post(
    request: Request, email: str = Form(...), password: str = Form(...)
):
    db = get_db(request)
    # Validate input with model
    try:
        validated = LoginRequest(email=email, password=password)
    except Exception as e:
        logger.warning(f"Login validation failed: {e}")
        template = jinja_env.get_template("login.html")
        return HTMLResponse(
            content=template.render(error="Invalid input"), status_code=400
        )

    try:
        user, session_id = await login_user(db, validated.email, validated.password)
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie(
            key="kfm_session",
            value=session_id,
            max_age=30 * 86400,
            httponly=True,
            samesite="strict",
            secure=True,
        )
        return resp
    except InvalidCredentialsError as err:
        template = jinja_env.get_template("login.html")
        return HTMLResponse(content=template.render(error=str(err)), status_code=400)


@app.get("/auth/register", response_class=HTMLResponse)
async def register_get(request: Request, error: str | None = None):
    template = jinja_env.get_template("register.html")
    return HTMLResponse(content=template.render(error=error))


@app.post("/auth/register")
async def register_post(
    request: Request, email: str = Form(...), password: str = Form(...)
):
    db = get_db(request)
    env = get_env_from_request(request)
    allow_signups = (
        getattr(env, "ALLOW_PUBLIC_SIGNUPS", "false") == "true" if env else False
    )

    # Validate input with model
    try:
        validated = RegisterRequest(email=email, password=password)
    except Exception as e:
        logger.warning(f"Register validation failed: {e}")
        template = jinja_env.get_template("register.html")
        return HTMLResponse(
            content=template.render(error="Invalid input"), status_code=400
        )

    try:
        await register_user(
            db, validated.email, validated.password, allow_public_signups=allow_signups
        )
        # Automatically log in after registration
        _, session_id = await login_user(db, validated.email, validated.password)
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie(
            key="kfm_session",
            value=session_id,
            max_age=30 * 86400,
            httponly=True,
            samesite="strict",
            secure=True,
        )
        return resp
    except RegistrationClosedError as err:
        template = jinja_env.get_template("register.html")
        return HTMLResponse(content=template.render(error=str(err)), status_code=403)
    except Exception as err:
        template = jinja_env.get_template("register.html")
        return HTMLResponse(content=template.render(error=str(err)), status_code=400)


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
    return JSONResponse(content={"deleted": True, "id": item_id})


@app.post("/api/search")
async def api_search(request: Request, body: SearchRequest):
    user = await require_user(request)
    db = get_db(request)
    env = get_env_from_request(request)
    # body is already validated by Pydantic
    results = await hybrid_search(
        db,
        env,
        user["id"],
        query=body.query,
        mode=body.mode,
        tag=body.tag,
        limit=body.limit,
    )
    return JSONResponse(content=results)


@app.post("/api/mcp")
async def mcp_endpoint(request: Request):
    user = await require_user(request)
    db = get_db(request)
    env = get_env_from_request(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    response_payload = await process_mcp_request(body, db, env, user)
    return JSONResponse(content=response_payload)


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
