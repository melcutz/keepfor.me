# UI Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reskin the app per `docs/superpowers/specs/2026-10-03-ui-refresh-design.md` (clean direction, blue chrome, categorical tag colors, portal-mark brand) with zero behavior changes.

**Architecture:** Template-only reskin (Jinja + Tailwind CDN classes) plus one tiny pure helper (`tag_palette_index`, md5-based, no I/O) wired into the three handlers that render item tags. Favicon uses an inline SVG data URI so no static route or bundling changes are needed.

**Tech Stack:** Jinja2 templates, Tailwind Play CDN, FastAPI, pytest, ruff.

---

## File map

- Modify `src/app.py` — add `hashlib` import, `TAG_PILL_CLASSES`, `TAG_DOT_CLASSES`, `tag_palette_index()`, `tag_styles_for()`; pass `tag_styles` in `library_page`, `search_htmx`, `reader_page`.
- Modify `templates/base.html` — SVG favicon link in `<head>`; header logo becomes portal mark + split-tone wordmark.
- Modify `templates/library.html` — `Library` headline + count subline; sidebar board rows with color dots + count badges; search box gets `⌘K` chip + blue focus ring.
- Modify `templates/partials/item_card.html` — favicon with two-stage fallback over first-tag-color letter tile; categorical tag pills; title hover blue.
- Modify `templates/reader.html` — tighter headline, palette pills (via `tag_styles`).
- Modify `templates/settings.html` — bookmarklet button indigo→blue. CAREFUL: the file has an **uncommitted** bookmarklet-positioning tweak (`window.open` with `left/top/popup`) — touch only the button's class attribute, keep the `href` byte-identical.
- Modify `templates/login.html`, `templates/register.html`, `templates/save_popup.html` — identical lockup (portal mark + split-tone wordmark); favicon link in `save_popup.html` `<head>` (it is standalone, does not extend `base.html`).
- Modify `tests/test_endpoints.py` — two new tests (palette determinism; search HTML shows favicon + pill class) plus one favicon-link assertion.
- Reference only: approved mockups in `.superpowers/brainstorm/2034394-1791013689/content/` (`showcase-v2.html` is the latest truth; `tag-colors.html` for the palette).

---

### Task 0: Spec correction (data-URI favicon, no static route)

The spec's "Favicon wiring" section demands a `/static` route, but `save_popup.html` works without any backend change if the favicon is an inline SVG data URI (identical rendering in prod Worker, local dev, and TestClient). Update the spec to match.

**Files:**
- Modify: `docs/superpowers/specs/2026-10-03-ui-refresh-design.md`

- [ ] **Step 1: Edit the favicon wiring paragraph**

Old:

```markdown
**Favicon wiring:** `<link rel="icon" type="image/svg+xml" href="/static/icon.svg">`
in `base.html` and the standalone `save_popup.html`. Requires a static-file
route for `/static` (new, see implementation plan) — `img-src`/`style-src`
CSP already permits same-origin.
```

New:

```markdown
**Favicon wiring:** inline SVG data URI (no static route, no bundling change —
works identically in the Worker, local dev, and TestClient):
`<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,...">`
(full URI in Task 2) in `base.html` and the standalone `save_popup.html`.
`static/icon.svg` is not created (YAGNI — nothing references a file URL).
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-10-03-ui-refresh-design.md
git commit -m "docs: favicon via data URI, no static route"
```

---

### Task 1: Tag palette helper + unit test

**Files:**
- Modify: `src/app.py`
- Test: `tests/test_endpoints.py` (append at end of file)

- [ ] **Step 1: Write the failing test**

```python
def test_tag_palette_index_is_deterministic():
    """Palette slot is stable per tag and covers all eight slots."""
    from src.app import TAG_DOT_CLASSES, TAG_PILL_CLASSES, tag_palette_index

    assert len(TAG_PILL_CLASSES) == 8
    assert len(TAG_DOT_CLASSES) == 8
    assert tag_palette_index("design") == tag_palette_index("design")
    for tag in ["design", "laravel", "cooking", "research"]:
        assert 0 <= tag_palette_index(tag) <= 7
    seen = {tag_palette_index(f"tag-{i}") for i in range(500)}
    assert seen == set(range(8))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_endpoints.py::test_tag_palette_index_is_deterministic -q`
Expected: FAIL with `ImportError` (names do not exist yet)

- [ ] **Step 3: Add the import**

Old (`src/app.py`, lines 1–4):

```python
import os
import time
from typing import Any
from urllib.parse import urlencode
```

New:

```python
import hashlib
import os
import time
from typing import Any
from urllib.parse import urlencode
```

- [ ] **Step 4: Add the palette block after `_safe_next()`** (before the `@app.get("/auth/login", ...)` decorator)

```python
TAG_PILL_CLASSES = [
    "bg-blue-50 text-blue-700",
    "bg-emerald-50 text-emerald-700",
    "bg-amber-50 text-amber-700",
    "bg-rose-50 text-rose-700",
    "bg-violet-50 text-violet-700",
    "bg-cyan-50 text-cyan-700",
    "bg-orange-50 text-orange-700",
    "bg-slate-100 text-slate-600",
]

