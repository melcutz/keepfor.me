-- Suggested tags from automatic keyphrase extraction (pending review)
CREATE TABLE IF NOT EXISTS suggested_tags (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    phrase TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(item_id, phrase)
);

CREATE INDEX IF NOT EXISTS idx_sugg_user_status ON suggested_tags(user_id, status);
CREATE INDEX IF NOT EXISTS idx_sugg_item ON suggested_tags(item_id);
