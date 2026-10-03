# UI Refresh — Design Spec (2026-10-03)

Direction **C (Clean upgrade)** with a new brand mark (portal bookmark, lockup A).
Inspiration: marqly.com's sidebar-with-counts, favicon rows, ⌘K search prominence.

## Goal

Make keepfor.me look designed instead of default, without changing behavior,
routes, or data. Same pages, same endpoints, same interactions — new skin
and a real logo.

## Non-goals

- No new features (no boards, teams, AI panels, view toggles).
- No backend changes, no migrations, no new routes.
- No webfont downloads (bundle + CSP stay untouched).
- Reader sepia/dark themes keep working as-is.

## Design language (all pages)

- **Palette:** page `slate-50` `#f8fafc`, cards white, hairlines `slate-200`
  `#e2e8f0`, ink `slate-900` `#0f172a`, secondary `slate-500` `#64748b`,
  faint `slate-400`/`slate-300` for counts and hints. One accent:
  `indigo-600` `#4f46e5` (title hover, tag pills, search focus ring,
  bookmarklet button). Status colors (amber extracting, red failed) unchanged.
- **Type:** system sans everywhere. Page headlines 700–750 weight,
  `-0.02em` to `-0.025em` tracking. Item titles 650 weight, `-0.01em`,
  shifting to indigo-600 on row hover. Metadata 12–12.5px slate-500.
  Mono only for tiny technical text (match scores).
- **Shape:** cards `rounded-xl`/`rounded-2xl`, pills `rounded-md` with
  indigo tint (`#eef2ff` bg, `#4f46e5` text), buttons `rounded-lg`.
  Subtle `shadow-sm`, rows lift on hover. `kbd` chips for shortcuts.

## Brand mark

**Icon** (`static/icon.svg`, new file):

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 44 44"><defs><linearGradient id="kfm" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2563eb"/><stop offset="1" stop-color="#0ea5e9"/></linearGradient></defs><rect x="2" y="2" width="40" height="40" rx="11" fill="url(#kfm)"/><path d="M17 11h10v16l-5-3.8-5 3.8z" fill="#fff"/></svg>
```

White bookmark glyph on a blue-600 → sky-500 gradient squircle. Legible at
16px, no emoji anywhere in the UI afterwards.

**Wordmark:** `Keepfor` in slate-900 + `.me` in blue-600 (`#2563eb`),
weight 800, tracking `-0.03em`. Used in the header logo, login/register
cards, and save-popup header.

**Favicon wiring:** `<link rel="icon" type="image/svg+xml" href="/static/icon.svg">`
in `base.html` and the standalone `save_popup.html`. Requires a static-file
route for `/static` (new, see implementation plan) — `img-src`/`style-src`
CSP already permits same-origin.

## Library (`templates/library.html`, `partials/item_card.html`)

- Sidebar rows become board rows: label + count badge (slate-100 pill,
  darker when active), active board filled slate-200. All data already
  passed to the template (`tags[].count`, `total_count`).
- Item rows gain a 20px rounded favicon: primary
  `https://www.google.com/s2/favicons?domain=<domain>&sz=64`, `onerror`
  fallback to `https://icons.duckduckgo.com/ip3/<domain>.ico`, CSS slate
  tile behind both. `<domain>` is the host of `item.canonical_url`
  (`item.canonical_url.split('/')[2]`), which the row already assumes is
  present when it renders the site name.
- Search input shows a visible `⌘K` hint chip; `:focus-within` gets the
  indigo ring. Existing `Cmd+K` JS shortcut unchanged.
- Page headline `Library` (tight display style) + muted count subline.
- Privacy note: item favicons disclose saved domains to Google/DDG at
  render time. Acceptable for a self-hosted single-user app; documented
  here so the tradeoff is explicit, not accidental.

## Reader (`templates/reader.html`)

Structure and controls unchanged (back link, Sans/Serif/Mono, A−/A+,
light/sepia/dark, Original link, theme localStorage hooks). Tightened
headline (750, `-0.022em`), muted 12.5px meta row, indigo-tint tag pills,
article body at 1.75 line-height, controls bar becomes a proper card.

## Settings (`templates/settings.html`)

Same three cards, forms, and endpoints. Bookmarklet button goes indigo
(primary action), token form/table spacing unified, MCP snippet keeps its
dark code block, import/export actions align in one row. Bookmarklet
popup-positioning JS (from the earlier fix) is unchanged.

## Auth (`templates/login.html`, `register.html`)

Centered card with the new icon + split-tone wordmark, same fields and
`next` handling. Cross-links preserve `next` as today.

## Testing

- Full suite `python3 -m pytest tests/ -q` (49 tests) must stay green.
- CI ruff forms exactly: `ruff check src/ tests/ --select=E,W,F,I,N`
  and `ruff format --check src/ tests/`.
- New test: library HTML contains a `s2/favicons?domain=` URL and the
  `onerror` DuckDuckGo fallback.
- Manual visual pass over library / reader / settings / login before merge,
  compared against the approved mockups in `.superpowers/brainstorm/`.
