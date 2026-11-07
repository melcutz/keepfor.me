# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Default implementations of provider seams for single-tenant self-hosting."""

from __future__ import annotations

import asyncio
import logging
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

logger = logging.getLogger(__name__)


class WorkersAIError(RuntimeError):
    """Base exception for Workers AI invocation failures."""

    def __init__(
        self,
        message: str,
        *,
        model: str | None = None,
        retryable: bool = True,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.model = model
        self.retryable = retryable
        self.cause = cause


class WorkersAIRetryableError(WorkersAIError):
    """Raised when a Workers AI failure is transient and retryable."""

    def __init__(
        self,
        message: str,
        *,
        model: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message, model=model, retryable=True, cause=cause)


class WorkersAITruncatedEmptyError(WorkersAIRetryableError):
    """Raised when Workers AI response is truncated with empty content."""

    def __init__(
        self,
        message: str = (
            "Workers AI response truncated with empty content (finish_reason=length)"
        ),
        *,
        model: str | None = None,
        finish_reason: str | None = "length",
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message, model=model, cause=cause)
        self.finish_reason = finish_reason


class WorkersAINonRetryableError(WorkersAIError):
    """Raised when a Workers AI failure is permanent and not retryable."""

    def __init__(
        self,
        message: str,
        *,
        model: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message, model=model, retryable=False, cause=cause)


def _log_ai_error(action: str, model: str, exc: BaseException) -> None:
    """Log safe error metadata without logging user content, prompts, or secrets."""
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    is_timeout = isinstance(exc, (asyncio.TimeoutError, TimeoutError))
    logger.warning(
        "Workers AI %s failed: model=%s error_type=%s timeout=%s status=%s",
        action,
        model,
        type(exc).__name__,
        is_timeout,
        status,
    )


def _log_truncated_empty_response(
    action: str, model: str, finish_reason: str | None
) -> None:
    """Log safe error metadata for truncated empty response without secrets."""
    logger.warning(
        "Workers AI %s response truncated with empty content: "
        "model=%s finish_reason=%s",
        action,
        model,
        finish_reason,
    )


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


def _extract_from_choice(choice: Any) -> Any:
    """Extract candidate text from a single choice entry (dict, object, or str)."""
    if isinstance(choice, str):
        return choice
    if isinstance(choice, dict):
        msg = choice.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
            if content is not None:
                return content
        elif msg is not None:
            content = getattr(msg, "content", None)
            if content is not None:
                return content
        return choice.get("text")
    if choice is not None:
        msg = getattr(choice, "message", None)
        if isinstance(msg, dict):
            content = msg.get("content")
            if content is not None:
                return content
        elif msg is not None:
            content = getattr(msg, "content", None)
            if content is not None:
                return content
        return getattr(choice, "text", None)
    return None


def _extract_text_response(
    res: Any,
    *,
    model: str | None = None,
    action: str = "generation",
) -> str | None:
    """Extract string response from Workers AI output.

    Handles legacy {'response': '...'}, OpenAI-compatible
    {'choices': [{'message': {'content': '...'}}]} / {'choices': [{'text': '...'}]},
    REST envelope {'result': ...}, and Pyodide JsProxy instances.

    Raises WorkersAITruncatedEmptyError when finish_reason indicates length/token
    exhaustion but content is empty (e.g. reasoning token starvation).
    """
    if res is None:
        return None
    if not hasattr(res, "_mock_return_value") and hasattr(res, "to_py"):
        try:
            res = res.to_py()
        except Exception:
            pass

    # Unwrap REST envelope if present
    if (
        isinstance(res, dict)
        and "result" in res
        and isinstance(res["result"], (dict, list, tuple))
        and "choices" not in res
        and "response" not in res
    ):
        res = res["result"]
    elif (
        not isinstance(res, dict)
        and hasattr(res, "result")
        and not hasattr(res, "choices")
        and not hasattr(res, "response")
    ):
        res = getattr(res, "result")

    if isinstance(res, str):
        stripped = res.strip()
        return stripped or None

    candidate: Any = None
    finish_reason: str | None = None
    first_choice: Any = None

    if isinstance(res, dict):
        candidate = res.get("response")
        finish_reason = res.get("finish_reason")
        choices = res.get("choices")
        if isinstance(choices, (list, tuple)) and choices:
            first_choice = choices[0]
            if candidate is None:
                candidate = _extract_from_choice(first_choice)
    else:
        candidate = getattr(res, "response", None)
        finish_reason = getattr(res, "finish_reason", None)
        choices = getattr(res, "choices", None)
        if isinstance(choices, (list, tuple)) and choices:
            first_choice = choices[0]
            if candidate is None:
                candidate = _extract_from_choice(first_choice)

    if first_choice is not None:
        choice_finish = (
            first_choice.get("finish_reason")
            if isinstance(first_choice, dict)
            else getattr(first_choice, "finish_reason", None)
        )
        if choice_finish is not None:
            finish_reason = choice_finish

    text = str(candidate).strip() if candidate is not None else ""
    is_empty = not text

    if (
        is_empty
        and finish_reason is not None
        and str(finish_reason).lower() in ("length", "max_tokens")
    ):
        target_model = model or "unknown"
        _log_truncated_empty_response(action, target_model, str(finish_reason))
        msg = (
            f"Workers AI {action} response truncated with empty content "
            f"(finish_reason={finish_reason})"
        )
        raise WorkersAITruncatedEmptyError(
            msg,
            model=target_model,
            finish_reason=str(finish_reason),
        )

    return text or None


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
        try:
            res = await env.AI.run(self.embedding_model, {"text": texts})
            if not hasattr(res, "_mock_return_value") and hasattr(res, "to_py"):
                try:
                    res = res.to_py()
                except Exception:
                    pass
            if isinstance(res, dict):
                return res.get("data", [])
            return getattr(res, "data", []) or []
        except Exception as exc:
            if isinstance(exc, WorkersAIError):
                raise
            _log_ai_error("embedding", self.embedding_model, exc)
            raise WorkersAIRetryableError(
                f"Workers AI embedding failed: {type(exc).__name__}",
                model=self.embedding_model,
                cause=exc,
            ) from exc

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
            return _extract_text_response(
                res, model=self.text_model, action="summarization"
            )
        except Exception as exc:
            if isinstance(exc, WorkersAIError):
                raise
            _log_ai_error("summarization", self.text_model, exc)
            raise WorkersAIRetryableError(
                f"Workers AI summarization failed: {type(exc).__name__}",
                model=self.text_model,
                cause=exc,
            ) from exc

    async def generate(
        self,
        env: Any,
        system: str,
        user: str,
        *,
        max_tokens: int = 400,
        json_schema: dict | None = None,
        timeout_s: float = 20.0,
        chat_template_kwargs: dict[str, Any] | None = None,
    ) -> str | None:
        if env is None or getattr(env, "AI", None) is None:
            return None
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        template_kwargs = (
            dict(chat_template_kwargs)
            if chat_template_kwargs is not None
            else {"enable_thinking": False}
        )
        payload: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "chat_template_kwargs": template_kwargs,
        }
        if json_schema is not None:
            if (
                isinstance(json_schema, dict)
                and "name" in json_schema
                and "schema" in json_schema
            ):
                schema_wrapper = json_schema
            else:
                schema_name = (
                    json_schema.get("name")
                    if isinstance(json_schema, dict) and "name" in json_schema
                    else "enrichment"
                )
                schema_wrapper = {
                    "name": schema_name,
                    "schema": json_schema,
                }
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": schema_wrapper,
            }
        try:
            res = await asyncio.wait_for(
                env.AI.run(self.text_model, payload),
                timeout=timeout_s,
            )
            out = _extract_text_response(
                res, model=self.text_model, action="generation"
            )
            if out is not None and json_schema is not None:
                out = clean_json_text(out)
            return out
        except Exception as exc:
            if isinstance(exc, WorkersAIError):
                raise
            _log_ai_error("generation", self.text_model, exc)
            raise WorkersAIRetryableError(
                f"Workers AI generation failed: {type(exc).__name__}",
                model=self.text_model,
                cause=exc,
            ) from exc
