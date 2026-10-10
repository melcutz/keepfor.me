# Stats Page Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Elevate `/stats` into a polished, theme-adaptive reading analytics dashboard by fixing dark/sepia mode contrast inversion, eliminating raw UUIDs in inbox health, making top domains clickable, displaying reading volume (words read and estimated reading time), and improving mobile heatmap and rhythm chart usability.

**Architecture:** 
- The CSS contribution tokens (`--contrib-s0` through `--contrib-s4`) are defined across light, sepia, and dark themes in `templates/base.html` and consumed by `templates/stats.html`.
- `keepfor/models/stats.py` expands the aggregate queries concurrently to compute total words read, reading time, human-friendly fallback titles for unread items, and ISO week date ranges.
- `keepfor/search/engine.py` enhances keyword search to match URL/canonical domains so clicking top domains immediately filters library items.
- A lightweight vanilla JS snippet in `templates/stats.html` automatically scrolls the trailing 365-day heatmap to the present week on mobile devices.

**Tech Stack:** Python 3.12, FastAPI, SQLite / Cloudflare D1, Jinja2, Tailwind CSS (via existing utility classes & CSS variables), pytest, Playwright.

**Spec:** Codebase audit & live inspection findings recorded in conversation context and captured in `scratch/stats_dark.png`, `scratch/stats_mobile_entire.png`, and `scratch/stats_page.html`.

## Global Constraints

- **Repo Root Requirement:** Run pytest strictly from the repository root: `python3 -m pytest tests/ -q`.
- **Lint Conformity:** Must pass CI lint rules: `ruff check keepfor/ tests/ --select=E,W,F,I,N` and `ruff format --check keepfor/ tests/`.
- **Bundle Budget:** Must stay strictly below 58,000 KiB: `uvx --from workers-py pywrangler deploy --dry-run`.
- **D1 Concurrency:** Independent queries in `keepfor/models/stats.py` must run concurrently via `asyncio.gather` to avoid sequential RPC roundtrips.
- **Pure CSS / Vanilla JS:** No heavy charting libraries (Chart.js, D3, etc.) — all charts remain lightweight server-rendered HTML/CSS.

## Review Focus

1. **Dark mode inversion:** Empty days in dark mode must be dark/subtle (`#262524`), while active days must be bright/luminous emerald (`#34d399`), reversing the current inverted bug where empty cells glow white.
2. **Oldest unread fallback:** Unread items with `title IS NULL` and empty or missing URL must display `"Untitled article"` rather than leaking a raw database UUID or raising an exception.
3. **Domain search fidelity:** Clicking a top domain (e.g. `example.com`) and navigating to `/?q=example.com` must return items from that domain even if the domain name never appears in the article's extracted body text.
4. **Zero-state reading volume:** Users with 0 opens and 0 archives must cleanly display `0 words · 0 mins` without division-by-zero or formatting errors.
5. **Rhythm chart min-height:** In weeks where activity count is small (e.g. 1-2 reads) compared to a huge bookmark import spike in another week, non-zero bars must maintain a minimum visible height (e.g. 4px) rather than collapsing to invisible sub-pixel slivers.

---

### Task 1: CSS Theme Tokens for Heatmap Cells & "Today" Indicator

**Files:**
- Modify: `templates/base.html:23-142`
- Modify: `templates/stats.html:6-15, 74`
- Test: `tests/test_stats.py`

**Interfaces:**
- Consumes: CSS theme classes (`theme-dark`, `theme-sepia`) on `<body>`.
- Produces: CSS variables `--contrib-s0` through `--contrib-s4` across light, dark, and sepia modes, and theme-adaptive styling for `.s0`..`.s4`.

- [ ] **Step 1: Write the failing test**

In `tests/test_stats.py`, add a test asserting that `/stats` renders `.s0` to `.s4` using CSS custom properties (`var(--contrib-s0)` to `var(--contrib-s4)`) and `base.html` defines the theme variables.

