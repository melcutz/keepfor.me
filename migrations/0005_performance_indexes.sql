-- Performance Indexes: Eliminate in-memory filesorts and full table scans

-- Composite index for user feed ordered by creation date (eliminates temp B-tree sort)
CREATE INDEX IF NOT EXISTS idx_items_user_created ON items(user_id, created_at DESC);

-- Composite index for user feed filtered by status and ordered by creation date
CREATE INDEX IF NOT EXISTS idx_items_user_status_created ON items(user_id, status, created_at DESC);

-- Index for PAT lookup by user ordered by creation date
CREATE INDEX IF NOT EXISTS idx_pats_user_created ON personal_access_tokens(user_id, created_at DESC);

-- Index for sessions by user (speeds up cascading deletes and user session lookups)
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
