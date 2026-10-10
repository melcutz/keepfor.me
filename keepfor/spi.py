# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Extension seams.

Core ships defaults (see keepfor.defaults); hosted deployments replace them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from fastapi import Request

    from keepfor.models.db import Database

CORE_API_VERSION = 1  # also exported from keepfor/__init__.py

Action = Literal[
    "save_item",
    "import",
    "summarize",
    "embed",
    "ask",
    "digest",
    "fetch",
    "reader_proxy",
    "mcp_call",
    "export",
]


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str  # == user_id in personal accounts (assumption A4)
    email: str
    role: str = "user"  # core self-host: first user is "admin"; cloud: always "user"
    plan: str = "self_hosted"
    scopes: frozenset[str] = frozenset({"*"})
    auth_kind: Literal["session", "pat", "other"] = "session"

    def to_user_dict(self) -> dict[str, Any]:
        """Shape existing route code expects: {"id","email","role", ...}."""
        return {
            "id": self.user_id,
            "email": self.email,
            "role": self.role,
            "tenant_id": self.tenant_id,
            "plan": self.plan,
            "scopes": sorted(self.scopes),
        }


@dataclass(frozen=True)
class TenantScope:
    tenant_id: str
    user_id: str
    db: Database  # data-plane database for this tenant (a shard in cloud)
    bucket: Any | None = None  # R2 binding or None
    index: Any | None = None  # Vectorize binding or None
    blob_prefix: str = ""  # "" in core; "t/<tenant_id>/" for new cloud tenants
    vector_namespace: str | None = None  # None in core; tenant id in cloud

    def blobs(self, env: Any = None) -> Any:
        from keepfor.storage.blobs import BlobStore

        bucket = (
            self.bucket
            if self.bucket is not None
            else (getattr(env, "BUCKET", None) if env else None)
        )
        return BlobStore(bucket, self.blob_prefix)

    def vectors(self, env: Any = None) -> Any:
        from keepfor.search.vectors import VectorIndex

        index = (
            self.index
            if self.index is not None
            else (getattr(env, "VECTORIZE", None) if env else None)
        )
        return VectorIndex(index, self.vector_namespace)

    def blob_key(self, *parts: str) -> str:
        clean_parts = [p.strip("/") for p in parts if p.strip("/")]
        key_suffix = "/".join(clean_parts)
        if self.blob_prefix:
            prefix = (
                self.blob_prefix
                if self.blob_prefix.endswith("/")
                else f"{self.blob_prefix}/"
            )
            return f"{prefix}{key_suffix}"
        return key_suffix


@runtime_checkable
class AuthProvider(Protocol):
    async def authenticate(self, request: Request) -> Principal | None: ...


@runtime_checkable
class ScopeProvider(Protocol):
    async def scope_for_principal(
        self, principal: Principal, env: Any
    ) -> TenantScope: ...

    async def scope_for_tenant(
        self, tenant_id: str, env: Any
    ) -> TenantScope: ...  # queue consumers, jobs


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""  # "", "limit", "read_only", "suspended", "unverified"
    upgrade_url: str | None = None


@runtime_checkable
class Entitlements(Protocol):
    async def check(self, tenant_id: str, action: Action, qty: int = 1) -> Decision: ...

    async def record(self, tenant_id: str, action: Action, qty: int = 1) -> None: ...


class EntitlementDenied(Exception):  # noqa: N818
    def __init__(self, decision: Decision):
        self.decision = decision
        super().__init__(f"Entitlement denied: {decision.reason}")


@runtime_checkable
class FetchBudget(Protocol):
    async def allow_fetch(self, tenant_id: str | None, url: str) -> bool: ...

    async def record_fetch(self, tenant_id: str | None, bytes_read: int) -> None: ...


@runtime_checkable
class EventBus(Protocol):
    async def publish(self, event: str, payload: dict[str, Any]) -> None: ...

    # Events published by core:
    # "item.saved", "item.extracted", "item.failed", "item.deleted"
    # payload keys: tenant_id, user_id, item_id (never article text)


@runtime_checkable
class AIProvider(Protocol):
    async def embed(
        self, env: Any, texts: list[str]
    ) -> list[list[float]]: ...  # 768 dims

    async def summarize(
        self, env: Any, text: str, *, max_words: int = 40
    ) -> str | None: ...

    async def generate(
        self,
        env: Any,
        system: str,
        user: str,
        *,
        max_tokens: int = 400,
        json_schema: dict | None = None,
        timeout_s: float = 20.0,
    ) -> str | None: ...


@dataclass
class Providers:
    auth: AuthProvider | None = (
        None  # None means keepfor.defaults.SessionPatAuthProvider
    )
    scope: ScopeProvider | None = (
        None  # None means keepfor.defaults.SingleTenantScopeProvider
    )
    entitlements: Entitlements | None = None  # None means AllowAllEntitlements
    fetch_budget: FetchBudget | None = None  # None means NoopFetchBudget
    events: EventBus | None = None  # None means NoopEventBus
    ai: AIProvider | None = None  # None means WorkersAIProvider

    def resolved(self) -> Providers:
        """Returns a copy with defaults filled in."""
        from keepfor import defaults

        return Providers(
            auth=(
                self.auth
                if self.auth is not None
                else defaults.SessionPatAuthProvider()
            ),
            scope=(
                self.scope
                if self.scope is not None
                else defaults.SingleTenantScopeProvider()
            ),
            entitlements=(
                self.entitlements
                if self.entitlements is not None
                else defaults.AllowAllEntitlements()
            ),
            fetch_budget=(
                self.fetch_budget
                if self.fetch_budget is not None
                else defaults.NoopFetchBudget()
            ),
            events=(
                self.events if self.events is not None else defaults.NoopEventBus()
            ),
            ai=(self.ai if self.ai is not None else defaults.WorkersAIProvider()),
        )
