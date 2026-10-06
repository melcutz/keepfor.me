# Analytics Page (Habit-First) — Design

Date: 2026-10-06. Status: approved, pending implementation plan.
Approach: hybrid C + opens log (user chose C over derived-only A and
full-event-log-first B, then chose opens-log-table over counter columns).

## Goal

Give users insights into their content beyond simple counts, and — first
and foremost — build a consistent "keep" habit. North-star priority:
habit loop first; library intelligence and AI/MCP proof-of-value are
later pillars layered onto the same page. Nothing else matters if users
don't keep things.

## Decisions (locked with user)

- Reward all three behaviors (save, triage, read) with one combined
  score, shown as a GitHub-style contributions graph over time.
- Weights tell a story (keep < process < read), all per event: save 1,
  tag attach / note-text edit / pin 1 each, archive 2, deduped open 3.
- Hero metric is the streak (consecutive days with score > 0), not the
  total — totals invite grinding, streaks invite showing up.
- Intensity buckets self-scale to the user's own median active day, so
  the graph stays meaningful from 10 saves to 10,000.
- Per-action daily caps: max 10 countable events per action type per day,
  so a 900-item import can't buy a month of glory. Imports backfill true
  save dates. Days are UTC calendar days (matches CURRENT_TIMESTAMP).
- "Read" = any reader visit (coarse, no dwell/scroll telemetry — PWA +
  privacy, keeps the write path trivial).
- Reads tracked in a tiny `item_opens` log table (exact day attribution,
  seeds the phase-2 event log), not counter columns.
- Scope: library page only for layout concerns; `/stats` is a new page
  linked in header nav. Search/MCP stats deferred to phase 2 (needs the
  event log + a query-text persistence decision).
- Constraints honored: D1-only state, stdlib + htmx only (no cold-start
  growth), dual-backend `db.py` discipline, form/htmx endpoints return
  HTML not JSON, no CORS changes, `ruff check` + `ruff format` per CI.

## S1. Keep Score + contributions graph

Day score = sum of weighted actions that day, from `items.created_at`
(saves), `item_tags.created_at` (tag attaches), note/pin writes, archive
transitions, and `item_opens.opened_at` (reads, deduped: opens within 5
minutes of the previous open of the same item score once; raw rows kept).

Graph: 53×7 year grid, intensity bucket 0–4 relative to user's median
active day. Hover/tap a cell → that day's breakdown (saved ×n, read ×n,
triaged ×n). Below: current-week bar strip ("how's this week going").

Streak = consecutive UTC-calendar days with score > 0 ending today or
yesterday (yesterday-ending keeps the streak alive until end of today).

## S2. Page (`GET /stats`, header nav link)

Top to bottom: (1) streak banner — current streak (big), longest streak,
total score, best day; (2) contributions graph + week strip (emotional
core); (3) activity rhythm — saves vs reads vs triaged per week, last 12
weeks grouped bars (am I backsliding?); (4) inbox health — unread count,
oldest untouched item with direct link (the gentle triage nag), archive
rate; (5) top tags & domains — compact ranked lists (counts), a teaser
of the phase-2 library-intelligence pillar, free from existing queries.

Not in phase 1: search/MCP stats, tag trendlines, export/share.

Empty states: quiet libraries get "save your first link" nudges, not an
empty grid; streak banner reads "Day 1 starts with one save."

## S3. Data + instrumentation (only backend work in phase 1)

Migration `0007_item_opens.sql`: `CREATE TABLE item_opens (id INTEGER
PRIMARY KEY, user_id, item_id, opened_at DEFAULT CURRENT_TIMESTAMP)` +
index `(user_id, opened_at)` for the day-bucket aggregations (matches
the composite-index discipline: WHERE user_id + ORDER/GROUP BY time).

Reader bump: in `GET /items/{id}`, after the existing SELECT, one
fire-and-forget `INSERT INTO item_opens` — never awaited before
responding, never allowed to fail the render. One extra D1 write per
human read; trivial volume.

Backfill: existing items get no fake history; graph starts honest from
deploy day; old saves still contribute save-day points via `created_at`.

Privacy: `item_opens` stores IDs + timestamp only — no content, no
query text. Phase-2 query-text decision stays deferred.

## S4. Edges + testing

- Every aggregation scoped `WHERE user_id = ?` (single-tenant-safe).
- Score-bucket math as pure-function unit tests (deterministic, no DB).
- Endpoint tests on the existing sqlite fixture pattern: save → score
  appears; open → score increases; refresh within 5 min → no double
  score; archive → 2 points.
- Visual check at desktop + 375px PWA; Cards/List/Compact unaffected
  (new page, no shared components beyond header nav).
