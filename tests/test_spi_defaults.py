# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for provider seams (spi.py) and default implementations."""

import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

import keepfor
from keepfor.auth.service import (
    create_pat,
    login_user,
    register_user,
    validate_pat,
    validate_session,
)
from keepfor.defaults import (
    AllowAllEntitlements,
    NoopEventBus,
    NoopFetchBudget,
    SessionPatAuthProvider,
    SingleTenantScopeProvider,
    WorkersAIProvider,
    default_scope,
)
from keepfor.models.db import Database
from keepfor.runtime import get_providers, reset_providers, set_providers
from keepfor.spi import (
    CORE_API_VERSION,
    AIProvider,
    AuthProvider,
    Decision,
    Entitlements,
    EventBus,
    FetchBudget,
    Principal,
    Providers,
    ScopeProvider,
    TenantScope,
)
from tests.fakes import FakeAI, FakeEnv, FakeR2Bucket, FakeVectorize


def test_spi_isolated_import():
    """Verify that importing keepfor.spi does NOT import keepfor.app."""
    code = (
        "import sys; import keepfor.spi; "
        "assert 'keepfor.app' not in sys.modules, 'keepfor.app was imported!'; "
        "print('OK')"
    )
    cmd = [sys.executable, "-c", code]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert res.returncode == 0, f"Import test failed: {res.stderr}"
    assert "OK" in res.stdout


def test_core_api_version():
    """Verify CORE_API_VERSION constant matches across exports."""
    assert CORE_API_VERSION == 1
    assert keepfor.CORE_API_VERSION == 1


def test_protocol_conformance():
    """Verify that default implementations satisfy runtime protocols."""
    assert isinstance(SessionPatAuthProvider(), AuthProvider)
    assert isinstance(SingleTenantScopeProvider(), ScopeProvider)
    assert isinstance(AllowAllEntitlements(), Entitlements)
    assert isinstance(NoopEventBus(), EventBus)
    assert isinstance(NoopFetchBudget(), FetchBudget)
    assert isinstance(WorkersAIProvider(), AIProvider)


class DummyRequest:
    """Minimal fake request for testing auth provider."""

    def __init__(
        self,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        env: Any = None,
        db: Database | None = None,
    ) -> None:
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.scope = {"env": env}
        self.state = MagicMock()
        self.state.db = db


@pytest.mark.asyncio
async def test_default_auth_pat(db: Database):
    """Verify default auth with Bearer PAT matches validate_pat."""
    user = await register_user(db, "patuser@example.com", "validpassword123")
    assert user is not None
    pat_info = await create_pat(db, user["id"], "test-token")
    token = pat_info["token"]

    auth = SessionPatAuthProvider(db=db)

    # Valid PAT
    req = DummyRequest(headers={"Authorization": f"Bearer {token}"})
    principal = await auth.authenticate(req)
    assert principal is not None
    assert principal.auth_kind == "pat"
    assert principal.user_id == user["id"]
    assert principal.tenant_id == user["id"]
    assert principal.email == "patuser@example.com"

    validated = await validate_pat(db, token)
    assert validated is not None
    assert principal.user_id == validated["id"]
    assert principal.email == validated["email"]
    assert principal.role == validated["role"]
    assert principal.to_user_dict() == {
        "id": user["id"],
        "email": user["email"],
        "role": user["role"],
        "tenant_id": user["id"],
        "plan": "self_hosted",
        "scopes": ["*"],
    }

    # Invalid PAT
    bad_req = DummyRequest(headers={"Authorization": "Bearer bad-token"})
    assert await auth.authenticate(bad_req) is None


@pytest.mark.asyncio
async def test_default_auth_session(db: Database):
    """Verify default auth with session cookies matches validate_session."""
    user = await register_user(db, "sessionuser@example.com", "validpassword123")
    assert user is not None
    _, session_id = await login_user(db, "sessionuser@example.com", "validpassword123")
    assert session_id is not None

    auth = SessionPatAuthProvider(db=db)

    # Test kfm_session cookie
    req_kfm = DummyRequest(cookies={"kfm_session": session_id})
    principal_kfm = await auth.authenticate(req_kfm)
    assert principal_kfm is not None
    assert principal_kfm.auth_kind == "session"
    assert principal_kfm.user_id == user["id"]

    validated = await validate_session(db, session_id)
    assert validated is not None
    assert principal_kfm.user_id == validated["id"]
    assert principal_kfm.email == validated["email"]

    # Test rk_session cookie
    req_rk = DummyRequest(cookies={"rk_session": session_id})
    principal_rk = await auth.authenticate(req_rk)
    assert principal_rk is not None
    assert principal_rk.auth_kind == "session"
    assert principal_rk.user_id == user["id"]

    # Test garbage session cookie
    req_bad = DummyRequest(cookies={"kfm_session": "not-a-valid-session"})
    assert await auth.authenticate(req_bad) is None

    # Test no credentials
    assert await auth.authenticate(DummyRequest()) is None


