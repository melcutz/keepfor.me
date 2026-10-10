# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from keepfor.utils.logging import get_request_id, logger


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
