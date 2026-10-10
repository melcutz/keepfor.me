# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Fixtures, test topology, and snapshot harness for tenant-isolation testing."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from keepfor.app import create_app
from keepfor.auth.service import (
    create_pat,
    login_user,
    register_user,
    validate_pat,
    validate_session,
)
from keepfor.models.db import Database
from keepfor.models.items import (
    add_suggestions,
    create_rule,
    record_open,
    save_item,
    toggle_pin_item,
    update_user_notes,
)
from keepfor.paths import migrations_dir
from keepfor.spi import Principal, Providers, TenantScope
from tests.fakes import FakeAI, FakeEnv, FakeQueue, FakeR2Bucket, FakeVectorize


@dataclass
class TenantFixture:
    user: dict[str, Any]
    db: Database
    scope: TenantScope
    session_id: str
    pat_token: str
    pat_id: str
    item_ids: list[str]
    html_item_id: str
    vector_item_id: str
    pinned_item_id: str
    tag_names: list[str]
    rule_id: str
    suggestion_id: str
    auth_headers: dict[str, str]
    cookies: dict[str, str]
    unique_keyword: str
    vector_val: float


class TestMultiTenantAuthProvider:
    """Authenticates requests across one or more tenant databases."""

    def __init__(self, tenant_dbs: dict[str, Database]):
        self.tenant_dbs = tenant_dbs

    async def authenticate(self, request: Request) -> Principal | None:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            for db in self.tenant_dbs.values():
                user = await validate_pat(db, token)
                if user:
                    return Principal(
                        user_id=user["id"],
                        tenant_id=user["id"],
                        email=user["email"],
                        role=user.get("role", "user"),
                        auth_kind="pat",
                    )

        session_id = request.cookies.get("kfm_session") or request.cookies.get(
            "rk_session"
        )
        if session_id:
            for db in self.tenant_dbs.values():
                user = await validate_session(db, session_id)
                if user:
                    return Principal(
                        user_id=user["id"],
                        tenant_id=user["id"],
                        email=user["email"],
                        role=user.get("role", "user"),
                        auth_kind="session",
                    )
        return None


class TestMultiTenantScopeProvider:
    """Routes principals to their respective TenantScope."""

    def __init__(self, scopes: dict[str, TenantScope]):
        self.scopes = scopes

    async def scope_for_principal(self, principal: Principal, env: Any) -> TenantScope:
        return self.scopes[principal.tenant_id]

    async def scope_for_tenant(self, tenant_id: str, env: Any) -> TenantScope:
        return self.scopes[tenant_id]


def _build_test_db() -> tuple[sqlite3.Connection, Database]:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    mig_dir = str(migrations_dir())
    schema_files = sorted(f for f in os.listdir(mig_dir) if f.endswith(".sql"))
    for name in schema_files:
        with open(os.path.join(mig_dir, name), "r", encoding="utf-8") as f:
            schema = "\n".join(
                line
                for line in f.read().splitlines()
                if not line.strip().startswith("--")
            )
            for stmt in schema.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
    conn.commit()
    return conn, Database(sqlite_conn=conn)


def _hash_rows(rows: list[dict[str, Any]]) -> str:
    dumped = json.dumps([dict(r) for r in rows], sort_keys=True, default=str)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def _hash_obj(obj: Any) -> str:
    dumped = json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


