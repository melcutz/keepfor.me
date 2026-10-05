# Browser Extension V1 Design — 2026-10-05

Option B approved: quick-save popup + Side Panel hub, client-side batching, no backend change.

## 1. Architecture & permissions

- `popup.html/js` stays fast path: single-save + `is_new` duplicate badge + "Open library" + "Open bulk panel".
- New `sidepanel.html/js`: Tabs / History / Search / Chat-lite tabs. Persists during batches.
- New `background.js` service worker: contextMenus + long-batch forwarding.
- Shared `lib/api.js`: `saveOne`, `saveMany(concurrency=5)`, `search`, `mcpCall` (Bearer PAT + workerUrl normalize).
- Manifest: add `tabs`, `history`, `contextMenus`, `sidePanel` perms; `side_panel.default_path`; `background.service_worker`. Keep `storage` + `host_permissions` (https remotes). Filter `chrome://`, `edge://`, `about:`, `chrome-extension://`; client-side URL dedupe.
- Batch = N× `POST /api/save`; concurrency 5; progress done/failed + retry-failed. Future `/api/save_batch` optional.

## 2. Tabs + History UX

- Tabs: `chrome.tabs.query({})` grouped by window; switcher This-window / All-windows; favicon+title+host rows, checkboxes (filtered/dupes unchecked); Select all/none + filter box + batch tags input; `Save N selected` + progress bar + error list + retry.
- History: range dropdown (24h/7d/30d/All) + search box → `chrome.history.search({text, startTime, maxResults})` (caps 100/500); filter/dedupe (latest visit wins); same checkbox + batch saver pattern. Title/URL only (server extracts content async).

## 3. Search + Chat-lite + popup extras

- Search tab: `POST /api/search {query, mode:"hybrid", limit:20}` → cards with open links.
- Chat-lite: `POST /api/mcp` JSON-RPC `tools/call search_library` → answer cards + open links. Grounded search, not generative. V2 adds generative loop.
- Popup: library link (stored workerUrl, default `https://app.keepfor.me`); duplicate via `is_new=false` → "Already saved — View in library"; context menus Save page / Save link → `saveOne()` + toast.
- AI optional, feature-detected (`window.ai`/LanguageModel/Summarizer): "Suggest tags" on single-save; hidden when unavailable; never blocks save.

## 4. Errors, testing, rollout

- Settings gate when missing PAT/workerUrl; 401 → invalid-token hint; per-URL error rows; "0 results (may have degraded)" for silent backend search fallback.
- Caps: warn >100, confirm >200. History maxResults + dedupe bound DOM. Empty states for no-match.
- Manual matrix: fresh-install gate; single new vs dupe; tabs window/all + filter + 50-batch w/ bad URL; history range+search; search; chat-lite; context menus; library link w/ trailing slash.
- No backend change; extension ships separately — zero Worker bundle impact.
- V1 = B. V2 deferred: `scripting`-based summarization + BYOK generative chat over MCP tools.

## Decisions

- Bulk scope: Both, with window switcher. History: search-then-pick. Batch UX: background w/ progress. Extras: duplicate check + context menu + smart tier. Smart V1: both auto-tags and chat-lite, AI optional.
