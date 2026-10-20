-- Migration 0008: Import source tracking for notes
-- Adds source, source_id columns and unique index for dedupe on re-imports.

ALTER TABLE items ADD COLUMN source TEXT;
ALTER TABLE items ADD COLUMN source_id TEXT;

-- Unique index for dedupe: one note per (user, source, source_id)
-- WHERE clause allows NULL source_id (notes created via UI have no source)
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_user_source_source_id
    ON items(user_id, source, source_id)
    WHERE source_id IS NOT NULL;

-- Composite indexes for common sorted queries with source filtering
CREATE INDEX IF NOT EXISTS idx_items_user_source_created
    ON items(user_id, source, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_user_type_source
    ON items(user_id, item_type, source, created_at DESC);