async def snapshot(
    db: Database,
    user_id: str,
    env: Any = None,
    scope: TenantScope | None = None,
) -> dict[str, str]:
    """Capture a cryptographic hash of all data belonging to tenant.

    Includes all 11 tenant tables from TENANT_TABLES, plus R2 blob keys and
    Vectorize vectors if env/scope is provided.
    Returns an empty dict if the tenant has no data anywhere.
    """
    res: dict[str, str] = {}

    # 1. items
    rows = await db.query_all(
        "SELECT * FROM items WHERE user_id = ? ORDER BY id;", (user_id,)
    )
    if rows:
        res["items"] = _hash_rows(rows)

    # 2. items_fts
    rows = await db.query_all(
        "SELECT * FROM items_fts WHERE user_id = ? ORDER BY item_id;", (user_id,)
    )
    if rows:
        res["items_fts"] = _hash_rows(rows)

    # 3. tags
    rows = await db.query_all(
        "SELECT * FROM tags WHERE user_id = ? ORDER BY id;", (user_id,)
    )
    if rows:
        res["tags"] = _hash_rows(rows)

    # 4. item_tags
    rows = await db.query_all(
        "SELECT it.* FROM item_tags it JOIN items i ON it.item_id = i.id "
        "WHERE i.user_id = ? ORDER BY it.item_id, it.tag_id;",
        (user_id,),
    )
    if rows:
        res["item_tags"] = _hash_rows(rows)

    # 5. chunks
    rows = await db.query_all(
        "SELECT * FROM chunks WHERE user_id = ? ORDER BY id;", (user_id,)
    )
    if rows:
        res["chunks"] = _hash_rows(rows)

    # 6. suggested_tags
    rows = await db.query_all(
        "SELECT * FROM suggested_tags WHERE user_id = ? ORDER BY id;", (user_id,)
    )
    if rows:
        res["suggested_tags"] = _hash_rows(rows)

    # 7. tag_rules
    rows = await db.query_all(
        "SELECT * FROM tag_rules WHERE user_id = ? ORDER BY id;", (user_id,)
    )
    if rows:
        res["tag_rules"] = _hash_rows(rows)

    # 8. rule_suggestion_dismissals
    rows = await db.query_all(
        "SELECT * FROM rule_suggestion_dismissals WHERE user_id = ? ORDER BY key;",
        (user_id,),
    )
    if rows:
        res["rule_suggestion_dismissals"] = _hash_rows(rows)

    # 9. item_opens
    rows = await db.query_all(
        "SELECT * FROM item_opens WHERE user_id = ? ORDER BY id;", (user_id,)
    )
    if rows:
        res["item_opens"] = _hash_rows(rows)

    # 10. sessions
    rows = await db.query_all(
        "SELECT * FROM sessions WHERE user_id = ? ORDER BY id;", (user_id,)
    )
    if rows:
        res["sessions"] = _hash_rows(rows)

    # 11. personal_access_tokens
    rows = await db.query_all(
        "SELECT * FROM personal_access_tokens WHERE user_id = ? ORDER BY id;",
        (user_id,),
    )
    if rows:
        res["personal_access_tokens"] = _hash_rows(rows)

    # 12. R2 objects
    bucket = getattr(env, "BUCKET", None) if env else (scope.bucket if scope else None)
    if bucket is not None and hasattr(bucket, "store"):
        r2_items = []
        blob_prefix = scope.blob_prefix if scope else ""
        for k, v in sorted(bucket.store.items()):
            if blob_prefix and k.startswith(blob_prefix):
                r2_items.append((k, hashlib.sha256(v).hexdigest()))
            elif not blob_prefix:
                user_item_ids = {
                    r["id"]
                    for r in (
                        await db.query_all(
                            "SELECT id FROM items WHERE user_id = ?", (user_id,)
                        )
                    )
                }
                if any(f"items/{iid}/" in k for iid in user_item_ids):
                    r2_items.append((k, hashlib.sha256(v).hexdigest()))
        if r2_items:
            res["_r2"] = _hash_obj(r2_items)

    # 13. Vectorize vectors
    vec = getattr(env, "VECTORIZE", None) if env else (scope.index if scope else None)
    if vec is not None and hasattr(vec, "store"):
        vec_items = []
        vec_ns = scope.vector_namespace if scope else None
        for (ns, vid), entry in sorted(
            vec.store.items(), key=lambda x: (str(x[0][0]), str(x[0][1]))
        ):
            if vec_ns is not None:
                if ns == vec_ns:
                    vec_items.append((vid, entry.get("values", [])))
            else:
                meta = entry.get("metadata", {})
                if meta.get("user_id") == user_id:
                    vec_items.append((vid, entry.get("values", [])))
        if vec_items:
            res["_vectorize"] = _hash_obj(vec_items)

    return res