```python
async def test_stats_heatmap_uses_theme_css_variables(client, db, test_user_data):
    """Heatmap cells must reference CSS custom properties for dark/sepia support."""
    await register_user(db, test_user_data["email"], test_user_data["password"])
    _, session_id = await login_user(
        db, test_user_data["email"], test_user_data["password"]
    )
    client.cookies["kfm_session"] = session_id

    response = client.get("/stats")
    assert response.status_code == 200
    # Templates must use CSS variables rather than hardcoded light cream colors
    assert "var(--contrib-s0)" in response.text
    assert "var(--contrib-s4)" in response.text
    # base.html must define the tokens for light, sepia, and dark
    assert "--contrib-s0:" in response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_stats.py::test_stats_heatmap_uses_theme_css_variables -v`
Expected: FAIL (assertion fails because `templates/stats.html` currently hardcodes `#ece9e2`).

- [ ] **Step 3: Implement theme tokens in `templates/base.html` and `templates/stats.html`**

In `templates/base.html`:
1. In `:root` (~line 31), add:
   ```css
   --contrib-s0: #e7e3da;
   --contrib-s1: #c9e0c2;
   --contrib-s2: #8fc183;
   --contrib-s3: #3f7d44;
   --contrib-s4: #1e3a2f;
   ```
2. In `body.theme-sepia` (~line 54), add:
   ```css
   --contrib-s0: #e4d7b5;
   --contrib-s1: #c5cca0;
   --contrib-s2: #8fa06d;
   --contrib-s3: #587342;
   --contrib-s4: #334e27;
   ```
3. In `body.theme-dark` (~line 93), add:
   ```css
   --contrib-s0: #262524;
   --contrib-s1: #0e3d28;
   --contrib-s2: #15623e;
   --contrib-s3: #22a366;
   --contrib-s4: #34d399;
   ```

In `templates/stats.html`:
1. Update `<style>` block (~line 8-12):
   ```css
   .s0 { background-color: var(--contrib-s0); }
   .s1 { background-color: var(--contrib-s1); }
   .s2 { background-color: var(--contrib-s2); }
   .s3 { background-color: var(--contrib-s3); }
   .s4 { background-color: var(--contrib-s4); }
   ```
2. Update week strip "today" indicator (~line 74):
   Change `{% if day.is_today %}border-slate-900{% else %}border-slate-200{% endif %}` to:
   `{% if day.is_today %}border-slate-900 dark:border-slate-100 ring-1 ring-slate-900 dark:ring-slate-100{% else %}border-slate-200 dark:border-stone-800{% endif %}`

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_stats.py::test_stats_heatmap_uses_theme_css_variables -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add templates/base.html templates/stats.html tests/test_stats.py
git commit -m "fix(stats): make heatmap cells theme-adaptive across dark, light, and sepia"
```

---

### Task 2: Oldest Unread Title Fallback (Prevent Raw UUIDs)

**Files:**
- Modify: `keepfor/models/stats.py:146-154, 250-259`
- Modify: `templates/stats.html:120-128`
- Test: `tests/test_stats.py`

**Interfaces:**
- Consumes: `oldest` row from `items` table (`id, title, canonical_url, url, created_at`).
- Produces: `stats["oldest_unread"]["title"]` guaranteed to be a human-readable string (falling back to host or `"Untitled article"`).

- [ ] **Step 1: Write the failing test**

In `tests/test_stats.py`:
```python
async def test_get_user_stats_oldest_unread_fallback_title(db, test_user_data):
    """When title is NULL, oldest_unread falls back to host or 'Untitled article', never UUID."""
    user = await register_user(db, test_user_data["email"], test_user_data["password"])
    uid = user["id"]
    # Insert an unread item with title=None
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title, read_state, created_at)"
        " VALUES ('uuid-1234', ?, 'https://news.ycombinator.com/item?id=1', 'https://news.ycombinator.com/item?id=1', NULL, 'unread', '2026-01-01 00:00:00');",
        (uid,),
    )

    stats = await get_user_stats(db, uid)
    assert stats["oldest_unread"] is not None
    assert stats["oldest_unread"]["id"] == "uuid-1234"
    assert stats["oldest_unread"]["title"] == "news.ycombinator.com"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_stats.py::test_get_user_stats_oldest_unread_fallback_title -v`
Expected: FAIL (assertion `stats["oldest_unread"]["title"] == "news.ycombinator.com"` fails because `title` is `None`).

- [ ] **Step 3: Implement title fallback in `keepfor/models/stats.py` and `templates/stats.html`**

In `keepfor/models/stats.py`:
1. In `get_user_stats()` query list (~line 147):
   Update query to fetch `canonical_url` and `url`:
   ```python
   db.query_first(
       "SELECT id, title, canonical_url, url, created_at FROM items"
       " WHERE user_id = ? AND read_state = 'unread'"
       " ORDER BY created_at ASC LIMIT 1;",
       (user_id,),
   )
   ```
2. In `get_user_stats()` oldest_unread processing (~line 251):
   ```python
   oldest_unread = None
   if oldest:
       raw_title = (oldest.get("title") or "").strip()
       if not raw_title:
           target_url = oldest.get("canonical_url") or oldest.get("url") or ""
           try:
               raw_title = urlparse(target_url).netloc
           except Exception:
               raw_title = ""
       display_title = raw_title if raw_title else "Untitled article"
       oldest_unread = {
           "id": oldest["id"],
           "title": display_title,
           "days": (
               today - datetime.date.fromisoformat(oldest["created_at"][:10])
           ).days,
       }
   ```
3. In `templates/stats.html:122`:
   Replace `{{ stats.oldest_unread.title or stats.oldest_unread.id }}` with:
   `{{ stats.oldest_unread.title or "Untitled article" }}`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_stats.py::test_get_user_stats_oldest_unread_fallback_title -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add keepfor/models/stats.py templates/stats.html tests/test_stats.py
git commit -m "fix(stats): provide clean hostname fallback for oldest unread title instead of raw UUID"
```

