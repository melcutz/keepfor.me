# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for fake Cloudflare bindings in tests/fakes.py."""

import json
import math

import pytest

from tests.fakes import FakeAI, FakeQueue, FakeR2Bucket, FakeVectorize


@pytest.mark.asyncio
async def test_r2_bucket_operations():
    bucket = FakeR2Bucket()

    # Put and get string
    await bucket.put(
        "test.txt",
        "hello r2",
        options={
            "customMetadata": {"user_id": "u1"},
            "httpMetadata": {"contentType": "text/plain"},
        },
    )
    obj = await bucket.get("test.txt")
    assert obj is not None
    assert await obj.text() == "hello r2"
    assert await obj.arrayBuffer() == b"hello r2"
    assert obj.customMetadata == {"user_id": "u1"}
    assert obj.httpMetadata == {"contentType": "text/plain"}
    assert obj.size == len("hello r2")

    # Put and get JSON
    payload = {"status": "ok", "count": 42}
    await bucket.put("data.json", json.dumps(payload))
    json_obj = await bucket.get("data.json")
    assert json_obj is not None
    assert await json_obj.json() == payload

    # List with prefix
    await bucket.put("articles/1.html", "article 1")
    await bucket.put("articles/2.html", "article 2")
    await bucket.put("other/file.txt", "other")

    list_res = await bucket.list({"prefix": "articles/"})
    assert len(list_res.objects) == 2
    keys = [o.key for o in list_res.objects]
    assert keys == ["articles/1.html", "articles/2.html"]

    # List with limit and pagination
    paged = await bucket.list({"prefix": "articles/", "limit": 1})
    assert len(paged.objects) == 1
    assert paged.objects[0].key == "articles/1.html"
    assert paged.truncated is True
    assert paged.cursor == "articles/1.html"

    # Delete
    await bucket.delete("test.txt")
    assert await bucket.get("test.txt") is None

    # Delete multiple
    await bucket.delete(["articles/1.html", "articles/2.html"])
    assert await bucket.get("articles/1.html") is None
    assert await bucket.get("articles/2.html") is None


@pytest.mark.asyncio
async def test_vectorize_cosine_similarity_ordering():
    vec = FakeVectorize()
    await vec.upsert(
        [
            {"id": "v_pos", "values": [1.0, 0.0, 0.0], "namespace": "test_ns"},
            {"id": "v_orth", "values": [0.0, 1.0, 0.0], "namespace": "test_ns"},
            {"id": "v_neg", "values": [-1.0, 0.0, 0.0], "namespace": "test_ns"},
        ]
    )

    query_vec = [1.0, 0.0, 0.0]
    result = await vec.query(query_vec, {"namespace": "test_ns", "topK": 3})
    matches = result.matches
    assert len(matches) == 3
    assert matches[0]["id"] == "v_pos"
    assert abs(matches[0]["score"] - 1.0) < 1e-4
    assert matches[1]["id"] == "v_orth"
    assert abs(matches[1]["score"] - 0.0) < 1e-4
    assert matches[2]["id"] == "v_neg"
    assert abs(matches[2]["score"] - (-1.0)) < 1e-4


@pytest.mark.asyncio
async def test_vectorize_namespace_isolation():
    vec = FakeVectorize()
    await vec.upsert(
        [
            {
                "id": "item_a",
                "values": [1.0, 0.0],
                "namespace": "tenant_a",
                "metadata": {"secret": "a"},
            },
            {
                "id": "item_b",
                "values": [1.0, 0.0],
                "namespace": "tenant_b",
                "metadata": {"secret": "b"},
            },
            {"id": "item_none", "values": [1.0, 0.0], "metadata": {"secret": "none"}},
        ]
    )

    # Query tenant_a - should only return item_a
    res_a = await vec.query(
        [1.0, 0.0], {"namespace": "tenant_a", "returnMetadata": True}
    )
    assert len(res_a.matches) == 1
    assert res_a.matches[0]["id"] == "item_a"
    assert res_a.matches[0]["metadata"] == {"secret": "a"}

    # Query tenant_b - should only return item_b
    res_b = await vec.query([1.0, 0.0], {"namespace": "tenant_b"})
    assert len(res_b.matches) == 1
    assert res_b.matches[0]["id"] == "item_b"

    # Query tenant_c - should return empty
    res_c = await vec.query([1.0, 0.0], {"namespace": "tenant_c"})
    assert len(res_c.matches) == 0

    # Query unnamespaced (namespace=None) - should only return item_none
    res_none = await vec.query([1.0, 0.0], {"namespace": None})
    assert len(res_none.matches) == 1
    assert res_none.matches[0]["id"] == "item_none"


