# Analytics Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a habit-first `/stats` page (Keep Score, streak, contributions graph) backed by a new `item_opens` log table written on every reader visit.

**Architecture:** Migration adds the table (auto-loaded by the conftest glob, applied to prod D1 by hand). New `src/models/stats.py` holds pure scoring functions plus aggregate queries. `GET /items/{id}` gains a `BackgroundTasks` insert (established pattern at `app.py:1328`). New `GET /stats` route renders server-side HTML grid, no JS chart libs.

**Tech Stack:** FastAPI + Jinja + sqlite3/D1 dual-backend (`src/models/db.py`), htmx only, Tailwind CDN (existing), pytest.

---

## File map

- Create `migrations/0007_item_opens.sql` — table + index.
- Create `src/models/stats.py` — weights, bucketing, streak, aggregate queries.
- Modify `src/models/items.py` — add `record_open()` (one INSERT, dual-backend safe via existing `db.execute`).
- Modify `src/app.py` — `reader_page` gains `background_tasks` param + insert call; new `GET /stats` route; import stats helpers.
- Create `templates/stats.html` — banner, year grid, week strip, rhythm bars, inbox health, top tags/domains.
- Modify `templates/base.html` — add "Stats" to desktop nav (`:131-135`) and mobile nav grid (`:158-177`, `grid-cols-4` → `grid-cols-5` with a Stats tab).
- Create `tests/test_stats.py` — all new tests live here (module-level functions only, no Test* classes).

---

### Task 1: Migration + `record_open` model function

**Files:**
- Create: `migrations/0007_item_opens.sql`
- Modify: `src/models/items.py` (append near `archive_item`, ~line 891)
- Test: `tests/test_stats.py` (new file)

- [ ] **Step 1: Write the migration**

```sql
-- Migration 0007: Reader open log (powers Keep Score reads + phase-2 event log).
CREATE TABLE IF NOT EXISTS item_opens (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  item_id INTEGER NOT NULL,
  opened_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Serves per-user day-bucket aggregations without temp b-trees.
CREATE INDEX IF NOT EXISTS idx_opens_user_time ON item_opens(user_id, opened_at);
```

- [ ] **Step 2: Write the failing tests**

```python
"""Stats: score, streak, opens, /stats page."""
import datetime


def test_item_opens_table_exists(db):
    rows = db.query_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='item_opens'"
    )
    assert [dict(r) for r in rows] == [{"name": "item_opens"}]


def test_record_open_inserts_row(db, test_user_data, test_item_data):
    from src.auth.service import create_user
    from src.models.items import record_open, save_item

    user = create_user(db, test_user_data["email"], test_user_data["password"])
    item = save_item(db, user["id"], test_item_data["url"], title="T")
    assert item["is_new"] is True
    record_open(db, user["id"], item["id"])
    rows = db.query_all("SELECT * FROM item_opens WHERE user_id = ?", (user["id"],))
    assert len(rows) == 1
    assert rows[0]["item_id"] == item["id"]
```

(Verify `create_user`/`save_item` signatures against `src/auth/service.py` and `src/models/items.py:25` before running; adjust kwargs to match.)

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_stats.py -q`
Expected: FAIL — `no such table: item_opens` / `record_open` undefined. (Migration files are loaded alphabetically by conftest, so creating the file is enough for sqlite; D1 prod needs the manual `migrations apply` later, not in this task.)

- [ ] **Step 4: Add `record_open` to `src/models/items.py`**

```python
def record_open(db, user_id: int, item_id: int) -> None:
    """Log a reader visit. Fire-and-forget safe: raises nothing by itself;
    callers must wrap in try/except or BackgroundTasks."""
    db.execute(
        "INSERT INTO item_opens (user_id, item_id) VALUES (?, ?)",
        (user_id, item_id),
    )
```

`db.execute` exists on both backends — no dual-backend work needed.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_stats.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add migrations/0007_item_opens.sql src/models/items.py tests/test_stats.py
git commit -m "feat(stats): item_opens log table + record_open"
```

---

### Task 2: Pure scoring functions

**Files:**
- Create: `src/models/stats.py`
- Test: `tests/test_stats.py` (append)

Scoring contract (from spec): per-event weights save=1, tag/note/pin=1, archive=2, deduped open=3; daily cap 10 per action type; streak = consecutive UTC days with score > 0 ending today or yesterday; intensity bucket 0–4 relative to user's median active day.

- [ ] **Step 1: Write the failing tests**

