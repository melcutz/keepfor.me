# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for the MCP get_account tool and the free-call hook (keepfor-cloud#173).

The tool reports the plan, the trial end and the quota of every metered
action. The call itself is free: it never consumes an `mcp_call` unit.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from keepfor.app import create_app
from keepfor.defaults import AllowAllEntitlements
from keepfor.mcp.server import MCP_TOOLS_BY_NAME, process_mcp_request
from keepfor.models.db import Database
from keepfor.spi import (
    Action,
    Decision,
    Principal,
    Providers,
    TenantScope,
    self_hosted_account,
)


class RecordingEntitlements:
    """Records the entitlement traffic and serves a per-tenant quota report."""

    def __init__(
        self,
        summaries: dict[str, dict[str, Any]] | None = None,
        deny_actions: set[str] | None = None,
        decision: Decision | None = None,
    ):
        self.summaries = summaries or {}
        self.deny_actions = deny_actions or set()
        self.decision = decision
        self.checks: list[tuple[str, str, int]] = []
        self.records: list[tuple[str, str, int]] = []

    async def check(self, tenant_id: str, action: Action, qty: int = 1) -> Decision:
        self.checks.append((tenant_id, action, qty))
        if action in self.deny_actions:
            return self.decision or Decision(allowed=False, reason="limit")
        return Decision(allowed=True)

    async def record(self, tenant_id: str, action: Action, qty: int = 1) -> None:
        self.records.append((tenant_id, action, qty))

    async def usage_summary(self, tenant_id: str) -> dict[str, Any]:
        return self.summaries.get(tenant_id, self_hosted_account())


class LegacyEntitlements:
    """A hosted provider that predates the usage_summary hook."""

    def __init__(self):
        self.checks: list[tuple[str, str, int]] = []

    async def check(self, tenant_id: str, action: Action, qty: int = 1) -> Decision:
        self.checks.append((tenant_id, action, qty))
        return Decision(allowed=True)

    async def record(self, tenant_id: str, action: Action, qty: int = 1) -> None:
        pass


class StubAuth:
    """Authenticates every request as the same principal."""

    def __init__(self, principal: Principal):
        self.principal = principal

    async def authenticate(self, request: Request) -> Principal | None:
        return self.principal


class StubScope:
    """Resolves every principal to the same tenant scope."""

    def __init__(self, db: Database, tenant_id: str, user_id: str):
        self.db = db
        self.tenant_id = tenant_id
        self.user_id = user_id

    async def scope_for_principal(self, principal: Principal, env: Any) -> TenantScope:
        return TenantScope(tenant_id=self.tenant_id, user_id=self.user_id, db=self.db)

    async def scope_for_tenant(self, tenant_id: str, env: Any) -> TenantScope:
        return TenantScope(tenant_id=tenant_id, user_id=tenant_id, db=self.db)


def _client(db: Database, entitlements: Any) -> TestClient:
    """A client whose app runs on the given entitlements provider."""
    principal = Principal(
        user_id="user-1", tenant_id="tenant-1", email="user-1@example.com"
    )
    providers = Providers(
        auth=StubAuth(principal),
        scope=StubScope(db, tenant_id="tenant-1", user_id="user-1"),
        entitlements=entitlements,
    )
    return TestClient(create_app(providers=providers))


def _call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """A tools/call request body."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


async def _account(
    db: Database,
    providers: Providers,
    scope: TenantScope,
    user_id: str,
) -> dict[str, Any]:
    """Calls get_account and returns the unwrapped payload."""
    res = await process_mcp_request(
        _call("get_account"),
        db,
        None,
        {"id": user_id},
        scope=scope,
        providers=providers,
    )
    assert "error" not in res, res["error"]
    return json.loads(res["result"]["content"][0]["text"])


# ==========================================
# The SPI hook
# ==========================================


@pytest.mark.asyncio
async def test_default_provider_reports_the_self_hosted_account():
    """The self-hosted default has no limits and no trial."""
    summary = await AllowAllEntitlements().usage_summary("tenant-1")
    assert summary == {"plan": "self-hosted", "trial_ends_at": None, "metrics": {}}


@pytest.mark.asyncio
async def test_self_hosted_account_is_a_fresh_dict():
    """The shape is never shared: a caller cannot mutate the next one."""
    first = self_hosted_account()
    first["metrics"]["mcp_call"] = {"limit": 1}
    assert self_hosted_account()["metrics"] == {}


# ==========================================
# The tool
# ==========================================


def test_get_account_is_a_read_only_tool():
    """The tool reads the quota, it never changes anything."""
    tool = MCP_TOOLS_BY_NAME["get_account"]
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["annotations"]["destructiveHint"] is False
    assert tool["inputSchema"]["properties"] == {}


@pytest.mark.asyncio
async def test_get_account_returns_the_provider_summary(db: Database):
    """The provider's report is what the agent gets."""
    entitlements = RecordingEntitlements(
        summaries={
            "tenant-1": {
                "plan": "trial",
                "trial_ends_at": "2026-11-01T00:00:00Z",
                "metrics": {
                    "mcp_call": {
                        "limit": 100,
                        "used": 12,
                        "remaining": 88,
                        "resets_at": "2026-11-01T00:00:00Z",
                    }
                },
            }
        }
    )
    providers = Providers(entitlements=entitlements).resolved()
    scope = TenantScope(tenant_id="tenant-1", user_id="user-1", db=db)

    account = await _account(db, providers, scope, "user-1")
    assert account["plan"] == "trial"
    assert account["trial_ends_at"] == "2026-11-01T00:00:00Z"
    assert account["metrics"]["mcp_call"]["remaining"] == 88


