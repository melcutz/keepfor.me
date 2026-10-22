# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Indexer hooks: which items the search index may keep.

An item of a library is not always text. A saved image, a video, an archive,
a payload the extractor could not read and a bookmark that holds nothing but
its URL have no use in the full text index or the vector search. The indexer
reads the hooks of this module before it chunks, embeds or writes a content
row, so a textless item is skipped up front instead of being written to the
index and then deleted after extraction (keepfor-cloud#120).

The predicates are public so every indexer, core's own processor and a hosted
ingest, reads one rule. `should_index_text` must stay in sync with the Ask
filter of the hosted deployment: it decides which items may be cited.
"""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlsplit

# The extensions of a URL that hold no text: an image, a video, an audio
# file, an archive or a binary. Hosted deployments save these files and
# reference them by title; extracting text from them is not meaningful.
NON_TEXT_EXTENSIONS = frozenset(
    {
        # images
        "jpg",
        "jpeg",
        "png",
        "gif",
        "webp",
        "svg",
        "ico",
        "bmp",
        "tif",
        "tiff",
        "avif",
        "heic",
        "heif",
        # video and audio
        "mp4",
        "m4v",
        "mov",
        "avi",
        "mkv",
        "webm",
        "mp3",
        "m4a",
        "wav",
        "ogg",
        "flac",
        "aac",
        "opus",
        # archives and binaries
        "zip",
        "tar",
        "gz",
        "tgz",
        "bz2",
        "xz",
        "7z",
        "rar",
        "dmg",
        "iso",
        "exe",
        "msi",
        "apk",
        "deb",
        "rpm",
        "bin",
    }
)

# The share of characters that may be unprintable before a body counts as a
# binary payload rather than readable text.
MAX_NON_PRINTABLE_SHARE = 0.3

# A body that is nothing but a URL holds no text.
_URL_ONLY_RE = re.compile(r"https?://\S+")


def _url_extension(url: str) -> str:
    """The extension of the file a URL points at, lower-cased."""
    raw = (url or "").strip()
    if not raw:
        return ""
    path = urlsplit(raw if "://" in raw else f"//{raw}").path
    name = path.rsplit("/", 1)[-1]
    _, dot, extension = name.rpartition(".")
    return extension.lower() if dot else ""


def _is_mostly_binary(text: str) -> bool:
    """True when the body is a binary payload rather than readable text."""
    sample = text[:2000]
    if not sample:
        return False
    binary = sum(
        1
        for char in sample
        if char != "\ufffd" and not (char.isprintable() or char in " \n\r\t")
    )
    return binary / len(sample) > MAX_NON_PRINTABLE_SHARE


def should_index_text(item: Mapping[str, Any]) -> bool:
    """True when the item holds text the full text index and vectors may keep.

    Reads an item mapping (an `items` row or an extraction result) with the
    keys `url`, `title`, `content_text` and `status`; missing keys are
    tolerated and skipped.

    False for: a failed extraction, an image, a video, an archive or another
    binary URL, a body that is mostly unprintable bytes, an item without
    text, a bare URL (title is the URL, no body) and a body that is nothing
    but a URL. Hosted deployments that ingest binary files keep their own
    title-only FTS row for such items instead (#119), so the item still
    shows up in a title search.
    """
    status = item.get("status")
    if status and str(status).strip().lower() in {"failed", "error"}:
        return False
    if _url_extension(item.get("url") or "") in NON_TEXT_EXTENSIONS:
        return False
    body = item.get("content_text") or ""
    text = body.strip()
    title = (item.get("title") or "").strip()
    if title == (item.get("url") or "").strip() and not text:
        # A bare URL item: the title is the URL and there is no body.
        return False
    if not text:
        # An item without text.
        return False
    if _URL_ONLY_RE.fullmatch(text):
        # The body is a URL and nothing else.
        return False
    if _is_mostly_binary(body):
        return False
    return True


def should_index_title(item: Mapping[str, Any]) -> bool:
    """True when the item may keep a title-only full text row.

    Broader than `should_index_text`: an item the content index skips still
    has a title to search (keepfor-cloud#119), so a title-only row keeps it
    findable in library search. Only an item without a title has nothing to
    index.
    """
    return bool((item.get("title") or "").strip())


__all__ = [
    "MAX_NON_PRINTABLE_SHARE",
    "NON_TEXT_EXTENSIONS",
    "should_index_text",
    "should_index_title",
]
