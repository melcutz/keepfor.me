# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Tenant management functions for data-plane cleanup."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from keepfor.models.db import Database


async def delete_all_tenant_rows(db: Database, user_id: str) -> None:
    """Delete all rows belonging to a tenant child-first to satisfy foreign keys.

    Deletes all tenant data across all tenant tables including items_fts.
    """
    # 1. item_opens
    await db.execute("DELETE FROM item_opens WHERE user_id = ?;", (user_id,))
    # 2. import_status
    await db.execute("DELETE FROM import_status WHERE user_id = ?;", (user_id,))
    # 3. rule_suggestion_dismissals
    await db.execute(
        "DELETE FROM rule_suggestion_dismissals WHERE user_id = ?;", (user_id,)
    )
    # 4. tag_rules
    await db.execute("DELETE FROM tag_rules WHERE user_id = ?;", (user_id,))
    # 5. suggested_tags
    await db.execute("DELETE FROM suggested_tags WHERE user_id = ?;", (user_id,))
    # 6. chunks
    await db.execute("DELETE FROM chunks WHERE user_id = ?;", (user_id,))
    # 7. item_tags (children of items and tags)
    await db.execute(
        """
        DELETE FROM item_tags
        WHERE item_id IN (SELECT id FROM items WHERE user_id = ?);
        """,
        (user_id,),
    )
    # 8. highlights (children of items)
    await db.execute("DELETE FROM highlights WHERE user_id = ?;", (user_id,))
    # 9. collection_items (children of collections and items)
    await db.execute(
        "DELETE FROM collection_items WHERE collection_id IN"
        " (SELECT id FROM collections WHERE user_id = ?);",
        (user_id,),
    )
    # 10. collections
    await db.execute("DELETE FROM collections WHERE user_id = ?;", (user_id,))
    # 11. items_fts (virtual table, no foreign keys)
    await db.execute("DELETE FROM items_fts WHERE user_id = ?;", (user_id,))
    # 12. items
    await db.execute("DELETE FROM items WHERE user_id = ?;", (user_id,))
    # 13. tags
    await db.execute("DELETE FROM tags WHERE user_id = ?;", (user_id,))
    # 14. personal_access_tokens
    await db.execute(
        "DELETE FROM personal_access_tokens WHERE user_id = ?;", (user_id,)
    )
    # 15. sessions
    await db.execute("DELETE FROM sessions WHERE user_id = ?;", (user_id,))