@pytest.mark.asyncio
async def test_default_scope(db: Database):
    """Verify SingleTenantScopeProvider and default_scope."""
    bucket = FakeR2Bucket()
    vectorize = FakeVectorize()
    env = FakeEnv(d1=db.d1, r2=bucket, vectorize=vectorize)

    provider = SingleTenantScopeProvider()
    principal = Principal(
        user_id="u123",
        tenant_id="u123",
        email="u123@example.com",
    )

    scope = await provider.scope_for_principal(principal, env)
    assert scope.tenant_id == "u123"
    assert scope.user_id == "u123"
    assert scope.blob_prefix == ""
    assert scope.vector_namespace is None
    assert scope.blob_key("items", "x", "clean.html") == "items/x/clean.html"

    tenant_scope = await provider.scope_for_tenant("t456", env)
    assert tenant_scope.tenant_id == "t456"
    assert tenant_scope.user_id == "t456"
    assert tenant_scope.blob_prefix == ""
    assert tenant_scope.vector_namespace is None
    assert tenant_scope.blob_key("items", "x", "clean.html") == "items/x/clean.html"

    # default_scope helper
    ds = default_scope(db, env, "u123")
    assert ds.tenant_id == "u123"
    assert ds.user_id == "u123"
    assert ds.blob_key("items", "x", "clean.html") == "items/x/clean.html"

    # With prefix
    prefixed_scope = TenantScope(
        tenant_id="t1",
        user_id="u1",
        db=db,
        blob_prefix="t/t1/",
    )
    assert (
        prefixed_scope.blob_key("items", "x", "clean.html") == "t/t1/items/x/clean.html"
    )


@pytest.mark.asyncio
async def test_allow_all_entitlements():
    """Verify AllowAllEntitlements allows all actions."""
    ent = AllowAllEntitlements()
    decision = await ent.check("tenant-1", "save_item")
    assert isinstance(decision, Decision)
    assert decision.allowed is True
    # Recording action should be a no-op
    await ent.record("tenant-1", "save_item", qty=5)


@pytest.mark.asyncio
async def test_noop_providers():
    """Verify NoopEventBus and NoopFetchBudget."""
    bus = NoopEventBus()
    await bus.publish(
        "item.saved",
        {"tenant_id": "t1", "user_id": "u1", "item_id": "i1"},
    )

    budget = NoopFetchBudget()
    assert await budget.allow_fetch("t1", "https://example.com") is True
    await budget.record_fetch("t1", 1024)


@pytest.mark.asyncio
async def test_workers_ai_provider():
    """Verify WorkersAIProvider with FakeAI."""
    ai = FakeAI()
    env = FakeEnv(ai=ai)
    provider = WorkersAIProvider()

    # Embed
    texts = ["hello world", "test embedding"]
    embeddings = await provider.embed(env, texts)
    assert len(embeddings) == 2
    assert len(embeddings[0]) == 768

    # Summarize
    sentence = "Python is an interpreted high-level programming language. "
    long_text = sentence * 10
    summary = await provider.summarize(env, long_text, max_words=30)
    assert summary is not None
    assert len(summary) > 0

    # Generate
    gen = await provider.generate(
        env,
        system="You are a helpful assistant",
        user="Summarize in 5 words",
    )
    assert gen is not None

    # Fallback with None env
    assert await provider.embed(None, ["test"]) == []
    assert await provider.summarize(None, "test") is None
    assert await provider.generate(None, "sys", "usr") is None


def test_runtime_providers_management():
    """Verify runtime get_providers, set_providers, and reset_providers."""
    reset_providers()

    p = get_providers()
    assert isinstance(p, Providers)
    assert isinstance(p.auth, AuthProvider)
    assert isinstance(p.scope, ScopeProvider)
    assert isinstance(p.entitlements, Entitlements)
    assert isinstance(p.fetch_budget, FetchBudget)
    assert isinstance(p.events, EventBus)
    assert isinstance(p.ai, AIProvider)

    # Custom providers
    custom_auth = SessionPatAuthProvider()
    custom_p = Providers(auth=custom_auth)
    set_providers(custom_p)
    current = get_providers()
    assert current.auth is custom_auth
    assert isinstance(current.scope, ScopeProvider)

    reset_providers()
