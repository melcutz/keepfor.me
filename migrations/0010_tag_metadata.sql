-- Migration 0010: Tag metadata (description, color, hierarchy)
-- Supports full tag management in MCP: create_tag with metadata,
-- describe_tag/update_tag, and parent/child hierarchy (e.g. work/research).

ALTER TABLE tags ADD COLUMN description TEXT;
ALTER TABLE tags ADD COLUMN color TEXT;
ALTER TABLE tags ADD COLUMN parent_id TEXT REFERENCES tags(id);

CREATE INDEX IF NOT EXISTS idx_tags_user_parent ON tags(user_id, parent_id);
