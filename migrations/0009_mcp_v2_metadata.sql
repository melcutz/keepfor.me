-- Migration 0009: MCP v2 metadata fields for agent research provenance
-- Adds agent_summary, why_saved, source_context, saved_by to items table
-- for search_passages, reading_history, and save_url/save_note v2.

ALTER TABLE items ADD COLUMN agent_summary TEXT;
ALTER TABLE items ADD COLUMN why_saved TEXT;
ALTER TABLE items ADD COLUMN source_context TEXT;
ALTER TABLE items ADD COLUMN saved_by TEXT;

-- Composite index to serve reading_history ordered by date
CREATE INDEX IF NOT EXISTS idx_items_user_created ON items(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_item_opens_user_time ON item_opens(user_id, opened_at DESC);