TAG_DOT_CLASSES = [
    "bg-blue-600",
    "bg-emerald-600",
    "bg-amber-500",
    "bg-rose-500",
    "bg-violet-500",
    "bg-cyan-500",
    "bg-orange-500",
    "bg-slate-400",
]


def tag_palette_index(tag: str) -> int:
    """Deterministic 0-7 palette slot for a tag name (md5, stable across processes)."""
    return hashlib.md5(tag.encode("utf-8")).digest()[0] % 8


def tag_styles_for(tags: list[str]) -> dict[str, tuple[str, str]]:
    """Map each tag name to (pill classes, dot class)."""
    styles = {}
    for t in dict.fromkeys(tags):
        i = tag_palette_index(t)
        styles[t] = (TAG_PILL_CLASSES[i], TAG_DOT_CLASSES[i])
    return styles
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_endpoints.py::test_tag_palette_index_is_deterministic -q`
Expected: PASS (1 passed)

- [ ] **Step 6: Lint**

Run: `ruff check src/ tests/ --select=E,W,F,I,N && ruff format --check src/ tests/`
Expected: `All checks passed!` + `files already formatted`

- [ ] **Step 7: Commit**

```bash
git add src/app.py tests/test_endpoints.py
git commit -m "feat: deterministic tag-to-palette mapping with tests"
```

---

### Task 2: Base brand (favicon + header lockup)

**Files:**
- Modify: `templates/base.html`
- Test: `tests/test_endpoints.py` (append at end of file)

Shared SVG snippet (use gradient id `kfm-base` here; other files use their own id to avoid duplicate IDs):

```html
<svg width="22" height="22" viewBox="0 0 44 44" aria-hidden="true"><defs><linearGradient id="kfm-base" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2563eb"/><stop offset="1" stop-color="#0ea5e9"/></linearGradient></defs><rect x="2" y="2" width="40" height="40" rx="11" fill="url(#kfm-base)"/><path d="M17 11h10v16l-5-3.8-5 3.8z" fill="#fff"/></svg>
```

- [ ] **Step 1: Write the failing test**

```python
def test_login_page_has_svg_favicon(client):
    """Favicon is an inline SVG data URI (no static route needed)."""
    response = client.get("/auth/login")
    assert response.status_code == 200
    assert 'rel="icon"' in response.text
    assert "data:image/svg+xml" in response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_endpoints.py::test_login_page_has_svg_favicon -q`
Expected: FAIL (`assert 'rel="icon"'` — no favicon link exists yet)

- [ ] **Step 3: Add the favicon link in `templates/base.html`** (inside `<head>`, after the `<title>` line)

```html
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 44 44'%3E%3Cdefs%3E%3ClinearGradient id='kfm' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%232563eb'/%3E%3Cstop offset='1' stop-color='%230ea5e9'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect x='2' y='2' width='40' height='40' rx='11' fill='url(%23kfm)'/%3E%3Cpath d='M17 11h10v16l-5-3.8-5 3.8z' fill='%23fff'/%3E%3C/svg%3E">
```

- [ ] **Step 4: Replace the header logo in `templates/base.html`**

Old:

```html
      <a href="/" class="flex items-center space-x-2 text-slate-900 font-bold tracking-tight text-lg">
        <span class="p-1.5 bg-slate-900 text-white rounded-lg">📚</span>
        <span>Keepfor.me</span>
      </a>
