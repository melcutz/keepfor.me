# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Unauthenticated GET / must redirect without opening a database first."""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from keepfor.app import create_app
from keepfor.models.db import Database
from keepfor.spi import Principal, Providers, TenantScope


def test_default_scope_fresh_install_redirects_to_register(db: Database) -> None:
    """Single-tenant default scope with no users sends visitors to register."""
    with patch("keepfor.deps.get_db", return_value=db):
        client = TestClient(create_app())
        resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/register"


def test_default_scope_with_users_redirects_to_login(
    db: Database, sqlite_conn: sqlite3.Connection
) -> None:
    """Single-tenant default scope with a user sends visitors to login."""
    sqlite_conn.execute(
        "INSERT INTO users (id, email, password_hash) VALUES ('u1', 'a@b.co', 'x')"
    )
    sqlite_conn.commit()
    with patch("keepfor.deps.get_db", return_value=db):
        client = TestClient(create_app())
        resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_scope_requiring_provider_redirects_to_login_without_db() -> None:
    """A multi-tenant provider has no scope pre-auth: never call get_db or users."""
    touched: list[str] = []

    class NoAuth:
        async def authenticate(self, request: Any) -> Principal | None:
            return None

    class ScopeThatMustNotBeUsed:
        async def scope_for_principal(
            self, principal: Principal, env: Any
        ) -> TenantScope:
            touched.append("scope_for_principal")
            raise AssertionError("scope must not be resolved without a user")

        async def scope_for_tenant(self, tenant_id: str, env: Any) -> TenantScope:
            touched.append("scope_for_tenant")
            raise AssertionError("scope must not be resolved without a user")

    providers = Providers(auth=NoAuth(), scope=ScopeThatMustNotBeUsed())
    # No patching of get_db: if library_page calls it before auth, it raises
    # RuntimeError("get_db() called before require_user()") and this test fails.
    client = TestClient(create_app(providers=providers), raise_server_exceptions=True)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
    assert touched == []
