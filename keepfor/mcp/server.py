# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from keepfor.models.db import Database

if TYPE_CHECKING:
    from keepfor.spi import TenantScope
from keepfor.models.items import (
    add_tags_to_item,
    delete_item,
    get_item,
    remove_tags_from_item,
    save_item,
    save_note,
    toggle_pin_item,
)
from keepfor.search.engine import get_recent_items, hybrid_search

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
        "it for content extraction and semantic indexing.",
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
            },
            "required": ["url"],
        },
    },
    {
        "name": "search_library",
        "description": "Searches the library by keyword and meaning using "
        "hybrid search (FTS5 + Vectorize).",
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
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max results to return.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_item",
        "description": "Retrieves the full extracted text content and metadata "
        "of a saved article by item ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "The unique ID of the library item.",
                }
            },
            "required": ["item_id"],
        },
    },
    {
        "name": "list_items",
        "description": "Lists recent items from the library with pagination "
        "and tag filtering.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Filter by tag."},
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
            },
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
    },
    {
        "name": "delete_item",
        "description": "Permanently deletes an item, its raw/clean snapshots, "
        "and its vector embeddings.",
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
    },
    {
        "name": "save_note",
        "description": "Saves a personal note, code snippet, prompt template, "
        "or idea to the user's personal knowledge vault and indexes it "
        "for hybrid search.",
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
            },
            "required": ["title", "content"],
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
        tags = arguments.get("tags", [])
        item, is_new = await save_item(db, env, user_id, url, tags, scope=scope)
        return {
            "id": item["id"],
            "url": item["url"],
            "status": item["status"],
            "is_new": is_new,
            "message": "URL saved and enqueued for extraction."
            if is_new
            else "Existing URL updated with tags.",
        }

    elif tool_name in ("search_library", "search"):
        query = arguments.get("query", "")
        mode = arguments.get("mode", "hybrid")
        tag = arguments.get("tag")
        limit = arguments.get("limit", 10)
        items, _ = await hybrid_search(
            db,
            env,
            user_id,
            query,
            mode=mode,
            tag=tag,
            limit=limit,
            scope=scope,
        )
        return [
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
        ]

    elif tool_name == "get_item":
        item_id = arguments.get("item_id")
        item = await get_item(db, user_id, item_id)
        if not item:
            raise ValueError(f"Item not found with id: {item_id}")
        return {
            "id": item["id"],
            "title": item.get("title"),
            "url": item["url"],
            "byline": item.get("byline"),
            "published_date": item.get("published_date"),
            "site_name": item.get("site_name"),
            "word_count": item.get("word_count"),
            "tags": item.get("tags", []),
            "content_text": item.get("content_text") or item.get("excerpt") or "",
            "item_type": item.get("item_type", "url"),
            "is_pinned": bool(item.get("is_pinned", 0)),
            "user_notes": item.get("user_notes"),
            "summary": item.get("summary"),
            "image_url": item.get("image_url"),
            "read_state": item.get("read_state", "unread"),
        }

    elif tool_name == "list_items":
        tag = arguments.get("tag")
        limit = arguments.get("limit", 20)
        offset = arguments.get("offset", 0)
        items = await get_recent_items(db, user_id, tag=tag, limit=limit, offset=offset)
        return [
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
        ]

    elif tool_name == "tag_item":
        item_id = arguments.get("item_id")
        add_tags = arguments.get("add_tags", [])
        remove_tags = arguments.get("remove_tags", [])
        if add_tags:
            await add_tags_to_item(db, user_id, item_id, add_tags)
        if remove_tags:
            await remove_tags_from_item(db, user_id, item_id, remove_tags)
        item = await get_item(db, user_id, item_id)
        return {"id": item_id, "tags": item.get("tags", []) if item else []}

    elif tool_name == "delete_item":
        item_id = arguments.get("item_id")
        success = await delete_item(db, env, user_id, item_id, scope=scope)
        return {"id": item_id, "deleted": success}

    elif tool_name == "save_note":
        title = arguments.get("title", "")
        content = arguments.get("content", "")
        tags = arguments.get("tags", [])
        is_pinned = bool(arguments.get("is_pinned", False))
        if not (title or "").strip() and not (content or "").strip():
            raise ValueError("save_note requires a title or content")
        note = await save_note(
            db, env, user_id, title, content, tags, is_pinned, scope=scope
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
        pinned = arguments.get("pinned")
        if pinned is None:
            new_state = await toggle_pin_item(db, user_id, item_id)
            if new_state is None:
                raise ValueError(f"Item not found with id: {item_id}")
            return {"id": item_id, "is_pinned": new_state}
        item = await get_item(db, user_id, item_id)
        if not item:
            raise ValueError(f"Item not found with id: {item_id}")
        want = bool(pinned)
        if bool(item.get("is_pinned", 0)) != want:
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
    and JSON-RPC responses, which carry no reply — the HTTP transport
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
                "serverInfo": {"name": "keepfor-me-mcp", "version": "0.1.0"},
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
