-- Migration 0016: Smart (query-backed) collections (GitHub issue #93)
--
-- A collection can be a saved search: its members are resolved at read
-- time through the existing search instead of being linked one by one, so
-- it stays current.
--
-- The two columns are nullable and NULL means "manual": every collection
-- that exists today keeps its current behaviour, and a collection that
-- gets no filter stays a manual one.
ALTER TABLE collections ADD COLUMN query TEXT;
ALTER TABLE collections ADD COLUMN tag TEXT;
