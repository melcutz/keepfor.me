# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""MCP resources and prompts (GitHub issue #23).

Resources are read-only views of the caller's own library:

- ``keepfor://item/{item_id}`` — one saved article or note.
- ``keepfor://tag/{name}`` — one tag of the caller's vocabulary.

Prompts are reusable templates that read the caller's own data:
``research_brief`` (what the user already saved about a topic) and
``weekly_review`` (what the user saved and opened recently).

Every query is scoped to the caller's ``user_id``, exactly like the tools.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote, urlparse

from keepfor.mcp.server import ToolError, _invalid, _not_found
from keepfor.models.items import get_item
from keepfor.models.tags import get_tag, list_tags
from keepfor.search.engine import hybrid_search, list_items_compact, reading_history

if TYPE_CHECKING:
    from keepfor.models.db import Database
    from keepfor.spi import TenantScope

# URI scheme of every resource this server exposes.
RESOURCE_SCHEME = "keepfor"

# Caps. A resource read is one item or one tag; a prompt lists a handful.
MAX_RESOURCE_URI_CHARS = 512
MAX_RESOURCE_CONTENT_CHARS = 20_000
MAX_RESOURCE_ITEMS = 50
MAX_RESOURCE_TAGS = 50
MAX_PROMPT_ARG_CHARS = 500
MAX_PROMPT_ITEMS = 20
DEFAULT_PROMPT_ITEMS = 10
MAX_PROMPT_DAYS = 90
DEFAULT_PROMPT_DAYS = 7

# Resources are views of the user's own data, useful to the user and to the
# assistant alike.
_RESOURCE_ANNOTATIONS = {"audience": ["user", "assistant"], "priority": 0.5}

MCP_RESOURCE_TEMPLATES = [
    {
        "uriTemplate": "keepfor://item/{item_id}",
        "name": "library_item",
        "title": "Library item",
        "description": (
            "A saved article or note from the user's library: metadata, "
            "tags and the stored text."
        ),
        "mimeType": "application/json",
        "annotations": _RESOURCE_ANNOTATIONS,
    },
    {
        "uriTemplate": "keepfor://tag/{name}",
        "name": "library_tag",
        "title": "Library tag",
        "description": "A tag of the user's vocabulary with the items carrying it.",
        "mimeType": "application/json",
        "annotations": _RESOURCE_ANNOTATIONS,
    },
]

MCP_PROMPTS = [
    {
        "name": "research_brief",
        "title": "Research brief",
        "description": (
            "Briefs the assistant on what the user already saved about a "
            "topic, so a research session starts from the user's own library."
        ),
        "arguments": [
            {
                "name": "topic",
                "description": "The topic to brief on.",
                "required": True,
            },
            {
                "name": "max_items",
                "description": (
                    f"How many saved items to include (1-{MAX_PROMPT_ITEMS}, "
                    f"default {DEFAULT_PROMPT_ITEMS})."
                ),
                "required": False,
            },
        ],
    },
    {
        "name": "weekly_review",
        "title": "Weekly review",
        "description": (
            "Reviews what the user saved over the last days and asks the "
            "assistant to summarize it and suggest what to read next."
        ),
        "arguments": [
            {
                "name": "days",
                "description": (
                    f"How many days back to review (1-{MAX_PROMPT_DAYS}, "
                    f"default {DEFAULT_PROMPT_DAYS})."
                ),
                "required": False,
            },
            {
                "name": "tag",
                "description": "Only items carrying this tag.",
                "required": False,
            },
        ],
    },
]


def item_uri(item_id: str) -> str:
    """The resource URI of one library item."""
    return f"{RESOURCE_SCHEME}://item/{quote(item_id, safe='')}"


def tag_uri(name: str) -> str:
    """The resource URI of one tag."""
    return f"{RESOURCE_SCHEME}://tag/{quote(name, safe='')}"


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    """Reads an integer argument, clamped to [low, high]."""
    if value is None or value == "":
        return default
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise _invalid(f"Expected a number, got {value!r}") from None
    return max(low, min(high, number))


def _capped_text(value: Any, name: str, *, required: bool = False) -> str:
    """A prompt argument as trimmed text, capped in length."""
    text = str(value or "").strip()
    if required and not text:
        raise _invalid(f"{name} is required")
    if len(text) > MAX_PROMPT_ARG_CHARS:
        raise _invalid(f"{name} is limited to {MAX_PROMPT_ARG_CHARS} characters")
    return text


