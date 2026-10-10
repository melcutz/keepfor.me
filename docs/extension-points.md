# Extension Points & Provider SPI Guide

Keepfor.me 1.1.0 exposes a clean Service Provider Interface (SPI) allowing hosted, enterprise, or multi-tenant deployments to extend and customize core behavior while preserving full compatibility with upstream updates.

Core defines and exports all SPI abstractions in [`keepfor.spi`](file:///home/melcutz/work/keepfor.me/keepfor/spi.py). Default single-tenant implementations live in [`keepfor.defaults`](file:///home/melcutz/work/keepfor.me/keepfor/defaults.py).

---

## Stability Contract: `CORE_API_VERSION`

The SPI protocol definitions and signatures are versioned by:

```python
from keepfor.spi import CORE_API_VERSION  # Currently: 1
```

- **Stability Guarantee**: Within a major `CORE_API_VERSION`, protocol signatures and contracts will not be removed or broken.
- Any breaking change to names, types, or required method arguments requires incrementing `CORE_API_VERSION`.

---

## 1. Authentication: `AuthProvider`

The `AuthProvider` protocol intercepts each incoming HTTP request to identify the authenticated `Principal`.

### Protocol Definition

```python
@runtime_checkable
class AuthProvider(Protocol):
    async def authenticate(self, request: Request) -> Principal | None: ...
```

### `Principal`

```python
@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    email: str
    role: str = "user"
    plan: str = "self_hosted"
    scopes: frozenset[str] = frozenset({"*"})
    auth_kind: Literal["session", "pat", "other"] = "session"
```

### Example Custom Implementation

```python
from fastapi import Request
from keepfor.spi import AuthProvider, Principal

class HeaderAuthProvider(AuthProvider):
    """Authenticate via proxy-injected headers (e.g. Cloudflare Access, Tailscale)."""

    async def authenticate(self, request: Request) -> Principal | None:
        user_id = request.headers.get("X-Remote-User")
        email = request.headers.get("X-Remote-Email")
        if not user_id or not email:
            return None
        return Principal(
            user_id=user_id,
            tenant_id=user_id,
            email=email,
            role="user",
            auth_kind="other",
        )
```

---

## 2. Multi-Tenancy: `ScopeProvider` & `TenantScope`

The `ScopeProvider` resolves storage, database, and index partitioning for a principal or background tenant task.

### Protocol Definition

```python
@runtime_checkable
class ScopeProvider(Protocol):
    async def scope_for_principal(self, principal: Principal, env: Any) -> TenantScope: ...
    async def scope_for_tenant(self, tenant_id: str, env: Any) -> TenantScope: ...
```

### `TenantScope`

```python
@dataclass(frozen=True)
class TenantScope:
    tenant_id: str
    user_id: str
    db: Database
    bucket: Any | None = None
    index: Any | None = None
    blob_prefix: str = ""
    vector_namespace: str | None = None

    def blobs(self, env: Any = None) -> BlobStore: ...
    def vectors(self, env: Any = None) -> VectorIndex: ...
```

### Example Custom Implementation

```python
from keepfor.spi import Principal, ScopeProvider, TenantScope
from keepfor.models.db import Database

class ShardedScopeProvider(ScopeProvider):
    def __init__(self, cluster_router):
        self.router = cluster_router

    async def scope_for_principal(self, principal: Principal, env: Any) -> TenantScope:
        db_shard = await self.router.get_db_for_tenant(principal.tenant_id, env)
        return TenantScope(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            db=Database(db_shard),
            bucket=getattr(env, "R2_BUCKET", None),
            index=getattr(env, "VECTORIZE", None),
            blob_prefix=f"t/{principal.tenant_id}/",
            vector_namespace=principal.tenant_id,
        )

    async def scope_for_tenant(self, tenant_id: str, env: Any) -> TenantScope:
        db_shard = await self.router.get_db_for_tenant(tenant_id, env)
        return TenantScope(
            tenant_id=tenant_id,
            user_id=tenant_id,
            db=Database(db_shard),
            bucket=getattr(env, "R2_BUCKET", None),
            index=getattr(env, "VECTORIZE", None),
            blob_prefix=f"t/{tenant_id}/",
            vector_namespace=tenant_id,
        )
```

---

## 3. Entitlements & Metering: `Entitlements`

Controls rate limits, feature gating, and usage quotas per tenant.

### Protocol Definition

```python
@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""  # "", "limit", "read_only", "suspended", "unverified"
    upgrade_url: str | None = None

@runtime_checkable
class Entitlements(Protocol):
    async def check(self, tenant_id: str, action: Action, qty: int = 1) -> Decision: ...
    async def record(self, tenant_id: str, action: Action, qty: int = 1) -> None: ...
```

Supported `Action` values: `"save_item"`, `"import"`, `"summarize"`, `"embed"`, `"ask"`, `"digest"`, `"fetch"`, `"reader_proxy"`, `"mcp_call"`, `"export"`.

### Example Custom Implementation

```python
from keepfor.spi import Action, Decision, Entitlements

class TieredEntitlements(Entitlements):
    def __init__(self, redis_client):
        self.redis = redis_client

    async def check(self, tenant_id: str, action: Action, qty: int = 1) -> Decision:
        if action == "summarize":
            count = await self.redis.get(f"usage:{tenant_id}:summarize") or 0
            if int(count) + qty > 100:
                return Decision(
                    allowed=False,
                    reason="limit",
                    upgrade_url="https://cloud.example.com/billing",
                )
        return Decision(allowed=True)

    async def record(self, tenant_id: str, action: Action, qty: int = 1) -> None:
        await self.redis.incrby(f"usage:{tenant_id}:{action}", qty)
```

---

## 4. Egress Quotas: `FetchBudget`

Tracks and limits outbound network fetches and reader proxy calls.

### Protocol Definition

```python
@runtime_checkable
class FetchBudget(Protocol):
    async def allow_fetch(
        self, tenant_id: str | None, url: str, *, action: Action = "fetch"
    ) -> bool: ...
    async def record_fetch(
        self, tenant_id: str | None, bytes_read: int, *, action: Action = "fetch"
    ) -> None: ...
```

### Example Custom Implementation

```python
from keepfor.spi import Action, FetchBudget

class DailyFetchBudget(FetchBudget):
    def __init__(self, max_bytes_per_day: int = 100 * 1024 * 1024):
        self.max_bytes = max_bytes_per_day
        self.usage: dict[str, int] = {}

    async def allow_fetch(
        self, tenant_id: str | None, url: str, *, action: Action = "fetch"
    ) -> bool:
        if not tenant_id:
            return True
        return self.usage.get(tenant_id, 0) < self.max_bytes

    async def record_fetch(
        self, tenant_id: str | None, bytes_read: int, *, action: Action = "fetch"
    ) -> None:
        if tenant_id:
            self.usage[tenant_id] = self.usage.get(tenant_id, 0) + bytes_read
```

---

## 5. Lifecycle Events: `EventBus`

Asynchronously dispatches domain events across the system without coupling business logic.

### Protocol Definition

```python
@runtime_checkable
class EventBus(Protocol):
    async def publish(self, event: str, payload: dict[str, Any]) -> None: ...
```

Published events:
- `item.saved`: new item inserted into database
- `item.extracted`: article extraction succeeded, snapshots stored
- `item.failed`: extraction failed (with failure reason)
- `item.deleted`: item deleted from database and storage

Payload structure guarantees:
- Always contains `tenant_id`, `user_id`, and `item_id`.
- Never contains raw article text or private user secrets.

### Example Custom Implementation

```python
import json
from keepfor.spi import EventBus

class WebhookEventBus(EventBus):
    def __init__(self, webhook_client):
        self.client = webhook_client

    async def publish(self, event: str, payload: dict[str, Any]) -> None:
        await self.client.post(
            f"https://events.internal/topics/{event}",
            json=payload,
        )
```

---

## 6. AI Inference: `AIProvider`

Abstracts embedding generation, summarization, and structured generation.

### Protocol Definition

```python
@runtime_checkable
class AIProvider(Protocol):
    async def embed(self, env: Any, texts: list[str]) -> list[list[float]]: ...
    async def summarize(self, env: Any, text: str, *, max_words: int = 40) -> str | None: ...
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
```

### Example Custom Implementation (OpenAI / Self-Hosted LLM)

```python
from typing import Any
import httpx
from keepfor.spi import AIProvider

class OpenAIAIProvider(AIProvider):
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def embed(self, env: Any, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": "text-embedding-3-small", "input": texts, "dimensions": 768},
            )
            data = res.json()
            return [row["embedding"] for row in data["data"]]

    async def summarize(self, env: Any, text: str, *, max_words: int = 40) -> str | None:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": f"Summarize in under {max_words} words."},
                        {"role": "user", "content": text[:3000]},
                    ],
                },
            )
            return res.json()["choices"][0]["message"]["content"]

    async def generate(
        self, env: Any, system: str, user: str, *, max_tokens: int = 400,
        json_schema: dict | None = None, timeout_s: float = 20.0
    ) -> str | None:
        # Implementation for custom generation
        return None
```

---

## 7. App Factory & Template Customization

Instantiate custom applications via `create_app()`:

```python
from fastapi import APIRouter
from keepfor.app import create_app
from keepfor.spi import Providers

app = create_app(
    providers=Providers(
        auth=MyCustomAuth(),
        scope=MyCustomScope(),
        entitlements=MyCustomEntitlements(),
    ),
    extra_routers=[billing_router, admin_router],
    template_dirs=["custom_templates/"],
    template_globals={"company_name": "Acme Read"},
    title="Acme Read-It-Later",
)
```

### Template Extension Blocks

Jinja2 templates expose extension blocks that child templates can override:

- `base.html`:
  - `{% block head_extra %}`: inject custom styles, scripts, or meta tags into `<head>`
  - `{% block banner %}`: system-wide notification or announcement banner
  - `{% block nav_extra %}`: additional links in navigation bar / sidebar
  - `{% block footer_extra %}`: custom footer elements
- `settings.html`:
  - `{% block settings_extra %}`: tenant billing, team management, or enterprise settings panels