---

### Task 3: Clickable Top Domains & Library Domain Search Support

**Files:**
- Modify: `keepfor/search/engine.py:148-183`
- Modify: `templates/stats.html:154`
- Test: `tests/test_stats.py`

**Interfaces:**
- Consumes: `top_domains` in `templates/stats.html`.
- Produces: Clickable `/?q={{ host|urlencode }}` links in `templates/stats.html`, and `search_fts` in `keepfor/search/engine.py` matching domain / URL terms.

- [ ] **Step 1: Write the failing test**

In `tests/test_stats.py`:
```python
async def test_stats_top_domains_are_clickable_links(client, db, test_user_data):
    """Top domain items must render as links to /?q=<domain>."""
    await register_user(db, test_user_data["email"], test_user_data["password"])
    _, session_id = await login_user(
        db, test_user_data["email"], test_user_data["password"]
    )
    client.cookies["kfm_session"] = session_id
    uid = (await db.query_first("SELECT id FROM users LIMIT 1;"))["id"]
    await save_item(db, MockEnv(), uid, "https://github.com/torvalds/linux")

    response = client.get("/stats")
    assert response.status_code == 200
    assert 'href="/?q=github.com"' in response.text


async def test_search_fts_matches_domain_terms(db, test_user_data):
    """Searching for a domain matches items having that domain in URL."""
    from keepfor.search.engine import search_fts

    user = await register_user(db, test_user_data["email"], test_user_data["password"])
    uid = user["id"]
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title, content_text, status)"
        " VALUES ('i1', ?, 'https://arstechnica.com/gadgets/1', 'https://arstechnica.com/gadgets/1', 'Gadget Review', 'Body text without site name', 'ok');",
        (uid,),
    )

    results = await search_fts(db, uid, "arstechnica.com")
    assert len(results) >= 1
    assert any(r["item_id"] == "i1" for r in results)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_stats.py::test_stats_top_domains_are_clickable_links tests/test_stats.py::test_search_fts_matches_domain_terms -v`
Expected: FAIL (assertion `'href="/?q=github.com"' in response.text` fails because domain is in a `<span>`, and `search_fts` does not find `i1`).

- [ ] **Step 3: Implement clickable domains and URL/domain query matching**

1. In `templates/stats.html:155`:
   Replace:
   ```html
   <span class="text-slate-800 font-medium truncate">{{ host }}</span>
   ```
   With:
   ```html
   <a href="/?q={{ host|urlencode }}" class="text-slate-800 font-medium hover:text-blue-600 truncate">{{ host }}</a>
   ```

