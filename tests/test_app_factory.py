# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for create_app application factory and route isolation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from keepfor.app import create_app
from keepfor.spi import Principal, Providers, TenantScope
from keepfor.templating import configure_templates

if TYPE_CHECKING:
    from pathlib import Path

    from keepfor.models.db import Database


PUBLIC_ROUTES = {
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/auth/login",
    "/auth/register",
    "/auth/logout",
    "/favicon.ico",
    "/manifest.webmanifest",
    "/icon-192.png",
    "/icon-512.png",
    "/icon-maskable.png",
    "/sw.js",
}


@pytest.fixture(autouse=True)
def reset_templates():
    """Ensure Jinja environment is restored after each test."""
    yield
    configure_templates([])


def test_default_app_serves_login_and_rejects_api_without_auth():
    """Default create_app() serves /auth/login and rejects /api/items with 401."""
    client = TestClient(create_app())
    resp_login = client.get("/auth/login")
    assert resp_login.status_code == 200

    resp_api = client.get("/api/items")
    assert resp_api.status_code == 401


def test_custom_app_without_auth_router():
    """App created with include_auth_routes=False returns 404 for /auth/login."""
    client = TestClient(create_app(include_auth_routes=False))
    resp = client.get("/auth/login")
    assert resp.status_code == 404


def test_custom_app_without_settings_router():
    """App created with include_settings_routes=False returns 404 for /settings."""
    client = TestClient(create_app(include_settings_routes=False))
    resp = client.get("/settings")
    assert resp.status_code == 404


def test_custom_extra_routers_override_or_extend():
    """Extra routers override core endpoints because they are registered first."""
    router = APIRouter()
    router.add_api_route(
        "/auth/login",
        lambda: PlainTextResponse("CUSTOM_LOGIN_PAGE"),
        methods=["GET"],
    )

    client = TestClient(create_app(extra_routers=[router]))
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert resp.text == "CUSTOM_LOGIN_PAGE"


def test_custom_auth_provider_injected(db: Database):
    """Custom AuthProvider with fixed Principal allows requests to pass."""

    class FixedAuthProvider:
        async def authenticate(self, request: Any) -> Principal | None:
            return Principal(
                user_id="user_factory_test",
                tenant_id="user_factory_test",
                email="fixed@example.com",
            )

    class FixedScopeProvider:
        async def scope_for_principal(
            self, principal: Principal, env: Any
        ) -> TenantScope:
            return TenantScope(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                db=db,
            )

        async def scope_for_tenant(self, tenant_id: str, env: Any) -> TenantScope:
            return TenantScope(
                tenant_id=tenant_id,
                user_id=tenant_id,
                db=db,
            )

    providers = Providers(auth=FixedAuthProvider(), scope=FixedScopeProvider())
    app = create_app(providers=providers)
    client = TestClient(app)

    resp = client.get("/api/items")
    assert resp.status_code == 200
    assert resp.json() == []


def test_custom_template_dirs_override(tmp_path: Path):
    """template_dirs overrides core templates (e.g. base.html)."""
    override_base = tmp_path / "base.html"
    override_base.write_text("OVERRIDE_BASE {% block content %}{% endblock %}")

    app = create_app(template_dirs=[tmp_path])
    client = TestClient(app)
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert "OVERRIDE_BASE" in resp.text


def test_unauthenticated_routes_require_login(db: Database):
    """Every non-public route requires auth (401/303, never 200/500)."""
    with patch("keepfor.deps.get_db", return_value=db):
        app = create_app()
        client = TestClient(app)

        for route in app.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if not path or not methods or path in PUBLIC_ROUTES:
                continue

            for method in methods:
                if method == "HEAD":
                    continue
                test_path = (
                    path.replace("{item_id}", "item-123")
                    .replace("{pat_id}", "pat-123")
                    .replace("{sugg_id}", "sugg-123")
                )
                payload = {
                    "url": "https://example.com",
                    "content": "sample content",
                    "title": "sample title",
                    "pattern": "all",
                    "name": "token-name",
                    "query": "query",
                }
                resp = client.request(
                    method,
                    test_path,
                    data=payload,
                    json=payload,
                    follow_redirects=False,
                )
                assert resp.status_code in (401, 303, 422), (
                    f"Route {method} {path} returned unexpected {resp.status_code}"
                )
                assert resp.status_code not in (200, 500), (
                    f"Route {method} {path} returned unauthenticated: "
                    f"{resp.status_code}"
                )
