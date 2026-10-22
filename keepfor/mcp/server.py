# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from typing import TYPE_CHECKING, Any

from keepfor.models.cleanup import (
    CLEANUP_CATEGORIES,
    DEFAULT_CLEANUP_LIMIT,
    DEFAULT_OLDER_THAN_DAYS,
    MAX_CLEANUP_LIMIT,
    find_cleanup,
)
from keepfor.models.collections import (
    MAX_COLLECTION_DESCRIPTION,
    MAX_COLLECTION_NAME,
    MAX_COLLECTION_QUERY,
    MAX_COLLECTION_TAG,
    MAX_COLLECTIONS_LIMIT,
    MAX_LISTED_ITEMS,
    add_items_to_collection,
    create_collection,
    delete_collection,
    list_collections,
    remove_items_from_collection,
    rename_collection,
)
from keepfor.models.db import Database
from keepfor.models.highlights import (
    MAX_HIGHLIGHT_COLOR,
    MAX_HIGHLIGHT_NOTE,
    MAX_HIGHLIGHT_OFFSET,
    MAX_HIGHLIGHT_TEXT,
    MAX_HIGHLIGHTS_LIMIT,
    create_highlight,
    delete_highlight,
    item_content_length,
    list_highlights,
    update_highlight,
)
from keepfor.models.items import (
    BULK_UPDATE_LIMIT,
    add_tags_to_item,
    bulk_update_items,
    create_tag,
    get_item,
    merge_tags,
    remove_tags_from_item,
    rename_tag,
    save_item,
    save_note,
    toggle_pin_item,
    trash_item,
)
from keepfor.models.tags import (
    SAMPLE_LIMIT,
    find_duplicate_tags,
    find_untagged_items,
    find_unused_tags,
    get_tag,
    list_tags,
    related_tags,
    retag_by_query,
    search_tags,
    set_item_tags,
    suggest_tags_for_item,
    suggest_tags_for_text,
    tag_filter_clause,
    tag_items,
    tag_stats,
    update_tag,
)
from keepfor.models.tags import (
    delete_tag as delete_tag_dry,
)
from keepfor.search.engine import (
    MAX_DIGEST_DATE_CHARS,
    MAX_DIGEST_TAG_CHARS,
    STATUS_GROUPS,
    find_items,
    hybrid_search,
    library_digest,
    list_items_compact,
    match_items,
    normalize_digest_bound,
    read_item_chunks,
    reading_history,
    related_items,
    resolve_item,
    search_chunks,
)
from keepfor.spi import SCOPE_READ, SCOPE_WRITE, scope_grants, scopes_of
from keepfor.utils.tagger import validate_tag_name

if TYPE_CHECKING:
    from keepfor.spi import TenantScope

# Protocol versions this server speaks, newest first. Streamable HTTP clients
# propose one in initialize params; the server echoes it back when supported.
SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

# Fields a compact list response can project onto. The default shape is
# deliberately tiny: id, title, url, saved_at and tags.
LIST_ITEM_FIELDS = (
    "id",
    "title",
    "url",
    "saved_at",
    "tags",
    "item_type",
    "is_pinned",
    "status",
    "last_opened_at",
)
DEFAULT_LIST_FIELDS = ("id", "title", "url", "saved_at", "tags")

# Cap on the number of source tags one merge_tags call may fold together.
MAX_MERGE_SOURCES = 20

# Cap on the tags one bulk_update call may add or remove.
MAX_BULK_TAGS = 20

# Shared tag filter arguments. Child tags count: "work" matches "work/research".
_TAG_FILTER_PROPS = {
    "tags_all": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Only items carrying ALL of these tags. Child tags count: "
            "'work' also matches 'work/research'."
        ),
    },
    "tags_any": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Only items carrying ANY of these tags. Child tags count: "
            "'work' also matches 'work/research'."
        ),
    },
    "tags_none": {
        "type": "array",
        "items": {"type": "string"},
        "description": ("Exclude items carrying any of these tags. Child tags count."),
    },
    "untagged": {
        "type": "boolean",
        "default": False,
        "description": "Only items without any tag.",
    },
}

# MCP methods served by keepfor.mcp.resources (resources + prompts).
_RESOURCE_PROMPT_METHODS = (
    "resources/list",
    "resources/templates/list",
    "resources/read",
    "prompts/list",
    "prompts/get",
)

_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
_WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}
_DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True}


def _tool_scope(tool: dict[str, Any]) -> str:
    """The scope a tool needs.

    The annotations are the source of truth: a read-only tool needs the
    `read` scope, a write or destructive tool needs the `write` scope.
    """
    annotations = tool.get("annotations") or {}
    return SCOPE_READ if annotations.get("readOnlyHint") else SCOPE_WRITE


class ToolError(Exception):
    """A structured tool failure: `kind` is a stable machine-readable code."""

    def __init__(self, kind: str, message: str, **details: Any):
        super().__init__(message)
        self.kind = kind
        self.details = details


def _invalid(message: str, **details: Any) -> ToolError:
    return ToolError("invalid_arguments", message, **details)


def _not_found(message: str, **details: Any) -> ToolError:
    return ToolError("not_found", message, **details)


def negotiate_protocol_version(params: dict[str, Any]) -> str | None:
    """Return the protocol version to answer with, or None if unsupported.

    A missing version is answered with the latest (lenient: older clients
    predate the negotiation dance). An explicitly unknown version returns
    None so the transport can reject it with 400.
    """
    requested = params.get("protocolVersion")
    if not requested:
        return LATEST_PROTOCOL_VERSION
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return None


def _encode_cursor(offset: int) -> str:
    """Encodes a pagination offset as an opaque cursor."""
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> int:
    """Decodes an opaque cursor back into a pagination offset."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        return int(raw.split(":", 1)[1])
    except (ValueError, IndexError, binascii.Error, UnicodeDecodeError) as exc:
        raise _invalid(f"Invalid cursor: {cursor!r}") from exc


def _project(item: dict[str, Any], fields: list[str] | None) -> dict[str, Any]:
    """Projects an item onto the requested fields (default: compact shape)."""
    wanted: tuple[str, ...] = DEFAULT_LIST_FIELDS
    if fields:
        wanted = tuple(f for f in fields if f in LIST_ITEM_FIELDS)
    return {f: item.get(f) for f in wanted}


def _as_list(value: Any) -> list[str]:
    """Normalizes a scalar or list argument into a list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _dry_run_flag(arguments: dict[str, Any]) -> bool:
    """Reads dry_run, failing closed: only a JSON boolean is accepted.

    A missing dry_run means a preview (True). Anything else - null, 0,
    "false" - is rejected instead of coerced, so a malformed request can
    never apply changes by accident.
    """
    if "dry_run" not in arguments:
        return True
    value = arguments["dry_run"]
    if not isinstance(value, bool):
        raise _invalid("dry_run must be a boolean")
    return value