2. In `keepfor/search/engine.py:search_fts`:
   In `search_fts(db: Database, user_id: str, query: str, limit: int = 50)`:
   When `query` contains a dot (`.` or `/` or `:`):
   Query `items` table for matching `canonical_url` or `url` and merge them with FTS results:
   ```python
   url_matches = []
   if "." in query or "/" in query or ":" in query:
       url_sql = """
           SELECT id as item_id, -100.0 as rank, excerpt as snippet
           FROM items
           WHERE user_id = ? AND (canonical_url LIKE ? OR url LIKE ?)
           LIMIT ?;
       """
       clean_domain = query.strip()
       like_term = f"%{clean_domain}%"
       url_matches = await db.query_all(url_sql, (user_id, like_term, like_term, limit))
   ```
   Combine with FTS results, deduplicating by `item_id`:
   ```python
   seen = set()
   combined = []
   for r in url_matches + (fts_matches or []):
       if r["item_id"] not in seen:
           seen.add(r["item_id"])
           combined.append(r)
   return combined[:limit]
   ```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_stats.py::test_stats_top_domains_are_clickable_links tests/test_stats.py::test_search_fts_matches_domain_terms -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add keepfor/search/engine.py templates/stats.html tests/test_stats.py
git commit -m "feat(stats): make top domains clickable and support domain matching in search"
```

---

### Task 4: Reading Metrics (Total Words Read & Estimated Reading Time)

**Files:**
- Modify: `keepfor/models/stats.py:83-293`
- Modify: `templates/stats.html:23-42`
- Test: `tests/test_stats.py`

**Interfaces:**
- Consumes: `word_count` from `items` table, `item_opens` table.
- Produces: `stats["words_read"]`, `stats["words_read_display"]`, `stats["reading_time_display"]`.

- [ ] **Step 1: Write the failing test**

In `tests/test_stats.py`:
```python
async def test_get_user_stats_words_read_and_reading_time(db, test_user_data):
    """Opened and archived items contribute word counts and formatted reading time."""
    user = await register_user(db, test_user_data["email"], test_user_data["password"])
    uid = user["id"]
    # item1: 1,500 words, opened
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title, word_count, read_state, created_at)"
        " VALUES ('w1', ?, 'https://example.com/w1', 'https://example.com/w1', 'W1', 1500, 'unread', '2026-01-01 00:00:00');",
        (uid,),
    )
    await record_open(db, uid, "w1")

    # item2: 3,500 words, archived
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title, word_count, read_state, created_at)"
        " VALUES ('w2', ?, 'https://example.com/w2', 'https://example.com/w2', 'W2', 3500, 'archived', '2026-01-01 00:00:00');",
        (uid,),
    )

    # item3: 10,000 words, unread and never opened (should NOT count)
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title, word_count, read_state, created_at)"
        " VALUES ('w3', ?, 'https://example.com/w3', 'https://example.com/w3', 'W3', 10000, 'unread', '2026-01-01 00:00:00');",
        (uid,),
    )

    stats = await get_user_stats(db, uid)
    # Total words read = 1500 + 3500 = 5000 words. At 200 wpm = 25 mins.
    assert stats["words_read"] == 5000
    assert stats["words_read_display"] == "5.0k words"
    assert stats["reading_time_display"] == "25 mins"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_stats.py::test_get_user_stats_words_read_and_reading_time -v`
Expected: FAIL (`KeyError: 'words_read'`).

- [ ] **Step 3: Implement reading metrics in `keepfor/models/stats.py` and `templates/stats.html`**

In `keepfor/models/stats.py`:
1. Add pure formatting helper:
   ```python
   def format_reading_metrics(words: int) -> tuple[str, str]:
       """Format words read and estimated reading time (200 wpm standard)."""
       if words <= 0:
           return "0 words", "0 mins"
       if words < 1000:
           words_str = f"{words} words"
       else:
           words_str = f"{words / 1000:.1f}k words"

       minutes = max(1, round(words / 200))
       if minutes < 60:
           time_str = f"{minutes} min{'s' if minutes != 1 else ''}"
       else:
           time_str = f"{minutes / 60:.1f} hrs"
       return words_str, time_str
   ```
2. In `get_user_stats()`, add query for `words_read` to `asyncio.gather`:
   ```python
   words_row = await db.query_first(
       "SELECT COALESCE(SUM(word_count), 0) AS total_words FROM items"
       " WHERE user_id = ? AND (read_state = 'archived' OR id IN ("
       "   SELECT DISTINCT item_id FROM item_opens WHERE user_id = ?"
       " ));",
       (user_id, user_id),
   )
   words_read = words_row["total_words"] if words_row else 0
   words_display, time_display = format_reading_metrics(words_read)
   ```
3. Return `words_read`, `words_read_display`, `reading_time_display` in the dict.
4. In `templates/stats.html`:
   In the Streak banner (`<section class="bg-white border ...">`), add reading volume metric:
   ```html
   <div>
     <p class="text-xs font-semibold uppercase tracking-wider text-slate-500">Read volume</p>
     <p class="text-xl font-bold text-slate-900">{{ stats.words_read_display }} <span class="text-xs font-normal text-slate-500">· ~{{ stats.reading_time_display }}</span></p>
   </div>
   ```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_stats.py::test_get_user_stats_words_read_and_reading_time -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add keepfor/models/stats.py templates/stats.html tests/test_stats.py
git commit -m "feat(stats): add total words read and estimated reading time metrics"
```

