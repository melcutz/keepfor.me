# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Default implementations of provider seams for single-tenant self-hosting."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from keepfor.auth.service import validate_pat, validate_session
from keepfor.models.db import Database
from keepfor.spi import (
    SCOPE_ALL,
    Action,
    Decision,
    Principal,
    TenantScope,
    scopes_of,
    self_hosted_account,
)

if TYPE_CHECKING:
    from fastapi import Request

DEFAULT_EMBEDDING_MODEL = "@cf/baai/bge-base-en-v1.5"
DEFAULT_TEXT_MODEL = "@cf/google/gemma-4-26b-a4b-it"


class SessionPatAuthProvider:
    """Authenticates via Bearer PAT header or session cookies."""

    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    def _resolve_db(self, request: Request) -> Database:
        if self._db is not None:
            return self._db
        from keepfor import deps

        return deps.get_db(request)

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
                    # No scope information means "all scopes" ("*"), so a
                    # token created before scopes existed keeps full access.
                    scopes=scopes_of(user),
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
                    scopes=frozenset({SCOPE_ALL}),
                    auth_kind="session",
                )

        return None


def _db_from_env(env: Any) -> Database:
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

    async def usage_summary(self, tenant_id: str) -> dict[str, Any]:
        """The self-hosted account: no plan limits, no trial."""
        return self_hosted_account()


class NoopEventBus:
    """Discards events for self-hosted instances."""

    async def publish(self, event: str, payload: dict[str, Any]) -> None:
        pass


class NoopFetchBudget:
    """Allows unlimited fetches for self-hosted instances."""

    async def allow_fetch(
        self, tenant_id: str | None, url: str, *, action: Action = "fetch"
    ) -> bool:
        return True

    async def record_fetch(
        self, tenant_id: str | None, bytes_read: int, *, action: Action = "fetch"
    ) -> None:
        pass


def clean_json_text(text: str) -> str:
    """Strip markdown code fences and reasoning tags from model JSON output."""
    if not text:
        return ""
    cleaned = text.strip()
    cleaned = re.sub(r"<(think|thought)>.*?</\1>", "", cleaned, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2 and lines[0].startswith("```"):
            if lines[-1].strip() == "```":
                cleaned = "\n".join(lines[1:-1]).strip()
            else:
                cleaned = "\n".join(lines[1:]).strip()
    return cleaned


def _extract_text_response(res: Any) -> str | None:
    """Extract string response from Workers AI output.

    Handles both legacy {'response': '...'} and OpenAI-compatible
    {'choices': [{'message': {'content': '...'}}]} / {'choices': [{'text': '...'}]}
    structures, as well as Pyodide JsProxy instances.
    """
    if res is None:
        return None
    if not hasattr(res, "_mock_return_value") and hasattr(res, "to_py"):
        try:
            res = res.to_py()
        except Exception:
            pass

    if isinstance(res, str):
        text = res.strip()
        return text or None

    if isinstance(res, dict):
        text = res.get("response")
        if text is None:
            choices = res.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    msg = first.get("message")
                    if isinstance(msg, dict):
                        text = msg.get("content")
                    elif "text" in first:
                        text = first.get("text")
                elif isinstance(first, str):
                    text = first
        if text is not None:
            s = str(text).strip()
            return s or None
        return None

    resp = getattr(res, "response", None)
    if resp is not None:
        s = str(resp).strip()
        return s or None

    choices = getattr(res, "choices", None)
    if isinstance(choices, list) and choices:
        first = choices[0]
        msg = getattr(first, "message", None)
        if msg is not None:
            cnt = getattr(msg, "content", None)
            if cnt is not None:
                s = str(cnt).strip()
                return s or None
        txt = getattr(first, "text", None)
        if txt is not None:
            s = str(txt).strip()
            return s or None

    return None


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
        if not hasattr(res, "_mock_return_value") and hasattr(res, "to_py"):
            try:
                res = res.to_py()
            except Exception:
                pass
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
            return _extract_text_response(res)
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
            out = _extract_text_response(res)
            if out is not None and json_schema is not None:
                out = clean_json_text(out)
            return out
        except Exception:
            return None
