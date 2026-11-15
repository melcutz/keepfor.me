-- Migration 0018: Composite and partial performance indexes v2
-- Eliminates full scans and temp B-tree sorts on correlated opens lookups,
-- per-item pending suggestions, live pinned/state browse feeds, import_items
-- reconciliation, and ordered chunk fetches.

-- Correlated MAX(opened_at) lookup per item and user
CREATE INDEX IF NOT EXISTS idx_item_opens_item_user_time
    ON item_opens(item_id, user_id, opened_at DESC);

-- Superseded by idx_item_opens_user_time from 0009_mcp_v2_metadata.sql
DROP INDEX IF EXISTS idx_opens_user_time;

-- Per-item pending tag suggestions ordered by score
CREATE INDEX IF NOT EXISTS idx_sugg_user_item_status_score
    ON suggested_tags(user_id, item_id, status, score DESC);

-- Live library browse feeds (pinned first, newest/oldest first)
CREATE INDEX IF NOT EXISTS idx_items_live_pinned_created_desc
    ON items(user_id, is_pinned DESC, created_at DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_items_live_pinned_created_asc
    ON items(user_id, is_pinned DESC, created_at ASC)
    WHERE deleted_at IS NULL;

-- Live library browse feed filtered by read_state
CREATE INDEX IF NOT EXISTS idx_items_live_state_pinned_created
    ON items(user_id, read_state, is_pinned DESC, created_at DESC)
    WHERE deleted_at IS NULL;

-- Import item reconciliation by user and linked item
CREATE INDEX IF NOT EXISTS idx_import_items_user_item
    ON import_items(user_id, item_id);

-- Ordered chunk fetch per item and user
CREATE INDEX IF NOT EXISTS idx_chunks_user_item_idx
    ON chunks(user_id, item_id, chunk_index);