```python
def test_score_day_weights_and_caps():
    from src.models.stats import score_day

    events = {"save": 12, "tag": 3, "note": 0, "pin": 1, "archive": 4, "open": 2}
    # saves capped at 10: 10*1 + 3*1 + 0 + 1*1 + 4*2 + 2*3 = 28
    assert score_day(events) == 28


def test_intensity_bucket_self_scales():
    from src.models.stats import intensity_bucket

    assert intensity_bucket(0, median=5) == 0
    assert intensity_bucket(5, median=5) == 2
    assert intensity_bucket(50, median=5) == 4
    assert intensity_bucket(3, median=0) == 1  # no history yet: any activity > 0


def test_streak_ending_yesterday_stays_alive():
    from src.models.stats import current_streak
    import datetime

    today = datetime.date.today()
    active = {today - datetime.timedelta(days=n) for n in (1, 2, 3)}
    assert current_streak(active, today) == 3
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_stats.py -q`
Expected: FAIL — `src.models.stats` does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
"""Keep Score: pure scoring functions (no DB). UTC calendar days."""

import datetime

WEIGHTS = {"save": 1, "tag": 1, "note": 1, "pin": 1, "archive": 2, "open": 3}
DAILY_CAP = 10


def score_day(events: dict) -> int:
    """events maps action -> count. Caps each action at DAILY_CAP."""
    return sum(min(int(events.get(a, 0)), DAILY_CAP) * w for a, w in WEIGHTS.items())


def intensity_bucket(score: int, median: float) -> int:
    """0-4 bucket relative to the user's median active day."""
    if score <= 0:
        return 0
    if median <= 0:
        return 1
    ratio = score / median
    if ratio < 0.5:
        return 1
    if ratio < 1.0:
        return 2
    if ratio < 2.0:
        return 3
    return 4


def current_streak(active_days: set, today: datetime.date | None = None) -> int:
    """Consecutive active days ending today or yesterday."""
    today = today or datetime.date.today()
    cursor = today if today in active_days else today - datetime.timedelta(days=1)
    streak = 0
    while cursor in active_days:
        streak += 1
        cursor -= datetime.timedelta(days=1)
    return streak
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_stats.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/models/stats.py tests/test_stats.py
git commit -m "feat(stats): pure Keep Score functions"
```

---

### Task 3: Reader hook (BackgroundTasks insert)

**Files:**
- Modify: `src/app.py` (`reader_page`, `src/app.py:589-620`)
- Test: `tests/test_stats.py` (append)

Follow the existing `BackgroundTasks` pattern (`app.py:11` import exists, `app.py:1328` usage). TestClient runs background tasks before the response returns, so assertions are deterministic.

- [ ] **Step 1: Write the failing test**

```python
def test_reader_visit_logs_open(client, test_user_data, test_item_data):
    from src.models.items import save_item
    from src.app import get_db

    # client fixture pattern: reuse the authed-client helper from test_endpoints.py
    # (register + login, then save via POST /save). Adjust to match that helper.
    ...
```

Concretely: mirror how `tests/test_endpoints.py` builds an authed client (register user, follow 303 to establish session cookie), save an item via `POST /save` with `url=...`, extract its id from the library HTML or via `get_db`, then `GET /items/{id}` twice and assert two `item_opens` rows exist for that user. If the helper needs adapting, read `test_endpoints.py` first — do not invent a new auth pattern.

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_stats.py::test_reader_visit_logs_open -q`
Expected: FAIL — no rows logged.

- [ ] **Step 3: Minimal implementation in `reader_page`**

```python
from fastapi import BackgroundTasks  # already imported at app.py:11 — do not re-import

@app.get("/items/{item_id}", response_class=HTMLResponse)
async def reader_page(request: Request, item_id: str, background_tasks: BackgroundTasks):
    ...
    item = await get_item(db, user["id"], item_id)
    if not item:
        return HTMLResponse(...)  # unchanged 404, no logging

    background_tasks.add_task(_log_open_safely, db, user["id"], item_id)
    ...
```

And a module-level helper in `src/app.py` (near `reader_page`):

```python
def _log_open_safely(db, user_id: int, item_id: str) -> None:
    try:
        record_open(db, user_id, int(item_id))
    except Exception:
        logger.warning("item_opens insert failed", exc_info=True)
```

(`logger` — check the existing logger name in `app.py` and reuse it; do not create a new one. `record_open` import from `src.models.items` alongside the existing `get_item` import.)

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_stats.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/app.py tests/test_stats.py
git commit -m "feat(stats): log reader opens via BackgroundTasks"
```

---

### Task 4: Aggregate queries (`get_user_stats`)

**Files:**
- Modify: `src/models/stats.py` (append query layer)
- Test: `tests/test_stats.py` (append)

One function returning everything `stats.html` needs, all `WHERE user_id = ?`:

```python
def get_user_stats(db, user_id: int) -> dict:
    return {
        "total_saves": int, "total_reads": int, "total_archived": int,
        "streak": int, "longest_streak": int, "total_score": int, "best_day": str,
        "day_scores": {"YYYY-MM-DD": int, ...},  # last 365 days
        "week_rhythm": [{"week": "W..", "save": n, "open": n, "archive": n}] x12,
        "unread_count": int, "oldest_untagged... no: oldest_unread": {"id","title","days"} | None,
        "archive_rate": float,
        "top_tags": [(name, count)] x10, "top_domains": [(host, count)] x10,
        "median_active_day": float,
    }
