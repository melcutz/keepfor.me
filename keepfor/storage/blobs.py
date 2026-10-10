# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Tenant-scoped blob storage wrapper over R2."""

from __future__ import annotations

from typing import Any

from keepfor.utils.logging import logger


class BlobStore:
    """Wrapper around Cloudflare R2 bucket with tenant prefix isolation."""

    def __init__(self, bucket: Any, prefix: str = "") -> None:
        self.bucket = bucket
        self.prefix = f"{prefix.rstrip('/')}/" if prefix else ""

    def key(self, *parts: str) -> str:
        """Construct full key from relative parts under prefix.

        Parts must not contain '..' or leading '/'.
        """
        for part in parts:
            part_str = str(part)
            if ".." in part_str:
                raise ValueError(f"Blob key part must not contain '..': {part!r}")
            if part_str.startswith("/"):
                raise ValueError(f"Blob key part must not start with '/': {part!r}")
        key_suffix = "/".join(str(p) for p in parts)
        return f"{self.prefix}{key_suffix}"

    async def get_text(self, key: str) -> str | None:
        """Fetch blob text content or return None if missing."""
        if self.bucket is None:
            return None
        try:
            obj = await self.bucket.get(key)
            if obj is None:
                return None
            if hasattr(obj, "text"):
                return await obj.text()
            raw = getattr(obj, "to_py", lambda: obj)()
            if isinstance(raw, str):
                return raw
            if isinstance(raw, (bytes, bytearray)):
                return raw.decode("utf-8")
            return None
        except Exception as exc:
            logger.warning("Failed to get blob %s: %s", key, exc)
            return None

    async def put_html(self, key: str, html: str) -> None:
        """Store HTML document with text/html content type."""
        if self.bucket is None:
            return
        await self.bucket.put(
            key,
            html,
            {"httpMetadata": {"contentType": "text/html; charset=utf-8"}},
        )

    async def delete_many(self, keys: list[str]) -> None:
        """Delete list of blob keys from bucket."""
        if not keys or self.bucket is None:
            return
        await self.bucket.delete(keys)

    async def delete_prefix(self, rel_prefix: str = "") -> int:
        """List and delete all blobs under prefix in pages of 1000.

        Used for tenant data purge. Returns total number of objects deleted.
        """
        if ".." in rel_prefix:
            raise ValueError(f"Prefix must not contain '..': {rel_prefix!r}")
        if self.bucket is None:
            return 0

        full_prefix = f"{self.prefix}{rel_prefix.lstrip('/')}"
        cursor = None
        total_deleted = 0

        while True:
            options: dict[str, Any] = {"prefix": full_prefix, "limit": 1000}
            if cursor:
                options["cursor"] = cursor
            res = await self.bucket.list(options)
            objects = getattr(res, "objects", [])
            if hasattr(objects, "to_py"):
                objects = objects.to_py()

            keys: list[str] = []
            for obj in objects:
                k = getattr(obj, "key", None)
                if k is None and isinstance(obj, dict):
                    k = obj.get("key")
                if k:
                    keys.append(str(k))

            if not keys:
                break

            await self.delete_many(keys)
            total_deleted += len(keys)

            truncated = getattr(res, "truncated", False)
            if hasattr(truncated, "to_py"):
                truncated = truncated.to_py()
            if not truncated:
                break

            cursor = getattr(res, "cursor", None)
            if hasattr(cursor, "to_py"):
                cursor = cursor.to_py()
            if not cursor:
                break

        return total_deleted
