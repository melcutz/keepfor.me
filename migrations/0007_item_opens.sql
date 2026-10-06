-- Migration 0007: Reader open log (powers Keep Score reads + phase-2 event log).
-- Note: id INTEGER PRIMARY KEY (rowid) is intentional for this append-only
-- log, unlike the TEXT UUID PKs elsewhere.
CREATE TABLE IF NOT EXISTS item_opens (
  id INTEGER PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  opened_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Serves per-user day-bucket aggregations without temp b-trees.
CREATE INDEX IF NOT EXISTS idx_opens_user_time ON item_opens(user_id, opened_at);