---

### Task 5: Mobile Heatmap Auto-Scroll to Current Week

**Files:**
- Modify: `templates/stats.html:51, 165`
- Test: `tests/test_stats.py`

**Interfaces:**
- Consumes: Heatmap container with `id="year-grid-container"`.
- Produces: Client-side auto-scroll to the rightmost edge so the present week is immediately visible on mobile devices.

- [ ] **Step 1: Write the failing test**

In `tests/test_stats.py`:
```python
async def test_stats_mobile_heatmap_scroll_script(client, db, test_user_data):
    """The year grid container must have id and script that scrolls to present week."""
    await register_user(db, test_user_data["email"], test_user_data["password"])
    _, session_id = await login_user(
        db, test_user_data["email"], test_user_data["password"]
    )
    client.cookies["kfm_session"] = session_id

    response = client.get("/stats")
    assert response.status_code == 200
    assert 'id="year-grid-container"' in response.text
    assert 'scrollLeft = container.scrollWidth' in response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_stats.py::test_stats_mobile_heatmap_scroll_script -v`
Expected: FAIL (`assert 'id="year-grid-container"' in response.text`).

- [ ] **Step 3: Implement scroll container ID and script in `templates/stats.html`**

1. In `templates/stats.html:51`:
   Add `id="year-grid-container"` to the section:
   ```html
   <section id="year-grid-container" class="bg-white border border-slate-200 rounded-xl p-6 shadow-sm overflow-x-auto">
   ```
2. At the bottom of `templates/stats.html`:
   ```html
   <script>
     (function() {
       var container = document.getElementById('year-grid-container');
       if (container) {
         // Auto-scroll to the current week (right edge) on narrow screens
         container.scrollLeft = container.scrollWidth;
       }
     })();
   </script>
   ```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_stats.py::test_stats_mobile_heatmap_scroll_script -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add templates/stats.html tests/test_stats.py
