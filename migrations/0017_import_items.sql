-- Migration 0017: Per-import item rows (keepfor-cloud#397)
-- Tracks each queued link, skipped link, and non-imported attachment for an
-- import upload without storing Keep note titles, bodies, or raw filenames.

CREATE TABLE IF NOT EXISTS import_items (
    id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES import_status(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'link',
    state TEXT NOT NULL DEFAULT 'queued',
    url TEXT,
    item_id TEXT,
    reason TEXT,
    mime_or_ext TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_import_items_user_import
    ON import_items(user_id, import_id, created_at ASC);