@pytest.mark.asyncio
async def test_get_account_falls_back_to_the_self_hosted_shape(db: Database):
    """A provider without the hook reports the self-hosted account."""
    providers = Providers(entitlements=LegacyEntitlements()).resolved()
    scope = TenantScope(tenant_id="tenant-1", user_id="user-1", db=db)

    account = await _account(db, providers, scope, "user-1")
    assert account == {"plan": "self-hosted", "trial_ends_at": None, "metrics": {}}


@pytest.mark.asyncio
async def test_get_account_is_tenant_scoped(db: Database):
    """User B's report never shows user A's quota."""
    entitlements = RecordingEntitlements(
        summaries={
            "tenant-a": {
                "plan": "pro",
                "trial_ends_at": None,
                "metrics": {"mcp_call": {"limit": 2000, "used": 1, "remaining": 1999}},
                "secret": "tenant-a-secret",
            },
            "tenant-b": {
                "plan": "trial",
                "trial_ends_at": None,
                "metrics": {"mcp_call": {"limit": 100, "used": 0, "remaining": 100}},
            },
        }
    )
    providers = Providers(entitlements=entitlements).resolved()
    scope_a = TenantScope(tenant_id="tenant-a", user_id="user-a", db=db)
    scope_b = TenantScope(tenant_id="tenant-b", user_id="user-b", db=db)

    account_a = await _account(db, providers, scope_a, "user-a")
    account_b = await _account(db, providers, scope_b, "user-b")

    assert account_a["plan"] == "pro"
    assert account_b["plan"] == "trial"
    assert "tenant-a-secret" not in json.dumps(account_b)
    assert account_b["metrics"]["mcp_call"]["limit"] == 100


@pytest.mark.asyncio
async def test_get_account_writes_nothing(db: Database):
    """The tool only reads the quota: no row is created."""
    before = await db.query_all("SELECT * FROM items;")
    providers = Providers(entitlements=RecordingEntitlements()).resolved()
    scope = TenantScope(tenant_id="tenant-1", user_id="user-1", db=db)

    await _account(db, providers, scope, "user-1")
    assert await db.query_all("SELECT * FROM items;") == before


# ==========================================
# The call is free
# ==========================================


def test_get_account_does_not_consume_an_mcp_call(db: Database):
    """Over HTTP: the free call neither checks nor records a unit."""
    entitlements = RecordingEntitlements()
    client = _client(db, entitlements)

    res = client.post("/api/mcp", json=_call("get_account"))
    assert res.status_code == 200
    assert "error" not in res.json(), res.json()
    assert entitlements.checks == []
    assert entitlements.records == []


def test_get_account_survives_a_denied_mcp_call(db: Database):
    """The quota report stays readable when the quota is exhausted."""
    entitlements = RecordingEntitlements(deny_actions={"mcp_call"})
    client = _client(db, entitlements)

    res = client.post("/api/mcp", json=_call("get_account"))
    assert res.status_code == 200
    assert "error" not in res.json(), res.json()
    account = json.loads(res.json()["result"]["content"][0]["text"])
    assert account["plan"] == "self-hosted"

    # Every other call is still refused: the free call is the exception.
    res = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert res.json()["error"]["code"] == -32001


def test_another_tool_still_costs_an_mcp_call(db: Database):
    """Only get_account is free."""
    entitlements = RecordingEntitlements()
    client = _client(db, entitlements)

    res = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
    )
    assert res.status_code == 200
    assert entitlements.checks == [("tenant-1", "mcp_call", 1)]
    assert entitlements.records == [("tenant-1", "mcp_call", 1)]


# ==========================================
# Limit errors carry the quota
# ==========================================


def test_limit_error_reports_remaining_and_resets_at(db: Database):
    """The refused call says what is left and when it resets."""
    entitlements = RecordingEntitlements(
        deny_actions={"mcp_call"},
        decision=Decision(
            allowed=False,
            reason="limit",
            remaining=7,
            resets_at="2026-11-01T00:00:00Z",
        ),
    )
    client = _client(db, entitlements)

    res = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    error = res.json()["error"]
    assert error["code"] == -32001
    assert error["message"] == "limit"
    assert error["data"] == {
        "remaining": 7,
        "resets_at": "2026-11-01T00:00:00Z",
    }


def test_limit_error_without_a_quota_keeps_the_old_payload(db: Database):
    """A provider that reports no quota gets the payload clients know."""
    entitlements = RecordingEntitlements(deny_actions={"mcp_call"})
    client = _client(db, entitlements)

    res = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    error = res.json()["error"]
    assert error == {"code": -32001, "message": "limit"}
