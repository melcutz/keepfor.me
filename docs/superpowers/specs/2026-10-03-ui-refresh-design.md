# UI Refresh — Design Spec (2026-10-03)

Direction **C (Clean upgrade)** with a new brand mark (portal bookmark, lockup A).
Inspiration: marqly.com's sidebar-with-counts, favicon rows, ⌘K search prominence.

## Goal

Make keepfor.me look designed instead of default, without changing behavior,
routes, or data. Same pages, same endpoints, same interactions — new skin
and a real logo.

## Non-goals

- No new features (no boards, teams, AI panels, view toggles).
- No migrations, no new routes, no schema changes. Exactly one tiny pure
  backend helper is allowed: deterministic tag→palette-class mapping
  (no I/O, fully unit-testable).
- No webfont downloads (bundle + CSP stay untouched).
- Reader sepia/dark themes keep working as-is.

## Design language (all pages)

- **Palette:** page `slate-50` `#f8fafc`, cards white, hairlines `slate-200`
  `#e2e8f0`, ink `slate-900` `#0f172a`, secondary `slate-500` `#64748b`,
  faint `slate-400`/`slate-300` for counts and hints. One chrome accent
  family, blue only: `blue-600` `#2563eb` (title hover, search focus ring,
  bookmarklet button, links) flowing into the icon's `sky-500` `#0ea5e9`.
  No violet/indigo in chrome — it clashes with the blue icon. Status
  colors (amber extracting, red failed) unchanged.
- **Tag palette (categorical, soft tints — the color in the app):** eight
  fixed pairs: blue `#eff6ff`/`#1d4ed8`, green `#ecfdf5`/`#047857`, amber
  `#fffbeb`/`#b45309`, rose `#fff1f2`/`#be123c`, violet `#f5f3ff`/`#6d28d9`,
  cyan `#ecfeff`/`#0e7490`, orange `#fff7ed`/`#c2410c`, slate `#f1f5f9`/
  `#475569`. A violet *tag* is fine — data, not chrome. Assignment is
  `md5(tag).digest()[0] % 8`, computed in `keepfor/app.py`
  (`tag_palette_index()` + `tag_styles_for()`) and passed as `tag_styles: dict[str, tuple[str, str]]` into
  every template that renders item tags (`library_page`, `search_htmx`,
  `reader_page`). Python's `hash()` is process-randomized, so md5 — never
  Jinja-side tricks. Sidebar board dots use the same mapping, so a tag's
  dot always matches its pills. Favicon fallback letter-tiles use the
  item's first tag color instead of slate.
- **Type:** system sans everywhere. Page headlines 700–750 weight,
  `-0.02em` to `-0.025em` tracking. Item titles 650 weight, `-0.01em`,
  shifting to blue-600 on row hover. Metadata 12–12.5px slate-500.
  Mono only for tiny technical text (match scores).
- **Shape:** cards `rounded-xl`/`rounded-2xl`, pills `rounded-md` in the
  tag palette, buttons `rounded-lg`.
  Subtle `shadow-sm`, rows lift on hover. `kbd` chips for shortcuts.

## Brand mark

**Icon** (`static/icon.svg`, new file):

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 44 44"><defs><linearGradient id="kfm" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2563eb"/><stop offset="1" stop-color="#0ea5e9"/></linearGradient></defs><rect x="2" y="2" width="40" height="40" rx="11" fill="url(#kfm)"/><path d="M17 11h10v16l-5-3.8-5 3.8z" fill="#fff"/></svg>
```

White bookmark glyph on a blue-600 → sky-500 gradient squircle. Legible at
16px. No emoji in brand marks (logo tiles); UI pictograms and the bookmarklet label are unchanged.

**Wordmark:** `Keepfor` in slate-900 + `.me` in blue-600 (`#2563eb`),
weight 800, tracking `-0.03em`. Used in the header logo, login/register
cards, and save-popup header.

**Lockup rule: the icon + split-tone wordmark appear as one identical unit
everywhere** — header, login, register, save-popup. No page may restyle,
recolor, resize the pairing, or drop either half (login previously showed
a plain-text site name; that inconsistency is explicitly out of scope to
repeat). Sizes vary by context (22px header/popup, 26px auth cards) with per-page gradient IDs; the pairing never varies.

**Favicon wiring:** inline SVG data URI (no static route, no bundling change —
works identically in the Worker, local dev, and TestClient):
`<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,...">`
(full URI in Task 2) in `base.html` and the standalone `save_popup.html`.
`static/icon.svg` is not created (YAGNI — nothing references a file URL).

## Library (`templates/library.html`, `partials/item_card.html`)

- Sidebar rows become board rows: colored dot (from `tag_styles`) +
  label + count badge (slate-100 pill, darker when active), active board
  filled slate-200. All data already passed to the template
  (`tags[].count`, `total_count`); only the color mapping is new.
- Item rows gain a 20px rounded favicon: primary
  `https://www.google.com/s2/favicons?domain=<domain>&sz=64`, `onerror`
  fallback to `https://icons.duckduckgo.com/ip3/<domain>.ico`, letter-tile
  behind both in the item's first tag color. `<domain>` is the host of
  `item.canonical_url` (`item.canonical_url.split('/')[2]`), which the row
  already assumes is present when it renders the site name.
- Item tag pills use the categorical palette via `tag_styles` (reader too).
- Search input shows a visible `⌘K` hint chip; `:focus-within` gets the
  blue ring. Existing `Cmd+K` JS shortcut unchanged.
- Page headline `Library` (tight display style) + muted count subline.
- Privacy note: item favicons disclose saved domains to Google/DDG at
  render time. Acceptable for a self-hosted single-user app; documented
  here so the tradeoff is explicit, not accidental.

## Reader (`templates/reader.html`)

Structure and controls unchanged (back link, Sans/Serif/Mono, A−/A+,
light/sepia/dark, Original link, theme localStorage hooks). Tightened
headline (750, `-0.022em`), muted 12.5px meta row, palette-colored tag
pills (same `tag_styles` mapping), article body at 1.75 line-height, controls bar becomes a proper card.

## Settings (`templates/settings.html`)

Same three cards, forms, and endpoints. Bookmarklet button goes blue
(primary action), token form/table spacing unified, MCP snippet keeps its
dark code block, import/export actions align in one row. Bookmarklet
popup-positioning JS (from the earlier fix) is unchanged.

## Auth (`templates/login.html`, `register.html`)

Centered card with the new icon + split-tone wordmark, same fields and
`next` handling. Cross-links preserve `next` as today.

## Testing

- Full suite `python3 -m pytest tests/ -q` (49 tests) must stay green.
- CI ruff forms exactly: `ruff check keepfor/ tests/ --select=E,W,F,I,N`
  and `ruff format --check keepfor/ tests/`.
- New test: library HTML contains a `s2/favicons?domain=` URL and the
  `onerror` DuckDuckGo fallback.
- New test: `tag_palette_index()` + `tag_styles_for()` is deterministic (same tag, same class
  across calls) and its range covers all eight palette classes.
- Manual visual pass over library / reader / settings / login before merge,
  compared against the approved mockups in `.superpowers/brainstorm/`.
