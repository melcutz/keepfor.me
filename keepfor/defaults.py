# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Default implementations of provider seams for single-tenant self-hosting."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from keepfor.auth.service import validate_pat, validate_session
from keepfor.models.db import Database
from keepfor.spi import (
    Action,
    Decision,
    Principal,
    TenantScope,
)

if TYPE_CHECKING:
    from fastapi import Request

DEFAULT_EMBEDDING_MODEL = "@cf/baai/bge-base-en-v1.5"
DEFAULT_TEXT_MODEL = "@cf/meta/llama-3.2-3b-instruct"


class SessionPatAuthProvider:
    """Authenticates via Bearer PAT header or session cookies."""

    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    def _resolve_db(self, request: Request) -> Database:
        if self._db is not None:
            return self._db
        state_db = getattr(getattr(request, "state", None), "db", None)
        if state_db is not None:
            return state_db
        env = (
            getattr(request, "scope", {}).get("env")
            if hasattr(request, "scope")
            else None
        )
        d1 = None
        if env:
            d1 = (
                getattr(env, "DB", None)
                or getattr(env, "keepfor_me_db", None)
                or getattr(env, "D1", None)
            )
        return Database(d1_binding=d1)

    async def authenticate(self, request: Request) -> Principal | None:
        db = self._resolve_db(request)

        # 1. Check Bearer PAT
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            user = await validate_pat(db, token)
            if user:
                return Principal(
                    user_id=user["id"],
                    tenant_id=user["id"],
                    email=user["email"],
                    role=user.get("role", "user"),
                    plan="self_hosted",
                    scopes=frozenset({"*"}),
                    auth_kind="pat",
                )

        # 2. Check Session Cookie (kfm_session or rk_session)
        session_id = request.cookies.get("kfm_session") or request.cookies.get(
            "rk_session"
        )
        if session_id:
            user = await validate_session(db, session_id)
            if user:
                return Principal(
                    user_id=user["id"],
                    tenant_id=user["id"],
                    email=user["email"],
                    role=user.get("role", "user"),
                    plan="self_hosted",
                    scopes=frozenset({"*"}),
                    auth_kind="session",
                )

        return None


def _db_from_env(env: Any) -> Database:
    d1 = None
    if env:
        d1 = (
            getattr(env, "DB", None)
            or getattr(env, "keepfor_me_db", None)
            or getattr(env, "D1", None)
        )
    return Database(d1_binding=d1)


def default_scope(db: Database, env: Any = None, user_id: str = "") -> TenantScope:
    """Helper to construct a default single-tenant TenantScope."""
    bucket = getattr(env, "BUCKET", None) if env else None
    index = getattr(env, "VECTORIZE", None) if env else None
    return TenantScope(
        tenant_id=user_id,
        user_id=user_id,
        db=db,
        bucket=bucket,
        index=index,
        blob_prefix="",
        vector_namespace=None,
    )


class SingleTenantScopeProvider:
    """Resolves single-tenant scope from environment bindings."""

    async def scope_for_principal(self, principal: Principal, env: Any) -> TenantScope:
        db = _db_from_env(env)
        bucket = getattr(env, "BUCKET", None) if env else None
        index = getattr(env, "VECTORIZE", None) if env else None
        return TenantScope(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            db=db,
            bucket=bucket,
            index=index,
            blob_prefix="",
            vector_namespace=None,
        )

    async def scope_for_tenant(self, tenant_id: str, env: Any) -> TenantScope:
        db = _db_from_env(env)
        bucket = getattr(env, "BUCKET", None) if env else None
        index = getattr(env, "VECTORIZE", None) if env else None
        return TenantScope(
            tenant_id=tenant_id,
            user_id=tenant_id,
            db=db,
            bucket=bucket,
            index=index,
            blob_prefix="",
            vector_namespace=None,
        )


class AllowAllEntitlements:
    """Permits all operations for self-hosted instances."""

    async def check(self, tenant_id: str, action: Action, qty: int = 1) -> Decision:
        return Decision(allowed=True)

    async def record(self, tenant_id: str, action: Action, qty: int = 1) -> None:
        pass


class NoopEventBus:
    """Discards events for self-hosted instances."""

    async def publish(self, event: str, payload: dict[str, Any]) -> None:
        pass


class NoopFetchBudget:
    """Allows unlimited fetches for self-hosted instances."""

    async def allow_fetch(self, tenant_id: str | None, url: str) -> bool:
        return True

    async def record_fetch(self, tenant_id: str | None, bytes_read: int) -> None:
        pass


class WorkersAIProvider:
    """Wraps Cloudflare Workers AI binding."""

    def __init__(
        self,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        text_model: str = DEFAULT_TEXT_MODEL,
    ) -> None:
        self.embedding_model = embedding_model
        self.text_model = text_model

    async def embed(self, env: Any, texts: list[str]) -> list[list[float]]:
        if env is None or getattr(env, "AI", None) is None:
            return []
        res = await env.AI.run(self.embedding_model, {"text": texts})
        if isinstance(res, dict):
            return res.get("data", [])
        return getattr(res, "data", []) or []

    async def summarize(
        self, env: Any, text: str, *, max_words: int = 40
    ) -> str | None:
        if env is None or getattr(env, "AI", None) is None:
            return None
        plain = (text or "").strip()
        if not plain:
            return None
        prompt = (
            f"Extract the core thesis and 2 key takeaways from this text"
            f" in under {max_words} words total:\n\n" + plain[:2000]
        )
        max_tokens = max(80, max_words * 2)
        try:
            res = await asyncio.wait_for(
                env.AI.run(
                    self.text_model,
                    {"prompt": prompt, "max_tokens": max_tokens},
                ),
                timeout=4.0,
            )
            if isinstance(res, dict):
                summary = str(res.get("response", "")).strip()
            else:
                summary = str(getattr(res, "response", "") or "").strip()
            return summary or None
        except Exception:
            return None

    async def generate(
        self,
        env: Any,
        system: str,
        user: str,
        *,
        max_tokens: int = 400,
        json_schema: dict | None = None,
        timeout_s: float = 20.0,
    ) -> str | None:
        if env is None or getattr(env, "AI", None) is None:
            return None
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        payload: dict[str, Any] = {"messages": messages, "max_tokens": max_tokens}
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": json_schema,
            }
        try:
            res = await asyncio.wait_for(
                env.AI.run(self.text_model, payload),
                timeout=timeout_s,
            )
            if isinstance(res, dict):
                out = str(res.get("response", "")).strip()
            else:
                out = str(getattr(res, "response", "") or "").strip()
            return out or None
        except Exception:
            return None