```

New:

```html
      <a href="/" class="flex items-center space-x-2 text-slate-900 font-extrabold tracking-[-0.03em] text-lg">
        <svg width="22" height="22" viewBox="0 0 44 44" aria-hidden="true"><defs><linearGradient id="kfm-base" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2563eb"/><stop offset="1" stop-color="#0ea5e9"/></linearGradient></defs><rect x="2" y="2" width="40" height="40" rx="11" fill="url(#kfm-base)"/><path d="M17 11h10v16l-5-3.8-5 3.8z" fill="#fff"/></svg>
        <span>Keepfor<span class="text-blue-600">.me</span></span>
      </a>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_endpoints.py -q`
Expected: PASS (all tests in file, including the 2 new ones)

- [ ] **Step 6: Commit**

```bash
git add templates/base.html tests/test_endpoints.py
git commit -m "feat: portal-mark header lockup and SVG favicon"
```

---

### Task 3: Library page (headline, sidebar, search, item cards)

**Files:**
- Modify: `src/app.py` (`library_page`, `search_htmx`)
- Modify: `templates/library.html`, `templates/partials/item_card.html`
- Test: `tests/test_endpoints.py` (append at end of file)

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_search_results_show_favicons_and_tag_colors(
    client, db, auth_headers
):
    """Item cards render favicon with fallback and palette-colored tags."""
    from src.app import TAG_PILL_CLASSES, tag_palette_index
    from src.models.items import save_item

    user = auth_headers["admin_user"]
    await save_item(db, None, user["id"], "https://example.com/article", ["design"])
    client.cookies["kfm_session"] = auth_headers["admin_session"]

    response = client.post(
        "/search", data={"query": "example", "mode": "keyword", "tag": ""}
    )
    assert response.status_code == 200
    assert "s2/favicons?domain=example.com" in response.text
    assert "onerror" in response.text
    assert TAG_PILL_CLASSES[tag_palette_index("design")] in response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_endpoints.py::test_search_results_show_favicons_and_tag_colors -q`
Expected: FAIL (`s2/favicons` not in current card markup). If it fails earlier (empty search results), debug `hybrid_search` keyword mode with `env=None` in `src/search/engine.py` — FTS must return the saved item.

- [ ] **Step 3: Wire `tag_styles` into both handlers in `src/app.py`**

In `library_page`, after `tags = await list_user_tags(db, user["id"])`, add:

```python
    tag_styles = tag_styles_for([t["name"] for t in tags])
```

and extend its render call to `template.render(current_user=user, items=items, tags=tags, active_tag=tag, query=q or "", tag_styles=tag_styles)`.

In `search_htmx`, after the `hybrid_search(...)` call, add:

```python
    tag_styles = tag_styles_for(
        [t for it in items for t in (it.get("tags") or [])]
    )
```

and change the card render to `template.render(item=it, tag_styles=tag_styles)`.

- [ ] **Step 4: Rewrite `templates/partials/item_card.html`**

Old (lines 1, 26–27, 36–44):

```html
<div id="item-card-{{ item.id }}" class="bg-white border border-slate-200 rounded-xl p-5 hover:shadow-md transition group relative">
```

```html
      <h3 class="text-base font-semibold text-slate-900 leading-snug group-hover:text-blue-600 transition">
        <a href="/items/{{ item.id }}">{{ item.title or item.url }}</a>
      </h3>
```

```html
      {% if item.tags %}
      <div class="flex flex-wrap gap-1.5 mt-3">
        {% for tag in item.tags %}
        <a href="/?tag={{ tag }}" class="px-2 py-0.5 bg-slate-100 hover:bg-slate-200 text-slate-600 text-xs rounded-md transition">
          #{{ tag }}
        </a>
        {% endfor %}
      </div>
      {% endif %}
```

New: insert the favicon block as the first child of the outer flex row (before `<div class="flex-1 pr-4">`):

```html
  {% set domain = (item.canonical_url or item.url).split('/')[2] %}
  {% set fallback = tag_styles.get(item.tags[0] if item.tags else '', ('bg-slate-100 text-slate-600', 'bg-slate-400')) %}
  <span class="relative w-5 h-5 shrink-0 mt-1">
    <span class="absolute inset-0 rounded-md text-[11px] font-extrabold flex items-center justify-center {{ fallback[0] }}">{{ (item.site_name or domain)[0] }}</span>
    <img src="https://www.google.com/s2/favicons?domain={{ domain }}&sz=64" alt="" loading="lazy" onerror="if(!this.dataset.fbk){this.dataset.fbk=1;this.src='https://icons.duckduckgo.com/ip3/{{ domain }}.ico';}else{this.remove();}" class="absolute inset-0 w-5 h-5 rounded-md bg-white">
  </span>
```