def _bounded_int(
    value: Any, default: int, name: str, minimum: int, maximum: int
) -> int:
    """Reads an integer argument, rejecting anything outside the allowed range."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"{name} must be an integer between {minimum} and {maximum}.")
    if value < minimum or value > maximum:
        raise _invalid(f"{name} must be between {minimum} and {maximum}, got {value}.")
    return value


def _int_arg(value: Any, default: int, *, maximum: int | None = None) -> int:
    """Reads an integer argument, rejecting non-integers and capping the value."""
    if value is None:
        number = default
    elif isinstance(value, bool) or not isinstance(value, int):
        raise _invalid("Expected an integer argument.")
    else:
        number = value
    if number < 0:
        raise _invalid("Expected a non-negative integer argument.")
    if maximum is not None and number > maximum:
        number = maximum
    return number


def _tag_filters(arguments: dict[str, Any]) -> dict[str, Any]:
    """Reads the tag filter arguments, folding the legacy tag/tags into tags_all."""
    tags_all = _as_list(arguments.get("tags_all"))
    if not tags_all:
        tags_all = _as_list(arguments.get("tags"))
    if not tags_all and arguments.get("tag"):
        tags_all = [str(arguments["tag"])]
    return {
        "tags_all": tags_all,
        "tags_any": _as_list(arguments.get("tags_any")),
        "tags_none": _as_list(arguments.get("tags_none")),
        "untagged": bool(arguments.get("untagged", False)),
    }


def _bulk_update_filters(arguments: dict[str, Any]) -> dict[str, Any]:
    """Reads and validates the bulk_update filter arguments.

    Returns the filter kwargs for match_items. An empty filter is
    rejected: it would match the whole library.
    """
    status = arguments.get("status")
    if status is not None:
        status = str(status).strip().lower()
        if status not in ("all", *STATUS_GROUPS):
            raise _invalid(
                "bulk_update status must be one of: all, "
                + ", ".join(sorted(STATUS_GROUPS))
            )
        if status == "all":
            status = None

    def _text(value: Any) -> str | None:
        clean = str(value).strip() if value is not None else ""
        return clean or None

    pinned = arguments.get("pinned")
    if pinned is not None and not isinstance(pinned, bool):
        raise _invalid("bulk_update pinned must be a boolean")

    filters = {
        "status": status,
        "since": _text(arguments.get("since")),
        "until": _text(arguments.get("until")),
        "domain": _text(arguments.get("domain")),
        "pinned": pinned,
        "item_type": _text(arguments.get("type")),
        **_tag_filters(arguments),
    }
    if not (
        filters["status"]
        or filters["since"]
        or filters["until"]
        or filters["domain"]
        or filters["pinned"] is not None
        or filters["item_type"]
        or filters["tags_all"]
        or filters["tags_any"]
        or filters["tags_none"]
        or filters["untagged"]
    ):
        raise _invalid(
            "bulk_update requires a filter (status, since, until, domain, "
            "pinned, type, tags_all, tags_any, tags_none or untagged): an "
            "empty filter would match the whole library."
        )
    return filters


def _highlight_field(
    arguments: dict[str, Any], field: str, max_chars: int
) -> str | None:
    """The new value of a highlight field.

    None when the argument is absent (the field stays as it is), an empty
    string when it is explicitly empty (the field is removed).
    """
    if field not in arguments:
        return None
    value = arguments[field]
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _invalid(f"{field} must be a string")
    if len(value) > max_chars:
        raise _invalid(
            f"A highlight {field} is limited to {max_chars} characters.",
            max_chars=max_chars,
        )
    return value


def _collection_filter(
    arguments: dict[str, Any], field: str, max_chars: int
) -> str | None:
    """The value of a smart-collection filter.

    None when the argument is absent (the filter stays as it is), an empty
    string when it is explicitly empty (the filter is removed).
    """
    if field not in arguments:
        return None
    value = arguments[field]
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _invalid(f"{field} must be a string")
    clean = value.strip()
    if len(clean) > max_chars:
        raise _invalid(
            f"A collection {field} is limited to {max_chars} characters.",
            max_chars=max_chars,
        )
    return clean


async def _smart_members(
    db: Database,
    env: Any,
    user_id: str,
    collection: dict[str, Any],
    scope: TenantScope | None,
) -> tuple[list[str], int]:
    """The member ids of a smart collection, resolved through the search.

    The members are the items the query finds, narrowed by the tag filter.
    They are resolved at read time, so the collection stays current.
    """
    items, total = await hybrid_search(
        db,
        env,
        user_id,
        collection.get("query") or "",
        tag=collection.get("tag"),
        limit=MAX_LISTED_ITEMS,
        scope=scope,
    )
    return [it["id"] for it in items], total


def _digest_date(value: Any, field: str) -> str | None:
    """Validates and normalizes an ISO date/datetime argument."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _invalid(f"{field} must be an ISO date or datetime string")
    raw = value.strip()
    if not raw:
        return None
    if len(raw) > MAX_DIGEST_DATE_CHARS:
        raise _invalid(
            f"{field} must be at most {MAX_DIGEST_DATE_CHARS} characters",
            max_length=MAX_DIGEST_DATE_CHARS,
        )
    try:
        return normalize_digest_bound(raw)
    except ValueError as exc:
        raise _invalid(
            f"{field} must be an ISO date or datetime, got {value!r}"
        ) from exc


