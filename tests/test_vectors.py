# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Tests for VectorIndex wrapper and namespace-based vector isolation."""

from typing import Any

import pytest

from keepfor.search.engine import search_vectorize
from keepfor.search.vectors import VectorIndex
from keepfor.spi import TenantScope
from tests.fakes import FakeAI, FakeEnv, FakeVectorize


class PyProxyMock:
    """Mock simulating Pyodide/JsProxy object with to_py method."""

    def __init__(self, data: Any):
        self._data = data

    def to_py(self) -> Any:
        return self._data

    def get(self, key: str, default: Any = None) -> Any:
        if isinstance(self._data, dict):
            return self._data.get(key, default)
        return default


@pytest.mark.asyncio
async def test_vector_index_none_index():
    """VectorIndex safely handles index=None."""
    vi = VectorIndex(None, namespace="t1")
    await vi.upsert([{"id": "v1", "values": [0.1, 0.2]}])
    res = await vi.query([0.1, 0.2], top_k=10)
    assert res == []
    await vi.delete(["v1", "v2"])


@pytest.mark.asyncio
async def test_vector_index_upsert_and_query_with_namespace():
    """VectorIndex injects namespace on upsert and filters by namespace on query."""
    fake_vec = FakeVectorize()
    vi_a = VectorIndex(fake_vec, namespace="tenant-a")
    vi_b = VectorIndex(fake_vec, namespace="tenant-b")

    # Upsert with identical vectors
    await vi_a.upsert(
        [
            {
                "id": "item_1",
                "values": [1.0, 0.0],
                "metadata": {"user_id": "user-a", "item_id": "1"},
            }
        ]
    )
    await vi_b.upsert(
        [
            {
                "id": "item_2",
                "values": [1.0, 0.0],
                "metadata": {"user_id": "user-b", "item_id": "2"},
            }
        ]
    )

    # Tenant A sees only tenant A
    res_a = await vi_a.query([1.0, 0.0], top_k=10)
    assert len(res_a) == 1
    assert res_a[0]["id"] == "item_1"
    assert res_a[0]["metadata"]["user_id"] == "user-a"

    # Tenant B sees only tenant B
    res_b = await vi_b.query([1.0, 0.0], top_k=10)
    assert len(res_b) == 1
    assert res_b[0]["id"] == "item_2"
    assert res_b[0]["metadata"]["user_id"] == "user-b"


@pytest.mark.asyncio
async def test_vector_index_without_namespace():
    """VectorIndex without namespace does not inject namespace."""
    fake_vec = FakeVectorize()
    vi = VectorIndex(fake_vec, namespace=None)

    await vi.upsert(
        [
            {
                "id": "item_none",
                "values": [0.5, 0.5],
                "metadata": {"user_id": "u1"},
            }
        ]
    )

    res = await vi.query([0.5, 0.5], top_k=5)
    assert len(res) == 1
    assert res[0]["id"] == "item_none"
    # In fake_vec store, key has None namespace
    assert (None, "item_none") in fake_vec.store


@pytest.mark.asyncio
async def test_vector_index_query_caps_top_k():
    """VectorIndex caps top_k at 50."""
    fake_vec = FakeVectorize()
    vi = VectorIndex(fake_vec, namespace=None)
    vectors = [
        {"id": f"v_{i}", "values": [1.0, float(i)], "metadata": {"i": i}}
        for i in range(60)
    ]
    await vi.upsert(vectors)

    res = await vi.query([1.0, 1.0], top_k=100)
    assert len(res) == 50


@pytest.mark.asyncio
async def test_vector_index_query_to_py_handling():
    """VectorIndex normalizes Pyodide-style js proxy objects."""

    class FakeProxyVectorize:
        async def query(self, vec, opts):
            # Returns object with to_py() or matches with to_py()
            return PyProxyMock(
                {
                    "matches": [
                        PyProxyMock(
                            {
                                "id": "v1",
                                "score": 0.95,
                                "metadata": PyProxyMock({"user_id": "u1"}),
                            }
                        )
                    ]
                }
            )

    vi = VectorIndex(FakeProxyVectorize(), namespace=None)
    res = await vi.query([1.0, 0.0])
    assert len(res) == 1
    assert res[0] == {
        "id": "v1",
        "score": 0.95,
        "metadata": {"user_id": "u1"},
    }


@pytest.mark.asyncio
async def test_vector_index_delete_chunking_and_namespace():
    """VectorIndex deletes in chunks of 100 with namespace."""
    fake_vec = FakeVectorize()
    vi_a = VectorIndex(fake_vec, namespace="ns-a")
    vi_b = VectorIndex(fake_vec, namespace="ns-b")

    # Upsert 250 items into ns-a and 10 items into ns-b
    items_a = [
        {"id": f"a_{i}", "values": [0.1, 0.2], "metadata": {}} for i in range(250)
    ]
    items_b = [
        {"id": f"b_{i}", "values": [0.1, 0.2], "metadata": {}} for i in range(10)
    ]
    await vi_a.upsert(items_a)
    await vi_b.upsert(items_b)

    assert len(fake_vec.store) == 260

    # Delete 250 items from ns-a in batches
    ids_to_del = [f"a_{i}" for i in range(250)]
    await vi_a.delete(ids_to_del)

    # All ns-a deleted, ns-b untouched
    assert len(fake_vec.store) == 10
    for i in range(10):
        assert ("ns-b", f"b_{i}") in fake_vec.store


@pytest.mark.asyncio
async def test_search_vectorize_post_filter_drops_foreign_user():
    """search_vectorize drops foreign user matches when no namespace is set."""
    fake_vec = FakeVectorize()
    fake_ai = FakeAI()
    env = FakeEnv(vectorize=fake_vec, ai=fake_ai)

    # Insert vector for user-other
    await fake_vec.upsert(
        [
            {
                "id": "chunk_other",
                "values": [0.1] * 768,
                "metadata": {"user_id": "user-other", "item_id": "item-other"},
            }
        ]
    )

    results = await search_vectorize(env, "user-me", "some query")
    assert len(results) == 0


@pytest.mark.asyncio
async def test_search_vectorize_with_tenant_scope():
    """search_vectorize uses scope's vector_namespace and retains matching results."""
    fake_vec = FakeVectorize()
    fake_ai = FakeAI()
    env = FakeEnv(vectorize=fake_vec, ai=fake_ai)

    scope_a = TenantScope(
        tenant_id="tenant-a",
        user_id="user-a",
        db=None,  # not used in search_vectorize
        bucket="b",
        blob_prefix="p/",
        vector_namespace="ns-a",
    )
    scope_b = TenantScope(
        tenant_id="tenant-b",
        user_id="user-b",
        db=None,
        bucket="b",
        blob_prefix="p/",
        vector_namespace="ns-b",
    )

    # Upsert vector in ns-a
    vi_a = scope_a.vectors(env)
    await vi_a.upsert(
        [
            {
                "id": "chunk_1",
                "values": [0.1] * 768,
                "metadata": {"user_id": "user-a", "item_id": "item-1"},
            }
        ]
    )

    # Search as tenant-b: should find nothing
    res_b = await search_vectorize(env, "user-b", "query", scope=scope_b)
    assert len(res_b) == 0

    # Search as tenant-a: finds item-1
    res_a = await search_vectorize(env, "user-a", "query", scope=scope_a)
    assert len(res_a) == 1
    assert res_a[0]["item_id"] == "item-1"
