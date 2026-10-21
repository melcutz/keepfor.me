-- Migration 0015: index the summary and the page description (#55)
--
-- The keyword side of the hybrid search rule needs them: an item that
-- names the concept only in its summary ("a local LLM runs offline") or
-- in its page description had no keyword hit and was dropped from the
-- results. New writes index them directly (the FTS content of the
-- processor and of _refresh_fts_for_item); this backfills the rows that
-- are already in the index.
--
-- The statement rewrites every FTS row, so it costs a few seconds on a
-- library of a few thousand items.

UPDATE items_fts
SET content_text = content_text || ' ' ||
    COALESCE((SELECT i.summary FROM items i WHERE i.id = items_fts.item_id), '') || ' ' ||
    COALESCE((SELECT i.excerpt FROM items i WHERE i.id = items_fts.item_id), '');
