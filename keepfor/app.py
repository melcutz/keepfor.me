# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

from keepfor import deps
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
from keepfor.utils.logging import get_request_id, logger

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

# Include core routers
app.include_router(auth_router)
app.include_router(settings_router)
app.include_router(api_router)
app.include_router(ui_router)


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
