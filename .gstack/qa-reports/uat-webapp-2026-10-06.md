# Web-app UAT report — app.keepfor.me — 2026-10-06

Method: Playwright Chromium, login as m@m.com (session cookie, no PAT minted this round).
Backup: full JSON export (878 items) taken before mutations. All QA artifacts
(items, tags) created with `uat-` prefix and deleted after. Library left at
880 = 878 baseline + 2 genuine user saves made during the session (warp.dev,
cloudflare dash — tagged, real URLs, not QA). Screenshots: /tmp/kfm-qa/screenshots/u*.

## Confirmed bugs

### 1. [HIGH] Status pills + header counts never update with filters
Every dimension tested — status htmx clicks, `?category=`, `?tag=`,
`?quick=1`, combos — pills stay `All·878 | Saved·573 | Extracting·5 | Failed·300`
and header stays `878 saves`. Structural, two causes:
- `keepfor/app.py:418` `get_status_counts(db, user_id)` takes no filter args
  (`keepfor/search/engine.py:94` — global GROUP BY), and `list_user_tags`
  (`keepfor/models/items.py:676`) likewise counts all items per tag.
- Status pills are htmx buttons swapping only `#items-list`
  (`templates/library.html:213-236`); they never re-render themselves.
The only filtered total shown is the pager line ("Showing X–Y of Z saves").
Per product call (user confirmed), pills should re-count within the active
view/tag/search filter.

### 2. [HIGH] JS TypeError on every library load breaks bulk-select refresh
`TypeError: Cannot read properties of null (reading 'addEventListener')`
fires on each `/` render (all variants). Root cause:
`templates/library.html:204`
`document.getElementById('items-list').addEventListener('change', …)`
executes at line ~204 while `<div id="items-list">` is at line ~275 —
parse order. The throw aborts that IIFE, so the change listener never
attaches. Proven in browser: check a bulk box → `#bulk-count` stays
"0 selected", zero hidden inputs in `#bulk-items` (screenshot u28).
Fix: move block after the list, or guard/defer to DOMContentLoaded.
(The identical NULL-GET seen on /tags traces to the intermediate `/`
load after login redirect, not tags itself — /tags alone throws nothing.)

### 3. [HIGH] Bulk checkbox physically unclickable
`item_card.html:8` checkbox is `absolute top-3 right-3`; the card action
row (delete/pin/archive buttons) overlaps it — Playwright real click fails
with "Delete item button … intercepts pointer events". Users can't tick
boxes with the mouse at all. Fix z-index/pointer-events or relocate.

### 4. [MEDIUM] Failed-item reader is misleading
Reader for a failed item shows the raw URL as title, a stray "None"
heading, and "Content extraction in progress…" — extraction already
failed and will never progress. Should state failure + reason + retry.

### 5. [MEDIUM] Favicon 404 storm in console
Dozens of `t*.gstatic.com/faviconV2` 404s for dead domains on every
library load (perf + console noise). Worse: `icons.duckduckgo.com/ip3/
127.0.0.1:8888.ico` and `localhost:8787.ico` — local-dev URLs stored in
prod item data leak into favicon resolution. Cap/stale-cache favicons;
skip non-public hosts.

### 6. [MEDIUM] `/api/items` range validation missing → 500s
`?limit=200` → 500, `?limit=-5` → 500 (`?limit=abc` correctly 422).
`api_list_items` passes limit/offset raw to SQL. Clamp to sane range.

### 7. [LOW] Misc
- `?status=bogus` silently ignored (shows all) — no feedback.
- `?page=99999` clamps to last page but URL keeps 99999, no notice.
- Mobile card meta row wraps bullets onto own lines (whitespace/stacked
  "• 2026-10-05 • Link Only") — minor responsive wart (screenshot u02).
- `POST /items/{id}/pin` (non-htmx) returns ~424KB HTML — wasteful for an
  action endpoint; check it doesn't render the full library page.

## Verified working (no action)
Save modal + `s`/Esc keyboard, reader (good item) incl. fonts/Copy/Pin,
archive/unarchive + archive view, pin shelf, notes, per-item tags,
tag filter, search incl. good empty state ("No matching articles found."),
styled "Article not found" page, DELETE → empty HTML + 404 after,
tags create/rename/delete/prune/rules UI, CSV import (queued),
save-popup/share forms, settings page, authed `/auth/login|register` → `/`,
logout, PWA routes, mobile nav/reader/tags layouts.

## Suggested fix order
1. Bulk-bar script order/guard + checkbox overlap (one area, biggest pain).
2. Scoped pill counts (needs product sign-off on semantics per pill).
3. API limit clamp (tiny).
4. Failed-reader state + favicon hygiene.
