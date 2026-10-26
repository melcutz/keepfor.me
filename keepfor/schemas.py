# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Pydantic models for request validation."""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


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


class CreatePATRequest(BaseModel):
    """Request to create a personal access token."""

    name: str = Field(..., description="Token name", min_length=1, max_length=100)
    scopes: List[Literal["read", "write", "*"]] = Field(
        default_factory=list,
        description=(
            "Granted scopes. An empty list means all scopes ('*'), the "
            "default for tokens created before scopes existed."
        ),
    )


class LoginRequest(BaseModel):
    """Request to log in."""

    email: str = Field(..., description="Email address")
    password: str = Field(..., description="Password", min_length=8)


class RegisterRequest(BaseModel):
    """Request to register."""

    email: str = Field(..., description="Email address")
    password: str = Field(..., description="Password", min_length=8)