async def list_resources(db: Database, user: dict[str, Any]) -> dict[str, Any]:
    """Lists the caller's own items and tags as concrete resources."""
    user_id = user["id"]
    items, tags = await asyncio.gather(
        list_items_compact(db, user_id, limit=MAX_RESOURCE_ITEMS),
        list_tags(db, user_id),
    )

    resources: list[dict[str, Any]] = []
    for item in items:
        resources.append(
            {
                "uri": item_uri(item["id"]),
                "name": item.get("title") or item.get("url") or item["id"],
                "description": item.get("url") or "",
                "mimeType": "application/json",
                "annotations": _RESOURCE_ANNOTATIONS,
            }
        )
    for tag in tags[:MAX_RESOURCE_TAGS]:
        resources.append(
            {
                "uri": tag_uri(tag["name"]),
                "name": tag["name"],
                "description": tag.get("description") or f"{tag['count']} items",
                "mimeType": "application/json",
                "annotations": _RESOURCE_ANNOTATIONS,
            }
        )
    return {"resources": resources}


async def read_resource(
    db: Database,
    user: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, Any]:
    """Reads one resource by URI and returns its contents."""
    uri = params.get("uri")
    if not uri or not isinstance(uri, str):
        raise _invalid("resources/read requires a uri")
    if len(uri) > MAX_RESOURCE_URI_CHARS:
        raise _invalid(f"uri is limited to {MAX_RESOURCE_URI_CHARS} characters")

    parsed = urlparse(uri)
    if parsed.scheme.lower() != RESOURCE_SCHEME:
        raise _invalid(f"Unsupported resource uri: {uri!r}")
    kind = parsed.netloc.lower()
    target = unquote(parsed.path).strip("/")
    if kind == "item":
        payload = await _read_item(db, user, target)
    elif kind == "tag":
        payload = await _read_tag(db, user, target)
    else:
        raise _invalid(f"Unknown resource type: {kind!r}")

    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(payload, ensure_ascii=False),
            }
        ]
    }


async def _read_item(
    db: Database, user: dict[str, Any], item_id: str
) -> dict[str, Any]:
    """The payload of one item resource, scoped to the caller's own items."""
    if not item_id:
        raise _invalid("keepfor://item/{item_id} requires an item id")
    item = await get_item(db, user["id"], item_id)
    if not item:
        raise _not_found("Item not found.")

    # Same shape as the get_item tool: the stored text, capped, with the
    # original length reported so a client knows it got a cut.
    content = item.get("content_text") or item.get("excerpt") or ""
    truncated = len(content) > MAX_RESOURCE_CONTENT_CHARS
    if truncated:
        content = content[:MAX_RESOURCE_CONTENT_CHARS]
    return {
        "id": item["id"],
        "title": item.get("title"),
        "url": item.get("url"),
        "tags": item.get("tags", []),
        "saved_at": item.get("created_at"),
        "item_type": item.get("item_type", "url"),
        "is_pinned": bool(item.get("is_pinned", 0)),
        "status": item.get("status"),
        "summary": item.get("summary"),
        "user_notes": item.get("user_notes"),
        "content": content,
        "content_length": item.get("content_length", 0),
        "truncated": truncated,
    }


async def _read_tag(db: Database, user: dict[str, Any], name: str) -> dict[str, Any]:
    """The payload of one tag resource, scoped to the caller's own tags."""
    if not name:
        raise _invalid("keepfor://tag/{name} requires a tag name")
    tag = await get_tag(db, user["id"], name)
    if not tag:
        raise _not_found("Tag not found.")

    items = await list_items_compact(
        db, user["id"], tags_all=[name], limit=MAX_RESOURCE_ITEMS + 1
    )
    truncated = len(items) > MAX_RESOURCE_ITEMS
    return {
        "name": tag["name"],
        "description": tag.get("description"),
        "color": tag.get("color"),
        "parent": tag.get("parent"),
        "items": [
            {
                "id": item["id"],
                "title": item.get("title"),
                "url": item.get("url"),
                "saved_at": item.get("saved_at"),
            }
            for item in items[:MAX_RESOURCE_ITEMS]
        ],
        "items_truncated": truncated,
    }


async def get_prompt(
    db: Database,
    env: Any,
    user: dict[str, Any],
    params: dict[str, Any],
    *,
    scope: TenantScope | None = None,
) -> dict[str, Any]:
    """Builds one prompt from the caller's own data."""
    name = params.get("name")
    if not name or not isinstance(name, str):
        raise _invalid("prompts/get requires a name")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        raise _invalid("prompts/get arguments must be an object")

    if name == "research_brief":
        return await _research_brief(db, env, user, args, scope=scope)
    if name == "weekly_review":
        return await _weekly_review(db, user, args)
    raise _not_found(f"Unknown prompt: {name}")


