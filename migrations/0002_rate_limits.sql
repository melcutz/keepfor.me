-- Rate limiting for the authentication endpoints.
--
-- One row per (key, window_start). `key` is either "ip:<address>" or
-- "acct:<email>"; see src/utils/rate_limit.py. Fixed windows keep this to a
-- single indexed row per key per 15 minutes, which matters because the login
-- path must stay cheap.
CREATE TABLE IF NOT EXISTS rate_limits (
    key TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key, window_start)
);

-- Supports the opportunistic cleanup of expired windows.
CREATE INDEX IF NOT EXISTS idx_rate_limits_window ON rate_limits(window_start);