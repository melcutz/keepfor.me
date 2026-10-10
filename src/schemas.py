# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Pydantic models for request/response validation and MCP tool arguments."""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# ==========================================
# Request Models
# ==========================================


class SaveItemRequest(BaseModel):
    """Request to save a URL to the library."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "url": "https://example.com/article",
                "tags": ["tech", "read-later"],
            }
        }
    )

    url: str = Field(..., description="The webpage or article URL to save")
    tags: List[str] = Field(default_factory=list, description="Optional list of tags")
    title: Optional[str] = Field(default=None, description="Optional custom title")


class SearchRequest(BaseModel):
    """Request to search the library."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"query": "machine learning", "mode": "hybrid", "limit": 10}
        }
    )

    query: str = Field(
        ..., description="Search query terms or semantic question", min_length=1
    )
    mode: Literal["hybrid", "keyword", "semantic"] = Field(
        default="hybrid", description="Search mode"
    )
    tag: Optional[str] = Field(default=None, description="Filter by tag")
    limit: int = Field(default=30, ge=1, le=100, description="Max results (1-100)")


class TagItemRequest(BaseModel):
    """Request to add or remove tags from an item."""

    item_id: str = Field(..., description="The library item ID")
    add_tags: List[str] = Field(default_factory=list, description="Tags to add")
    remove_tags: List[str] = Field(default_factory=list, description="Tags to remove")


class ImportRequest(BaseModel):
    """Request to import bookmarks."""

    format: Literal["csv", "netscape"] = Field(..., description="Import format")
    content: str = Field(..., description="File content (base64 or raw text)")


class CreatePATRequest(BaseModel):
    """Request to create a personal access token."""

    name: str = Field(..., description="Token name", min_length=1, max_length=100)


class LoginRequest(BaseModel):
    """Request to log in."""

    email: str = Field(..., description="Email address")
    password: str = Field(..., description="Password", min_length=8)


class RegisterRequest(BaseModel):
    """Request to register."""

    email: str = Field(..., description="Email address")
    password: str = Field(..., description="Password", min_length=8)


# ==========================================
# Response Models
# ==========================================


class TagResponse(BaseModel):
    """Tag response."""

    id: str
    name: str
    created_at: datetime


class ItemResponse(BaseModel):
    """Library item response."""

    id: str
    url: str
    canonical_url: str
    title: Optional[str] = None
    byline: Optional[str] = None
    excerpt: Optional[str] = None
    site_name: Optional[str] = None
    published_date: Optional[str] = None
    status: str  # 'queued', 'completed', 'failed'
    fail_reason: Optional[str] = None
    word_count: int = 0
    tags: List[TagResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class SearchResponse(BaseModel):
    """Search response."""

    items: List[ItemResponse]
    total: int
    query: str
    mode: str


class SaveItemResponse(BaseModel):
    """Response after saving an item."""

    id: str
    url: str
    status: str
    is_new: bool
    message: str


class ListItemsResponse(BaseModel):
    """Response for listing items."""

    items: List[ItemResponse]
    total: int
    offset: int
    limit: int


class CreatePATResponse(BaseModel):
    """Response after creating a PAT."""

    id: str
    name: str
    token: str  # Only returned once, on creation
    created_at: datetime


class PATResponse(BaseModel):
    """Personal access token info (without secret)."""

    id: str
    name: str
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    created_at: datetime


class UserResponse(BaseModel):
    """User response."""

    id: str
    email: str
    role: str
    created_at: datetime


class ErrorResponse(BaseModel):
    """Standard error response."""

    detail: str
    error_code: Optional[str] = None


# ==========================================
# MCP Tool Argument Models (for validation)
# ==========================================


class MCPSaveURLArgs(BaseModel):
    """MCP: save_url tool arguments."""

    url: str = Field(..., description="The webpage or article URL to save")
    tags: List[str] = Field(default_factory=list, description="Optional list of tags")


class MCPSearchLibraryArgs(BaseModel):
    """MCP: search_library tool arguments."""

    query: str = Field(..., description="Search query terms or semantic question")
    mode: Literal["hybrid", "keyword", "semantic"] = Field(default="hybrid")
    tag: Optional[str] = Field(default=None, description="Filter by tag")
    limit: int = Field(default=10, ge=1, le=100)


class MCPGetItemArgs(BaseModel):
    """MCP: get_item tool arguments."""

    item_id: str = Field(..., description="The unique ID of the library item")


class MCPListItemsArgs(BaseModel):
    """MCP: list_items tool arguments."""

    tag: Optional[str] = Field(default=None, description="Filter by tag")
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class MCPTagItemArgs(BaseModel):
    """MCP: tag_item tool arguments."""

    item_id: str = Field(..., description="The library item ID")
    add_tags: List[str] = Field(default_factory=list, description="Tags to add")
    remove_tags: List[str] = Field(default_factory=list, description="Tags to remove")


class MCPDeleteItemArgs(BaseModel):
    """MCP: delete_item tool arguments."""

    item_id: str = Field(..., description="The library item ID to delete")
