# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from keepfor import deps, runtime
from keepfor.middleware import LoggingMiddleware, SecurityHeadersMiddleware
from keepfor.models.items import record_open
from keepfor.routes import api_router, auth_router, settings_router, ui_router
from keepfor.routes._shared import (
    PER_PAGE_OPTIONS,
    TAG_DOT_CLASSES,
    TAG_PILL_CLASSES,
    _is_safe_path,
    _pager_context,
    _safe_next,
    build_ai_markdown,
    tag_palette_index,
    tag_styles_for,
)
from keepfor.routes.settings import (
    FAIL_CLEANUP_CHOICES,
    FAIL_CLEANUP_PATTERNS,
    FAIL_GROUP_ORDER,
    get_fail_group_counts,
)
from keepfor.spi import EntitlementDenied, Providers
from keepfor.templating import configure_templates


def create_app(
    providers: Providers | None = None,
    *,
    extra_routers: Sequence[APIRouter] = (),
    include_auth_routes: bool = True,
    include_settings_routes: bool = True,
    template_dirs: Sequence[Path] = (),
    template_globals: Mapping[str, Any] | None = None,
    extra_middleware: Sequence[tuple[type, dict[str, Any]]] = (),
    title: str = "Keepfor.me API & UI",
) -> FastAPI:
    prov = (providers or Providers()).resolved()
    runtime.set_providers(prov)

    if template_dirs or template_globals:
        configure_templates(extra_dirs=template_dirs, globals=template_globals)

    app = FastAPI(title=title, version="0.1.0")
    app.state.providers = prov

    for mw_cls, mw_kwargs in extra_middleware:
        app.add_middleware(mw_cls, **mw_kwargs)

    app.add_middleware(LoggingMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    for r in extra_routers:
        app.include_router(r)

    if include_auth_routes:
        app.include_router(auth_router)
    if include_settings_routes:
        app.include_router(settings_router)

    app.include_router(api_router)
    app.include_router(ui_router)

    @app.exception_handler(EntitlementDenied)
    async def entitlement_denied_handler(
        request: Request, exc: EntitlementDenied
    ) -> HTMLResponse | JSONResponse:
        decision = exc.decision
        accept = request.headers.get("accept", "")
        if request.url.path.startswith("/api/") or "application/json" in accept:
            return JSONResponse(
                status_code=402,
                content={
                    "error": "entitlement_denied",
                    "reason": decision.reason,
                    "upgrade_url": decision.upgrade_url,
                },
            )
        from keepfor.templating import jinja_env

        is_htmx = bool(request.headers.get("hx-request"))
        base_tmpl = "partials/fragment.html" if is_htmx else "base.html"
        template = jinja_env.get_template("limit_reached.html")
        html = template.render(
            reason=decision.reason,
            upgrade_url=decision.upgrade_url,
            base_template=base_tmpl,
            is_htmx=is_htmx,
        )
        return HTMLResponse(content=html, status_code=402)

    return app


app = create_app()


# Backwards compatibility re-exports
def get_env_from_request(request: Request) -> Any:
    """Extracts Cloudflare env bindings from ASGI scope or fallback."""
    return deps.get_env_from_request(request)


def get_providers(request: Request):
    """Get providers from request state, app state, or runtime fallback."""
    return deps.get_providers(request)


async def get_current_user(request: Request) -> dict[str, Any] | None:
    return await deps.get_current_user(request)


async def require_user(request: Request) -> dict[str, Any]:
    return await deps.require_user(request)


def get_scope(request: Request):
    return deps.get_scope(request)


def get_db(request: Request):
    return deps.get_db(request)


__all__ = [
    "app",
    "create_app",
    "LoggingMiddleware",
    "SecurityHeadersMiddleware",
    "get_env_from_request",
    "get_providers",
    "get_current_user",
    "require_user",
    "get_scope",
    "get_db",
    "_safe_next",
    "_is_safe_path",
    "_pager_context",
    "build_ai_markdown",
    "get_fail_group_counts",
    "record_open",
    "tag_palette_index",
    "tag_styles_for",
    "TAG_DOT_CLASSES",
    "TAG_PILL_CLASSES",
    "PER_PAGE_OPTIONS",
    "FAIL_CLEANUP_CHOICES",
    "FAIL_CLEANUP_PATTERNS",
    "FAIL_GROUP_ORDER",
]