def _digest_tag(value: Any) -> str | None:
    """Validates the optional tag filter of the digest."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid("tag must be a string")
    raw = value.strip()
    if not raw:
        return None
    if len(raw) > MAX_DIGEST_TAG_CHARS:
        raise _invalid(
            f"tag must be at most {MAX_DIGEST_TAG_CHARS} characters",
            max_length=MAX_DIGEST_TAG_CHARS,
        )
    return raw


MCP_TOOLS = [
    {
        "name": "save_url",
        "description": "Saves a URL to the user's personal library and enqueues "
        "it for content extraction and semantic indexing. Use this to capture "
        "web pages for later reading. For saving personal notes or snippets, "
        "use save_note instead.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The webpage or article URL to save.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of tags.",
                },
                "agent_summary": {
                    "type": "string",
                    "description": "Optional AI-generated summary of the content.",
                },
                "why_saved": {
                    "type": "string",
                    "description": "Optional user's reason for saving this item.",
                },
                "source_context": {
                    "type": "string",
                    "description": (
                        "Optional context from the conversation/task "
                        "leading to the save."
                    ),
                },
                "saved_by": {
                    "type": "string",
                    "description": (
                        "Optional name of the agent/client that saved the item."
                    ),
                },
            },
            "required": ["url"],
        },
        "annotations": _WRITE,
    },
    {
        "name": "save_note",
        "description": "Saves a personal note, code snippet, prompt template, "
        "or idea to the user's personal knowledge vault and indexes it "
        "for hybrid search. Use this for self-authored content. For saving "
        "web pages, use save_url instead.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Title of the note",
                },
                "content": {
                    "type": "string",
                    "description": "Markdown body content",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags",
                },
                "is_pinned": {
                    "type": "boolean",
                    "default": False,
                    "description": "Pin note to top",
                },
                "agent_summary": {
                    "type": "string",
                    "description": "Optional AI-generated summary of the content.",
                },
                "why_saved": {
                    "type": "string",
                    "description": "Optional user's reason for saving this item.",
                },
                "source_context": {
                    "type": "string",
                    "description": (
                        "Optional context from the conversation/task "
                        "leading to the save."
                    ),
                },
                "saved_by": {
                    "type": "string",
                    "description": (
                        "Optional name of the agent/client that saved the item."
                    ),
                },
            },
            "required": ["title", "content"],
        },
        "annotations": _WRITE,
    },
    {
        "name": "search_library",
        "description": "Searches the library by keyword and meaning using "
        "hybrid search (FTS5 + Vectorize). Matches saved articles, personal "
        "notes, and notes imported from Google Keep, Evernote (ENEX) or "
        "Markdown exports. Returns item-level results with snippets. For "
        "passage-level citations with snapshot URLs, use search_passages "
        "instead. For an exact title or url lookup, use find_item instead "
        "(no search, no embeddings).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query terms or semantic question.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["hybrid", "keyword", "semantic"],
                    "default": "hybrid",
                },
                "tag": {
                    "type": "string",
                    "description": "Filter results by a specific tag.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by multiple tags (AND).",
                },
                **_TAG_FILTER_PROPS,
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max results to return.",
                },
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "description": "Pagination offset.",
                },
                "cursor": {
                    "type": "string",
                    "description": "Opaque cursor for pagination (alt. to offset).",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Projection: fields to include in response.",
                },
            },
            "required": ["query"],
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "search_passages",
        "description": "Searches the library at passage level using hybrid search "
        "over semantic chunks. Returns top passages with item_id, chunk_id, "
        "char offsets, snapshot_url (permanent R2 archive), saved_at, and score. "
        "Has a max_chars budget to control response size. Use this when you need "
        "precise citations with permanent links that survive link rot. For item-level "
        "results, use search_library instead.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query terms or semantic question.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["hybrid", "keyword", "semantic"],
                    "default": "hybrid",
                },
                "tag": {
                    "type": "string",
                    "description": "Filter by a specific tag.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by multiple tags (AND).",
                },
                **_TAG_FILTER_PROPS,
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max passages to return.",
                },
                "max_chars": {
                    "type": "integer",
                    "default": 4000,
                    "description": (
                        "Total character budget for all passages "
                        "combined (~4 chars/token)."
                    ),
                },
                "cursor": {
                    "type": "string",
                    "description": "Opaque cursor for pagination.",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Projection: fields to include in response.",
                },
            },
            "required": ["query"],
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "read_item",
        "description": (
            "Reads an item's content in paginated chunks for token-efficient "
            "retrieval. Returns text, next_cursor, total_chunks, and outline. "
            "Use cursor to page through long articles. For metadata-only access, "
            "use get_item (which caps content_text by default)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "The unique ID of the library item.",
                },
                "cursor": {
                    "type": "integer",
                    "default": 0,
                    "description": "Chunk index to start reading from.",
                },
                "max_tokens": {
                    "type": "integer",
                    "default": 2000,
                    "description": (
                        "Approximate token budget for returned text (~4 chars/token)."
                    ),
                },
                "format": {
                    "type": "string",
                    "enum": ["markdown", "text"],
                    "default": "markdown",
                    "description": "Output format.",
                },
            },
            "required": ["item_id"],
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "get_item",
        "description": "Retrieves metadata and excerpt of a saved article. "
        "Identify the item with item_id, url, or an exact title. "
        "By default, content_text is capped to the excerpt for token efficiency; "
        "the result reports truncated (true when the returned text is shorter "
        "than the stored content) and content_length (the full content length). "
        "Use include_content=true to get full text (not recommended for large items). "
        "For paginated full-text reading, use read_item instead.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "The unique ID of the library item.",
                },
                "url": {
                    "type": "string",
                    "description": "The item's url (exact or normalized match).",
                },
                "title": {
                    "type": "string",
                    "description": "The item's exact title (case-insensitive).",
                },
                "include_content": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "If true, returns full content_text. "
                        "Default false returns excerpt only."
                    ),
                },
            },
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "find_item",
        "description": "Finds items by exact or prefix title, or by exact or "
        "normalized url, and returns their ids. No search, no embeddings: this is "
        "the cheap way to go from a title you know to an item_id. Use "
        "get_item or read_item with the returned id. Use search_library when you "
        "only remember a topic, not a title.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Exact title or title prefix to look for.",
                },
                "url": {
                    "type": "string",
                    "description": "Exact or normalized url to look for.",
                },
                **_TAG_FILTER_PROPS,
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Max ids to return.",
                },
            },
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "list_items",
        "description": "Lists library items in a compact shape (id, title, url, "
        "saved_at, tags - no body text) with sorting, filters and pagination. "
        "Use sort=saved for newest first, sort=opened for most recently opened, "
        "sort=title for alphabetical. Filters: since/until (saved date range), "
        "domain (host), pinned, type (note|url). For full-text search use "
        "search_library; for reading history use reading_history.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sort": {
                    "type": "string",
                    "enum": ["saved", "opened", "title"],
                    "default": "saved",
                    "description": "Sort order.",
                },
                "since": {
                    "type": "string",
                    "description": "ISO datetime: only items saved at/after it.",
                },
                "until": {
                    "type": "string",
                    "description": "ISO datetime: only items saved at/before it.",
                },
                "domain": {
                    "type": "string",
                    "description": "Only items from this host (e.g. example.com).",
                },
                "pinned": {
                    "type": "boolean",
                    "description": "Only pinned (true) or only unpinned (false).",
                },
                "type": {
                    "type": "string",
                    "enum": ["note", "url"],
                    "description": "Only notes or only saved urls.",
                },
                "tag": {"type": "string", "description": "Filter by tag."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by multiple tags (AND).",
                },
                **_TAG_FILTER_PROPS,
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Number of items to return.",
                },
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "description": "Pagination offset.",
                },
                "cursor": {
                    "type": "string",
                    "description": "Opaque cursor for pagination (alt. to offset).",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Projection: fields to include. Default: "
                        "id, title, url, saved_at, tags."
                    ),
                },
            },
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "reading_history",
        "description": (
            "Returns reading history with date filtering. Uses created_at (saved) "
            "and item_opens (opened) tables. Answers 'what did I read about X "
            "last month'. Supports filtering by event type (saved/opened), "
            "date range, query, and tag. For simple recent items, use "
            "list_items instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": "ISO datetime: start of date range (inclusive).",
                },
                "until": {
                    "type": "string",
                    "description": "ISO datetime: end of date range (inclusive).",
                },
                "query": {
                    "type": "string",
                    "description": "Optional search query over history.",
                },
                "tag": {
                    "type": "string",
                    "description": "Filter by tag.",
                },
                **_TAG_FILTER_PROPS,
                "event": {
                    "type": "string",
                    "enum": ["saved", "opened", "all"],
                    "default": "all",
                    "description": "Event type to filter by.",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Max results to return.",
                },
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "description": "Pagination offset.",
                },
                "cursor": {
                    "type": "string",
                    "description": "Opaque cursor for pagination.",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Projection: fields to include in response.",
                },
            },
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "related_items",
        "description": (
            "Returns related items by vector similarity. Provide either item_id "
            "(uses item's content as query) or text (embeds the text). Excludes "
            "the source item itself. Includes one-line reasons (shared tags or "
            "semantic similarity). No competitor has this feature."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "The library item ID to find related items for.",
                },
                "text": {
                    "type": "string",
                    "description": "Alternative: raw text to find similar items for.",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max related items to return.",
                },
            },
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "tag_item",
        "description": "Adds or removes tags on a single saved library item. "
        "For tagging many items at once use tag_items; to replace the whole tag "
        "set of one item use set_item_tags.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "description": "The library item ID."},
                "add_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags to add.",
                },
                "remove_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags to remove.",
                },
            },
            "required": ["item_id"],
        },
        "annotations": _WRITE,
    },
    {
        "name": "list_tags",
        "description": "Lists the user's tags with item count and last-used date. "
        "Use sort=count for the most used, sort=name alphabetically, sort=recent "
        "for the most recently used. prefix filters by name, min_count hides tags "
        "used fewer than N times. Use search_tags for fuzzy name lookup.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sort": {
                    "type": "string",
                    "enum": ["count", "name", "recent"],
                    "default": "count",
                    "description": "Sort order.",
                },
                "prefix": {
                    "type": "string",
                    "description": "Only tags whose name starts with this prefix.",
                },
                "min_count": {
                    "type": "integer",
                    "description": "Only tags used at least this many times.",
                },
            },
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "create_tag",
        "description": "Creates a tag, optionally with a description, a color and "
        "a parent tag for hierarchy (e.g. parent=work creates work/research). "
        "Idempotent: an existing tag is not modified, use update_tag for that.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Tag name."},
                "description": {
                    "type": "string",
                    "description": "Optional human description of the tag.",
                },
                "color": {
                    "type": "string",
                    "description": "Optional color, e.g. #ff8800.",
                },
                "parent": {
                    "type": "string",
                    "description": "Optional parent tag name (created if missing).",
                },
            },
            "required": ["name"],
        },
        "annotations": _WRITE,
    },
    {
        "name": "describe_tag",
        "description": "Returns a tag's metadata: description, color, parent, "
        "child tags, item count and last use. Use update_tag to change them.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Tag name."},
            },
            "required": ["name"],
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "update_tag",
        "description": "Edits a tag's description, color and/or parent. Only the "
        'arguments you pass are changed; description="" clears the description '
        'and parent="" detaches the tag from its parent.',
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Tag name."},
                "description": {
                    "type": "string",
                    "description": "New description (empty string clears it).",
                },
                "color": {"type": "string", "description": "New color."},
                "parent": {
                    "type": "string",
                    "description": "New parent tag name (empty string detaches).",
                },
            },
            "required": ["name"],
        },
        "annotations": _WRITE,
    },
    {
        "name": "rename_tag",
        "description": "Renames a tag; every item carrying it is updated. "
        "Renaming onto an existing tag merges the two. Use merge_tags to fold "
        "several tags into one at once.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "old": {"type": "string", "description": "Current tag name."},
                "new": {"type": "string", "description": "New tag name."},
            },
            "required": ["old", "new"],
        },
        "annotations": _WRITE,
    },
    {
        "name": "merge_tags",
        "description": "Merges several source tags into one target tag: every item "
        "carrying a source tag ends up carrying the target tag, and the source "
        "tags disappear. At most 20 source tags per call.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags to merge away (at most 20).",
                },
                "target": {
                    "type": "string",
                    "description": "Tag that survives the merge.",
                },
            },
            "required": ["sources", "target"],
        },
        "annotations": _WRITE,
    },
    {
        "name": "delete_tag",
        "description": "Removes a tag from every item that carries it. Items are "
        "never deleted, only untagged. Defaults to dry_run=true, which returns "
        "the number of affected items and a sample without changing anything; "
        "pass dry_run=false to apply.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Tag name."},
                "dry_run": {
                    "type": "boolean",
                    "default": True,
                    "description": "Preview only (true) or apply (false).",
                },
            },
            "required": ["name"],
        },
        "annotations": _DESTRUCTIVE,
    },
    {
        "name": "tag_items",
        "description": "Adds and/or removes tags across many items at once. "
        "Defaults to dry_run=true, which returns the number of affected items "
        "and a sample without changing anything; pass dry_run=false to apply. "
        "At most 100 items are processed per call; the result reports "
        "total_matched and truncated.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Library item ids to tag.",
                },
                "add": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags to add.",
                },
                "remove": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags to remove.",
                },
                "dry_run": {
                    "type": "boolean",
                    "default": True,
                    "description": "Preview only (true) or apply (false).",
                },
            },
            "required": ["item_ids"],
        },
        "annotations": _WRITE,
    },
    {
        "name": "retag_by_query",
        "description": "Finds items by title/url substring (plus optional tag "
        "filters) and adds/removes tags on them. Defaults to dry_run=true, which "
        "returns the affected items without changing anything; pass dry_run=false "
        "to apply. At most 100 items are processed per call; the result reports "
        "total_matched and truncated.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Substring matched against title and url.",
                },
                **_TAG_FILTER_PROPS,
                "add": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags to add.",
                },
                "remove": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags to remove.",
                },
                "dry_run": {
                    "type": "boolean",
                    "default": True,
                    "description": "Preview only (true) or apply (false).",
                },
            },
        },
        "annotations": _WRITE,
    },
    {
        "name": "bulk_update",
        "description": "Updates many library items at once: sets the status, "
        "pins or unpins, and adds or removes tags on every item matching a "
        "filter. Filters: status group (saved/extracting/failed), saved date "
        "range (since/until), domain, pinned, type (note/url) and tags. At "
        "least one filter is required. Defaults to dry_run=true, which "
        "reports what would change without touching anything; pass "
        "dry_run=false to apply. At most 200 items are updated per call; the "
        "result reports total_matched, affected and truncated. Use list_items "
        "to see what a filter matches before applying.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["all", "saved", "extracting", "failed"],
                    "description": "Only items in this status group: 'saved' "
                    "(extracted), 'extracting' (queued) or 'failed'.",
                },
                "since": {
                    "type": "string",
                    "description": "ISO datetime: only items saved at/after it.",
                },
                "until": {
                    "type": "string",
                    "description": "ISO datetime: only items saved at/before it.",
                },
                "domain": {
                    "type": "string",
                    "description": "Only items from this host (e.g. example.com).",
                },
                "pinned": {
                    "type": "boolean",
                    "description": "Only pinned (true) or only unpinned (false).",
                },
                "type": {
                    "type": "string",
                    "enum": ["note", "url"],
                    "description": "Only notes or only saved urls.",
                },
                **_TAG_FILTER_PROPS,
                "set_status": {
                    "type": "string",
                    "enum": ["saved", "extracting", "failed"],
                    "description": "Set the status of the matched items: "
                    "'saved' (extracted), 'extracting' (queued again - it does "
                    "not re-run extraction) or 'failed'.",
                },
                "pin": {
                    "type": "boolean",
                    "description": "Pin (true) or unpin (false) the matched items.",
                },
                "add_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags to add (at most 20).",
                },
                "remove_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags to remove (at most 20).",
                },
                "dry_run": {
                    "type": "boolean",
                    "default": True,
                    "description": "Preview only (true) or apply (false).",
                },
            },
        },
        "annotations": _DESTRUCTIVE,
    },
    {
        "name": "set_item_tags",
        "description": "Replaces the whole tag set of one item with the given "
        "tags. Tags not in the list are removed. Use tag_item to add or remove "
        "a few tags without touching the rest.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "description": "The library item ID."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The complete set of tags the item should have.",
                },
            },
            "required": ["item_id", "tags"],
        },
        "annotations": _WRITE,
    },
    {
        "name": "suggest_tags",
        "description": "Suggests tags for an item (item_id) or for free text "
        "(text). Suggestions are drawn from the user's existing tag vocabulary "
        "first, then from keyphrases. No network calls. Use tag_items or "
        "set_item_tags to apply them.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "Library item to suggest tags for.",
                },
                "text": {
                    "type": "string",
                    "description": "Alternative: free text to suggest tags for.",
                },
            },
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "search_tags",
        "description": "Fuzzy search over tag names and descriptions: matches "
        "substrings and separator variants, so 'machinelearning' finds "
        "'machine-learning'. Use list_tags to enumerate, search_tags to look one up.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to look for."},
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Max tags to return.",
                },
            },
            "required": ["query"],
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "tag_stats",
        "description": "Returns tag usage statistics: total tags, total items, "
        "tagged and untagged item counts, and the most used tags.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "related_tags",
        "description": "Returns tags that co-occur with a tag on the same items, "
        "most frequent first. Use it to find a tag's neighbourhood in the "
        "vocabulary.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Tag name."},
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max related tags to return.",
                },
            },
            "required": ["tag"],
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "tag_cleanup",
        "description": "Finds cleanup candidates in the tag vocabulary: "
        "near-duplicate tags (case/plural/separator/acronym variants such as "
        "'ml' and 'machine-learning'), unused tags with no items, and untagged "
        "items. Read-only: it reports what could be cleaned up, it never "
        "changes anything.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Max untagged items to sample.",
                },
            },
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "find_cleanup",
        "description": "Suggests cleanup candidates in the library: duplicate "
        "items (same normalized url), dead links, failed extractions, "
        "never-opened old items, untagged items, and near-duplicate tags. Each "
        "category reports the candidates, the total number of candidates and "
        "whether the list was truncated. The item categories list one id with "
        "a reason; the group categories list one entry per group: duplicates "
        "gives the group's ids (capped), its size and whether the id list was "
        "truncated, near-duplicate tags gives the group's tag names. "
        "Read-only: it reports what could be cleaned up, it never changes "
        "anything. Dead links are items whose recorded failure is an origin "
        "HTTP error and failed extractions are every other recorded failure; "
        "no network requests are made, so a link that was never fetched is "
        "not detected.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "categories": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(CLEANUP_CATEGORIES)},
                    "description": (
                        "Categories to check (default: all). One or more of "
                        + ", ".join(CLEANUP_CATEGORIES)
                        + "."
                    ),
                },
                "older_than_days": {
                    "type": "integer",
                    "default": DEFAULT_OLDER_THAN_DAYS,
                    "description": (
                        "For never_opened: how old an item must be (in days) "
                        "to count as never opened."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "default": DEFAULT_CLEANUP_LIMIT,
                    "description": (
                        f"Max entries per category (at most {MAX_CLEANUP_LIMIT})."
                        " For duplicates, one entry per group of duplicate urls."
                    ),
                },
            },
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "create_highlight",
        "description": "Highlights a passage of a saved item: stores the quoted "
        "text and the character offsets it covers inside the item's "
        "content_text. Use it to mark the parts of an article worth "
        "re-reading. Use list_highlights to read the highlights of an item "
        "back and delete_highlight to remove one.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "The library item ID the highlight belongs to.",
                },
                "text": {
                    "type": "string",
                    "description": "The quoted passage.",
                },
                "start_offset": {
                    "type": "integer",
                    "default": 0,
                    "description": (
                        "First character offset of the passage inside "
                        "content_text. Defaults to 0."
                    ),
                },
                "end_offset": {
                    "type": "integer",
                    "description": (
                        "Offset just past the last character of the passage. "
                        "Defaults to start_offset plus the length of text."
                    ),
                },
                "note": {
                    "type": "string",
                    "description": "Optional note about the highlight.",
                },
                "color": {
                    "type": "string",
                    "description": "Optional color, e.g. #ffd60a.",
                },
            },
            "required": ["item_id", "text"],
        },
        "annotations": _WRITE,
    },
    {
        "name": "list_highlights",
        "description": "Lists the highlights of one saved item, ordered by "
        "their position in the text. Each highlight carries the quoted text, "
        "its character offsets in content_text, an optional note and color. "
        "Use get_item to fetch the content the offsets refer to.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "The library item ID to list highlights of.",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Max highlights to return (at most 100).",
                },
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "description": "Pagination offset.",
                },
            },
            "required": ["item_id"],
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "delete_highlight",
        "description": "Deletes one highlight. The item it belongs to is never "
        "deleted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "highlight_id": {
                    "type": "string",
                    "description": "The highlight ID to delete.",
                },
            },
            "required": ["highlight_id"],
        },
        "annotations": _DESTRUCTIVE,
    },
    {
        "name": "update_highlight",
        "description": "Edits a highlight: its note and/or its color. The quoted "
        "text and the offsets are never changed, and the item it belongs to "
        "is untouched. An empty note or color removes it. Use list_highlights "
        "to read a highlight back.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "highlight_id": {
                    "type": "string",
                    "description": "The highlight ID to edit.",
                },
                "note": {
                    "type": "string",
                    "description": (
                        "The new note. An empty string removes the note. "
                        "Leave the argument out to keep it."
                    ),
                },
                "color": {
                    "type": "string",
                    "description": (
                        "The new color, e.g. #ffd60a. An empty string removes "
                        "the color. Leave the argument out to keep it."
                    ),
                },
            },
            "required": ["highlight_id"],
        },
        "annotations": _WRITE,
    },
    {
        "name": "create_collection",
        "description": "Creates a collection: a named bucket of library items, "
        "optionally with a description. Idempotent: an existing collection "
        "with the same name is returned unchanged. With a query and/or a tag "
        "the collection is a smart one: its members are the items the search "
        "finds, resolved at read time. Use list_collections to see the "
        "collections and what they hold.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Collection name.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional human description of the collection.",
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Optional search query. With it the collection is a "
                        "smart one: its members are the items the search finds."
                    ),
                },
                "tag": {
                    "type": "string",
                    "description": (
                        "Optional tag filter. It narrows the members of a "
                        "smart collection to the items carrying that tag."
                    ),
                },
            },
            "required": ["name"],
        },
        "annotations": _WRITE,
    },
    {
        "name": "list_collections",
        "description": "Lists the user's collections with their kind (manual or "
        "smart), the number of items each holds and the ids of the items it "
        "holds (at most 20 per collection, with items_truncated when there "
        "are more). A smart collection holds the items its query finds, "
        "resolved at read time. Use add_to_collection to put items in and "
        "remove_from_collection to take them out.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Max collections to return (at most 50).",
                },
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "description": "Pagination offset.",
                },
            },
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "add_to_collection",
        "description": "Adds library items to a collection, addressed by "
        "collection name or id. Items that are not yours, that do not exist, "
        "or that are already in the collection are skipped: the result "
        "reports how many were added. At most 100 items are processed per "
        "call.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": {
                    "type": "string",
                    "description": "Collection name or id.",
                },
                "item_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Library item ids to add (at most 100).",
                },
            },
            "required": ["collection", "item_ids"],
        },
        "annotations": _WRITE,
    },
    {
        "name": "remove_from_collection",
        "description": "Removes library items from a collection. The items "
        "themselves are never deleted, only the link to the collection. At "
        "most 100 items are processed per call.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": {
                    "type": "string",
                    "description": "Collection name or id.",
                },
                "item_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Library item ids to remove (at most 100).",
                },
            },
            "required": ["collection", "item_ids"],
        },
        "annotations": _DESTRUCTIVE,
    },
    {
        "name": "rename_collection",
        "description": "Renames a collection, addressed by its current name or "
        "id, and sets its smart filter. The new name must be free; an "
        "existing collection is never merged into. A query and/or a tag turn "
        "the collection into a smart one (its members are resolved by the "
        "search); an empty query removes the filter.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": {
                    "type": "string",
                    "description": "Current collection name or id.",
                },
                "new_name": {
                    "type": "string",
                    "description": "The new collection name.",
                },
                "query": {
                    "type": "string",
                    "description": (
                        "The new search query. An empty string removes it. "
                        "Leave the argument out to keep it."
                    ),
                },
                "tag": {
                    "type": "string",
                    "description": (
                        "The new tag filter. An empty string removes it. "
                        "Leave the argument out to keep it."
                    ),
                },
            },
            "required": ["collection", "new_name"],
        },
        "annotations": _WRITE,
    },
    {
        "name": "delete_collection",
        "description": "Deletes a collection and its links. The items it held "
        "are never deleted. To empty a collection without deleting it, use "
        "remove_from_collection.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": {
                    "type": "string",
                    "description": "Collection name or id.",
                },
            },
            "required": ["collection"],
        },
        "annotations": _DESTRUCTIVE,
    },
    {
        "name": "delete_item",
        "description": "Moves an item to the Trash. The item leaves the library "
        "but is not destroyed: it stays restorable for 30 days, and only the "
        "user can delete it for good from the Trash view.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "The library item ID to move to the Trash.",
                }
            },
            "required": ["item_id"],
        },
        "annotations": _DESTRUCTIVE,
    },
    {
        "name": "pin_item",
        "description": "Pins or unpins an item to/from the top shelf of the library.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "pinned": {"type": "boolean"},
            },
            "required": ["item_id"],
        },
        "annotations": _WRITE,
    },
    {
        "name": "library_digest",
        "description": "Summarizes the library for a period: how many items "
        "were saved and opened, how many are still unread, the most used "
        "tags, and a short list of unread highlights. Every count covers "
        "the period (a tag filter narrows it further). Read-only and "
        "compact: 'truncated' says a list or a string was cut. Use it for "
        "a recap ('what did I save last week'); use list_items or "
        "reading_history for the items themselves.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": (
                        "ISO date/datetime: only saves and opens at/after it."
                    ),
                },
                "until": {
                    "type": "string",
                    "description": (
                        "ISO date/datetime: only saves and opens at/before it."
                    ),
                },
                "tag": {
                    "type": "string",
                    "description": (
                        "Restrict the digest to items carrying this tag "
                        "(child tags count)."
                    ),
                },
            },
        },
        "annotations": _READ_ONLY,
    },
]


MCP_TOOLS_BY_NAME = {tool["name"]: tool for tool in MCP_TOOLS}


async def handle_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    db: Database,
    env: Any,
    user: dict[str, Any],
    *,
    scope: TenantScope | None = None,
) -> Any:
    user_id = user["id"]

    if tool_name == "save_url":
        url = arguments.get("url")
        if not url or not isinstance(url, str):
            raise _invalid("save_url requires a valid url")
        tags = _as_list(arguments.get("tags"))
        # v2 fields
        agent_summary = arguments.get("agent_summary")
        why_saved = arguments.get("why_saved")
        source_context = arguments.get("source_context")
        saved_by = arguments.get("saved_by")
        item, is_new = await save_item(
            db,
            env,
            user_id,
            url,
            tags,
            scope=scope,
            agent_summary=agent_summary,
            why_saved=why_saved,
            source_context=source_context,
            saved_by=saved_by,
        )
        return {
            "id": item["id"],
            "url": item["url"],
            "status": item["status"],
            "is_new": is_new,
            "message": "URL saved and enqueued for extraction."
            if is_new
            else "Existing URL updated with tags.",
        }

    elif tool_name == "search_passages":
        query = arguments.get("query", "")
        if not query:
            raise _invalid("search_passages requires a query")
        mode = arguments.get("mode", "hybrid")
        limit = arguments.get("limit", 10)
        max_chars = arguments.get("max_chars", 4000)
        passages, total = await search_chunks(
            db,
            env,
            user_id,
            query,
            mode=mode,
            limit=limit,
            max_chars=max_chars,
            scope=scope,
            **_tag_filters(arguments),
        )
        return {
            "passages": passages,
            "total": total,
            "limit": limit,
            "max_chars": max_chars,
        }

    elif tool_name == "read_item":
        item_id = arguments.get("item_id")
        if not item_id or not isinstance(item_id, str):
            raise _invalid("item_id is required")
        cursor = arguments.get("cursor", 0)
        max_tokens = arguments.get("max_tokens", 2000)
        format = arguments.get("format", "markdown")
        result = await read_item_chunks(
            db,
            env,
            user_id,
            item_id,
            cursor=cursor,
            max_tokens=max_tokens,
            format=format,
        )
        return result

    elif tool_name in ("search_library", "search"):
        query = arguments.get("query", "")
        if not query:
            raise _invalid("search_library requires a query")
        mode = arguments.get("mode", "hybrid")
        limit = arguments.get("limit", 10)
        offset = arguments.get("offset", 0)
        cursor = arguments.get("cursor")
        if cursor:
            offset = _decode_cursor(cursor)
        items, total = await hybrid_search(
            db,
            env,
            user_id,
            query,
            mode=mode,
            limit=limit,
            offset=offset,
            scope=scope,
            **_tag_filters(arguments),
        )
        return {
            "items": [
                {
                    "id": it["id"],
                    "title": it.get("title") or it["url"],
                    "url": it["url"],
                    "excerpt": it.get("snippet") or it.get("excerpt"),
                    "tags": it.get("tags", []),
                    "status": it.get("status"),
                    "rrf_score": it.get("rrf_score"),
                    "item_type": it.get("item_type", "url"),
                    "is_pinned": bool(it.get("is_pinned", 0)),
                    "user_notes": it.get("user_notes"),
                }
                for it in items
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    elif tool_name == "get_item":
        item_id = arguments.get("item_id")
        url = arguments.get("url")
        title = arguments.get("title")
        if not item_id and not url and not title:
            raise _invalid("get_item requires item_id, url or title")
        include_content = arguments.get("include_content", False)
        row = await resolve_item(db, user_id, item_id=item_id, url=url, title=title)
        if not row:
            raise _not_found("No item found for the given item_id, url or title.")
        found_item = await get_item(
            db, user_id, row["id"], include_content=include_content
        )
        if not found_item:
            raise _not_found("Item not found.")
        return {
            "id": found_item["id"],
            "title": found_item.get("title"),
            "url": found_item["url"],
            "byline": found_item.get("byline"),
            "published_date": found_item.get("published_date"),
            "site_name": found_item.get("site_name"),
            "word_count": found_item.get("word_count"),
            "tags": found_item.get("tags", []),
            "content_text": (
                found_item.get("content_text") or found_item.get("excerpt") or ""
            ),
            "truncated": bool(found_item.get("truncated", False)),
            "content_length": found_item.get("content_length", 0),
            "item_type": found_item.get("item_type", "url"),
            "is_pinned": bool(found_item.get("is_pinned", 0)),
            "user_notes": found_item.get("user_notes"),
            "summary": found_item.get("summary"),
            "image_url": found_item.get("image_url"),
            "read_state": found_item.get("read_state", "unread"),
            "saved_at": found_item.get("created_at"),
            "agent_summary": found_item.get("agent_summary"),
            "why_saved": found_item.get("why_saved"),
            "source_context": found_item.get("source_context"),
            "saved_by": found_item.get("saved_by"),
        }

    elif tool_name == "find_item":
        title = arguments.get("title")
        url = arguments.get("url")
        if not title and not url:
            raise _invalid("find_item requires title or url")
        limit = arguments.get("limit", 20)
        items = await find_items(
            db,
            user_id,
            title=title,
            url=url,
            limit=limit,
            **_tag_filters(arguments),
        )
        return {
            "ids": [it["id"] for it in items],
            "count": len(items),
            "items": [_project(it, None) for it in items],
        }

    elif tool_name == "list_items":
        limit = arguments.get("limit", 20)
        offset = arguments.get("offset", 0)
        cursor = arguments.get("cursor")
        if cursor:
            offset = _decode_cursor(cursor)
        fields = arguments.get("fields")
        items = await list_items_compact(
            db,
            user_id,
            sort=arguments.get("sort", "saved"),
            since=arguments.get("since"),
            until=arguments.get("until"),
            domain=arguments.get("domain"),
            pinned=arguments.get("pinned"),
            item_type=arguments.get("type"),
            limit=limit + 1,
            offset=offset,
            **_tag_filters(arguments),
        )
        has_more = len(items) > limit
        items = items[:limit]
        return {
            "items": [_project(it, fields) for it in items],
            "limit": limit,
            "offset": offset,
            "next_cursor": _encode_cursor(offset + limit) if has_more else None,
        }

    elif tool_name == "reading_history":
        limit = arguments.get("limit", 20)
        offset = arguments.get("offset", 0)
        cursor = arguments.get("cursor")
        if cursor:
            offset = _decode_cursor(cursor)
        items, total = await reading_history(
            db,
            user_id,
            since=arguments.get("since"),
            until=arguments.get("until"),
            query=arguments.get("query"),
            event=arguments.get("event", "all"),
            limit=limit,
            offset=offset,
            **_tag_filters(arguments),
        )
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    elif tool_name == "related_items":
        item_id = arguments.get("item_id")
        text = arguments.get("text")
        if not item_id and not text:
            raise _invalid("related_items requires either item_id or text")
        limit = arguments.get("limit", 10)
        items = await related_items(
            db, env, user_id, item_id=item_id, text=text, limit=limit, scope=scope
        )
        return {
            "items": items,
            "limit": limit,
        }

    elif tool_name == "tag_item":
        item_id = arguments.get("item_id")
        if not item_id or not isinstance(item_id, str):
            raise _invalid("item_id is required")
        add_tags = _as_list(arguments.get("add_tags"))
        remove_tags = _as_list(arguments.get("remove_tags"))
        if add_tags:
            await add_tags_to_item(db, user_id, item_id, add_tags)
        if remove_tags:
            await remove_tags_from_item(db, user_id, item_id, remove_tags)
        tag_item_res = await get_item(db, user_id, item_id)
        tags = tag_item_res.get("tags", []) if tag_item_res else []
        return {"id": item_id, "tags": tags}

    elif tool_name == "list_tags":
        tag_rows = await list_tags(
            db,
            user_id,
            sort=arguments.get("sort", "count"),
            prefix=arguments.get("prefix"),
            min_count=arguments.get("min_count"),
        )
        return {"tags": tag_rows, "count": len(tag_rows)}

    elif tool_name == "create_tag":
        name = arguments.get("name")
        if not name or not isinstance(name, str):
            raise _invalid("create_tag requires a name")
        existing = await get_tag(db, user_id, name)
        clean = await create_tag(
            db,
            user_id,
            name,
            description=arguments.get("description"),
            color=arguments.get("color"),
            parent=arguments.get("parent"),
        )
        if not clean:
            raise _invalid("Invalid tag name.")
        return {
            "name": clean,
            "created": existing is None,
            "tag": await get_tag(db, user_id, clean),
        }

    elif tool_name == "describe_tag":
        name = arguments.get("name")
        if not name or not isinstance(name, str):
            raise _invalid("describe_tag requires a name")
        tag = await get_tag(db, user_id, name)
        if not tag:
            raise _not_found("Tag not found.")
        children = await db.query_all(
            "SELECT name FROM tags WHERE user_id = ? AND parent_id = ?"
            " ORDER BY name ASC;",
            (user_id, tag["id"]),
        )
        count = await db.query_first(
            "SELECT COUNT(*) as c FROM item_tags it JOIN items i"
            " ON i.id = it.item_id AND i.user_id = ? AND i.deleted_at IS NULL"
            " WHERE it.tag_id = ?;",
            (user_id, tag["id"]),
        )
        return {
            **tag,
            "children": [r["name"] for r in children],
            "count": count["c"] if count else 0,
        }

    elif tool_name == "update_tag":
        name = arguments.get("name")
        if not name or not isinstance(name, str):
            raise _invalid("update_tag requires a name")
        updated = await update_tag(
            db,
            user_id,
            name,
            description=arguments.get("description"),
            color=arguments.get("color"),
            parent=arguments.get("parent"),
        )
        if not updated:
            raise _not_found("Tag not found.")
        return {"updated": True, "tag": await get_tag(db, user_id, name)}

    elif tool_name == "rename_tag":
        old = arguments.get("old")
        new = arguments.get("new")
        if not old or not new:
            raise _invalid("rename_tag requires old and new")
        outcome = await rename_tag(db, user_id, old, new)
        if outcome == "not_found":
            raise _not_found("Tag not found.")
        if outcome == "invalid":
            raise _invalid("Invalid tag name.")
        return {"result": outcome, "tag": await get_tag(db, user_id, new)}

    elif tool_name == "merge_tags":
        sources = _as_list(arguments.get("sources"))
        target = arguments.get("target")
        if not sources or not target:
            raise _invalid("merge_tags requires sources and target")
        if len(sources) > MAX_MERGE_SOURCES:
            raise _invalid(
                f"merge_tags accepts at most {MAX_MERGE_SOURCES} source tags.",
                max_sources=MAX_MERGE_SOURCES,
                received=len(sources),
            )
        outcome = await merge_tags(db, user_id, sources, target)
        if outcome == "invalid":
            raise _invalid("Invalid tag name.")
        return {"result": outcome, "tag": await get_tag(db, user_id, target)}

    elif tool_name == "delete_tag":
        name = arguments.get("name")
        if not name or not isinstance(name, str):
            raise _invalid("delete_tag requires a name")
        dry_run = _dry_run_flag(arguments)
        result = await delete_tag_dry(db, user_id, name, dry_run=dry_run)
        if not result["found"]:
            raise _not_found("Tag not found.")
        return {**result, "dry_run": dry_run}

    elif tool_name == "tag_items":
        item_ids = _as_list(arguments.get("item_ids"))
        if not item_ids:
            raise _invalid("tag_items requires item_ids")
        dry_run = _dry_run_flag(arguments)
        result = await tag_items(
            db,
            user_id,
            item_ids,
            add=_as_list(arguments.get("add")),
            remove=_as_list(arguments.get("remove")),
            dry_run=dry_run,
        )
        return {**result, "dry_run": dry_run}

    elif tool_name == "retag_by_query":
        add = _as_list(arguments.get("add"))
        remove = _as_list(arguments.get("remove"))
        if not add and not remove:
            raise _invalid("retag_by_query requires add and/or remove")
        dry_run = _dry_run_flag(arguments)
        filters = _tag_filters(arguments)
        tag_clause, tag_params = await tag_filter_clause(
            db,
            user_id,
            tags_all=filters["tags_all"],
            tags_any=filters["tags_any"],
            tags_none=filters["tags_none"],
            untagged=filters["untagged"],
        )
        result = await retag_by_query(
            db,
            user_id,
            query=arguments.get("query"),
            add=add,
            remove=remove,
            dry_run=dry_run,
            tag_clause=tag_clause,
            tag_params=tag_params,
        )
        return {**result, "dry_run": dry_run}

    elif tool_name == "bulk_update":
        filters = _bulk_update_filters(arguments)
        set_status = arguments.get("set_status")
        if set_status is not None:
            set_status = str(set_status).strip().lower()
            if set_status not in STATUS_GROUPS:
                raise _invalid(
                    "bulk_update set_status must be one of: "
                    + ", ".join(sorted(STATUS_GROUPS))
                )
        pin = arguments.get("pin")
        if pin is not None and not isinstance(pin, bool):
            raise _invalid("bulk_update pin must be a boolean")
        add_tags = _as_list(arguments.get("add_tags"))
        remove_tags = _as_list(arguments.get("remove_tags"))
        if len(add_tags) > MAX_BULK_TAGS or len(remove_tags) > MAX_BULK_TAGS:
            raise _invalid(
                f"bulk_update accepts at most {MAX_BULK_TAGS} tags per list."
            )
        for label, names in (("add_tags", add_tags), ("remove_tags", remove_tags)):
            invalid = [n for n in names if not validate_tag_name(n or "")]
            if invalid:
                raise _invalid(
                    f"bulk_update {label} has an invalid tag name: {invalid[0]!r}. "
                    "A tag is 2-64 characters of a-z, 0-9, space, _ or -."
                )
        if set_status is None and pin is None and not add_tags and not remove_tags:
            raise _invalid(
                "bulk_update requires a change: set_status, pin, add_tags or "
                "remove_tags."
            )

        dry_run = _dry_run_flag(arguments)
        total_matched, item_ids = await match_items(
            db, user_id, limit=BULK_UPDATE_LIMIT, **filters
        )
        affected = len(item_ids)
        result = {
            "total_matched": total_matched,
            "affected": affected,
            "truncated": total_matched > affected,
            "sample": item_ids[:SAMPLE_LIMIT],
            "dry_run": dry_run,
        }
        if not dry_run and item_ids:
            await bulk_update_items(
                db,
                user_id,
                item_ids,
                set_status=STATUS_GROUPS[set_status][0] if set_status else None,
                pin=pin,
                add_tags=add_tags,
                remove_tags=remove_tags,
            )
        return result

    elif tool_name == "set_item_tags":
        item_id = arguments.get("item_id")
        if not item_id or not isinstance(item_id, str):
            raise _invalid("set_item_tags requires an item_id")
        result = await set_item_tags(
            db, user_id, item_id, _as_list(arguments.get("tags"))
        )
        if not result["found"]:
            raise _not_found("Item not found.")
        return result

    elif tool_name == "suggest_tags":
        item_id = arguments.get("item_id")
        text = arguments.get("text")
        if not item_id and not text:
            raise _invalid("suggest_tags requires item_id or text")
        if item_id:
            suggestions = await suggest_tags_for_item(db, user_id, item_id)
        else:
            if not isinstance(text, str):
                raise _invalid("suggest_tags text must be a string")
            suggestions = await suggest_tags_for_text(db, user_id, text)
        return {"suggestions": suggestions, "count": len(suggestions)}

    elif tool_name == "search_tags":
        query = arguments.get("query")
        if not query or not isinstance(query, str):
            raise _invalid("search_tags requires a query")
        found_tags = await search_tags(
            db, user_id, query, limit=arguments.get("limit", 20)
        )
        return {"tags": found_tags, "count": len(found_tags)}

    elif tool_name == "tag_stats":
        return await tag_stats(db, user_id)

    elif tool_name == "related_tags":
        tag = arguments.get("tag")
        if not tag or not isinstance(tag, str):
            raise _invalid("related_tags requires a tag")
        related = await related_tags(db, user_id, tag, limit=arguments.get("limit", 10))
        return {"tags": related, "count": len(related)}

    elif tool_name == "tag_cleanup":
        limit = arguments.get("limit", 20)
        return {
            "near_duplicates": await find_duplicate_tags(db, user_id),
            "unused_tags": await find_unused_tags(db, user_id),
            "untagged_items": await find_untagged_items(db, user_id, limit=limit),
        }

    elif tool_name == "find_cleanup":
        categories = _as_list(arguments.get("categories"))
        unknown = [c for c in categories if c not in CLEANUP_CATEGORIES]
        if unknown:
            raise _invalid(
                "Unknown find_cleanup categories: "
                + ", ".join(unknown)
                + ". Valid categories: "
                + ", ".join(CLEANUP_CATEGORIES)
                + ".",
                valid_categories=list(CLEANUP_CATEGORIES),
            )
        limit = _bounded_int(
            arguments.get("limit"),
            DEFAULT_CLEANUP_LIMIT,
            "limit",
            1,
            MAX_CLEANUP_LIMIT,
        )
        older_than_days = _bounded_int(
            arguments.get("older_than_days"),
            DEFAULT_OLDER_THAN_DAYS,
            "older_than_days",
            1,
            3650,
        )
        return await find_cleanup(
            db,
            user_id,
            categories=categories or None,
            older_than_days=older_than_days,
            limit=limit,
        )

    elif tool_name == "create_highlight":
        item_id = arguments.get("item_id")
        if not item_id or not isinstance(item_id, str):
            raise _invalid("create_highlight requires an item_id")
        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _invalid("create_highlight requires text")
        text = text.strip()
        if len(text) > MAX_HIGHLIGHT_TEXT:
            raise _invalid(
                f"Highlight text is limited to {MAX_HIGHLIGHT_TEXT} characters.",
                max_chars=MAX_HIGHLIGHT_TEXT,
                received=len(text),
            )
        note = arguments.get("note")
        if note is not None:
            if not isinstance(note, str):
                raise _invalid("note must be a string")
            if len(note) > MAX_HIGHLIGHT_NOTE:
                raise _invalid(
                    f"A highlight note is limited to {MAX_HIGHLIGHT_NOTE} characters.",
                    max_chars=MAX_HIGHLIGHT_NOTE,
                )
        color = arguments.get("color")
        if color is not None:
            if not isinstance(color, str):
                raise _invalid("color must be a string")
            if len(color) > MAX_HIGHLIGHT_COLOR:
                raise _invalid(
                    f"A highlight color is limited to {MAX_HIGHLIGHT_COLOR}"
                    " characters.",
                    max_chars=MAX_HIGHLIGHT_COLOR,
                )
        start_offset = _int_arg(arguments.get("start_offset"), 0)
        end_offset = _int_arg(arguments.get("end_offset"), start_offset + len(text))
        if end_offset < start_offset:
            raise _invalid("end_offset must not be smaller than start_offset")
        if end_offset > MAX_HIGHLIGHT_OFFSET:
            raise _invalid(
                f"Offsets are limited to {MAX_HIGHLIGHT_OFFSET} characters.",
                max_offset=MAX_HIGHLIGHT_OFFSET,
            )
        content_length = await item_content_length(db, user_id, item_id)
        if content_length is None:
            raise _not_found("Item not found.")
        # A length of 0 means the item has no stored content yet (still
        # queued): there is nothing to check the offsets against.
        if content_length and end_offset > content_length:
            raise _invalid(
                f"end_offset {end_offset} is past the end of the item content"
                f" ({content_length} characters).",
                content_length=content_length,
            )
        highlight = await create_highlight(
            db,
            user_id,
            item_id,
            text,
            start_offset,
            end_offset,
            note=note,
            color=color,
        )
        if not highlight:
            raise _not_found("Item not found.")
        return {"highlight": highlight, "created": True}

    elif tool_name == "list_highlights":
        item_id = arguments.get("item_id")
        if not item_id or not isinstance(item_id, str):
            raise _invalid("list_highlights requires an item_id")
        limit = _int_arg(arguments.get("limit"), 20, maximum=MAX_HIGHLIGHTS_LIMIT)
        offset = _int_arg(arguments.get("offset"), 0)
        highlights = await list_highlights(
            db, user_id, item_id, limit=limit, offset=offset
        )
        return {
            "highlights": highlights,
            "count": len(highlights),
            "limit": limit,
            "offset": offset,
        }

    elif tool_name == "delete_highlight":
        highlight_id = arguments.get("highlight_id")
        if not highlight_id or not isinstance(highlight_id, str):
            raise _invalid("delete_highlight requires a highlight_id")
        deleted = await delete_highlight(db, user_id, highlight_id)
        if not deleted:
            raise _not_found("Highlight not found.")
        return {"id": highlight_id, "deleted": True}

    elif tool_name == "update_highlight":
        highlight_id = arguments.get("highlight_id")
        if not highlight_id or not isinstance(highlight_id, str):
            raise _invalid("update_highlight requires a highlight_id")
        note = _highlight_field(arguments, "note", MAX_HIGHLIGHT_NOTE)
        color = _highlight_field(arguments, "color", MAX_HIGHLIGHT_COLOR)
        highlight = await update_highlight(
            db, user_id, highlight_id, note=note, color=color
        )
        if not highlight:
            raise _not_found("Highlight not found.")
        return {"highlight": highlight, "updated": True}

    elif tool_name == "create_collection":
        name = arguments.get("name")
        if not isinstance(name, str) or not name.strip():
            raise _invalid("create_collection requires a name")
        if len(name.strip()) > MAX_COLLECTION_NAME:
            raise _invalid(
                f"A collection name is limited to {MAX_COLLECTION_NAME} characters.",
                max_chars=MAX_COLLECTION_NAME,
            )
        description = arguments.get("description")
        if description is not None:
            if not isinstance(description, str):
                raise _invalid("description must be a string")
            if len(description) > MAX_COLLECTION_DESCRIPTION:
                raise _invalid(
                    f"A collection description is limited to"
                    f" {MAX_COLLECTION_DESCRIPTION} characters.",
                    max_chars=MAX_COLLECTION_DESCRIPTION,
                )
        query = _collection_filter(arguments, "query", MAX_COLLECTION_QUERY)
        tag = _collection_filter(arguments, "tag", MAX_COLLECTION_TAG)
        collection = await create_collection(
            db,
            user_id,
            name,
            description=description,
            query=query or None,
            tag=tag or None,
        )
        if not collection:
            raise _invalid("Invalid collection name.")
        return collection

    elif tool_name == "list_collections":
        limit = _int_arg(arguments.get("limit"), 20, maximum=MAX_COLLECTIONS_LIMIT)
        offset = _int_arg(arguments.get("offset"), 0)
        collections = await list_collections(db, user_id, limit=limit, offset=offset)
        # A smart collection holds no links: its members are resolved now,
        # through the search, so the collection stays current.
        smart = [c for c in collections if c["kind"] == "smart"]
        if smart:
            resolved = await asyncio.gather(
                *[
                    _smart_members(db, env, user_id, collection, scope)
                    for collection in smart
                ]
            )
            for collection, (item_ids, count) in zip(smart, resolved):
                collection["item_ids"] = item_ids
                collection["item_count"] = count
                collection["items_truncated"] = count > len(item_ids)
        return {
            "collections": collections,
            "count": len(collections),
            "limit": limit,
            "offset": offset,
        }

    elif tool_name == "add_to_collection":
        collection = arguments.get("collection")
        if not collection or not isinstance(collection, str):
            raise _invalid("add_to_collection requires a collection")
        item_ids = [i for i in _as_list(arguments.get("item_ids")) if i]
        if not item_ids:
            raise _invalid("add_to_collection requires item_ids")
        result = await add_items_to_collection(db, user_id, collection, item_ids)
        if not result["found"]:
            raise _not_found("Collection not found.")
        if result.get("smart"):
            raise _invalid(
                f"Collection '{result['collection']}' is a smart collection: its "
                "members are resolved by its query, so no item can be added to "
                "it. Use rename_collection to change the filter."
            )
        return result

    elif tool_name == "remove_from_collection":
        collection = arguments.get("collection")
        if not collection or not isinstance(collection, str):
            raise _invalid("remove_from_collection requires a collection")
        item_ids = [i for i in _as_list(arguments.get("item_ids")) if i]
        if not item_ids:
            raise _invalid("remove_from_collection requires item_ids")
        result = await remove_items_from_collection(db, user_id, collection, item_ids)
        if not result["found"]:
            raise _not_found("Collection not found.")
        if result.get("smart"):
            raise _invalid(
                f"Collection '{result['collection']}' is a smart collection: its "
                "members are resolved by its query, so no item can be removed "
                "from it. Use rename_collection to change the filter."
            )
        return result

    elif tool_name == "rename_collection":
        collection = arguments.get("collection")
        new_name = arguments.get("new_name")
        if not collection or not isinstance(collection, str):
            raise _invalid("rename_collection requires a collection")
        if not isinstance(new_name, str) or not new_name.strip():
            raise _invalid("rename_collection requires a new_name")
        if len(new_name.strip()) > MAX_COLLECTION_NAME:
            raise _invalid(
                f"A collection name is limited to {MAX_COLLECTION_NAME} characters.",
                max_chars=MAX_COLLECTION_NAME,
            )
        query = _collection_filter(arguments, "query", MAX_COLLECTION_QUERY)
        tag = _collection_filter(arguments, "tag", MAX_COLLECTION_TAG)
        outcome = await rename_collection(
            db, user_id, collection, new_name, query=query, tag=tag
        )
        if outcome["outcome"] == "not_found":
            raise _not_found("Collection not found.")
        if outcome["outcome"] == "invalid":
            raise _invalid("Invalid collection name.")
        if outcome["outcome"] == "exists":
            raise _invalid("A collection with that name already exists.")
        renamed = outcome["collection"]
        return {
            "collection": renamed["name"],
            "renamed": True,
            "kind": renamed["kind"],
            "query": renamed["query"],
            "tag": renamed["tag"],
        }

    elif tool_name == "delete_collection":
        collection = arguments.get("collection")
        if not collection or not isinstance(collection, str):
            raise _invalid("delete_collection requires a collection")
        collection_row = await delete_collection(db, user_id, collection)
        if not collection_row:
            raise _not_found("Collection not found.")
        return collection_row

    elif tool_name == "delete_item":
        item_id = arguments.get("item_id")
        if not item_id or not isinstance(item_id, str):
            raise _invalid("item_id is required")
        success = await trash_item(db, user_id, item_id)
        return {
            "id": item_id,
            "trashed": success,
            "message": (
                "Moved to Trash. The user can restore it for 30 days."
                if success
                else "Item not found."
            ),
        }

    elif tool_name == "save_note":
        title = arguments.get("title", "")
        content = arguments.get("content", "")
        tags = _as_list(arguments.get("tags"))
        is_pinned = bool(arguments.get("is_pinned", False))
        # v2 fields
        agent_summary = arguments.get("agent_summary")
        why_saved = arguments.get("why_saved")
        source_context = arguments.get("source_context")
        saved_by = arguments.get("saved_by")
        if not (title or "").strip() and not (content or "").strip():
            raise _invalid("save_note requires a title or content")
        note = await save_note(
            db,
            env,
            user_id,
            title,
            content,
            tags,
            is_pinned,
            scope=scope,
            agent_summary=agent_summary,
            why_saved=why_saved,
            source_context=source_context,
            saved_by=saved_by,
        )
        return {
            "id": note["id"],
            "title": note["title"],
            "status": note["status"],
            "item_type": "note",
            "message": "Note saved and indexed for hybrid search.",
        }

    elif tool_name == "pin_item":
        item_id = arguments.get("item_id")
        if not item_id or not isinstance(item_id, str):
            raise _invalid("item_id is required")
        pinned = arguments.get("pinned")
        if pinned is None:
            new_state = await toggle_pin_item(db, user_id, item_id)
            if new_state is None:
                raise _not_found("Item not found.")
            return {"id": item_id, "is_pinned": new_state}
        target_item = await get_item(db, user_id, item_id)
        if not target_item:
            raise _not_found("Item not found.")
        want = bool(pinned)
        if bool(target_item.get("is_pinned", 0)) != want:
            await toggle_pin_item(db, user_id, item_id)
        return {"id": item_id, "is_pinned": want}

    elif tool_name == "library_digest":
        since = _digest_date(arguments.get("since"), "since")
        until = _digest_date(arguments.get("until"), "until")
        if since and until and since > until:
            raise _invalid("since must not be after until")
        return await library_digest(
            db,
            user_id,
            since=since,
            until=until,
            tag=_digest_tag(arguments.get("tag")),
        )

    else:
        raise _not_found(f"Unknown tool: {tool_name}")


async def process_mcp_request(
    body: dict[str, Any],
    db: Database,
    env: Any,
    user: dict[str, Any],
    *,
    scope: TenantScope | None = None,
) -> dict[str, Any] | None:
    """Handles an incoming JSON-RPC 2.0 MCP message.

    Returns the response payload for requests, or None for notifications
    and JSON-RPC responses, which carry no reply: the HTTP transport
    answers those with 202 and an empty body (stateless server: nothing
    to cancel or correlate, so they are accepted and ignored).
    """
    if "method" not in body or "id" not in body:
        return None

    if scope is None:
        from keepfor.runtime import get_providers
        from keepfor.spi import Principal

        providers = get_providers()
        if providers.scope:
            # A token without scope info is never rejected, it is treated as
            # unrestricted ("*").
            principal = Principal(
                user_id=user["id"],
                tenant_id=user.get("tenant_id", user["id"]),
                email=user.get("email", ""),
                role=user.get("role", "user"),
                plan=user.get("plan", "self_hosted"),
                scopes=scopes_of(user),
            )
            scope = await providers.scope.scope_for_principal(principal, env)
        else:
            from keepfor.defaults import default_scope

            scope = default_scope(db, env, user["id"])

    method = body.get("method")
    req_id = body.get("id")
    scopes = scopes_of(user)

    if method == "initialize":
        params = body.get("params", {})
        version = negotiate_protocol_version(params)
        if version is None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32602,
                    "message": (
                        f"Unsupported protocol version: {params.get('protocolVersion')}"
                    ),
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {"name": "keepfor-me-mcp", "version": "0.2.0"},
            },
        }

    elif method == "tools/list":
        tools = [t for t in MCP_TOOLS if scope_grants(scopes, _tool_scope(t))]
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}

    elif method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        tool = MCP_TOOLS_BY_NAME.get(tool_name)
        if tool is None:
            # Fail closed: a handler without a schema entry must not skip
            # the scope check.
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32602,
                    "message": f"Unknown tool: {tool_name}",
                    "data": {"type": "not_found", "tool": tool_name},
                },
            }
        required = _tool_scope(tool)
        if not scope_grants(scopes, required):
            # Refused before the tool runs: nothing is written.
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32001,
                    "message": (
                        f"Tool '{tool_name}' needs the '{required}' scope; "
                        f"this token has {', '.join(sorted(scopes)) or 'none'}."
                    ),
                    "data": {
                        "type": "insufficient_scope",
                        "tool": tool_name,
                        "required_scope": required,
                        "scopes": sorted(scopes),
                    },
                },
            }
        try:
            res = await handle_tool_call(
                tool_name, arguments, db, env, user, scope=scope
            )
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            # Compact JSON: the payload is read by agents, so
                            # indentation is wasted tokens (and breaks the
                            # 4 KB budget for a default list_items page).
                            "text": json.dumps(res, ensure_ascii=False),
                        }
                    ]
                },
            }
        except ToolError as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32602,
                    "message": str(exc),
                    "data": {"type": exc.kind, "tool": tool_name, **exc.details},
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32603,
                    "message": str(exc),
                    "data": {"type": "internal_error", "tool": tool_name},
                },
            }

    elif method in _RESOURCE_PROMPT_METHODS:
        # Resources and prompts live in their own module: it keeps this
        # dispatch table small and the module is only imported when a
        # client actually asks for a resource or a prompt.
        from keepfor.mcp.resources import handle_resource_request

        return await handle_resource_request(method, body, db, env, user, scope=scope)

    else:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32601,
                "message": f"Method '{method}' not found",
            },
        }
