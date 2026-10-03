import json
from typing import Any

from src.models.db import Database
from src.models.items import (
    add_tags_to_item,
    delete_item,
    get_item,
    remove_tags_from_item,
    save_item,
)
from src.search.engine import get_recent_items, hybrid_search

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
]


async def handle_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    db: Database,
    env: Any,
    user: dict[str, Any],
) -> Any:
    user_id = user["id"]

    if tool_name == "save_url":
        url = arguments.get("url")
        tags = arguments.get("tags", [])
        item, is_new = await save_item(db, env, user_id, url, tags)
        return {
            "id": item["id"],
            "url": item["url"],
            "status": item["status"],
            "is_new": is_new,
            "message": "URL saved and enqueued for extraction."
            if is_new
            else "Existing URL updated with tags.",
        }

    elif tool_name == "search_library":
        query = arguments.get("query", "")
        mode = arguments.get("mode", "hybrid")
        tag = arguments.get("tag")
        limit = arguments.get("limit", 10)
        items = await hybrid_search(
            db, env, user_id, query, mode=mode, tag=tag, limit=limit
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
        success = await delete_item(db, env, user_id, item_id)
        return {"id": item_id, "deleted": success}

    else:
        raise ValueError(f"Unknown tool: {tool_name}")


async def process_mcp_request(
    body: dict[str, Any], db: Database, env: Any, user: dict[str, Any]
) -> dict[str, Any]:
    """Handles an incoming JSON-RPC 2.0 MCP request."""
    method = body.get("method")
    req_id = body.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
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
            res = await handle_tool_call(tool_name, arguments, db, env, user)
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