@pytest.mark.asyncio
async def test_vectorize_top_k_and_metadata_filter():
    vec = FakeVectorize()
    vectors = [
        {
            "id": f"doc_{i}",
            "values": [1.0, float(i)],
            "namespace": "docs",
            "metadata": {"lang": "python" if i % 2 == 0 else "rust", "order": i},
        }
        for i in range(10)
    ]
    await vec.upsert(vectors)

    # topK cap
    res_cap = await vec.query([1.0, 0.0], {"namespace": "docs", "topK": 4})
    assert len(res_cap.matches) == 4

    # Metadata filter exact match
    res_py = await vec.query(
        [1.0, 0.0],
        {
            "namespace": "docs",
            "topK": 10,
            "filter": {"lang": "python"},
            "returnMetadata": True,
        },
    )
    assert len(res_py.matches) == 5
    for m in res_py.matches:
        assert m["metadata"]["lang"] == "python"

    # Metadata filter with $eq syntax
    res_rust = await vec.query(
        [1.0, 0.0],
        {
            "namespace": "docs",
            "topK": 10,
            "filter": {"lang": {"$eq": "rust"}},
            "returnMetadata": True,
        },
    )
    assert len(res_rust.matches) == 5
    for m in res_rust.matches:
        assert m["metadata"]["lang"] == "rust"


@pytest.mark.asyncio
async def test_vectorize_get_and_delete_by_ids():
    vec = FakeVectorize()
    await vec.upsert(
        [
            {"id": "doc_1", "values": [1.0, 0.0], "namespace": "ns1"},
            {"id": "doc_2", "values": [0.0, 1.0], "namespace": "ns1"},
            {"id": "doc_1", "values": [0.5, 0.5], "namespace": "ns2"},
        ]
    )

    # Fetch by IDs in ns1
    fetched = await vec.getByIds(["doc_1", "doc_2"], {"namespace": "ns1"})
    assert len(fetched) == 2
    assert {d["id"] for d in fetched} == {"doc_1", "doc_2"}

    # Fetch doc_1 in ns2
    fetched_ns2 = await vec.getByIds("doc_1", {"namespace": "ns2"})
    assert len(fetched_ns2) == 1
    assert fetched_ns2[0]["values"] == [0.5, 0.5]

    # Delete in ns1
    del_res = await vec.deleteByIds(["doc_1"], {"namespace": "ns1"})
    assert del_res["count"] == 1

    # Verify doc_1 in ns1 is gone but doc_1 in ns2 remains
    after_del_ns1 = await vec.getByIds("doc_1", {"namespace": "ns1"})
    assert len(after_del_ns1) == 0
    after_del_ns2 = await vec.getByIds("doc_1", {"namespace": "ns2"})
    assert len(after_del_ns2) == 1


@pytest.mark.asyncio
async def test_ai_deterministic_embeddings():
    ai = FakeAI()

    # Repeated calls produce identical vectors
    res1 = await ai.run("@cf/baai/bge-base-en-v1.5", {"text": "machine learning"})
    res2 = await ai.run("@cf/baai/bge-base-en-v1.5", {"text": "machine learning"})

    vec1 = res1["data"][0]
    vec2 = res2["data"][0]
    assert len(vec1) == 768
    assert len(vec2) == 768
    assert vec1 == vec2

    # Vector is normalized to unit length
    norm = math.sqrt(sum(x * x for x in vec1))
    assert abs(norm - 1.0) < 1e-5

    # Different text produces different vectors
    res3 = await ai.run("@cf/baai/bge-base-en-v1.5", {"text": "distributed systems"})
    vec3 = res3["data"][0]
    assert vec1 != vec3

    # Batch input
    res_batch = await ai.run(
        "@cf/baai/bge-base-en-v1.5", {"text": ["text a", "text b"]}
    )
    assert len(res_batch["data"]) == 2
    assert len(res_batch["data"][0]) == 768
    assert len(res_batch["data"][1]) == 768


@pytest.mark.asyncio
async def test_ai_scripting_and_reset():
    ai = FakeAI()

    # Scripted mock response
    def custom_run(model, inputs):
        return {"custom": f"handled {model}"}

    FakeAI.script(custom_run)
    res = await ai.run("my-model", {"prompt": "test"})
    assert res == {"custom": "handled my-model"}

    # Reset
    FakeAI.script(None)
    fallback = await ai.run("my-model", {"prompt": "test"})
    assert fallback == {"response": "Fake AI summary response."}


@pytest.mark.asyncio
async def test_queue_operations():
    q = FakeQueue()
    await q.send({"type": "process", "id": 1})
    await q.send_batch([{"type": "process", "id": 2}, {"type": "process", "id": 3}])
    assert len(q.sent) == 3
    assert q.sent[0]["id"] == 1
    assert q.sent[1]["id"] == 2
    assert q.sent[2]["id"] == 3


def test_fake_env_fixtures(
    fake_env, db, fake_bucket, fake_vectorize, fake_ai, fake_queue
):
    assert fake_env.keepfor_me_db is db
    assert fake_env.DB is db
    assert fake_env.BUCKET is fake_bucket
    assert fake_env.VECTORIZE is fake_vectorize
    assert fake_env.AI is fake_ai
    assert fake_env.QUEUE is fake_queue
    assert fake_env.ALLOW_PUBLIC_SIGNUPS == "false"