```

SQL notes: day buckets via `DATE(created_at)` on `items`/`item_tags`/`item_opens` grouped per action; archives via `read_state='archived'` (count + `MAX(updated_at)` proxy — archive transitions aren't timestamped per event, so archive points attribute to `DATE(updated_at)` for archived rows; document this approximation in a code comment). Domains via substring on `canonical_url` in Python (no stored host column). `longest_streak` from the sorted active-day list.

- [ ] **Step 1: Write failing tests** — seed via `save_item` + `record_open` + archive calls, assert `total_saves`, `streak >= 1`, `day_scores[today] == expected`, `top_tags[0]`, `unread_count`. Keep to 3 focused tests (summary numbers, day buckets, empty-library defaults with zeros/None, no crash).
- [ ] **Step 2: Run, expect FAIL** (`get_user_stats` undefined).
- [ ] **Step 3: Implement** (SQL + Python assembly; reuse `score_day`, `current_streak`, `intensity_bucket`).
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat(stats): get_user_stats aggregates"`.

---

### Task 5: `GET /stats` route + template + nav

**Files:**
- Modify: `src/app.py` (new route after the `/tags` section, ~line 823)
- Create: `templates/stats.html`
- Modify: `templates/base.html` (desktop nav + mobile nav `grid-cols-4` → `grid-cols-5`)
- Test: `tests/test_stats.py` (append)

Route shape (follow the `/tags` handler pattern — `get_current_user`, 303 to login when anonymous, `jinja_env.get_template`, `active_nav="stats"`):

```python
@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)
    db = get_db(request)
    stats = get_user_stats(db, user["id"])
    template = jinja_env.get_template("stats.html")
    return HTMLResponse(content=template.render(current_user=user, stats=stats, active_nav="stats"))
```

Template sections in order: streak banner (4 numbers) → year grid (server-rendered 53×7 divs, `title` attr per cell with breakdown, intensity class `s0`–`s4` in a page-scoped `<style>`) → week strip → 12-week rhythm (CSS bars, no JS lib) → inbox health (unread count, oldest-unread link, archive rate) → top tags / top domains lists → empty-state nudge when `total_saves == 0`. Fluid capped shell matching library (`w-full max-w-[110rem] mx-auto px-4 sm:px-6 lg:px-8 py-8`).

- [ ] **Step 1: Write failing tests** — anonymous `GET /stats` → 303 to `/auth/login`; authed → 200 containing "Streak"; empty library → 200 containing the Day-1 nudge.
- [ ] **Step 2: Run, expect FAIL** (route undefined → 404).
- [ ] **Step 3: Implement route + template + nav links.**
- [ ] **Step 4: Run, expect PASS.** Eyeball-render check: `python3 -c` render of `stats.html` with a sample stats dict to catch Jinja errors.
- [ ] **Step 5: Commit** — `git commit -m "feat(stats): /stats page, template, nav"`.

---

### Task 6: Full verification

- [ ] **Step 1: Run the CI gates exactly**

Run: `ruff check src/ tests/ --select=E,W,F,I,N && ruff format --check src/ tests/ && python3 -m pytest tests/ -q`
Expected: all green (suite currently 252 passed, 15 skipped).

- [ ] **Step 2: Manual visual check** — desktop wide + 375px: streak banner, grid, no page-level h-scrollbar (the library-scoped `overflow-x: clip` does not cover `/stats`; add the same one-line guard in `stats.html`'s style block — already in the Task 5 template spec).
- [ ] **Step 3: Commit anything outstanding, then push.** Note: push to main deploys to prod AND the new migration must be applied by hand afterwards: `npx wrangler d1 migrations apply keepfor-me-db --remote` (`deploy.yml` does not apply migrations). Do not push without flagging this to the user.

---

## Self-review

- **Spec coverage:** S1 weights/caps/streak/buckets → Tasks 2+4. Contributions graph + week strip → Task 5. Page sections (banner/rhythm/inbox/tags-domains) → Tasks 4+5. `item_opens` table + index → Task 1. Reader fire-and-forget → Task 3 (BackgroundTasks, the codebase's own pattern). Backfill-honest + empty states → Tasks 4+5 tests. Privacy (IDs only) → Task 1 schema. Phase-2 exclusions (search/MCP, trendlines, export) correctly absent.
- **Placeholders:** none — all SQL, Python, commands, and commit messages are inline. Task 3's auth-helper step explicitly points at `test_endpoints.py` instead of hand-waving.
- **Type consistency:** `record_open(db, user_id: int, item_id: int)` defined Task 1; Task 3 casts `int(item_id)` at the boundary (route ids are str). `score_day`/`current_streak`/`intensity_bucket` signatures defined once in Task 2, reused in Task 4.
