-- Migration 0011: Persisted notes-import status
-- One row per upload (per user) with the running counters and a capped list
-- of error messages, so the import result survives a page refresh.

CREATE TABLE IF NOT EXISTS import_status (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    notes_total INTEGER NOT NULL DEFAULT 0,
    notes_processed INTEGER NOT NULL DEFAULT 0,
    notes_imported INTEGER NOT NULL DEFAULT 0,
    duplicates_skipped INTEGER NOT NULL DEFAULT 0,
    trashed_skipped INTEGER NOT NULL DEFAULT 0,
    urls_found INTEGER NOT NULL DEFAULT 0,
    urls_queued INTEGER NOT NULL DEFAULT 0,
    urls_skipped INTEGER NOT NULL DEFAULT 0,
    attachments_not_imported INTEGER NOT NULL DEFAULT 0,
    parse_errors INTEGER NOT NULL DEFAULT 0,
    plan_skipped INTEGER NOT NULL DEFAULT 0,
    errors TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_import_status_user_created
    ON import_status(user_id, created_at DESC);
