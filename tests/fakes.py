# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Fake Cloudflare bindings for testing.

Faithful, namespace-aware in-memory fakes for R2, Vectorize, Workers AI,
and Queues to test multi-tenancy and isolation without live bindings.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
from collections.abc import Callable
from typing import Any


class FakeR2Object:
    """In-memory representation of an R2 object."""

    def __init__(
        self,
        key: str,
        data: bytes,
        custom_metadata: dict[str, Any] | None = None,
        http_metadata: dict[str, Any] | None = None,
    ):
        self.key = key
        self._data = data
        self.size = len(data)
        self.customMetadata = custom_metadata or {}
        self.httpMetadata = http_metadata or {}

    async def text(self) -> str:
        return self._data.decode("utf-8")

    async def arrayBuffer(self) -> bytes:  # noqa: N802
        return self._data

    async def json(self) -> Any:
        return json.loads(self._data.decode("utf-8"))


class FakeR2Bucket:
    """In-memory simulation of a Cloudflare R2 bucket."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, Any]] = {}
        self.puts: list[str] = []
        self.deletes: list[str] = []

    async def get(self, key: str) -> FakeR2Object | None:
        if key not in self.store:
            return None
        data = self.store[key]
        meta = self.metadata.get(key, {})
        return FakeR2Object(
            key,
            data,
            custom_metadata=meta.get("customMetadata"),
            http_metadata=meta.get("httpMetadata"),
        )

    async def put(
        self,
        key: str,
        value: str | bytes | bytearray,
        options: dict[str, Any] | None = None,
    ) -> FakeR2Object:
        if isinstance(value, str):
            data = value.encode("utf-8")
        elif isinstance(value, (bytes, bytearray)):
            data = bytes(value)
        else:
            data = str(value).encode("utf-8")

        self.store[key] = data
        self.metadata[key] = options or {}
        self.puts.append(key)
        return FakeR2Object(key, data)

    async def delete(self, key_or_keys: str | list[str]) -> None:
        if isinstance(key_or_keys, str):
            keys = [key_or_keys]
        else:
            keys = list(key_or_keys)

        for k in keys:
            self.store.pop(k, None)
            self.metadata.pop(k, None)
            self.deletes.append(k)

    async def list(self, options: dict[str, Any] | None = None) -> Any:
        options = options or {}
        prefix = options.get("prefix", "")
        limit = options.get("limit", 1000)
        cursor = options.get("cursor", None)

        matched_keys = sorted([k for k in self.store if k.startswith(prefix)])
        start_idx = 0
        if cursor and cursor in matched_keys:
            start_idx = matched_keys.index(cursor) + 1

        page = matched_keys[start_idx : start_idx + limit]
        truncated = (start_idx + limit) < len(matched_keys)
        next_cursor = page[-1] if truncated and page else None

        objects = [
            FakeR2Object(
                k,
                self.store[k],
                custom_metadata=self.metadata.get(k, {}).get("customMetadata"),
            )
            for k in page
        ]

        class R2ListResult:
            def __init__(self, objs: list[FakeR2Object], trunc: bool, cur: str | None):
                self.objects = objs
                self.truncated = trunc
                self.cursor = cur

        return R2ListResult(objects, truncated, next_cursor)


class FakeVectorizeQueryResult:
    """Result object returned by FakeVectorize.query."""

    def __init__(self, matches: list[dict[str, Any]]):
        self.matches = matches

    def __getitem__(self, item: str) -> Any:
        if item == "matches":
            return self.matches
        raise KeyError(item)


class FakeVectorize:
    """Namespace-aware in-memory simulation of Cloudflare Vectorize."""

    def __init__(self):
        # Keyed by (namespace, id) -> vector dict
        self.store: dict[tuple[str | None, str], dict[str, Any]] = {}

    async def upsert(self, vectors: list[dict[str, Any]]) -> dict[str, Any]:
        """Upsert vectors.

        Each vector dict has: id, values, metadata, optional namespace.
        """
        for v in vectors:
            ns = v.get("namespace")
            vec_id = str(v["id"])
            self.store[(ns, vec_id)] = dict(v)
        return {"count": len(vectors)}

    async def query(
        self, vector: list[float], options: dict[str, Any] | None = None
    ) -> FakeVectorizeQueryResult:
        """Query vectors by cosine similarity with namespace isolation and filtering."""
        options = options or {}
        top_k = options.get("topK", 10)
        target_ns = options.get("namespace", None)
        ret_meta = options.get("returnMetadata", False)
        filt = options.get("filter", None)

        v1 = vector
        norm1 = math.sqrt(sum(x * x for x in v1)) if v1 else 0.0

        candidates: list[tuple[float, dict[str, Any]]] = []
        for (ns, _vec_id), entry in self.store.items():
            if ns != target_ns:
                continue

            meta = entry.get("metadata") or {}
            if filt:
                matches_filter = True
                for fk, fv in filt.items():
                    if isinstance(fv, dict) and "$eq" in fv:
                        expected = fv["$eq"]
                    else:
                        expected = fv
                    if meta.get(fk) != expected:
                        matches_filter = False
                        break
                if not matches_filter:
                    continue

            v2 = entry.get("values", [])
            norm2 = math.sqrt(sum(x * x for x in v2)) if v2 else 0.0
            if norm1 > 0 and norm2 > 0:
                dot = sum(a * b for a, b in zip(v1, v2))
                score = dot / (norm1 * norm2)
            else:
                score = 0.0

            candidates.append((score, entry))

        candidates.sort(key=lambda x: x[0], reverse=True)
        top_candidates = candidates[:top_k]

        matches: list[dict[str, Any]] = []
        for score, entry in top_candidates:
            m: dict[str, Any] = {
                "id": entry["id"],
                "score": float(score),
            }
            if ret_meta in ("all", "indexed", True):
                m["metadata"] = entry.get("metadata") or {}
            matches.append(m)

        return FakeVectorizeQueryResult(matches)

    async def getByIds(  # noqa: N802
        self, ids: str | list[str], options: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Fetch vectors by IDs, optionally scoped by namespace."""
        if isinstance(ids, str):
            ids = [ids]
        ns = options.get("namespace") if options and isinstance(options, dict) else None
        results: list[dict[str, Any]] = []
        for vid in ids:
            key = (ns, str(vid))
            if key in self.store:
                results.append(self.store[key])
        return results

    async def deleteByIds(  # noqa: N802
        self, ids: str | list[str], options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Delete vectors by IDs, optionally scoped by namespace."""
        if isinstance(ids, str):
            ids = [ids]
        ns = options.get("namespace") if options and isinstance(options, dict) else None
        deleted = 0
        for vid in ids:
            key = (ns, str(vid))
            if key in self.store:
                del self.store[key]
                deleted += 1
        return {"count": deleted}


class FakeAI:
    """In-memory simulation of Cloudflare Workers AI."""

    _scripted_fn: Callable[..., Any] | None = None

    def __init__(self):
        self._instance_scripted_fn: Callable[..., Any] | None = None

    @classmethod
    def script(cls, callable_fn: Callable[..., Any] | None) -> None:
        """Set a global script callable to handle ai.run calls."""
        cls._scripted_fn = (
            staticmethod(callable_fn) if callable_fn is not None else None
        )

    def script_instance(self, callable_fn: Callable[..., Any] | None) -> None:
        """Set an instance-specific script callable."""
        self._instance_scripted_fn = callable_fn

    async def run(self, model: str, inputs: dict[str, Any]) -> Any:
        script_fn = self._instance_scripted_fn or FakeAI._scripted_fn
        if script_fn is not None:
            res = script_fn(model, inputs)
            if asyncio.iscoroutine(res):
                res = await res
            return res

        # Embeddings generation (deterministic from sha256(text), unit normalized)
        if "text" in inputs:
            texts = inputs["text"]
            if isinstance(texts, str):
                texts = [texts]
            embeddings: list[list[float]] = []
            for t in texts:
                seed = hashlib.sha256(t.encode("utf-8")).digest()
                rng = random.Random(seed)
                vec = [rng.gauss(0.0, 1.0) for _ in range(768)]
                norm = math.sqrt(sum(x * x for x in vec))
                if norm > 0:
                    vec = [x / norm for x in vec]
                embeddings.append(vec)
            return {"data": embeddings}

        # Text generation fallback
        return {"response": "Fake AI summary response."}


class FakeQueue:
    """In-memory simulation of a Cloudflare Queue producer."""

    def __init__(self):
        self.sent: list[Any] = []

    async def send(self, msg: Any) -> None:
        self.sent.append(msg)

    async def send_batch(self, msgs: list[Any]) -> None:
        self.sent.extend(msgs)


class FakeEnv:
    """Simulation of the Cloudflare Worker env container."""

    def __init__(
        self,
        db: Any = None,
        bucket: FakeR2Bucket | None = None,
        vectorize: FakeVectorize | None = None,
        ai: FakeAI | None = None,
        queue: FakeQueue | None = None,
        **kwargs: Any,
    ):
        self.keepfor_me_db = db
        self.DB = db
        self.BUCKET = bucket if bucket is not None else FakeR2Bucket()
        self.VECTORIZE = vectorize if vectorize is not None else FakeVectorize()
        self.AI = ai if ai is not None else FakeAI()
        self.QUEUE = queue if queue is not None else FakeQueue()
        self.ALLOW_PUBLIC_SIGNUPS = kwargs.pop("ALLOW_PUBLIC_SIGNUPS", "false")
        for k, v in kwargs.items():
            setattr(self, k, v)