git commit -m "feat(stats): auto-scroll heatmap to present week on mobile"
```

---

### Task 6: Rhythm Chart Usability (Calendar Date Tooltips & Min Bar Height)

**Files:**
- Modify: `keepfor/models/stats.py:222-245`
- Modify: `templates/stats.html:95-101`
- Test: `tests/test_stats.py`

**Interfaces:**
- Consumes: `week_rhythm` in `keepfor/models/stats.py`.
- Produces: `w["label"]`, `w["date_range"]` on `stats.week_rhythm`, and `min-height: 4px` on non-zero bars in `templates/stats.html`.

- [ ] **Step 1: Write the failing test**

In `tests/test_stats.py`:
```python
async def test_stats_rhythm_has_date_tooltips_and_min_height(client, db, test_user_data):
    """Rhythm chart bars must have calendar date ranges and minimum visible height when > 0."""
    await register_user(db, test_user_data["email"], test_user_data["password"])
    _, session_id = await login_user(
        db, test_user_data["email"], test_user_data["password"]
    )
    client.cookies["kfm_session"] = session_id
    uid = (await db.query_first("SELECT id FROM users LIMIT 1;"))["id"]
    item, _ = await save_item(db, MockEnv(), uid, "https://example.com/rhythm-test")
    await record_open(db, uid, item["id"])

    response = client.get("/stats")
    assert response.status_code == 200
    # Bars for non-zero actions must contain min-height to survive import spikes
    assert "min-height: 4px" in response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_stats.py::test_stats_rhythm_has_date_tooltips_and_min_height -v`
Expected: FAIL (`assert "min-height: 4px" in response.text`).

- [ ] **Step 3: Implement date ranges and min-height in `keepfor/models/stats.py` and `templates/stats.html`**

1. In `keepfor/models/stats.py:222`:
   Compute Monday and Sunday for each ISO week:
   ```python
   def _week_range_str(year: int, week: int) -> str:
       mon = datetime.date.fromisocalendar(year, week, 1)
       sun = mon + datetime.timedelta(days=6)
       if mon.month == sun.month:
           return f"{mon.strftime('%b')} {mon.day}–{sun.day}"
       return f"{mon.strftime('%b %d')} – {sun.strftime('%b %d')}"
   ```
   Add `date_range` and `date_label` to `week_rhythm` dictionary items:
   ```python
   week_rhythm = [
       {
           "week": f"{year}-W{week:02d}",
           "date_range": _week_range_str(year, week),
           "date_label": datetime.date.fromisocalendar(year, week, 1).strftime("%b %-d"),
           "save": rhythm[(year, week)]["save"],
           "open": rhythm[(year, week)]["open"],
           "archive": rhythm[(year, week)]["archive"],
       }
       for year, week in week_keys
   ]
   ```
2. In `templates/stats.html:95-101`:
   Update bar rendering to include `date_range` and minimum height:
   ```html
   <div class="flex items-end justify-center gap-[3px] w-full h-full" title="{{ w.date_range }}: {{ w.save }} saves, {{ w.open }} reads, {{ w.archive }} archived">
     <div class="w-2 sm:w-3 rounded-t bg-blue-600" style="height: {{ (100 * w.save / max_rhythm_action) | round(1) if max_rhythm_action else 0 }}%;{% if w.save > 0 %} min-height: 4px;{% endif %}"></div>
     <div class="w-2 sm:w-3 rounded-t bg-emerald-600" style="height: {{ (100 * w.open / max_rhythm_action) | round(1) if max_rhythm_action else 0 }}%;{% if w.open > 0 %} min-height: 4px;{% endif %}"></div>
     <div class="w-2 sm:w-3 rounded-t bg-amber-600" style="height: {{ (100 * w.archive / max_rhythm_action) | round(1) if max_rhythm_action else 0 }}%;{% if w.archive > 0 %} min-height: 4px;{% endif %}"></div>
   </div>
   ```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_stats.py::test_stats_rhythm_has_date_tooltips_and_min_height -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add keepfor/models/stats.py templates/stats.html tests/test_stats.py
git commit -m "feat(stats): add calendar date tooltips and min bar height to rhythm chart"
```

---

### Task 7: Full System Verification (Pytest, Lint, Bundle Dry-Run, Playwright Screenshot)

**Files:**
- Test & Verification across entire repo

- [ ] **Step 1: Run complete test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All tests pass (>= 242 passed, 1 skipped).

- [ ] **Step 2: Run CI exact lint checks**

Run:
```bash
ruff check keepfor/ tests/ --select=E,W,F,I,N
ruff format --check keepfor/ tests/
```
Expected: All checks pass cleanly without errors.

- [ ] **Step 3: Run pywrangler bundle size dry-run**

Run: `uvx --from workers-py pywrangler deploy --dry-run`
Expected: Bundle size under 58,000 KiB budget gate (around 52,600 KiB).

- [ ] **Step 4: Run Playwright test script to inspect dark & sepia themes**

Run scratch script with Playwright to verify that dark mode heatmap renders dark background with vibrant emerald cells and zero contrast inversions.

- [ ] **Step 5: Commit & Final Wrap-up**

```bash
git commit --allow-empty -m "chore: verify full test suite, lint, and bundle size for stats enhancements"
```
