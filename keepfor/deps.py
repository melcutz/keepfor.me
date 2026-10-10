# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Request-scoped dependencies and provider resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

from keepfor.models.db import Database

if TYPE_CHECKING:
    from keepfor.spi import Providers, TenantScope


def get_env_from_request(request: Request) -> Any:
    """Extracts Cloudflare env bindings from ASGI scope or fallback."""
    scope = getattr(request, "scope", {})
    return scope.get("env", None) if isinstance(scope, dict) else None


def get_providers(request: Request) -> Providers:
    """Get providers from request state, app state, or runtime fallback."""
    state = getattr(request, "state", None)
    providers = getattr(state, "providers", None) if state is not None else None
    if providers is not None:
        return providers

    app = getattr(request, "app", None)
    app_state = getattr(app, "state", None) if app is not None else None
    providers = getattr(app_state, "providers", None) if app_state is not None else None
    if providers is not None:
        return providers

    from keepfor.runtime import get_providers as get_runtime_providers

    return get_runtime_providers()


async def get_current_user(request: Request) -> dict[str, Any] | None:
    """Authenticate request principal and attach scope to request state."""
    state = getattr(request, "state", None)
    if state is not None and getattr(state, "principal", None) is not None:
        return state.principal.to_user_dict()

    providers = get_providers(request)
    if providers.auth is None:
        return None

    principal = await providers.auth.authenticate(request)
    if principal is None:
        return None

    env = get_env_from_request(request)
    if providers.scope is not None:
        scope = await providers.scope.scope_for_principal(principal, env)
        if scope.db is None or (scope.db.d1 is None and scope.db.sqlite is None):
            try:
                scope_db = get_db(request)
                if scope_db.d1 is not None or scope_db.sqlite is not None:
                    from keepfor.spi import TenantScope

                    scope = TenantScope(
                        tenant_id=scope.tenant_id,
                        user_id=scope.user_id,
                        db=scope_db,
                        bucket=scope.bucket,
                        index=scope.index,
                        blob_prefix=scope.blob_prefix,
                        vector_namespace=scope.vector_namespace,
                    )
            except Exception:
                pass
        if state is not None:
            state.scope = scope

    if state is not None:
        state.principal = principal

    return principal.to_user_dict()


async def require_user(request: Request) -> dict[str, Any]:
    """Require authenticated user, raising 401 if missing."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def get_scope(request: Request) -> TenantScope:
    """Get TenantScope from request state, raising if unauthenticated."""
    state = getattr(request, "state", None)
    scope = getattr(state, "scope", None) if state is not None else None
    if scope is None:
        raise RuntimeError("get_scope() called before authenticate/require_user()")
    return scope


def is_single_tenant(request: Request) -> bool:
    """True when the app runs with the default single-tenant scope provider."""
    from keepfor.defaults import SingleTenantScopeProvider

    return isinstance(get_providers(request).scope, SingleTenantScopeProvider)


def get_db(request: Request) -> Database:
    """Get Database connection from tenant scope or single-tenant default."""
    state = getattr(request, "state", None)
    scope = getattr(state, "scope", None) if state is not None else None
    if scope is not None and getattr(scope, "db", None) is not None:
        return scope.db

    # Check state.db fallback
    state_db = getattr(state, "db", None) if state is not None else None
    if state_db is not None:
        return state_db

    providers = get_providers(request)
    from keepfor.defaults import SingleTenantScopeProvider

    if isinstance(providers.scope, SingleTenantScopeProvider):
        env = get_env_from_request(request)
        d1 = None
        sqlite_conn = None
        if env:
            d1 = (
                getattr(env, "DB", None)
                or getattr(env, "keepfor_me_db", None)
                or getattr(env, "D1", None)
            )
            sqlite_conn = getattr(env, "sqlite_conn", None)
        return Database(d1_binding=d1, sqlite_conn=sqlite_conn)

    raise RuntimeError("get_db() called before require_user(); authenticate first")