async def _seed_single_tenant(
    db: Database,
    email: str,
    password: str,
    prefix: str,
    vector_namespace: str,
    fake_env: FakeEnv,
    unique_keyword: str,
    vector_val: float,
    seed_vectors: bool = True,
) -> TenantFixture:
    user = await register_user(db, email, password, allow_public_signups=True)
    user_id = user["id"]
    _, session_id = await login_user(db, email, password)
    pat = await create_pat(db, user_id, f"PAT for {email}")

    scope = TenantScope(
        tenant_id=user_id,
        user_id=user_id,
        db=db,
        bucket=fake_env.BUCKET,
        index=fake_env.VECTORIZE,
        blob_prefix=prefix,
        vector_namespace=vector_namespace,
    )

    # Item 1: with R2 clean html
    item1, _ = await save_item(
        db,
        fake_env,
        user_id,
        f"https://example.com/html-{user_id}",
        ["tag1", f"tag_{user_id[:6]}"],
        scope=scope,
    )
    await db.execute(
        "UPDATE items SET title = ? WHERE user_id = ? AND id = ?;",
        (f"Title {unique_keyword}", user_id, item1["id"]),
    )
    clean_html = (
        f"<html><body><p>Article for {email}: {unique_keyword}</p></body></html>"
    )
    blobs = scope.blobs(fake_env)
    await blobs.put_html(blobs.key("items", item1["id"], "clean.html"), clean_html)

    # Item 2: with vectors (if seeded)
    item2, _ = await save_item(
        db,
        fake_env,
        user_id,
        f"https://example.com/vec-{user_id}",
        ["tag1"],
        scope=scope,
    )
    if seed_vectors:
        chunk_id = f"chunk_{item2['id']}_0"
        await db.execute(
            "INSERT INTO chunks (id, item_id, user_id, chunk_index, token_count) "
            "VALUES (?, ?, ?, ?, ?);",
            (chunk_id, item2["id"], user_id, 0, 100),
        )
        vectors = scope.vectors(fake_env)
        await vectors.upsert(
            [
                {
                    "id": chunk_id,
                    "values": [vector_val] * 768,
                    "metadata": {
                        "item_id": item2["id"],
                        "user_id": user_id,
                        "title": f"Vector article for {email}",
                    },
                }
            ]
        )

    # Item 3: pinned with notes and opened
    item3, _ = await save_item(
        db,
        fake_env,
        user_id,
        f"https://example.com/pinned-{user_id}",
        ["tag2"],
        scope=scope,
    )
    await toggle_pin_item(db, user_id, item3["id"])
    await update_user_notes(db, user_id, item3["id"], f"Notes for {email}")
    await record_open(db, user_id, item3["id"])

    # 1 rule
    rule_id = await create_rule(
        db, user_id, "domain", f"example-{user_id}.com", "rule-tag"
    )

    # 1 suggestion
    await add_suggestions(
        db,
        user_id,
        item1["id"],
        [{"phrase": f"sugg-{user_id}", "score": 0.88}],
    )
    sugg_row = await db.query_first(
        "SELECT id FROM suggested_tags WHERE user_id = ? AND item_id = ?;",
        (user_id, item1["id"]),
    )
    sugg_id = sugg_row["id"] if sugg_row else ""

    return TenantFixture(
        user=user,
        db=db,
        scope=scope,
        session_id=session_id,
        pat_token=pat["token"],
        pat_id=pat["id"],
        item_ids=[item1["id"], item2["id"], item3["id"]],
        html_item_id=item1["id"],
        vector_item_id=item2["id"],
        pinned_item_id=item3["id"],
        tag_names=["tag1", "tag2"],
        rule_id=rule_id,
        suggestion_id=sugg_id,
        auth_headers={"Authorization": f"Bearer {pat['token']}"},
        cookies={"kfm_session": session_id},
        unique_keyword=unique_keyword,
        vector_val=vector_val,
    )


