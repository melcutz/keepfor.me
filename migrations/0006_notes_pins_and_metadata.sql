-- Migration 0006: Notes, Pinning, User Notes, Cover Images, Summaries, Read State
-- Supports multi-view layouts, pinned shelf, quick notes, AI summaries, triage.

ALTER TABLE items ADD COLUMN item_type TEXT NOT NULL DEFAULT 'url';
ALTER TABLE items ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE items ADD COLUMN user_notes TEXT;
ALTER TABLE items ADD COLUMN image_url TEXT;
ALTER TABLE items ADD COLUMN summary TEXT;
ALTER TABLE items ADD COLUMN read_state TEXT NOT NULL DEFAULT 'unread';

-- Composite index to serve sorted pinned and unpinned feeds without temp b-trees
CREATE INDEX IF NOT EXISTS idx_items_user_pinned_created ON items(user_id, is_pinned DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_user_type_state ON items(user_id, item_type, read_state, created_at DESC);