headline becomes:

```html
      <h3 class="text-[15.5px] font-semibold text-slate-900 leading-snug tracking-[-0.01em] group-hover:text-blue-600 transition">
```

tags become:

```html
      {% if item.tags %}
      <div class="flex flex-wrap gap-1.5 mt-3">
        {% for tag in item.tags %}
        {% set ts = tag_styles.get(tag, ('bg-slate-100 text-slate-600', 'bg-slate-400')) %}
        <a href="/?tag={{ tag }}" class="px-2 py-0.5 {{ ts[0] }} text-xs font-medium rounded-md transition">
          #{{ tag }}
        </a>
        {% endfor %}
      </div>
      {% endif %}
```

- [ ] **Step 5: Update `templates/library.html`**

(a) Sidebar rows — old:

```html
          <a href="/" class="flex items-center justify-between px-2.5 py-1.5 rounded-lg text-xs font-medium {% if not active_tag %}bg-slate-200 text-slate-900{% else %}text-slate-600 hover:bg-slate-100{% endif %}">
            <span>All Items</span>
            <span class="text-slate-400">{{ total_count or items|length }}</span>
          </a>
          {% for t in tags %}
          <a href="/?tag={{ t.name }}" class="flex items-center justify-between px-2.5 py-1.5 rounded-lg text-xs font-medium {% if active_tag == t.name %}bg-slate-200 text-slate-900{% else %}text-slate-600 hover:bg-slate-100{% endif %}">
            <span>#{{ t.name }}</span>
            <span class="text-slate-400">{{ t.count }}</span>
          </a>
          {% endfor %}
```

New: same structure, but each row starts with a color dot from `tag_styles` (`All Items` keeps a blue dot) and counts become badge pills:

```html
          <a href="/" class="flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-xs font-medium {% if not active_tag %}bg-slate-200 text-slate-900{% else %}text-slate-600 hover:bg-slate-100{% endif %}">
            <span class="w-2 h-2 rounded-full bg-blue-600 shrink-0"></span>
            <span>All Items</span>
            <span class="ml-auto text-slate-400 bg-slate-100 rounded-md px-1.5">{{ total_count or items|length }}</span>
          </a>
          {% for t in tags %}
          {% set ts = tag_styles.get(t.name, ('bg-slate-100 text-slate-600', 'bg-slate-400')) %}
          <a href="/?tag={{ t.name }}" class="flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-xs font-medium {% if active_tag == t.name %}bg-slate-200 text-slate-900{% else %}text-slate-600 hover:bg-slate-100{% endif %}">
            <span class="w-2 h-2 rounded-full {{ ts[1] }} shrink-0"></span>
            <span>#{{ t.name }}</span>
            <span class="ml-auto text-slate-400 bg-slate-100 rounded-md px-1.5">{{ t.count }}</span>
          </a>
          {% endfor %}
```

(b) Headline — insert before the `<!-- Search & Mode Bar -->` comment:

```html
      <div class="mb-5">
        <h1 class="text-[28px] font-bold tracking-[-0.025em] text-slate-900">Library</h1>
        <p class="text-xs text-slate-500 mt-1">{{ total_count or items|length }} saves · search by keyword or meaning</p>
      </div>
```

(c) Search box — old container:

```html
      <div class="bg-white border border-slate-200 rounded-xl p-3 shadow-sm flex flex-col sm:flex-row items-center gap-3">
```

New (add blue focus ring):

```html
      <div class="bg-white border border-slate-200 rounded-xl p-3 shadow-sm flex flex-col sm:flex-row items-center gap-3 focus-within:border-blue-600 focus-within:ring-2 focus-within:ring-blue-100">
```

and inside the search wrapper div, after the `<input ...>` line, add the hint chip:

```html
          <kbd class="absolute inset-y-0 right-3 hidden sm:flex items-center px-1.5 text-[11px] bg-slate-100 border border-slate-200 border-b-2 rounded-md text-slate-400 font-sans pointer-events-none">⌘K</kbd>
```

