# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from keepfor.models.db import Database
from keepfor.models.items import (
    add_tags_to_item,
    delete_item,
    get_item,
    remove_tags_from_item,
    save_item,
    save_note,
    toggle_pin_item,
)
from keepfor.search.engine import (
    get_recent_items,
    hybrid_search,
    read_item_chunks,
    reading_history,
    related_items,
    search_chunks,
)

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
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
        },
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
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    },
    {
        "name": "search_library",
        "description": "Searches the library by keyword and meaning using "
        "hybrid search (FTS5 + Vectorize). Matches saved articles, personal "
        "notes, and notes imported from Google Keep, Evernote (ENEX) or "
        "Markdown exports. Returns item-level results with snippets. For "
        "passage-level citations with snapshot URLs, use search_passages "
        "instead.",
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
                "untagged": {
                    "type": "boolean",
                    "default": False,
                    "description": "Show only items without tags.",
                },
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
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
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
                    "description": "Filter results by a specific tag.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by multiple tags (AND).",
                },
                "untagged": {
                    "type": "boolean",
                    "default": False,
                    "description": "Show only items without tags.",
                },
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
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
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
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    },
    {
        "name": "get_item",
        "description": "Retrieves metadata and excerpt of a saved article by item ID. "
        "By default, content_text is capped to the excerpt for token efficiency. "
        "Use include_content=true to get full text (not recommended for large items). "
        "For paginated full-text reading, use read_item instead.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "The unique ID of the library item.",
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
            "required": ["item_id"],
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    },
    {
        "name": "list_items",
        "description": "Lists recent items from the library with pagination "
        "and tag filtering. Supports cursor-based pagination. For search, "
        "use search_library. For reading history with open events, use "
        "reading_history instead.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Filter by tag."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by multiple tags (AND).",
                },
                "untagged": {
                    "type": "boolean",
                    "default": False,
                    "description": "Show only items without tags.",
                },
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
                    "description": "Projection: fields to include in response.",
                },
            },
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
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
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
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
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    },
    {
        "name": "tag_item",
        "description": "Adds or removes tags on a saved library item.",
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
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    },
    {
        "name": "delete_item",
        "description": "Permanently deletes an item, its raw/clean snapshots, "
        "and its vector embeddings. This action cannot be undone.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "The library item ID to delete.",
                }
            },
            "required": ["item_id"],
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
        },
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
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    },
]


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
            raise ValueError("save_url requires a valid url")
        tags = arguments.get("tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
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
            raise ValueError("search_passages requires a query")
        mode = arguments.get("mode", "hybrid")
        tag = arguments.get("tag")
        tags = arguments.get("tags")
        untagged = arguments.get("untagged", False)
        limit = arguments.get("limit", 10)
        max_chars = arguments.get("max_chars", 4000)
        passages, total = await search_chunks(
            db,
            env,
            user_id,
            query,
            mode=mode,
            tag=tag,
            tags=tags,
            untagged=untagged,
            limit=limit,
            max_chars=max_chars,
            scope=scope,
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
            raise ValueError("item_id is required")
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
        mode = arguments.get("mode", "hybrid")
        tag = arguments.get("tag")
        tags = arguments.get("tags")
        untagged = arguments.get("untagged", False)
        limit = arguments.get("limit", 10)
        offset = arguments.get("offset", 0)
        items, total = await hybrid_search(
            db,
            env,
            user_id,
            query,
            mode=mode,
            tag=tag,
            tags=tags,
            untagged=untagged,
            limit=limit,
            offset=offset,
            scope=scope,
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
        if not item_id or not isinstance(item_id, str):
            raise ValueError("item_id is required")
        include_content = arguments.get("include_content", False)
        found_item = await get_item(
            db, user_id, item_id, include_content=include_content
        )
        if not found_item:
            raise ValueError(f"Item not found with id: {item_id}")
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
            "item_type": found_item.get("item_type", "url"),
            "is_pinned": bool(found_item.get("is_pinned", 0)),
            "user_notes": found_item.get("user_notes"),
            "summary": found_item.get("summary"),
            "image_url": found_item.get("image_url"),
            "read_state": found_item.get("read_state", "unread"),
            "agent_summary": found_item.get("agent_summary"),
            "why_saved": found_item.get("why_saved"),
            "source_context": found_item.get("source_context"),
            "saved_by": found_item.get("saved_by"),
        }

    elif tool_name == "list_items":
        tag = arguments.get("tag")
        tags = arguments.get("tags")
        untagged = arguments.get("untagged", False)
        limit = arguments.get("limit", 20)
        offset = arguments.get("offset", 0)
        items = await get_recent_items(
            db,
            user_id,
            tag=tag,
            tags=tags,
            untagged=untagged,
            limit=limit,
            offset=offset,
        )
        return {
            "items": [
                {
                    "id": it["id"],
                    "title": it.get("title") or it["url"],
                    "url": it["url"],
                    "excerpt": it.get("excerpt"),
                    "tags": it.get("tags", []),
                    "status": it.get("status"),
                    "item_type": it.get("item_type", "url"),
                    "is_pinned": bool(it.get("is_pinned", 0)),
                }
                for it in items
            ],
            "limit": limit,
            "offset": offset,
        }

    elif tool_name == "reading_history":
        since = arguments.get("since")
        until = arguments.get("until")
        query = arguments.get("query")
        tag = arguments.get("tag")
        event = arguments.get("event", "all")
        limit = arguments.get("limit", 20)
        offset = arguments.get("offset", 0)
        items, total = await reading_history(
            db,
            user_id,
            since=since,
            until=until,
            query=query,
            tag=tag,
            event=event,
            limit=limit,
            offset=offset,
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
            raise ValueError("related_items requires either item_id or text")
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
            raise ValueError("item_id is required")
        add_tags = arguments.get("add_tags") or []
        if not isinstance(add_tags, list):
            add_tags = [str(add_tags)]
        remove_tags = arguments.get("remove_tags") or []
        if not isinstance(remove_tags, list):
            remove_tags = [str(remove_tags)]
        if add_tags:
            await add_tags_to_item(db, user_id, item_id, add_tags)
        if remove_tags:
            await remove_tags_from_item(db, user_id, item_id, remove_tags)
        tag_item_res = await get_item(db, user_id, item_id)
        tags = tag_item_res.get("tags", []) if tag_item_res else []
        return {"id": item_id, "tags": tags}

    elif tool_name == "delete_item":
        item_id = arguments.get("item_id")
        if not item_id or not isinstance(item_id, str):
            raise ValueError("item_id is required")
        success = await delete_item(db, env, user_id, item_id, scope=scope)
        return {"id": item_id, "deleted": success}

    elif tool_name == "save_note":
        title = arguments.get("title", "")
        content = arguments.get("content", "")
        tags = arguments.get("tags", [])
        is_pinned = bool(arguments.get("is_pinned", False))
        # v2 fields
        agent_summary = arguments.get("agent_summary")
        why_saved = arguments.get("why_saved")
        source_context = arguments.get("source_context")
        saved_by = arguments.get("saved_by")
        if not (title or "").strip() and not (content or "").strip():
            raise ValueError("save_note requires a title or content")
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
            raise ValueError("item_id is required")
        pinned = arguments.get("pinned")
        if pinned is None:
            new_state = await toggle_pin_item(db, user_id, item_id)
            if new_state is None:
                raise ValueError(f"Item not found with id: {item_id}")
            return {"id": item_id, "is_pinned": new_state}
        target_item = await get_item(db, user_id, item_id)
        if not target_item:
            raise ValueError(f"Item not found with id: {item_id}")
        want = bool(pinned)
        if bool(target_item.get("is_pinned", 0)) != want:
            await toggle_pin_item(db, user_id, item_id)
        return {"id": item_id, "is_pinned": want}

    else:
        raise ValueError(f"Unknown tool: {tool_name}")


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
            principal = Principal(
                user_id=user["id"],
                tenant_id=user.get("tenant_id", user["id"]),
                email=user.get("email", ""),
                role=user.get("role", "user"),
                plan=user.get("plan", "self_hosted"),
                scopes=frozenset(user.get("scopes", ["*"])),
            )
            scope = await providers.scope.scope_for_principal(principal, env)
        else:
            from keepfor.defaults import default_scope

            scope = default_scope(db, env, user["id"])

    method = body.get("method")
    req_id = body.get("id")

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
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "keepfor-me-mcp", "version": "0.2.0"},
            },
        }

    elif method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": MCP_TOOLS}}

    elif method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
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
                            "text": json.dumps(res, indent=2, ensure_ascii=False),
                        }
                    ]
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(exc)},
            }

    else:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method '{method}' not found"},
        }
