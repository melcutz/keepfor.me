-- Migration 0012: per-token scopes (GitHub issue #29, keepfor-cloud#61).
-- A personal access token can be limited to read-only: over MCP it then
-- sees only the read tools and every write or destructive tool is refused.
--
-- The column is nullable and NULL means "all scopes" ("*"), so every token
-- created before this migration keeps its full access unchanged.
ALTER TABLE personal_access_tokens ADD COLUMN scopes TEXT;