async def _research_brief(
    db: Database,
    env: Any,
    user: dict[str, Any],
    args: dict[str, Any],
    *,
    scope: TenantScope | None = None,
) -> dict[str, Any]:
    """Briefs the assistant on what the user saved about a topic."""
    topic = _capped_text(args.get("topic"), "topic", required=True)
    limit = _clamp_int(args.get("max_items"), DEFAULT_PROMPT_ITEMS, 1, MAX_PROMPT_ITEMS)

    items, _ = await hybrid_search(db, env, user["id"], topic, limit=limit, scope=scope)

    lines = [
        f"Research brief for {topic!r}.",
        "",
        f"The user has {len(items)} saved item(s) about this topic in their "
        "Keepfor library:",
    ]
    if items:
        for item in items:
            title = item.get("title") or item.get("url") or item["id"]
            tags = ", ".join(item.get("tags") or [])
            lines.append(
                f"- {title} - {item.get('url')} (tags: {tags or 'none'}) - "
                f"{item_uri(item['id'])}"
            )
    else:
        lines.append("- (nothing saved about this topic yet)")
    lines += [
        "",
        "Write a research brief that builds on what the user already saved.",
        "Read the items first (the read_item tool returns their full text), "
        "then say what the library already covers and what is missing.",
    ]
    return {
        "description": f"Research brief on {topic}",
        "messages": [
            {"role": "user", "content": {"type": "text", "text": "\n".join(lines)}}
        ],
    }


async def _weekly_review(
    db: Database,
    user: dict[str, Any],
    args: dict[str, Any],
) -> dict[str, Any]:
    """Reviews what the user saved over the last days."""
    days = _clamp_int(args.get("days"), DEFAULT_PROMPT_DAYS, 1, MAX_PROMPT_DAYS)
    tag = _capped_text(args.get("tag"), "tag") or None
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    items, _ = await reading_history(
        db, user["id"], since=since, tag=tag, limit=MAX_PROMPT_ITEMS
    )

    scope_note = f" (tag: {tag})" if tag else ""
    lines = [
        f"Weekly review of the last {days} day(s){scope_note}.",
        "",
        f"The user saved {len(items)} item(s) in that period:",
    ]
    if items:
        for item in items:
            title = item.get("title") or item.get("url") or item["id"]
            lines.append(f"- {title} - {item.get('url')} - {item_uri(item['id'])}")
    else:
        lines.append("- (nothing saved in that period)")
    lines += [
        "",
        "Summarize what the user saved, name the themes that repeat, and "
        "suggest what to read next.",
    ]
    return {
        "description": f"Weekly review of the last {days} days",
        "messages": [
            {"role": "user", "content": {"type": "text", "text": "\n".join(lines)}}
        ],
    }


async def handle_resource_request(
    method: str,
    body: dict[str, Any],
    db: Database,
    env: Any,
    user: dict[str, Any],
    *,
    scope: TenantScope | None = None,
) -> dict[str, Any]:
    """Handles one MCP resources/prompts request, JSON-RPC envelope included."""
    req_id = body.get("id")
    params = body.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    try:
        if method == "resources/list":
            result = await list_resources(db, user)
        elif method == "resources/templates/list":
            result = {"resourceTemplates": MCP_RESOURCE_TEMPLATES}
        elif method == "resources/read":
            result = await read_resource(db, user, params)
        elif method == "prompts/list":
            result = {"prompts": MCP_PROMPTS}
        elif method == "prompts/get":
            result = await get_prompt(db, env, user, params, scope=scope)
        else:
            raise _not_found(f"Unknown method: {method}")
    except ToolError as exc:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": _error_code(method, exc.kind),
                "message": str(exc),
                "data": {"type": exc.kind, **exc.details},
            },
        }
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32603,
                "message": str(exc),
                "data": {"type": "internal_error"},
            },
        }

    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error_code(method: str, kind: str) -> int:
    """The JSON-RPC code for a failure, as the MCP spec names them.

    The spec asks for -32002 when a resource does not exist and -32602 when
    a prompt name is unknown. `data.type` stays the machine-readable
    discriminator the tools use.
    """
    if kind == "not_found" and method == "resources/read":
        return -32002
    return -32602
