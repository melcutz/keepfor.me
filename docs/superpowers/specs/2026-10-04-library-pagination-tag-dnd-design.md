# Library Pagination + Drag-and-Drop Tagging — Design

Date: 2026-10-04. Status: approved, pending implementation plan.

## Goal

Two features on the main library view (`/`, `templates/library.html`):

1. Proper pagination: user-selectable records per page, easy page navigation
   that stays correct on the PWA.
2. Drag-and-drop tags onto articles from the tag list, and drag-away removal
   — with a touch-safe equivalent, since HTML5 DnD never fires on mobile.

## Decisions (locked with user)

- Touch tagging: tap-to-assign (tap chip to arm, tap cards to toggle).
- Page turns: in-place htmx swaps with `hx-push-url`, not full-page links.
- Pagination scope: browse and search results both.

## 1. Pagination

### Engine (`keepfor/search/engine.py`)

- `hybrid_search` gains `offset: int = 0` and returns `(items, total)`.
- Empty query: pass `limit`/`offset` to `get_recent_items`; add a `COUNT(*)`
  query with the identical tag/status filters for the total.
- With query: rank the full candidate window (FTS + vector top-50 each, as
  today), apply tag/status filters to the whole ranked list,
  `total = len(filtered)`, return `filtered[offset:offset + limit]`.
  Known ceiling: roughly 100 ranked candidates, so deep pages thin out.
  The UI reports "showing X–Y of ~Z" to make the approximation explicit.
- Callers updated: `library_page`, `search_htmx`, MCP `search_library`
  (unwraps items, ignores total), affected tests.

### View

- `/` accepts `page` (default 1) and `per_page` (default 20; options
  10/20/30/50, clamped server-side). Out-of-range page renders the last
  page, never an empty list.
- Page turns and the per-page `<select>` POST to `/search` with
  `hx-target="#items-list"` and `hx-push-url="true"` so URL, bookmarks,
  and back button stay correct.
- `/search` returns cards plus the pager bar and result-count line as
  `hx-swap-oob` fragments (controls live outside `#items-list`).
- Changing per-page, tag, status, or query resets to page 1.

## 2. Tag assignment

### Endpoint

- New `POST /items/{item_id}/tags` accepting `add=[...]` or `remove=[...]`,
  reusing `add_tags_to_item` / `remove_tags_from_item` from
  `keepfor/models/items.py`. Returns HTML (project rule: form/htmx endpoints
  return HTML, not JSON): the re-rendered `item_card.html`, swapped
  `outerHTML` on `#item-card-{id}`.

### Desktop

- Sidebar tag rows `draggable=true`; cards highlight on `dragover`; drop
  fires the POST. Removal: drag a pill onto a trash chip that appears only
  during drags; dropping anywhere else cancels. Pills also carry an `×`
  fallback.

### Touch / PWA

- Tap a tag chip to arm it (sticky highlight + hint bar), tap cards to
  toggle the armed tag, tap the chip again to disarm. Pill `×` removes
  directly. No hover dependence; all targets at least 44px.
- One small inline `<script>` in `library.html` only (no JS bundle exists;
  `static/` holds just `.gitkeep`). htmx performs the POSTs; JS handles
  drag visuals and armed-tag state via hidden inputs.

## 3. PWA safeguards

- Pager collapses on narrow screens: prev/next + "Page 2 of 9" + compact
  numbers; no horizontal overflow at 360px.
- Pager sits in normal flow above the fixed bottom tab bar (no sticky
  overlap), safe-area padding respected.
- Page-jump input keeps the 16px floor (no iOS auto-zoom).

## 4. Testing

- Engine unit tests: offset slicing, totals for browse and search paths.
- Endpoint tests: page/per_page clamping, out-of-range page, OOB pager
  fragment present, tag add/remove roundtrip returns updated card.
- Regression test: per-page/tag/search changes reset to page 1.
- Full suite + CI ruff (`check keepfor/ tests/ --select=E,W,F,I,N`,
  `format --check`) must pass.

## Non-goals

Infinite scroll, SSE streaming, per-user per-page persistence (the URL
carries pagination state), new JS build tooling.