@dataclass
class IsolationHarness:
    tenant_a: TenantFixture
    tenant_b: TenantFixture
    app: FastAPI
    client: TestClient
    env: FakeEnv
    topology: str


@pytest.fixture(params=["shared_db", "separate_db"])
async def harness(request) -> IsolationHarness:
    topology = request.param

    fake_bucket = FakeR2Bucket()
    fake_vectorize = FakeVectorize()
    fake_ai = FakeAI()
    fake_queue = FakeQueue()

    if topology == "shared_db":
        _, shared_db = _build_test_db()
        fake_env = FakeEnv(
            db=shared_db,
            bucket=fake_bucket,
            vectorize=fake_vectorize,
            ai=fake_ai,
            queue=fake_queue,
        )
        tenant_a = await _seed_single_tenant(
            db=shared_db,
            email="tenant_a@example.com",
            password="password_a_123",
            prefix="t/user_a/",
            vector_namespace="user_a",
            fake_env=fake_env,
            unique_keyword="platypus_keyword_tenant_a",
            vector_val=0.42,
        )
        tenant_b = await _seed_single_tenant(
            db=shared_db,
            email="tenant_b@example.com",
            password="password_b_123",
            prefix="t/user_b/",
            vector_namespace="user_b",
            fake_env=fake_env,
            unique_keyword="armadillo_keyword_tenant_b",
            vector_val=0.84,
            seed_vectors=False,
        )
        tenant_dbs = {tenant_a.user["id"]: shared_db, tenant_b.user["id"]: shared_db}
        tenant_scopes = {
            tenant_a.user["id"]: tenant_a.scope,
            tenant_b.user["id"]: tenant_b.scope,
        }
    else:  # separate_db
        _, db_a = _build_test_db()
        _, db_b = _build_test_db()
        fake_env = FakeEnv(
            db=db_a,
            bucket=fake_bucket,
            vectorize=fake_vectorize,
            ai=fake_ai,
            queue=fake_queue,
        )
        tenant_a = await _seed_single_tenant(
            db=db_a,
            email="tenant_a@example.com",
            password="password_a_123",
            prefix="t/user_a/",
            vector_namespace="user_a",
            fake_env=fake_env,
            unique_keyword="platypus_keyword_tenant_a",
            vector_val=0.42,
        )
        tenant_b = await _seed_single_tenant(
            db=db_b,
            email="tenant_b@example.com",
            password="password_b_123",
            prefix="t/user_b/",
            vector_namespace="user_b",
            fake_env=fake_env,
            unique_keyword="armadillo_keyword_tenant_b",
            vector_val=0.84,
            seed_vectors=False,
        )
        tenant_dbs = {tenant_a.user["id"]: db_a, tenant_b.user["id"]: db_b}
        tenant_scopes = {
            tenant_a.user["id"]: tenant_a.scope,
            tenant_b.user["id"]: tenant_b.scope,
        }

    auth_prov = TestMultiTenantAuthProvider(tenant_dbs)
    scope_prov = TestMultiTenantScopeProvider(tenant_scopes)
    providers = Providers(auth=auth_prov, scope=scope_prov)

    app = create_app(providers=providers)

    class InjectEnvMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            request.scope["env"] = fake_env
            return await call_next(request)

    app.add_middleware(InjectEnvMiddleware)
    client = TestClient(app)

    return IsolationHarness(
        tenant_a=tenant_a,
        tenant_b=tenant_b,
        app=app,
        client=client,
        env=fake_env,
        topology=topology,
    )
