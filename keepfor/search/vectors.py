# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Tenant-scoped Vectorize index wrapper."""

from __future__ import annotations

from typing import Any

from keepfor.utils.logging import logger


def _to_py(obj: Any) -> Any:
    """Normalize JS proxy objects to Python objects, ignoring mocks."""
    if obj is None:
        return None
    if hasattr(obj, "_mock_return_value") or hasattr(obj, "_mock_wraps"):
        return obj
    to_py_fn = getattr(obj, "to_py", None)
    if callable(to_py_fn) and not hasattr(to_py_fn, "_mock_return_value"):
        try:
            return to_py_fn()
        except Exception:
            return obj
    return obj


class VectorIndex:
    """Wrapper around Cloudflare Vectorize with namespace isolation."""

    def __init__(self, index: Any, namespace: str | None = None) -> None:
        self.index = index
        self.namespace = namespace

    async def upsert(self, vectors: list[dict[str, Any]]) -> None:
        """Upsert vectors into Vectorize.

        Adds {'namespace': ns} to each vector dict when namespace is set.
        """
        if not vectors or self.index is None:
            return
        if self.namespace is not None:
            vectors = [{**v, "namespace": self.namespace} for v in vectors]
        await self.index.upsert(vectors)

    async def query(self, vector: list[float], top_k: int = 50) -> list[dict[str, Any]]:
        """Query vectors by cosine similarity.

        Returns normalized list of dicts: {'id': str, 'score': float, 'metadata': dict}.
        """
        if self.index is None:
            return []
        options: dict[str, Any] = {
            "topK": min(top_k, 50),
            "returnMetadata": "all",
        }
        if self.namespace is not None:
            options["namespace"] = self.namespace

        res = await self.index.query(vector, options)
        res = _to_py(res)
        if isinstance(res, dict):
            matches = res.get("matches", [])
        else:
            matches = getattr(res, "matches", [])
        matches = _to_py(matches)

        results: list[dict[str, Any]] = []
        for m in matches:
            m = _to_py(m)
            m_id = m.get("id") if isinstance(m, dict) else getattr(m, "id", "")
            m_score = (
                m.get("score", 0.0) if isinstance(m, dict) else getattr(m, "score", 0.0)
            )
            m_meta = (
                m.get("metadata")
                if isinstance(m, dict)
                else getattr(m, "metadata", None)
            )
            m_meta = _to_py(m_meta)
            results.append(
                {
                    "id": str(m_id),
                    "score": float(m_score),
                    "metadata": dict(m_meta) if isinstance(m_meta, dict) else {},
                }
            )
        return results

    async def delete(self, ids: list[str]) -> None:
        """Delete vectors by IDs in chunks of at most 100.

        Applies namespace option when namespace is set.
        """
        if not ids or self.index is None:
            return
        options = {"namespace": self.namespace} if self.namespace is not None else None

        for i in range(0, len(ids), 100):
            batch = ids[i : i + 100]
            del_fn = getattr(
                self.index, "deleteByIds", getattr(self.index, "delete", None)
            )
            if del_fn is None:
                logger.warning("Vectorize index has neither deleteByIds nor delete")
                continue
            if options is not None:
                try:
                    await del_fn(batch, options)
                except TypeError:
                    await del_fn(batch)
            else:
                await del_fn(batch)
