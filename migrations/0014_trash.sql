-- Migration 0014: Trash (soft delete)
-- items.deleted_at marks an item as moved to the Trash. NULL means live.
-- Trashed items keep their row, their R2 snapshots and their vectors so the
-- user can restore them; only the Trash view ("Delete forever") or the
-- 30-day purge removes them for good.

ALTER TABLE items ADD COLUMN deleted_at TIMESTAMP;

-- Serves the per-user trash listing and the "is it live?" filter.
CREATE INDEX IF NOT EXISTS idx_items_user_deleted ON items(user_id, deleted_at);
