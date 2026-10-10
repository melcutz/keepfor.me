# Codebase Audit Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remediate all functional bugs, N+1 RPC performance bottlenecks, export data loss/injection flaws, browser extension defects, repo hygiene issues, deprecation warnings, and silent error suppressions identified in the 2026-10-08 codebase audit.

**Architecture:** Update `STATUS_GROUPS` in the search engine to map `"saved"` to `("ok", "saved")`; batch tag lookups in `get_pinned_items` and `export_library_json` to avoid D1 RPC waterfalls; include migration 0006 metadata in JSON exports and HTML-escape Netscape exports; fix extension popup settings placeholder and title payload; clean up tracked Miniflare cache/state, add `.venv/` to `.gitignore`, and remove dead `keepfor/config.py`, `pydantic-settings`, and obsolete dev dependencies; modernize `datetime.utcnow()` to timezone-aware UTC and Pydantic models to `ConfigDict`; and add structured warning logs for Vectorize and R2 failures.

**Tech Stack:** Python 3.11/3.12, FastAPI, SQLite / Cloudflare D1, Pydantic V2, JavaScript (Chrome Extension MV3), Pytest, Ruff.

**Spec:** [`docs/2026-10-08-codebase-audit-findings.md`](file:///home/melcutz/work/keepfor.me/docs/2026-10-08-codebase-audit-findings.md)

## Global Constraints

- Run pytest from the repository root: `python3 -m pytest tests/ -q` (subdirectories fail namespace package resolution).
- Linter and formatter must match CI exactly: `ruff check keepfor/ tests/ --select=E,W,F,I,N` and `ruff format --check keepfor/ tests/`.
- Heavy parsers (`trafilatura`, `bs4`) must remain deferred and out of the module-scope import path.
- HTML form and htmx endpoints must return HTML, not JSON.
- Maintain dual-backend SQLite/D1 compatibility in `keepfor/models/db.py` whenever modifying database query methods.
- Cloudflare worker bundle must stay under the 58,000 KiB CI gate (`uvx --from workers-py pywrangler deploy --dry-run`).

## Review Focus

1. **Quick notes with whitespace or empty metadata in status filters**: Notes with status `'saved'` must be counted in `get_status_counts` under `"saved"` and returned by `get_recent_items(status="saved")` without crashing.
2. **Batch tag mapping with empty vs multiple tags**: `get_pinned_items` and `export_library_json` must correctly map items with 0 tags to `[]` and items with multiple tags to their full tag lists without dropping items.
3. **Export behavior on empty library**: `export_library_json` and `export_library_html` on an account with zero items must return `[]` and valid empty Netscape bookmark HTML without executing malformed SQL.
4. **HTML injection and entity escaping in Netscape export**: URLs, tags, and titles containing `"`, `&`, `<`, and `>` must be escaped via `html.escape` to prevent corrupting bookmark files.
5. **Extension popup save when title is empty**: When `titleInput` is empty or whitespace-only, the payload should omit `title` (or set `undefined`) so the server retains auto-derived title behavior.

---

### Task 1: Status Filter Support for Quick Notes (Finding 1.1)

**Files:**
- Modify: [`keepfor/search/engine.py:15-20`](file:///home/melcutz/work/keepfor.me/keepfor/search/engine.py#L15-L20)
- Test: [`tests/test_status_filters.py`](file:///home/melcutz/work/keepfor.me/tests/test_status_filters.py)

**Interfaces:**
- Consumes: [`STATUS_GROUPS`](file:///home/melcutz/work/keepfor.me/keepfor/search/engine.py#L15), [`save_note()`](file:///home/melcutz/work/keepfor.me/keepfor/models/items.py#L765), [`get_status_counts()`](file:///home/melcutz/work/keepfor.me/keepfor/search/engine.py#L94), [`get_recent_items()`](file:///home/melcutz/work/keepfor.me/keepfor/search/engine.py#L420)
- Produces: Updated `STATUS_GROUPS["saved"] = ("ok", "saved")`

- [ ] **Step 1: Write the failing test**

In [`tests/test_status_filters.py`](file:///home/melcutz/work/keepfor.me/tests/test_status_filters.py), add `test_quick_notes_included_in_saved_status_group`:

```python
@pytest.mark.asyncio
async def test_quick_notes_included_in_saved_status_group(db):
    from keepfor.auth.service import register_user
    from keepfor.models.items import save_note
    from keepfor.search.engine import get_recent_items, get_status_counts

    user = await register_user(db, "note_status@test.local", "password123")
    user_id = user["id"]

    # Save a quick note (hardcodes status='saved')
    note = await save_note(
        db, None, user_id, "Note Title", "Note content text", ["testtag"]
    )
    assert note["status"] == "saved"

    # Status counts must count the note under "saved"
    counts = await get_status_counts(db, user_id)
    assert counts["saved"] == 1
    assert counts["all"] == 1

    # Filter ?status=saved must return the note
    items = await get_recent_items(db, user_id, status="saved")
    assert len(items) == 1
    assert items[0]["id"] == note["id"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_status_filters.py::test_quick_notes_included_in_saved_status_group -v`
Expected: FAIL with `assert counts["saved"] == 1` (where `counts["saved"]` is `0`).

- [ ] **Step 3: Update `STATUS_GROUPS` in `keepfor/search/engine.py`**

In [`keepfor/search/engine.py`](file:///home/melcutz/work/keepfor.me/keepfor/search/engine.py#L15-L19), update `STATUS_GROUPS`:

```python
STATUS_GROUPS = {
    "extracting": ("queued", "fetching"),
    "saved": ("ok", "saved"),
    "failed": ("failed",),
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_status_filters.py::test_quick_notes_included_in_saved_status_group -v`
Expected: PASS

- [ ] **Step 5: Run full status filter tests & lint**

Run: `python3 -m pytest tests/test_status_filters.py -q && ruff check keepfor/search/engine.py --select=E,W,F,I,N && ruff format --check keepfor/search/engine.py`
Expected: 16 passed, all checks passed.

- [ ] **Step 6: Commit**

```bash
git add keepfor/search/engine.py tests/test_status_filters.py
git commit -m "fix(search): include status='saved' in saved status group for quick notes"
```

---

### Task 2: Batched Tag Loading for Pinned Items Shelf (Finding 2.1)

**Files:**
- Modify: [`keepfor/models/items.py:730-742`](file:///home/melcutz/work/keepfor.me/keepfor/models/items.py#L730-L742)
- Test: [`tests/test_notes_and_pins.py`](file:///home/melcutz/work/keepfor.me/tests/test_notes_and_pins.py)

**Interfaces:**
- Consumes: [`Database.query_all()`](file:///home/melcutz/work/keepfor.me/keepfor/models/db.py#L22)
- Produces: [`get_pinned_items(db: Database, user_id: str) -> list[dict[str, Any]]`](file:///home/melcutz/work/keepfor.me/keepfor/models/items.py#L730) with batched tag resolution

- [ ] **Step 1: Write the failing test**

In [`tests/test_notes_and_pins.py`](file:///home/melcutz/work/keepfor.me/tests/test_notes_and_pins.py), expand `test_get_pinned_items` or add `test_get_pinned_items_batch_tags`:

```python
@pytest.mark.asyncio
async def test_get_pinned_items_batch_tags(user_with_env):
    user, env, db = user_with_env
    # Item 1 with 2 tags
    i1, _ = await save_item(db, env, user["id"], "https://example.com/pin1", ["tag1", "tag2"])
    await toggle_pin_item(db, user["id"], i1["id"])

    # Item 2 with 0 tags
    i2, _ = await save_item(db, env, user["id"], "https://example.com/pin2", [])
    await toggle_pin_item(db, user["id"], i2["id"])

    # Item 3 with 1 tag
    i3, _ = await save_item(db, env, user["id"], "https://example.com/pin3", ["tag3"])
    await toggle_pin_item(db, user["id"], i3["id"])

    pinned = await get_pinned_items(db, user["id"])
    assert len(pinned) == 3
    tags_by_id = {p["id"]: p["tags"] for p in pinned}
    assert set(tags_by_id[i1["id"]]) == {"tag1", "tag2"}
    assert tags_by_id[i2["id"]] == []
    assert set(tags_by_id[i3["id"]]) == {"tag3"}
```

- [ ] **Step 2: Run test to verify existing behavior passes or fails**

Run: `python3 -m pytest tests/test_notes_and_pins.py::test_get_pinned_items_batch_tags -v`
Expected: PASS (current implementation passes functionally but does N+1 queries).

- [ ] **Step 3: Implement batched tag lookup in `get_pinned_items`**

In [`keepfor/models/items.py`](file:///home/melcutz/work/keepfor.me/keepfor/models/items.py#L730-L742), replace the loop calling `get_item_tags` with a single batch `IN (...)` query:

```python
async def get_pinned_items(db: Database, user_id: str) -> list[dict[str, Any]]:
    """Pinned shelf items, newest first, with tags attached."""
    rows = await db.query_all(
        "SELECT * FROM items WHERE user_id = ? AND is_pinned = 1"
        " ORDER BY created_at DESC;",
        (user_id,),
    )
    if not rows:
        return []

    item_ids = [r["id"] for r in rows]
    placeholders = ",".join("?" for _ in item_ids)
    tag_rows = await db.query_all(
        f"""
        SELECT it.item_id, t.name as tag_name
        FROM item_tags it
        JOIN tags t ON it.tag_id = t.id
        WHERE it.item_id IN ({placeholders});
        """,
        tuple(item_ids),
    )
    item_tags_map: dict[str, list[str]] = {}
    for tr in tag_rows:
        item_tags_map.setdefault(tr["item_id"], []).append(tr["tag_name"])

    out: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        item["tags"] = item_tags_map.get(r["id"], [])
        out.append(item)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_notes_and_pins.py::test_get_pinned_items_batch_tags -v`
Expected: PASS

- [ ] **Step 5: Run all notes and pins tests & lint**

Run: `python3 -m pytest tests/test_notes_and_pins.py -q && ruff check keepfor/models/items.py --select=E,W,F,I,N && ruff format --check keepfor/models/items.py`
Expected: 11 passed, all checks passed.

- [ ] **Step 6: Commit**

```bash
git add keepfor/models/items.py tests/test_notes_and_pins.py
git commit -m "perf(items): batch tag retrieval in get_pinned_items to eliminate N+1 D1 RPCs"
```

---

### Task 3: Export Fixes — RPC Batching, Metadata Retention, and HTML Escaping (Findings 2.2, 3.1, 3.2)

**Files:**
- Modify: [`keepfor/utils/importer.py:82-132`](file:///home/melcutz/work/keepfor.me/keepfor/utils/importer.py#L82-L132)
- Test: [`tests/test_importer.py`](file:///home/melcutz/work/keepfor.me/tests/test_importer.py)

**Interfaces:**
- Consumes: [`export_library_json(db: Database, user_id: str) -> list[dict[str, Any]]`](file:///home/melcutz/work/keepfor.me/keepfor/utils/importer.py#L82), [`export_library_html(db: Database, user_id: str) -> str`](file:///home/melcutz/work/keepfor.me/keepfor/utils/importer.py#L111)
- Produces:
  - `export_library_json`: Single user-scoped tag query, includes `user_notes, item_type, is_pinned, image_url, summary, read_state`.
  - `export_library_html`: Escaped URLs, tags, and titles using `html.escape`.

- [ ] **Step 1: Write the failing tests**

In [`tests/test_importer.py`](file:///home/melcutz/work/keepfor.me/tests/test_importer.py), add tests for metadata retention, batched export, empty export, and HTML escaping:

```python
import html
import pytest
from keepfor.auth.service import register_user
from keepfor.models.items import save_item, save_note, update_user_notes, toggle_pin_item
from keepfor.utils.importer import export_library_json, export_library_html


@pytest.mark.asyncio
async def test_export_library_json_includes_metadata_and_batches_tags(db):
    user = await register_user(db, "exporter@test.local", "password123")
    user_id = user["id"]

    # Empty export test (Review Focus 3)
    empty_export = await export_library_json(db, user_id)
    assert empty_export == []
    empty_html = await export_library_html(db, user_id)
    assert "<!DOCTYPE NETSCAPE-Bookmark-file-1>" in empty_html
    assert "<DT><A" not in empty_html

    # Save item with tags and note
    item, _ = await save_item(db, None, user_id, "https://example.com/article", ["news", "tech"])
    await update_user_notes(db, user_id, item["id"], "My custom note")
    await toggle_pin_item(db, user_id, item["id"])

    # Save a quick note
    note = await save_note(db, None, user_id, "Quick Note", "Some note text", ["ideas"])

    exported = await export_library_json(db, user_id)
    assert len(exported) == 2
    by_id = {it["id"]: it for it in exported}

    # Verify migration 0006 metadata retained
    exp_item = by_id[item["id"]]
    assert set(exp_item["tags"]) == {"news", "tech"}
    assert exp_item["user_notes"] == "My custom note"
    assert exp_item["is_pinned"] == 1
    assert exp_item["read_state"] == "unread"
    assert "image_url" in exp_item
    assert "summary" in exp_item
    assert exp_item["item_type"] == "url"

    exp_note = by_id[note["id"]]
    assert exp_note["item_type"] == "note"
    assert set(exp_note["tags"]) == {"ideas"}


@pytest.mark.asyncio
async def test_export_library_html_escapes_entities(db):
    user = await register_user(db, "html_esc@test.local", "password123")
    user_id = user["id"]

    # Insert item with malicious / special characters in title and url
    dangerous_url = "https://example.com/test?a=1&b=2\"<script>alert(1)</script>"
    item, _ = await save_item(db, None, user_id, dangerous_url, ["tag&1", "tag\"2"])
    await db.execute(
        "UPDATE items SET title = ? WHERE id = ?;",
        ("Dangerous <Title> & \"Quotes\"", item["id"]),
    )

    exported_html = await export_library_html(db, user_id)
    assert "<script>" not in exported_html
    assert "&lt;script&gt;" in exported_html or "%3Cscript%3E" in exported_html
    assert "Dangerous &lt;Title&gt; &amp; &quot;Quotes&quot;" in exported_html
    assert 'TAGS="tag&amp;1,tag&quot;2"' in exported_html or 'TAGS="tag&amp;1,tag&quot;2"' in html.unescape(exported_html)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_importer.py::test_export_library_json_includes_metadata_and_batches_tags tests/test_importer.py::test_export_library_html_escapes_entities -v`
Expected: FAIL with `KeyError: 'user_notes'` or unescaped HTML tag assertion.

- [ ] **Step 3: Implement metadata retention, tag batching, and HTML escaping in `keepfor/utils/importer.py`**

In [`keepfor/utils/importer.py`](file:///home/melcutz/work/keepfor.me/keepfor/utils/importer.py#L82-L131):
1. Import `html`.
2. Update `export_library_json` query:
   ```python
   async def export_library_json(db: Database, user_id: str) -> list[dict[str, Any]]:
       """Exports entire user library as structured JSON."""
       rows = await db.query_all(
           """
           SELECT id, url, canonical_url, title, byline, site_name,
                  published_date, excerpt, status, word_count, created_at,
                  user_notes, item_type, is_pinned, image_url, summary, read_state
           FROM items
           WHERE user_id = ?
           ORDER BY created_at DESC;
           """,
           (user_id,),
       )
       if not rows:
           return []

       all_tags = await db.query_all(
           """
           SELECT it.item_id, t.name
           FROM item_tags it
           JOIN tags t ON it.tag_id = t.id
           WHERE t.user_id = ?;
           """,
           (user_id,),
       )
       tags_by_item: dict[str, list[str]] = {}
       for tr in all_tags:
           tags_by_item.setdefault(tr["item_id"], []).append(tr["name"])

       items = []
       for r in rows:
           item = dict(r)
           item["tags"] = tags_by_item.get(r["id"], [])
           items.append(item)
       return items
   ```
3. Update `export_library_html`:
   ```python
   async def export_library_html(db: Database, user_id: str) -> str:
       """Exports library as standard Netscape Bookmark HTML."""
       items = await export_library_json(db, user_id)
       html_lines = [
           "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
           "<!-- This is an automatically generated file. -->",
           '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
           "<TITLE>Keepfor.me Library Export</TITLE>",
           "<H1>Bookmarks</H1>",
           "<DL><p>",
       ]
       for it in items:
           tags_str = ",".join(it["tags"])
           epoch = int(time.time())
           title = it.get("title") or it["url"]
           safe_url = html.escape(it["url"], quote=True)
           safe_tags = html.escape(tags_str, quote=True)
           safe_title = html.escape(title)
           html_lines.append(
               f'    <DT><A HREF="{safe_url}" ADD_DATE="{epoch}" TAGS="{safe_tags}">{safe_title}</A>'
           )
       html_lines.append("</DL><p>")
       return "\n".join(html_lines)
   ```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_importer.py -v`
Expected: PASS (all 5 tests pass).

- [ ] **Step 5: Run linter and formatter**

Run: `ruff check keepfor/utils/importer.py tests/test_importer.py --select=E,W,F,I,N && ruff format --check keepfor/utils/importer.py tests/test_importer.py`
Expected: All checks passed.

- [ ] **Step 6: Commit**

```bash
git add keepfor/utils/importer.py tests/test_importer.py
git commit -m "fix(importer): batch tags, retain 0006 metadata in json export, and escape netscape html"
```

---

### Task 4: Browser Extension Popup Settings and Title Payload (Findings 4.1, 4.2)

**Files:**
- Modify: [`browser-extension/popup.html:197`](file:///home/melcutz/work/keepfor.me/browser-extension/popup.html#L197), [`browser-extension/popup.js:90-95`](file:///home/melcutz/work/keepfor.me/browser-extension/popup.js#L90-L95)
- Test: [`tests/test_browser_extension.py`](file:///home/melcutz/work/keepfor.me/tests/test_browser_extension.py)

**Interfaces:**
- Consumes: [`browser-extension/popup.html`](file:///home/melcutz/work/keepfor.me/browser-extension/popup.html), [`browser-extension/popup.js`](file:///home/melcutz/work/keepfor.me/browser-extension/popup.js)
- Produces: Valid placeholder `https://app.keepfor.me`, `title` included in save payload

- [ ] **Step 1: Write the failing tests**

In [`tests/test_browser_extension.py`](file:///home/melcutz/work/keepfor.me/tests/test_browser_extension.py), add checks in `test_popup_html_has_required_ids` and `test_popup_js_save_contract`:

```python
def test_popup_html_settings_placeholder():
    html = POPUP_HTML.read_text()
    assert 'placeholder="https://app.keepfor.me"' in html
    assert 'placeholder="https://keepfor.me"' not in html


def test_popup_js_includes_title_in_payload():
    js = POPUP_JS.read_text()
    # Must read titleInput and include in payload
    assert "titleInput" in js
    assert "title" in js
    assert re.search(r"title:\s*(title|titleInput\.value)", js)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_browser_extension.py::test_popup_html_settings_placeholder tests/test_browser_extension.py::test_popup_js_includes_title_in_payload -v`
Expected: FAIL.

- [ ] **Step 3: Update `popup.html` and `popup.js`**

1. In [`browser-extension/popup.html:197`](file:///home/melcutz/work/keepfor.me/browser-extension/popup.html#L197):
   ```html
   <input type="text" id="worker-url" placeholder="https://app.keepfor.me" />
   ```
2. In [`browser-extension/popup.js`](file:///home/melcutz/work/keepfor.me/browser-extension/popup.js#L90-L95):
   ```javascript
   const tags = tagsInput.value.split(",").map(t => t.trim()).filter(Boolean);
   const title = titleInput.value.trim();
   const payload = {
     url: urlInput.value.trim(),
     title: title || undefined,
     tags: tags
   };
   ```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_browser_extension.py -k "test_popup_" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add browser-extension/popup.html browser-extension/popup.js tests/test_browser_extension.py
git commit -m "fix(extension): update worker url placeholder to app.keepfor.me and include title in save payload"
```

---

### Task 5: Repo Hygiene, Git Untracking, and Dependency Trimming (Findings 5.1, 5.2, 6.1, 6.2, 6.3)

**Files:**
- Modify: [`.gitignore`](file:///home/melcutz/work/keepfor.me/.gitignore), [`pyproject.toml`](file:///home/melcutz/work/keepfor.me/pyproject.toml), [`wrangler.jsonc`](file:///home/melcutz/work/keepfor.me/wrangler.jsonc)
- Delete: [`keepfor/config.py`](file:///home/melcutz/work/keepfor.me/keepfor/config.py)
- Untrack: `.wrangler/`

**Interfaces:**
- Consumes: Git repository tracking, `pyproject.toml` dependencies, `wrangler.jsonc` vars
- Produces: Clean git index without `.wrangler/`, `.gitignore` covering `.venv/` and `venv/`, trimmed `pyproject.toml` without `pydantic-settings` or `types-pydantic`, removed `SESSION_SECRET`

- [ ] **Step 1: Write test or verification check**

Verify via commands that:
1. `git ls-files .wrangler/` returns 0 files.
2. `keepfor/config.py` does not exist and no code imports `pydantic_settings`.
3. `wrangler.jsonc` does not define `SESSION_SECRET`.
4. `.venv/` and `venv/` are present in `.gitignore`.

- [ ] **Step 2: Untrack `.wrangler/` from git**

Run:
```bash
git rm -r --cached .wrangler/
```

- [ ] **Step 3: Update `.gitignore`**

In [`.gitignore`](file:///home/melcutz/work/keepfor.me/.gitignore):
Add `.venv/` and `venv/`:
```gitignore
# Python
__pycache__/
*.py[cod]
*$py.class
.venv/
venv/
.venv-workers/
python_modules/
pylock.toml
```

- [ ] **Step 4: Delete `keepfor/config.py` and trim dependencies in `pyproject.toml`**

1. Delete [`keepfor/config.py`](file:///home/melcutz/work/keepfor.me/keepfor/config.py):
   `rm keepfor/config.py`
2. In [`pyproject.toml`](file:///home/melcutz/work/keepfor.me/pyproject.toml):
   Remove `"pydantic-settings>=2.0.0",` from `dependencies`.
   Remove `"types-pydantic>=0.1.1"` from `project.optional-dependencies.dev`.

- [ ] **Step 5: Remove `SESSION_SECRET` from `wrangler.jsonc`**

In [`wrangler.jsonc:94`](file:///home/melcutz/work/keepfor.me/wrangler.jsonc#L94), delete the `"SESSION_SECRET": "...",` entry under `vars`.

- [ ] **Step 6: Verify lint and tests pass without dead config**

Run:
`ruff check keepfor/ tests/ --select=E,W,F,I,N && ruff format --check keepfor/ tests/`
`python3 -m pytest tests/ -q`
Expected: All tests pass, lint passes cleanly.

- [ ] **Step 7: Commit**

```bash
git add .gitignore pyproject.toml wrangler.jsonc
git rm keepfor/config.py
git commit -m "chore(repo): untrack miniflare state, ignore .venv, remove dead config.py, pydantic-settings, and SESSION_SECRET"
```

---

### Task 6: Modernization & Deprecation Elimination (Findings 7.1, 7.2)

**Files:**
- Modify: [`keepfor/utils/logging.py:39`](file:///home/melcutz/work/keepfor.me/keepfor/utils/logging.py#L39), [`keepfor/app.py:1365`](file:///home/melcutz/work/keepfor.me/keepfor/app.py#L1365), [`keepfor/schemas.py:19, 40`](file:///home/melcutz/work/keepfor.me/keepfor/schemas.py#L19)
- Test: [`tests/test_endpoints.py`](file:///home/melcutz/work/keepfor.me/tests/test_endpoints.py)

**Interfaces:**
- Consumes: Python 3.12+ `datetime.now(timezone.utc)`, Pydantic V2 `ConfigDict`
- Produces: 0 deprecation warnings from `datetime.utcnow()` and Pydantic V1 `class Config`

- [ ] **Step 1: Verify current warning count**

Run: `python3 -m pytest tests/test_auto_tag.py -W error::DeprecationWarning`
Expected: FAILS with `DeprecationWarning: datetime.datetime.utcnow() is deprecated`.

- [ ] **Step 2: Replace `datetime.utcnow()` in `keepfor/utils/logging.py`**

In [`keepfor/utils/logging.py`](file:///home/melcutz/work/keepfor.me/keepfor/utils/logging.py#L3):
Import `timezone`:
```python
from datetime import datetime, timezone
```
In [`JSONFormatter.format`](file:///home/melcutz/work/keepfor.me/keepfor/utils/logging.py#L39):
```python
"timestamp": datetime.now(timezone.utc).isoformat(),
```

- [ ] **Step 3: Replace `datetime.utcnow()` in `keepfor/app.py`**

In [`keepfor/app.py:1364-1366`](file:///home/melcutz/work/keepfor.me/keepfor/app.py#L1364-L1366):
```python
cutoff = (
    datetime.datetime.now(datetime.timezone.utc)
    - datetime.timedelta(minutes=REQUEUE_STUCK_MINUTES)
).strftime("%Y-%m-%d %H:%M:%S")
```

- [ ] **Step 4: Replace deprecated `class Config:` in `keepfor/schemas.py`**

In [`keepfor/schemas.py`](file:///home/melcutz/work/keepfor.me/keepfor/schemas.py#L6):
Import `ConfigDict`:
```python
from pydantic import BaseModel, ConfigDict, Field
```
Replace `class Config:` in `SaveItemRequest`:
```python
class SaveItemRequest(BaseModel):
    """Request to save a URL to the library."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "url": "https://example.com/article",
                "tags": ["tech", "read-later"],
            }
        }
    )

    url: str = Field(..., description="The webpage or article URL to save")
    tags: List[str] = Field(default_factory=list, description="Optional list of tags")
    title: Optional[str] = Field(default=None, description="Optional custom title")
```
Replace `class Config:` in `SearchRequest`:
```python
class SearchRequest(BaseModel):
    """Request to search the library."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"query": "machine learning", "mode": "hybrid", "limit": 10}
        }
    )

    query: str = Field(
        ..., description="Search query terms or semantic question", min_length=1
    )
    mode: Literal["hybrid", "keyword", "semantic"] = Field(
        default="hybrid", description="Search mode"
    )
    tag: Optional[str] = Field(default=None, description="Filter by tag")
    limit: int = Field(default=30, ge=1, le=100, description="Max results (1-100)")
```

- [ ] **Step 5: Run pytest and check warning count**

Run: `python3 -m pytest tests/test_auto_tag.py -q`
Expected: 0 `utcnow` deprecation warnings and 0 `PydanticDeprecatedSince20` warnings.

- [ ] **Step 6: Run lint and commit**

Run: `ruff check keepfor/ tests/ --select=E,W,F,I,N && ruff format --check keepfor/ tests/`
```bash
git add keepfor/utils/logging.py keepfor/app.py keepfor/schemas.py
git commit -m "refactor: replace deprecated datetime.utcnow() and modernize Pydantic schemas to ConfigDict"
```

---

### Task 7: Vector Search and Resource Deletion Error Logging (Findings 8.1, 8.2)

**Files:**
- Modify: [`keepfor/search/engine.py:236-237`](file:///home/melcutz/work/keepfor.me/keepfor/search/engine.py#L236-L237), [`keepfor/models/items.py:224-236`](file:///home/melcutz/work/keepfor.me/keepfor/models/items.py#L224-L236)
- Test: [`tests/test_items_and_search.py`](file:///home/melcutz/work/keepfor.me/tests/test_items_and_search.py)

**Interfaces:**
- Consumes: [`logger`](file:///home/melcutz/work/keepfor.me/keepfor/utils/logging.py#L86) in `keepfor/utils/logging.py`
- Produces: Warning log records on `search_vectorize` failure, Vectorize deletion failure, and R2 deletion failure

- [ ] **Step 1: Write the failing tests**

In [`tests/test_items_and_search.py`](file:///home/melcutz/work/keepfor.me/tests/test_items_and_search.py), add:

```python
import logging
from unittest.mock import AsyncMock, MagicMock
from keepfor.search.engine import search_vectorize


@pytest.mark.asyncio
async def test_search_vectorize_logs_warning_on_failure(caplog):
    mock_env = MagicMock()
    mock_env.AI.run = AsyncMock(side_effect=RuntimeError("AI binding timeout"))
    mock_env.VECTORIZE = MagicMock()

    with caplog.at_level(logging.WARNING):
        res = await search_vectorize(mock_env, "user-123", "test query")
        assert res == []
        assert any("Vector search query failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_delete_item_logs_warning_on_vectorize_and_r2_failure(user_with_env, caplog):
    user, env, db = user_with_env
    item, _ = await save_item(db, env, user["id"], "https://example.com/delete-fail")

    # Add chunk so vectorize delete is triggered
    await db.execute(
        "INSERT INTO chunks (id, item_id, user_id) VALUES (?, ?, ?);",
        ("chunk-1", item["id"], user["id"]),
    )

    env.VECTORIZE = MagicMock()
    env.VECTORIZE.deleteByIds = AsyncMock(side_effect=RuntimeError("Vectorize error"))
    env.BUCKET = MagicMock()
    env.BUCKET.delete = AsyncMock(side_effect=RuntimeError("R2 error"))

    with caplog.at_level(logging.WARNING):
        deleted = await delete_item(db, env, user["id"], item["id"])
        assert deleted is True
        messages = [r.message for r in caplog.records]
        assert any("Failed to delete vector embeddings" in m for m in messages)
        assert any("Failed to delete R2 snapshots" in m for m in messages)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_items_and_search.py::test_search_vectorize_logs_warning_on_failure tests/test_items_and_search.py::test_delete_item_logs_warning_on_vectorize_and_r2_failure -v`
Expected: FAIL (no warning log emitted).

- [ ] **Step 3: Add error logging to `search_vectorize` and `delete_item`**

1. In [`keepfor/search/engine.py`](file:///home/melcutz/work/keepfor.me/keepfor/search/engine.py):
   Import logger:
   ```python
   from keepfor.utils.logging import logger
   ```
   In `search_vectorize`:
   ```python
       except Exception as exc:
           logger.warning("Vector search query failed: %s", exc)
           return []
   ```
2. In [`keepfor/models/items.py`](file:///home/melcutz/work/keepfor.me/keepfor/models/items.py#L224-L236):
   ```python
       if chunk_ids and hasattr(env, "VECTORIZE") and env.VECTORIZE is not None:
           try:
               await env.VECTORIZE.deleteByIds(chunk_ids)
           except Exception as exc:
               logger.warning("Failed to delete vector embeddings for item %s: %s", item_id, exc)

       # 2. Delete R2 snapshots
       if hasattr(env, "BUCKET") and env.BUCKET is not None:
           try:
               await env.BUCKET.delete(f"items/{item_id}/raw.html")
               await env.BUCKET.delete(f"items/{item_id}/clean.html")
           except Exception as exc:
               logger.warning("Failed to delete R2 snapshots for item %s: %s", item_id, exc)
   ```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_items_and_search.py::test_search_vectorize_logs_warning_on_failure tests/test_items_and_search.py::test_delete_item_logs_warning_on_vectorize_and_r2_failure -v`
Expected: PASS.

- [ ] **Step 5: Run full items and search tests & lint**

Run: `python3 -m pytest tests/test_items_and_search.py -q && ruff check keepfor/search/engine.py keepfor/models/items.py --select=E,W,F,I,N && ruff format --check keepfor/search/engine.py keepfor/models/items.py`
Expected: 18 passed, lint checks clean.

- [ ] **Step 6: Commit**

```bash
git add keepfor/search/engine.py keepfor/models/items.py tests/test_items_and_search.py
git commit -m "fix(observability): log warnings when vector search or vectorize/r2 deletions fail"
```

---

### Task 8: Full Verification, Clean Test Suite & Bundle Gate Check

**Files:**
- All affected files

- [ ] **Step 1: Run complete test suite**

Run: `python3 -m pytest tests/ -q`
Expected: 300+ passed, 0 failures, ~0 deprecation warnings from codebase.

- [ ] **Step 2: Run CI lint checks**

Run:
```bash
ruff check keepfor/ tests/ --select=E,W,F,I,N
ruff format --check keepfor/ tests/
```
Expected: All checks passed.

- [ ] **Step 3: Run pywrangler deploy dry-run to verify bundle size**

Run: `uvx --from workers-py pywrangler deploy --dry-run`
Expected: Total modules <= 4,500 and total size comfortably below the 58,000 KiB gate.
