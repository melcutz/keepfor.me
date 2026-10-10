# Tags Redesign (Big-Bang) — Design

Date: 2026-10-05. Status: approved, pending implementation plan.
Approach: big-bang (user chose Approach 2 over phased). All four sections
land together: manager, finding, in-flow, rules.

## Goal

Replace the `/tags` table UI (user: "futile — rename can be inline, delete
can be a simple x") with an intelligent tag system: inline management with
full hygiene, multi-tag finding, smart in-flow tagging, and user rules with
history-mined suggestions. Researched against Raindrop.io, Pinboard,
Wallabag, Obsidian Tag Wrangler, Evernote/Bear.

## Decisions (locked with user)

- Cleanup scope: full hygiene (inline rename, x delete, search, sort
  name/count/recent, Unused section + prune, normalization). Priority #1.
- Finding scope: multi-tag AND + Untagged filter. Priority #2.
- In-flow scope: autocomplete everywhere + recents/suggestions inline +
  bulk-apply. Priority #3.
- Automation scope: tagging rules (Wallabag-style) + rule suggestions mined
  from history. Priority #4.
- Constraints honored: D1-only state, stdlib + htmx only (no cold-start
  growth), `ruff check` + `ruff format` per CI, form/htmx endpoints return
  HTML not JSON, no CORS changes, `validate_tag_name` normalization stays.

## S1. Manager (`/tags` replaces table)

Pill-rows instead of `<table>`: colored dot + `#name` (click filters
library), count (click filters), click name swaps in rename input (Enter
commits via htmx `POST /tags/rename`, Esc cancels), hover/focus `x` deletes
via htmx `POST /tags/delete` with confirm. Header: search-tags input
(client-side filter), sort select (name / count desc / recent), per-row
checkbox + "Merge selected into [____] [Merge]" bar posting
`old_names[] + new_name` to new `POST /tags/merge` (reuses `rename_tag`
collision logic in one D1 batch/transaction). "Unused (0 items)" group at
bottom + "Prune all unused". "New tag" single input stays. Suggestions
section unchanged in this pass.

Data: no schema change. `models/items.py::merge_tags()` wraps sequential
`rename_tag` calls in one batch. Errors: invalid rename renders inline
422 message, no redirect; delete of missing tag is idempotent 303.
Tests: rename roundtrip, merge-two-into-one counts, prune-unused deletes
only zero-count, invalid rename rejected.

## S2. Finding (multi-tag AND + Untagged)

Sidebar + mobile chips become toggles. Active set in `?tag=a,b`
(comma-joined, alphabetical, URL-encoded) and `#active-tag-input`. Active
tags render filled with per-tag `x`. "All" clears. `?tag=__untagged__`
sentinel (server rejects creating a real tag with that name) shows
tagless items. Search + status pills preserve the set on `/search`.

Engine (`keepfor/search/engine.py`): FTS adds one `EXISTS (item_tags...)`
clause per tag; vector/RRF branch filters post-rank by membership; counts
respect the filter. Set capped at 10 tags. Unknown tag yields empty
result with clear-all affordance, not 404. Single `?tag=x` keeps working.
Tests: intersection-only, untagged-only, known+unknown empty, legacy
single-tag URL.

## S3. In-flow (autocomplete + recents/suggestions + bulk)

`GET /tags/suggest?q=...` JSON (max 8, prefix match, ranked most-used then
most-recent, excludes attached) backs datalist/dropdown in save popup,
share sheet, extension (online; free text offline fallback), and card add.
Keys: arrows + Enter/Tab pick, Esc closes, comma commits. Ranking computed
from `list_user_tags` counts + recency, no schema change.

Recents/suggestions row on save/item forms: 5 most-recent pills + up to 3
pending RAKE suggestions for that URL (accept-in-place attaches).
Bulk: per-card checkbox (long-press on touch) + sticky bar
(count + Add [____] [Apply] + Remove [select] [Apply] + Clear) posting as
form fields to new `POST /items/bulk-tags` (`item_ids` repeated, `add`,
`remove`; max 100 ids),
ownership-checked per item, one D1 batch, returns re-rendered cards HTML
(form/htmx rule). Tests: ranking order, prefix
filter, attached exclusion, bulk roundtrip + cap + isolation.

## S4. Rules + suggestions + ingest hook

New table via `migrations/0004_tag_rules.sql`: `tag_rules (id, user_id,
field CHECK IN (domain,title,url), substr lowercased 2..64 chars, tag,
created_at)` + index on `user_id`. CRUD section on `/tags`:
`POST /tags/rules/create`, `POST /tags/rules/delete`, inline add-row and
per-rule `x`; `validate_tag_name` on tag, `__untagged__` rejected, 50
rules/user cap.

Ingest (`consumer/processor.py` after `match_existing_tags`): one indexed
SELECT per batch, substring match per rule field, union into auto-apply
set, single `add_tags_to_item`. Fail-open (log, never fail extraction).
Queue/bulk imports apply per item in worker, not request.

Miner ("Suggested rules" on `/tags`): domain->tag and title-keyword->tag
pairs with precision >= 80% and support >= 5 propose "Always tag X as
#y? [Create] [Dismiss]". Dismissals persist in new
`rule_suggestion_dismissals (user_id, key, created_at)` table (same
migration file). Stdlib only. Tests: CRUD, ingest domain/title match and
non-match, failure fail-open, miner threshold + dismiss persistence.

## Risks (big-bang specific)

Single PR touches `app.py`, `models/items.py`, `search/engine.py`,
`consumer/processor.py`, `templates/tags.html`, `templates/library.html`,
`save_popup.html`, extension popup, plus migration 0004. Mitigate with
per-section test files and a D1 migration dry-run on a copy before
`--remote` apply (deploy.yml does not apply migrations).
