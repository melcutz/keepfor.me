CREATE TABLE IF NOT EXISTS tag_rules (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    field TEXT NOT NULL,
    substr TEXT NOT NULL,
    tag TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, field, substr, tag)
);

CREATE INDEX IF NOT EXISTS idx_rules_user ON tag_rules(user_id);

CREATE TABLE IF NOT EXISTS rule_suggestion_dismissals (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(user_id, key)
);
