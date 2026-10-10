# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Closed list of tenant tables for tenant isolation audits and scoping."""

TENANT_TABLES: tuple[str, ...] = (
    "items",
    "items_fts",
    "tags",
    "item_tags",
    "chunks",
    "suggested_tags",
    "tag_rules",
    "rule_suggestion_dismissals",
    "item_opens",
    "sessions",
    "personal_access_tokens",
)