The wrapper div is `class="relative flex-1 w-full"` so the absolute chip docks right. (`pointer-events-none` keeps htmx input clicks working.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -q`
Expected: PASS, 52 tests (49 existing + 3 new)

- [ ] **Step 7: Lint**

Run: `ruff check src/ tests/ --select=E,W,F,I,N && ruff format --check src/ tests/`
Expected: `All checks passed!` + `files already formatted`

- [ ] **Step 8: Commit**

```bash
git add src/app.py templates/library.html templates/partials/item_card.html tests/test_endpoints.py
git commit -m "feat: warm library page with favicons and tag colors"
```

---

### Task 4: Reader tightening

**Files:**
- Modify: `src/app.py` (`reader_page`), `templates/reader.html`

- [ ] **Step 1: Wire `tag_styles` into `reader_page`** — after `item = await get_item(...)` (and its 404 check), add:

```python
    tag_styles = tag_styles_for(item.get("tags") or [])
```

and extend the render call with `tag_styles=tag_styles`.

- [ ] **Step 2: Tighten the headline** — old:

```html
        <h1 class="text-2xl sm:text-3xl font-bold tracking-tight text-slate-900 leading-tight">
```

New:

```html
        <h1 class="text-2xl sm:text-3xl font-extrabold tracking-[-0.022em] text-slate-900 leading-tight">
```

- [ ] **Step 3: Palette pills** — old:

```html
        <div class="flex flex-wrap gap-1.5 mt-4">
          {% for tag in item.tags %}
          <span class="px-2 py-0.5 bg-slate-100 text-slate-600 text-xs rounded-md">#{{ tag }}</span>
          {% endfor %}
        </div>
```

New:

```html
        <div class="flex flex-wrap gap-1.5 mt-4">
          {% for tag in item.tags %}
          {% set ts = tag_styles.get(tag, ('bg-slate-100 text-slate-600', 'bg-slate-400')) %}
          <span class="px-2 py-0.5 {{ ts[0] }} text-xs font-medium rounded-md">#{{ tag }}</span>
          {% endfor %}
        </div>
```

(Controls bar, themes, and meta row stay exactly as they are.)

- [ ] **Step 4: Run the suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (52 tests)

- [ ] **Step 5: Commit**

```bash
git add src/app.py templates/reader.html
git commit -m "feat: tighten reader headline and tag pills"
```

---

### Task 5: Settings button

**Files:**
- Modify: `templates/settings.html` (class attribute ONLY)

- [ ] **Step 1: Recolor the bookmarklet button** — replace ONLY the string `bg-indigo-600 hover:bg-indigo-700` with `bg-blue-600 hover:bg-blue-700` in the bookmarklet anchor. Do NOT touch the `href` — it contains the uncommitted popup-positioning JS that must survive byte-identical.

- [ ] **Step 2: Run the suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (52 tests)

- [ ] **Step 3: Commit**

```bash
git add templates/settings.html
git commit -m "feat: blue bookmarklet button in settings"
```

---

### Task 6: Auth + popup lockups

**Files:**
- Modify: `templates/login.html`, `templates/register.html`, `templates/save_popup.html`
- Test: `tests/test_endpoints.py` (append at end of file)

Lockup snippet (gradient ids unique per file: `kfm-login`, `kfm-register`, `kfm-popup`):

```html
<svg width="26" height="26" viewBox="0 0 44 44" aria-hidden="true"><defs><linearGradient id="kfm-ID" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2563eb"/><stop offset="1" stop-color="#0ea5e9"/></linearGradient></defs><rect x="2" y="2" width="40" height="40" rx="11" fill="url(#kfm-ID)"/><path d="M17 11h10v16l-5-3.8-5 3.8z" fill="#fff"/></svg>
<span class="font-extrabold tracking-[-0.03em]">Keepfor<span class="text-blue-600">.me</span></span>
```

- [ ] **Step 1: Write the failing test**

```python
def test_auth_pages_share_identical_lockup(client):
    """Login, register, and popup use the same icon + split-tone wordmark."""
    for path in ["/auth/login", "/auth/register"]:
        page = client.get(path)
        assert page.status_code == 200
        assert "Keepfor<span" in page.text
        assert "text-blue-600" in page.text
        assert "linearGradient" in page.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_endpoints.py::test_auth_pages_share_identical_lockup -q`
Expected: FAIL (`Keepfor<span` absent — current headings are plain text)

- [ ] **Step 3: `templates/login.html`** — old header block:

```html
    <div class="text-center mb-6">
      <span class="text-3xl">📚</span>
      <h2 class="text-xl font-bold text-slate-900 mt-2">Welcome to Keepfor.me</h2>
      <p class="text-xs text-slate-500 mt-1">Sign in to your personal read-it-later library</p>
    </div>
```

New:

```html
    <div class="text-center mb-6">
      <div class="flex items-center justify-center gap-2 text-xl">
        <svg width="26" height="26" viewBox="0 0 44 44" aria-hidden="true"><defs><linearGradient id="kfm-login" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2563eb"/><stop offset="1" stop-color="#0ea5e9"/></linearGradient></defs><rect x="2" y="2" width="40" height="40" rx="11" fill="url(#kfm-login)"/><path d="M17 11h10v16l-5-3.8-5 3.8z" fill="#fff"/></svg>
        <span class="font-extrabold tracking-[-0.03em] text-slate-900">Keepfor<span class="text-blue-600">.me</span></span>
      </div>
      <p class="text-xs text-slate-500 mt-2">Sign in to your personal read-it-later library</p>
    </div>
```

- [ ] **Step 4: `templates/register.html`** — old:

```html
    <div class="text-center mb-6">
      <span class="text-3xl">🔑</span>
      <h2 class="text-xl font-bold text-slate-900 mt-2">Initialize Keepfor.me</h2>
      <p class="text-xs text-slate-500 mt-1">First registered user claims administrative ownership</p>
    </div>
```

New: same lockup row (gradient id `kfm-register`) above the kept heading:

```html
    <div class="text-center mb-6">
      <div class="flex items-center justify-center gap-2 text-xl">
        <svg width="26" height="26" viewBox="0 0 44 44" aria-hidden="true"><defs><linearGradient id="kfm-register" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2563eb"/><stop offset="1" stop-color="#0ea5e9"/></linearGradient></defs><rect x="2" y="2" width="40" height="40" rx="11" fill="url(#kfm-register)"/><path d="M17 11h10v16l-5-3.8-5 3.8z" fill="#fff"/></svg>
        <span class="font-extrabold tracking-[-0.03em] text-slate-900">Keepfor<span class="text-blue-600">.me</span></span>
      </div>
      <h2 class="text-xl font-bold text-slate-900 mt-2">Initialize Keepfor.me</h2>
      <p class="text-xs text-slate-500 mt-1">First registered user claims administrative ownership</p>
    </div>
```

- [ ] **Step 5: `templates/save_popup.html`** — in `<head>`, add the same favicon `<link>` from Task 2 Step 3. In the body header block, old:

```html
    <div class="flex items-center space-x-2 mb-4">
      <span class="p-1 bg-slate-900 text-white rounded">📚</span>
      <h2 class="text-base font-bold text-slate-900">Save to Keepfor.me</h2>
    </div>
```

New:

```html
    <div class="flex items-center space-x-2 mb-4">
      <svg width="22" height="22" viewBox="0 0 44 44" aria-hidden="true"><defs><linearGradient id="kfm-popup" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2563eb"/><stop offset="1" stop-color="#0ea5e9"/></linearGradient></defs><rect x="2" y="2" width="40" height="40" rx="11" fill="url(#kfm-popup)"/><path d="M17 11h10v16l-5-3.8-5 3.8z" fill="#fff"/></svg>
      <h2 class="text-base font-extrabold tracking-[-0.03em] text-slate-900">Keepfor<span class="text-blue-600">.me</span></h2>
    </div>
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (53 tests)

- [ ] **Step 7: Commit**

```bash
git add templates/login.html templates/register.html templates/save_popup.html tests/test_endpoints.py
git commit -m "feat: identical brand lockup on auth and popup pages"
```

---

### Task 7: Final verification (no commit)

- [ ] **Step 1: Full suite**

Run: `python3 -m pytest tests/ -q`
Expected: 53 passed

- [ ] **Step 2: CI lint forms, exactly**

Run: `ruff check src/ tests/ --select=E,W,F,I,N && ruff format --check src/ tests/`
Expected: `All checks passed!` + `files already formatted`

- [ ] **Step 3: Review the diff**

Run: `git status --porcelain && git log --oneline -8`
Expected: only intended files changed; `.wrangler/` and `.superpowers/` untouched/uncommitted; one commit per task above.

- [ ] **Step 4: Manual visual pass** — render library, reader, settings, login and compare against `.superpowers/brainstorm/2034394-1791013689/content/showcase-v2.html` and `tag-colors.html`. The companion server auto-exits after 30 min idle; restart with `scripts/start-server.sh --project-dir /home/melcutz/work/keepfor.me` from the brainstorming skill's `scripts/` dir if needed.
