# Tags Redesign (Big-Bang) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `/tags` table with inline pill-row management and ship multi-tag filtering, smart in-flow tagging, and tagging rules in one big-bang change.

**Architecture:** Model functions first (TDD, `db` fixture, no network), then endpoints reusing them, then templates. Engine keeps backward-compatible `tag:` param and gains `tags:`/`untagged:` params. One new migration (`0004_tag_rules.sql`, auto-applied by `tests/conftest.py`).

**Tech Stack:** FastAPI + Jinja + htmx (existing), D1/SQLite via `keepfor/models/db.py`, stdlib only (`urllib.parse`, `collections`), `pytest` with `TestClient` + `client`/`auth_headers` fixtures copied from `tests/test_tags_page.py`.

---

## File map

- Create: `migrations/0004_tag_rules.sql`
- Create: `tests/test_tags_manager.py`, `tests/test_tag_filter.py`, `tests/test_tag_suggest_bulk.py`, `tests/test_tag_rules.py`
- Create: `templates/partials/tag_list.html`
- Modify: `keepfor/models/items.py` (merge_tags, prune_unused_tags, suggest_tags, bulk_update_tags, rules CRUD, match_rules, suggest_rules)
- Modify: `keepfor/search/engine.py` (UNTAGGED_SENTINEL, parse_tag_filter, multi-tag + untagged in get_recent_items/count_recent_items/hybrid_search)
- Modify: `keepfor/app.py` (POST /tags/merge, POST /tags/prune, htmx branches on rename/delete, GET /tags/suggest, POST /items/bulk-tags, rules endpoints, library/search wiring, save-popup recents context)
- Modify: `keepfor/consumer/processor.py` (rules hook inside existing fail-open try)
- Modify: `templates/tags.html`, `templates/library.html`, `templates/partials/item_card.html`, `templates/save_popup.html`
- Deviation from spec S3 (documented): extension popup keeps free-text input; autocomplete ships on web surfaces only (extension fetch runs in a different auth context than the session-cookie `/tags/suggest` endpoint).

---

### Task 1: Migration 0004 (tag_rules + dismissal memory)

**Files:**
- Create: `migrations/0004_tag_rules.sql`
- Test: `tests/test_tag_rules.py` (migration smoke test only; rules tests come in Task 10)

- [ ] **Step 1: Write the migration file**

`conftest.py` strips `--` comment lines then splits on `;`, so the file must contain no `;` inside comments or string literals.

```sql
CREATE TABLE IF NOT EXISTS tag_rules (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    field TEXT NOT NULL,
    substr TEXT NOT NULL,
    tag TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, field, substr, tag)
);

CREATE INDEX IF NOT EXISTS idx_rules_user ON tag_rules(user_id);

CREATE TABLE IF NOT EXISTS rule_suggestion_dismissals (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(user_id, key)
);
```

- [ ] **Step 2: Write the failing-then-passing smoke test**

```python
"""Rules tables exist and enforce uniqueness (migration 0004)."""

import uuid

import pytest

from keepfor.auth.service import register_user


@pytest.mark.asyncio
async def test_tag_rules_tables_exist(db):
    user = await register_user(db, "rules@keepfor.me", "password123")
    rid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO tag_rules (id, user_id, field, substr, tag)"
        " VALUES (?, ?, ?, ?, ?);",
        (rid, user["id"], "domain", "arxiv.org", "research"),
    )
    row = await db.query_first(
        "SELECT field, substr, tag FROM tag_rules WHERE id = ?;", (rid,)
    )
    assert (row["field"], row["substr"], row["tag"]) == (
        "domain",
        "arxiv.org",
        "research",
    )
```

- [ ] **Step 3: Run it**

Run: `python3 -m pytest tests/test_tag_rules.py -q`
Expected: PASS (conftest auto-applies every `migrations/*.sql`)

- [ ] **Step 4: Commit**

```bash
git add migrations/0004_tag_rules.sql tests/test_tag_rules.py
git commit -m "feat(tags): migration 0004 tag_rules tables"
```

---

### Task 2: merge_tags + prune_unused_tags + sentinel pin

**Files:**
- Modify: `keepfor/models/items.py` (append after `delete_tag`, before `add_suggestions`)
- Test: `tests/test_tags_manager.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Model tests for tag merge, prune, and the untagged sentinel."""

import pytest

from keepfor.auth.service import register_user
from keepfor.models.items import (
    add_tags_to_item,
    create_tag,
    list_user_tags,
    merge_tags,
    prune_unused_tags,
)


async def _seed(db, user_id, item_id, url, tags):
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status)"
        " VALUES (?, ?, ?, ?, 'ok');",
        (item_id, user_id, url, url),
    )
    if tags:
        await add_tags_to_item(db, user_id, item_id, tags)


@pytest.mark.asyncio
async def test_merge_two_tags_moves_item_links(db):
    user = await register_user(db, "merge@keepfor.me", "password123")
    await _seed(db, user["id"], "m1", "https://example.com/1", ["ai"])
    await _seed(db, user["id"], "m2", "https://example.com/2", ["ml"])
    result = await merge_tags(db, user["id"], ["ai", "ml"], "tech")
    assert result == "merged"
    names = {t["name"]: t["count"] for t in await list_user_tags(db, user["id"])}
    assert names == {"tech": 2}


@pytest.mark.asyncio
async def test_merge_invalid_new_name_rejected(db):
    user = await register_user(db, "merge2@keepfor.me", "password123")
    await _seed(db, user["id"], "m1", "https://example.com/1", ["ai"])
    assert await merge_tags(db, user["id"], ["ai"], "bad/name") == "invalid"
    names = [t["name"] for t in await list_user_tags(db, user["id"])]
    assert names == ["ai"]


@pytest.mark.asyncio
async def test_prune_unused_deletes_only_zero_count(db):
    user = await register_user(db, "prune@keepfor.me", "password123")
    await create_tag(db, user["id"], "empty")
    await _seed(db, user["id"], "m1", "https://example.com/1", ["used"])
    assert await prune_unused_tags(db, user["id"]) == 1
    names = [t["name"] for t in await list_user_tags(db, user["id"])]
    assert names == ["used"]


@pytest.mark.asyncio
async def test_untagged_sentinel_cannot_be_created(db):
    user = await register_user(db, "sent@keepfor.me", "password123")
    assert await create_tag(db, user["id"], "__untagged__") is None
    assert await list_user_tags(db, user["id"]) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_tags_manager.py -q`
Expected: FAIL with ImportError/AttributeError on `merge_tags`

- [ ] **Step 3: Implement (append after `delete_tag` in `keepfor/models/items.py`)**

```python
async def merge_tags(
    db: Database, user_id: str, old_names: list[str], new_name: str
) -> str:
    """Merges several tags into one via rename-onto-existing.

    Returns 'merged', 'unchanged', 'not_found' or 'invalid'.
    """
    from keepfor.utils.tagger import validate_tag_name

    if not validate_tag_name(new_name or ""):
        return "invalid"
    seen = "not_found"
    for raw in old_names or []:
        res = await rename_tag(db, user_id, raw, new_name)
        if res in ("merged", "unchanged"):
            seen = "merged"
    return seen


async def prune_unused_tags(db: Database, user_id: str) -> int:
    """Deletes zero-item tags for the user; returns the deleted count."""
    rows = await db.query_all(
        """
        SELECT t.id
        FROM tags t
        LEFT JOIN item_tags it ON t.id = it.tag_id
        WHERE t.user_id = ? AND it.item_id IS NULL;
        """,
        (user_id,),
    )
    for row in rows:
        await db.execute("DELETE FROM tags WHERE id = ?;", (row["id"],))
    return len(rows)
```

Note: `__untagged__` needs no code — `validate_tag_name` rejects a leading
underscore, so `create_tag` already returns `None`; the test pins it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_tags_manager.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add keepfor/models/items.py tests/test_tags_manager.py
git commit -m "feat(tags): merge_tags and prune_unused_tags models"
```

---

### Task 3: POST /tags/merge, POST /tags/prune, htmx branches on rename/delete

**Files:**
- Modify: `keepfor/app.py` (tags endpoints near `/tags/rename`)
- Test: append to `tests/test_tags_page.py` (reuse its `client`, `auth_headers`, `_login`, `_seed_item`)

- [ ] **Step 1: Write the failing endpoint tests** (append to `tests/test_tags_page.py`)

```python
@pytest.mark.asyncio
async def test_merge_tags_via_form(client, db, auth_headers):
    _login(client, auth_headers)
    user_id = auth_headers["admin_user"]["id"]
    await _seed_item(db, user_id, "https://a.example/", ["ai"])
    await _seed_item(db, user_id, "https://b.example/", ["ml"])
    response = client.post(
        "/tags/merge",
        data={"old_names": "ai, ml", "new_name": "tech"},
        follow_redirects=False,
    )
    assert response.status_code in (200, 303, 307, 308)
    from keepfor.models.items import get_item_tags, list_user_tags

    names = {t["name"]: t["count"] for t in await list_user_tags(db, user_id)}
    assert names == {"tech": 2}


@pytest.mark.asyncio
async def test_prune_tags_via_form(client, db, auth_headers):
    _login(client, auth_headers)
    user_id = auth_headers["admin_user"]["id"]
    await _seed_item(db, user_id, "https://a.example/", ["used"])
    from keepfor.models.items import create_tag

    await create_tag(db, user_id, "empty")
    response = client.post("/tags/prune", follow_redirects=False)
    assert response.status_code in (200, 303, 307, 308)
    from keepfor.models.items import list_user_tags

    assert [t["name"] for t in await list_user_tags(db, user_id)] == ["used"]


@pytest.mark.asyncio
async def test_rename_returns_fragment_for_htmx(client, db, auth_headers):
    _login(client, auth_headers)
    user_id = auth_headers["admin_user"]["id"]
    await _seed_item(db, user_id, "https://a.example/", ["oldname"])
    response = client.post(
        "/tags/rename",
        data={"old_name": "oldname", "new_name": "newname"},
        headers={"hx-request": "true"},
    )
    assert response.status_code == 200
    assert "newname" in response.text
    assert "<table" not in response.text
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_tags_page.py -q`
Expected: FAIL (404 on `/tags/merge`, `/tags/prune`; rename returns 303 not 200)

- [ ] **Step 3: Implement in `keepfor/app.py`**

Add a fragment helper next to `tags_page` (uses already-imported `list_user_tags` and `tag_styles_for`):

```python
def _tags_list_html(db_tags: list[dict]) -> str:
    tag_styles = tag_styles_for([t["name"] for t in db_tags])
    template = jinja_env.get_template("partials/tag_list.html")
    return template.render(tags=db_tags, tag_styles=tag_styles)


async def _render_tags_list(db, user_id: str) -> str:
    tags = await list_user_tags(db, user_id)
    return _tags_list_html(tags)
```

Branch the rename/delete endpoints on the htmx header (non-htmx keeps the 303):

```python
@app.post("/tags/rename")
async def tags_rename(
    request: Request, old_name: str = Form(""), new_name: str = Form("")
):
    user = await require_user(request)
    db = get_db(request)
    await rename_tag(db, user["id"], old_name, new_name)
    if request.headers.get("hx-request"):
        return HTMLResponse(content=await _render_tags_list(db, user["id"]))
    return RedirectResponse(url="/tags", status_code=303)
```

Apply the identical `hx-request` branch to `tags_delete`. Add the two new endpoints (`merge_tags` and `prune_unused_tags` are imported from `keepfor.models.items` alongside the existing tag imports):

```python
@app.post("/tags/merge")
async def tags_merge(
    request: Request, old_names: str = Form(""), new_name: str = Form("")
):
    user = await require_user(request)
    db = get_db(request)
    names = [n.strip() for n in old_names.split(",") if n.strip()]
    await merge_tags(db, user["id"], names, new_name)
    if request.headers.get("hx-request"):
        return HTMLResponse(content=await _render_tags_list(db, user["id"]))
    return RedirectResponse(url="/tags", status_code=303)


@app.post("/tags/prune")
async def tags_prune(request: Request):
    user = await require_user(request)
    db = get_db(request)
    await prune_unused_tags(db, user["id"])
    if request.headers.get("hx-request"):
        return HTMLResponse(content=await _render_tags_list(db, user["id"]))
    return RedirectResponse(url="/tags", status_code=303)
```

- [ ] **Step 4: Run to verify**

Run: `python3 -m pytest tests/test_tags_page.py tests/test_tags_manager.py -q`
Expected: all pass (the fragment test needs `partials/tag_list.html` from Task 4 — if it 500s here, continue to Task 4, then re-run)

- [ ] **Step 5: Commit**

```bash
git add keepfor/app.py tests/test_tags_page.py
git commit -m "feat(tags): merge/prune endpoints, htmx fragments on rename/delete"
```

---

### Task 4: tags.html rewrite (pill-rows, search/sort/merge UI, unused group)

**Files:**
- Create: `templates/partials/tag_list.html`
- Modify: `templates/tags.html`

- [ ] **Step 1: Write the partial** `templates/partials/tag_list.html`

```html
<div id="tag-list" class="divide-y divide-slate-100">
  {% for t in tags %}
  {% set ts = tag_styles.get(t.name, ('bg-slate-100 text-slate-600', 'bg-slate-400')) %}
  <div class="tag-row flex items-center gap-2.5 px-3 py-2.5" data-name="{{ t.name }}" data-count="{{ t.count }}">
    <input type="checkbox" value="{{ t.name }}" class="tag-select w-4 h-4 accent-slate-900 shrink-0" aria-label="Select tag {{ t.name }}">
    <span class="w-2 h-2 rounded-full {{ ts[1] }} shrink-0"></span>
    <a href="/?tag={{ t.name }}" class="font-medium text-slate-800 hover:underline truncate">#{{ t.name }}</a>
    <a href="/?tag={{ t.name }}" class="text-xs text-slate-400 hover:underline shrink-0">{{ t.count }}</a>
    <button type="button" class="tag-rename-btn px-2 py-1 text-xs text-slate-500 hover:text-slate-800 hover:bg-slate-100 rounded-md shrink-0" data-rename="{{ t.name }}">Rename</button>
    <form class="tag-rename-form hidden items-center gap-1.5" hx-post="/tags/rename" hx-target="#tag-list" hx-swap="outerHTML">
      <input type="hidden" name="old_name" value="{{ t.name }}">
      <input type="text" name="new_name" value="{{ t.name }}" required aria-label="New name for {{ t.name }}"
             class="w-32 px-2 py-1 border border-slate-300 rounded-md text-xs focus:outline-none focus:ring-2 focus:ring-slate-900">
      <button type="submit" class="px-2 py-1 bg-slate-900 text-white rounded-md text-xs font-medium">Save</button>
    </form>
    <form class="ml-auto shrink-0" hx-post="/tags/delete" hx-target="#tag-list" hx-swap="outerHTML"
          hx-confirm="Delete tag #{{ t.name }}? Items stay in your library, untagged.">
      <input type="hidden" name="name" value="{{ t.name }}">
      <button type="submit" aria-label="Delete tag {{ t.name }}"
              class="w-8 h-8 text-lg leading-none text-slate-300 hover:text-red-600 hover:bg-red-50 rounded-full">&times;</button>
    </form>
  </div>
  {% endfor %}
</div>
```

- [ ] **Step 2: Rewrite the All-tags section of `templates/tags.html`**

Replace the `<table>` block with: search input + sort select + merge bar + the partial + unused group + prune-all. Keep the New-tag and Suggestions sections untouched.

```html
<section class="bg-white border border-slate-200 rounded-xl p-6 shadow-sm">
  <h3 class="text-base font-semibold text-slate-900 mb-4">All tags ({{ tags|length }})</h3>
  {% if tags %}
  <div class="flex flex-wrap items-center gap-2 mb-3">
    <input type="search" id="tag-search" placeholder="Filter tags…"
           class="flex-1 min-w-40 px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-900">
    <select id="tag-sort" aria-label="Sort tags"
            class="px-3 py-2 border border-slate-300 rounded-lg text-sm bg-white">
      <option value="count">Most used</option>
      <option value="name">Name A–Z</option>
    </select>
  </div>
  <div id="merge-bar" class="hidden flex-wrap items-center gap-2 mb-3 px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg">
    <span id="merge-count" class="text-xs text-slate-600"></span>
    <span class="text-xs text-slate-500">into</span>
    <input type="text" id="merge-target" placeholder="target name"
           class="w-32 px-2 py-1 border border-slate-300 rounded-md text-xs">
    <button type="button" id="merge-go"
            class="px-2.5 py-1 bg-slate-900 text-white rounded-md text-xs font-medium">Merge</button>
  </div>
  <div class="border border-slate-200 rounded-lg overflow-hidden">
    {% include "partials/tag_list.html" %}
  </div>
  {% set unused = tags|selectattr('count', 'equalto', 0)|list %}
  {% if unused %}
  <div class="mt-4 flex items-center gap-2 text-xs text-slate-500">
    <span>{{ unused|length }} unused tag{{ 's' if unused|length != 1 }}: {{ unused|map(attribute='name')|join(', ') }}</span>
    <form hx-post="/tags/prune" hx-target="#tag-list" hx-swap="outerHTML" class="inline">
      <button type="submit" class="px-2.5 py-1 border border-slate-200 rounded-md font-medium hover:bg-slate-50">Prune all unused</button>
    </form>
  </div>
  {% endif %}
  {% else %}
  <p class="text-xs text-slate-500">No tags yet. Create one above, or add tags when saving a link.</p>
  {% endif %}
</section>
<script>
(function () {
  var list = document.getElementById('tag-list');
  if (!list) return;
  // Rename toggle (delegated so htmx swaps keep working).
  list.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-rename]');
    if (!btn) return;
    var row = btn.closest('[data-name]');
    var form = row.querySelector('.tag-rename-form');
    form.classList.toggle('hidden');
    form.classList.toggle('flex');
    btn.classList.toggle('hidden');
    var input = form.querySelector('input[name="new_name"]');
    if (input) { input.focus(); input.select(); }
  });
  // Client-side search filter.
  var search = document.getElementById('tag-search');
  if (search) search.addEventListener('input', function () {
    var q = search.value.trim().toLowerCase();
    list.querySelectorAll('[data-name]').forEach(function (row) {
      row.style.display = row.getAttribute('data-name').includes(q) ? '' : 'none';
    });
  });
  // Client-side sort.
  var sort = document.getElementById('tag-sort');
  if (sort) sort.addEventListener('change', function () {
    var rows = Array.from(list.querySelectorAll('[data-name]'));
    rows.sort(function (a, b) {
      if (sort.value === 'name') return a.getAttribute('data-name').localeCompare(b.getAttribute('data-name'));
      return (+b.getAttribute('data-count')) - (+a.getAttribute('data-count'));
    });
    rows.forEach(function (r) { list.appendChild(r); });
  });
  // Merge bar visibility + submit.
  var bar = document.getElementById('merge-bar');
  var count = document.getElementById('merge-count');
  var target = document.getElementById('merge-target');
  function selected() { return Array.from(list.querySelectorAll('.tag-select:checked')).map(function (c) { return c.value; }); }
  list.addEventListener('change', function () {
    var sel = selected();
    bar.classList.toggle('hidden', sel.length < 2);
    bar.classList.toggle('flex', sel.length >= 2);
    if (count) count.textContent = 'Merge ' + sel.length + ' tags';
  });
  var go = document.getElementById('merge-go');
  if (go) go.addEventListener('click', function () {
    var sel = selected();
    if (sel.length < 2 || !target.value.trim()) return;
    htmx.ajax('POST', '/tags/merge', {target: '#tag-list', swap: 'outerHTML',
      values: {old_names: sel.join(', '), new_name: target.value.trim()}});
  });
})();
</script>
```

- [ ] **Step 3: Run the tags tests (covers Task 3's fragment test too)**

Run: `python3 -m pytest tests/test_tags_page.py -q`
Expected: all pass, including `test_rename_returns_fragment_for_htmx`

- [ ] **Step 4: Commit**

```bash
git add templates/partials/tag_list.html templates/tags.html
git commit -m "feat(tags): pill-row manager with inline rename, merge, prune"
```

---

### Task 5: Engine multi-tag AND + untagged

**Files:**
- Modify: `keepfor/search/engine.py`
- Test: `tests/test_tag_filter.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Multi-tag AND filtering and the untagged sentinel."""

import pytest

from keepfor.auth.service import register_user
from keepfor.models.items import add_tags_to_item
from keepfor.search.engine import (
    count_recent_items,
    get_recent_items,
    hybrid_search,
    parse_tag_filter,
)


async def _seed(db, user_id, item_id, url, tags):
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status)"
        " VALUES (?, ?, ?, ?, 'ok');",
        (item_id, user_id, url, url),
    )
    if tags:
        await add_tags_to_item(db, user_id, item_id, tags)


def test_parse_tag_filter_splits_and_caps():
    tags, untagged = parse_tag_filter(" tech, AI ,,tech")
    assert tags == ["tech", "ai"]
    assert untagged is False
    tags, untagged = parse_tag_filter("__untagged__")
    assert (tags, untagged) == ([], True)
    tags, untagged = parse_tag_filter("tech, __untagged__")
    assert (tags, untagged) == ([], True)
    assert parse_tag_filter("") == ([], False)
    assert parse_tag_filter(None) == ([], False)


@pytest.mark.asyncio
async def test_browse_multi_tag_returns_intersection(db):
    user = await register_user(db, "flt@keepfor.me", "password123")
    await _seed(db, user["id"], "f1", "https://example.com/1", ["tech", "ai"])
    await _seed(db, user["id"], "f2", "https://example.com/2", ["tech"])
    items = await get_recent_items(db, user["id"], tags=["tech", "ai"])
    assert [i["id"] for i in items] == ["f1"]
    assert await count_recent_items(db, user["id"], tags=["tech", "ai"]) == 1


@pytest.mark.asyncio
async def test_browse_untagged_returns_only_tagless(db):
    user = await register_user(db, "flt2@keepfor.me", "password123")
    await _seed(db, user["id"], "f1", "https://example.com/1", ["tech"])
    await _seed(db, user["id"], "f2", "https://example.com/2", [])
    items = await get_recent_items(db, user["id"], untagged=True)
    assert [i["id"] for i in items] == ["f2"]
    assert await count_recent_items(db, user["id"], untagged=True) == 1


@pytest.mark.asyncio
async def test_legacy_single_tag_still_works(db):
    user = await register_user(db, "flt3@keepfor.me", "password123")
    await _seed(db, user["id"], "f1", "https://example.com/1", ["Tech"])
    items = await get_recent_items(db, user["id"], tag="tech")
    assert [i["id"] for i in items] == ["f1"]
    items, total = await hybrid_search(db, None, user["id"], "", tag="tech")
    assert total == 1
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_tag_filter.py -q`
Expected: FAIL with ImportError on `parse_tag_filter`

- [ ] **Step 3: Implement in `keepfor/search/engine.py`**

Add after `STATUS_GROUPS`:

```python
UNTAGGED_SENTINEL = "__untagged__"
MAX_TAG_FILTERS = 10


def parse_tag_filter(raw: str | None) -> tuple[list[str], bool]:
    """Split a comma-joined `?tag=` value into (tags, untagged).

    Lowercases, drops empties and duplicates, caps at MAX_TAG_FILTERS.
    The sentinel selects tagless items and is mutually exclusive with tags.
    """
    if not raw:
        return [], False
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    if UNTAGGED_SENTINEL in parts:
        return [], True
    seen: list[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return seen[:MAX_TAG_FILTERS], False
```

Change the `tag:`-bearing signatures to accept the new filters while keeping
`tag` working (`get_recent_items`, `count_recent_items`, `hybrid_search`):

```python
async def get_recent_items(
    db, user_id, tag=None, tags=None, untagged=False, limit=20, offset=0, status=None
):
```

Keep all existing params in order; insert `tags`/`untagged` right after `tag`.
Normalize at the top of each of the three functions:

```python
    tag_list = list(tags) if tags else ([tag] if tag else [])
```

In `get_recent_items`, replace the `if tag:` branch with:

```python
    if tag_list:
        exists = " ".join(
            "AND EXISTS (SELECT 1 FROM item_tags it%d JOIN tags t%d"
            " ON it%d.tag_id = t%d.id WHERE it%d.item_id = i.id"
            " AND LOWER(t%d.name) = LOWER(?))" % ((i,) * 6)
            for i in range(len(tag_list))
        )
        sql = f"""
        SELECT i.id, i.url, i.canonical_url, i.title, i.byline, i.site_name,
               i.published_date, i.excerpt, i.status, i.fail_reason, i.is_fallback,
               i.word_count, i.created_at
        FROM items i
        WHERE i.user_id = ?{status_clause} {exists}
        ORDER BY i.created_at DESC
        LIMIT ? OFFSET ?;
    """
        rows = await db.query_all(
            sql, (user_id, *status_params, *tag_list, limit, offset)
        )
    elif untagged:
        sql = f"""
            SELECT id, url, canonical_url, title, byline, site_name,
                   published_date, excerpt, status, fail_reason, is_fallback,
                   word_count, created_at
            FROM items
            WHERE user_id = ?{status_clause.replace("i.status", "status")}
              AND NOT EXISTS (SELECT 1 FROM item_tags itx WHERE itx.item_id = items.id)
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?;
        """
        rows = await db.query_all(sql, (user_id, *status_params, limit, offset))
    else:
        ...  # existing unfiltered branch, unchanged
```

Mirror the same three-way split in `count_recent_items` (COUNT queries with
the identical WHERE shapes). In `hybrid_search`: same signature additions and
normalization; the browse path forwards `tags=tag_list, untagged=untagged`;
the RRF assembly filter becomes:

```python
            if tag_list:
                lower = {t.lower() for t in item_tags}
                if any(t not in lower for t in tag_list):
                    continue
            elif untagged and item_tags:
                continue
```

- [ ] **Step 4: Run to verify**

Run: `python3 -m pytest tests/test_tag_filter.py tests/test_library_paging.py tests/test_pagination.py -q`
Expected: all pass (existing single-tag callers unchanged)

- [ ] **Step 5: Commit**

```bash
git add keepfor/search/engine.py tests/test_tag_filter.py
git commit -m "feat(tags): multi-tag AND and untagged filters in engine"
```

---

### Task 6: Library + search wiring and toggle UI

**Files:**
- Modify: `keepfor/app.py` (`library_page`, `search_htmx`)
- Modify: `templates/library.html`

- [ ] **Step 1: Rewire `library_page`**

Replace `tag=tag` in the `hybrid_search` calls with parsed filters:

```python
from keepfor.search.engine import parse_tag_filter  # add to engine imports

    tag_list, untagged_only = parse_tag_filter(tag)
    items, total = await hybrid_search(
        ...,
        query=q or "",
        tags=tag_list,
        untagged=untagged_only,
        ...,
    )
```

Apply to both `hybrid_search` calls in `library_page` and both in
`search_htmx` (there `tag_list, untagged_only = parse_tag_filter(clean_tag)`).
Render `active_tags=tag_list, active_untagged=untagged_only` and keep
`active_tag=tag` in the template context for one release (harmless,
lets old partials keep working). Update `pager_qs` to use the raw `tag`
string unchanged (comma-joined value round-trips as-is).

In `templates/library.html`, keep the htmx search/status input in sync:
```html
<input type="hidden" id="active-tag-input" name="tag" value="{{ active_tags|join(',') if active_tags else (active_tag or '') }}">
```

- [ ] **Step 2: Rewrite the sidebar/chip tag links as toggles and add active filter bar**

Sidebar row (desktop): replace the `href="/?tag={{ t.name }}"` links with
toggle links computed from `active_tags` (clicking a tag clears `__untagged__`, and clicking Untagged clears other tags):

```html
{% for t in tags %}
{% set ts = tag_styles.get(t.name, ('bg-slate-100 text-slate-600', 'bg-slate-400')) %}
{% if t.name in active_tags %}
<a href="/?tag={{ active_tags|reject('equalto', t.name)|join(',') }}" ...>#{{ t.name }} ×</a>
{% else %}
<a href="/?tag={{ (active_tags + [t.name])|reject('equalto', '__untagged__')|join(',') }}" ...>#{{ t.name }}</a>
{% endif %}
{% endfor %}
{% if active_untagged %}
<a href="/">Untagged ×</a>
{% else %}
<a href="/?tag=__untagged__">Untagged</a>
{% endif %}
```

Apply the same toggle shape to the mobile `#mobile-tags` chips. Empty-query
`tag=` (all removed) renders as `href="/"`. Active pills use the filled
`bg-slate-900 text-white` style already in the file. Keep `draggable`,
`data-tag-source`, tag-mode and DnD handlers untouched.

Add an Active Filters bar directly above the item feed (`#items-list`) in `templates/library.html`:

```html
{% if active_tags or active_untagged %}
<div id="active-filters-bar" class="flex flex-wrap items-center gap-1.5 p-2 bg-slate-50 border border-slate-200 rounded-lg text-xs mb-3">
  <span class="text-slate-500 font-medium mr-1">Filtered by:</span>
  {% for at in active_tags %}
  <a href="/?tag={{ active_tags|reject('equalto', at)|join(',') }}"
     class="inline-flex items-center gap-1 px-2.5 py-1 bg-slate-900 text-white rounded-full font-medium hover:bg-slate-800">
    #{{ at }} <span class="text-slate-400 hover:text-white">&times;</span>
  </a>
  {% endfor %}
  {% if active_untagged %}
  <a href="/"
     class="inline-flex items-center gap-1 px-2.5 py-1 bg-slate-900 text-white rounded-full font-medium hover:bg-slate-800">
    Untagged <span class="text-slate-400 hover:text-white">&times;</span>
  </a>
  {% endif %}
  <a href="/" class="ml-auto text-slate-500 hover:text-slate-800 underline text-xs">Clear all</a>
</div>
{% endif %}
```

- [ ] **Step 3: Empty-state for filtered-out results**

In `search_htmx` change the no-items branch to:

```python
    if not items:
        msg = "No saves match these tags." if (tag_list or untagged_only) else "No matching articles found."
        clear = ' <a href="/" class="underline">Clear filters</a>' if (tag_list or untagged_only) else ""
        return HTMLResponse(
            '<div class="text-center py-12 text-slate-400 text-xs">'
            + msg + clear + "</div>" + pager_html
        )
```

- [ ] **Step 4: Run the suite for fallout**

Run: `python3 -m pytest tests/test_endpoints.py tests/test_library_paging.py tests/test_status_filters.py tests/test_tag_filter.py -q`
Expected: all pass (`test_library_mobile_nav_and_tag_strip` needs `#mobile-tags` intact)

- [ ] **Step 5: Commit**

```bash
git add keepfor/app.py templates/library.html
git commit -m "feat(tags): multi-tag toggle filters and untagged view"
```

---

### Task 7: GET /tags/suggest (ranked autocomplete + recents)

**Files:**
- Modify: `keepfor/models/items.py` (append `suggest_tags`)
- Modify: `keepfor/app.py` (endpoint after `tags_delete`)
- Test: `tests/test_tag_suggest_bulk.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Ranked tag suggestions and bulk updates."""

import pytest

from keepfor.auth.service import register_user
from keepfor.models.items import add_tags_to_item, suggest_tags


async def _seed(db, user_id, item_id, url, tags):
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status)"
        " VALUES (?, ?, ?, ?, 'ok');",
        (item_id, user_id, url, url),
    )
    if tags:
        await add_tags_to_item(db, user_id, item_id, tags)


@pytest.mark.asyncio
async def test_suggest_ranks_frequent_first_then_prefix(db):
    user = await register_user(db, "sg@keepfor.me", "password123")
    await _seed(db, user["id"], "s1", "https://example.com/1", ["tech", "ai"])
    await _seed(db, user["id"], "s2", "https://example.com/2", ["tech"])
    await _seed(db, user["id"], "s3", "https://example.com/3", ["cooking"])
    # Distinct timestamps: CURRENT_TIMESTAMP has 1s precision, so ties would
    # make the recency tiebreak nondeterministic.
    await db.execute("UPDATE items SET created_at = '2026-01-01 00:00:01';")
    await db.execute("UPDATE items SET created_at = '2026-01-01 00:00:03' WHERE id = 's3';")
    assert [r["name"] for r in await suggest_tags(db, user["id"], "")] == [
        "tech",
        "cooking",
        "ai",
    ]
    assert [r["name"] for r in await suggest_tags(db, user["id"], "a")] == ["ai"]
    assert await suggest_tags(db, user["id"], "", exclude=["tech"]) == [
        {"name": "cooking", "count": 1},
        {"name": "ai", "count": 1},
    ]
```

Recency tiebreak (`cooking` before `ai`): both count 1, `s3` inserted after
`s1`. `suggest_tags` orders ties by most recent use.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_tag_suggest_bulk.py -q`
Expected: FAIL with ImportError on `suggest_tags`

- [ ] **Step 3: Implement the model** (append in `keepfor/models/items.py`)

```python
async def suggest_tags(
    db: Database,
    user_id: str,
    q: str = "",
    exclude: list[str] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Tags ranked most-used then most-recently-used, prefix-filtered.

    Empty `q` returns the recents list. `exclude` skips attached tags.
    """
    prefix = (q or "").strip().lower()
    excluded = {e.strip().lower() for e in (exclude or []) if e.strip()}
    rows = await db.query_all(
        """
        SELECT t.name, COUNT(it.item_id) AS count, MAX(i.created_at) AS recent
        FROM tags t
        LEFT JOIN item_tags it ON t.id = it.tag_id
        LEFT JOIN items i ON i.id = it.item_id
        WHERE t.user_id = ? AND LOWER(t.name) LIKE ?
        GROUP BY t.id, t.name
        ORDER BY count DESC, recent DESC
        LIMIT ?;
        """,
        (user_id, prefix + "%", limit + len(excluded)),
    )
    out = [
        {"name": r["name"], "count": r["count"]}
        for r in rows
        if r["name"].lower() not in excluded
    ]
    return out[:limit]
```

Endpoint in `keepfor/app.py` (no new imports: plain return values serialize to
JSON; `HTTPException` is already imported):

```python
@app.get("/tags/suggest")
async def tags_suggest(request: Request, q: str = "", exclude: str = ""):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    db = get_db(request)
    return await suggest_tags(
        db, user["id"], q, [e for e in exclude.split(",") if e.strip()]
    )
```

- [ ] **Step 4: Run to verify**

Run: `python3 -m pytest tests/test_tag_suggest_bulk.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add keepfor/models/items.py keepfor/app.py tests/test_tag_suggest_bulk.py
git commit -m "feat(tags): ranked tag suggest endpoint"
```

---

### Task 8: In-flow UI (datalists, recents row, card add input)

**Files:**
- Modify: `templates/save_popup.html`, `templates/partials/item_card.html`, `templates/library.html`
- Modify: `keepfor/app.py` (recents context on save-popup GET routes)

- [ ] **Step 1: Locate every save-popup render call**

Run: `grep -rn 'get_template("save_popup.html")' keepfor/`
Expected: the share-target handler plus the bookmarklet GET route. Add
`recent_tags=[r["name"] for r in await suggest_tags(db, user["id"], "", limit=5)]`
to each render context (`suggest_tags` import already added in Task 7).

- [ ] **Step 2: Datalist + recents in `save_popup.html`**

Replace the tags input block with:

```html
<div>
  <label class="block text-sm font-semibold uppercase text-slate-500 mb-1">Tags (comma separated)</label>
  <input type="text" name="tags" placeholder="tech, news, reading" autofocus list="tag-suggest-all" id="tags-input" autocomplete="off"
         class="w-full px-3 py-2 border border-slate-300 rounded-lg text-base focus:outline-none focus:ring-2 focus:ring-slate-900">
  <datalist id="tag-suggest-all"></datalist>
  {% if recent_tags %}
  <div class="flex flex-wrap gap-1.5 mt-2">
    {% for rt in recent_tags %}
    <button type="button" data-recent-tag="{{ rt }}"
            class="px-2.5 py-1 bg-slate-100 text-slate-600 rounded-full text-xs font-medium hover:bg-slate-200">#{{ rt }}</button>
    {% endfor %}
  </div>
  {% endif %}
</div>
<script>
(function () {
  var input = document.getElementById('tags-input');
  var list = document.getElementById('tag-suggest-all');
  if (!input || !list) return;
  input.addEventListener('input', function () {
    var parts = input.value.split(',');
    var q = parts[parts.length - 1].trim();
    fetch('/tags/suggest?q=' + encodeURIComponent(q) + '&exclude=' + encodeURIComponent(parts.slice(0, -1).join(',')))
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (items) {
        list.innerHTML = '';
        items.forEach(function (it) {
          var o = document.createElement('option');
          o.value = it.name;
          list.appendChild(o);
        });
      }).catch(function () {});
  });
  document.querySelectorAll('[data-recent-tag]').forEach(function (b) {
    b.addEventListener('click', function () {
      var cur = input.value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
      if (!cur.includes(b.getAttribute('data-recent-tag'))) cur.push(b.getAttribute('data-recent-tag'));
      input.value = cur.join(', ');
      input.focus();
    });
  });
})();
</script>
```

- [ ] **Step 3: Per-card add input in `partials/item_card.html` and datalist in `library.html`**

In `templates/library.html`, define the global datalist populated with the user's tags:

```html
<datalist id="tag-suggest-all">
  {% for t in tags %}<option value="{{ t.name }}"></option>{% endfor %}
</datalist>
```

In `templates/partials/item_card.html`, inside the tags `<div class="flex flex-wrap gap-1.5 mt-3">` (after the pills
loop, still inside the `{% if item.tags %}` block) plus an `{% else %}` branch
so untagged cards also get the input:

```html
<form hx-post="/items/{{ item.id }}/tags" hx-target="#item-card-{{ item.id }}" hx-swap="outerHTML"
      class="inline-flex items-center gap-1">
  <input type="text" name="add" placeholder="+ add tag" list="tag-suggest-all" autocomplete="off" aria-label="Add tag"
         class="w-24 px-2 py-1 border border-dashed border-slate-300 rounded-full text-xs focus:outline-none focus:border-slate-500">
</form>
```

Note: `hx-post` with an `add` field reuses `item_tags_update` unchanged
(comma-split already handled server-side). Using `list="tag-suggest-all"` leverages the user's tag vocabulary rather than duplicating datalists per card.

- [ ] **Step 4: Run the suite for regressions**

Run: `python3 -m pytest tests/test_endpoints.py tests/test_library_paging.py tests/test_tags_page.py -q`
Expected: all pass (card markup change is additive)

- [ ] **Step 5: Commit**

```bash
git add templates/save_popup.html templates/partials/item_card.html templates/library.html keepfor/app.py
git commit -m "feat(tags): autocomplete datalists, recents, per-card add"
```

---

### Task 9: Bulk-apply (model, endpoint, selection bar)

**Files:**
- Modify: `keepfor/models/items.py` (append `bulk_update_tags`)
- Modify: `keepfor/app.py` (POST `/items/bulk-tags`)
- Modify: `templates/library.html` (checkboxes + sticky bar)
- Test: append to `tests/test_tag_suggest_bulk.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
@pytest.mark.asyncio
async def test_bulk_add_and_remove_roundtrip(db):
    from keepfor.models.items import bulk_update_tags, get_item

    user = await register_user(db, "blk@keepfor.me", "password123")
    await _seed(db, user["id"], "b1", "https://example.com/1", ["old"])
    await _seed(db, user["id"], "b2", "https://example.com/2", ["old"])
    done = await bulk_update_tags(db, user["id"], ["b1", "b2"], ["new"], ["old"])
    assert done == 2
    assert (await get_item(db, user["id"], "b1"))["tags"] == ["new"]
    assert (await get_item(db, user["id"], "b2"))["tags"] == ["new"]


@pytest.mark.asyncio
async def test_bulk_ignores_foreign_items_and_caps_ids(db):
    from keepfor.models.items import bulk_update_tags, get_item_tags

    user = await register_user(db, "blk2@keepfor.me", "password123")
    other = await register_user(db, "blk3@keepfor.me", "password123")
    await _seed(db, other["id"], "bx", "https://example.com/x", [])
    ids = ["bx"] + ["missing-%d" % i for i in range(150)]
    assert await bulk_update_tags(db, user["id"], ids, ["hi"], []) == 0
    assert await get_item_tags(db, "bx") == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_tag_suggest_bulk.py -q`
Expected: FAIL with ImportError on `bulk_update_tags`

- [ ] **Step 3: Implement the model** (append in `keepfor/models/items.py`)

```python
BULK_TAG_LIMIT = 100


async def bulk_update_tags(
    db: Database,
    user_id: str,
    item_ids: list[str],
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> int:
    """Adds/removes tags across owned items; skips foreign and missing ids."""
    add = [t for t in (add or []) if t.strip()]
    remove = [t for t in (remove or []) if t.strip()]
    done = 0
    for item_id in (item_ids or [])[:BULK_TAG_LIMIT]:
        row = await db.query_first(
            "SELECT id FROM items WHERE id = ? AND user_id = ?;",
            (item_id, user_id),
        )
        if not row:
            continue
        if add:
            await add_tags_to_item(db, user_id, item_id, add)
        if remove:
            await remove_tags_from_item(db, user_id, item_id, remove)
        done += 1
    return done
```

Endpoint (plain form POST → 303 full reload; import `bulk_update_tags`
with the other tag imports):

```python
@app.post("/items/bulk-tags")
async def items_bulk_tags(
    request: Request,
    item_ids: list[str] = Form([]),
    add: str = Form(""),
    remove: str = Form(""),
    next: str = Form("/"),
):
    user = await require_user(request)
    db = get_db(request)
    await bulk_update_tags(
        db,
        user["id"],
        item_ids,
        [t for t in add.split(",") if t.strip()],
        [t for t in remove.split(",") if t.strip()],
    )
    return RedirectResponse(url=_safe_next(next), status_code=303)
```

`_safe_next` already exists in `keepfor/app.py` (used by suggestion handlers).

- [ ] **Step 4: Selection bar in `templates/library.html`**

Append before `#tag-trash`, plus per-card checkboxes via the card template.
In `partials/item_card.html` outer div add a hidden checkbox (bulk mode
reveals it):

```html
<input type="checkbox" value="{{ item.id }}" aria-label="Select item"
       class="bulk-select hidden absolute top-3 right-3 w-4 h-4 accent-slate-900">
```

Sticky bar + toggle in `library.html`:

```html
<div class="flex items-center gap-2">
  <button id="bulk-toggle" aria-pressed="false"
          class="shrink-0 px-3 py-2 rounded-full text-xs font-medium bg-white border border-slate-200 text-slate-600">☑ Select</button>
</div>
<form id="bulk-bar" action="/items/bulk-tags" method="POST" class="hidden sticky bottom-4 z-40 items-center gap-2 px-3 py-2 bg-white border border-slate-200 rounded-xl shadow-lg">
  <input type="hidden" name="next" value="/">
  <span id="bulk-count" class="text-xs text-slate-600 whitespace-nowrap"></span>
  <input type="text" name="add" placeholder="Add tags…" autocomplete="off" list="tag-suggest-all"
         class="flex-1 min-w-24 px-2 py-1.5 border border-slate-200 rounded-lg text-xs">
  <input type="text" name="remove" placeholder="Remove tags…" autocomplete="off" list="tag-suggest-all"
         class="flex-1 min-w-24 px-2 py-1.5 border border-slate-200 rounded-lg text-xs">
  <datalist id="tag-suggest-all"></datalist>
  <button type="submit" class="px-3 py-1.5 bg-slate-900 text-white rounded-lg text-xs font-medium shrink-0">Apply</button>
</form>
<div id="bulk-items"></div>
<script>
(function () {
  var toggle = document.getElementById('bulk-toggle');
  var bar = document.getElementById('bulk-bar');
  var holder = document.getElementById('bulk-items');
  var count = document.getElementById('bulk-count');
  if (!toggle || !bar) return;
  var on = false;
  function refresh() {
    var boxes = document.querySelectorAll('#items-list .bulk-select');
    var checked = Array.from(boxes).filter(function (b) { return b.checked; });
    holder.innerHTML = '';
    checked.forEach(function (b) {
      var h = document.createElement('input');
      h.type = 'hidden'; h.name = 'item_ids'; h.value = b.value;
      holder.appendChild(h);
    });
    count.textContent = checked.length + ' selected';
  }
  toggle.addEventListener('click', function () {
    on = !on;
    toggle.setAttribute('aria-pressed', on ? 'true' : 'false');
    document.querySelectorAll('#items-list .bulk-select').forEach(function (b) {
      b.classList.toggle('hidden', !on);
      if (!on) b.checked = false;
    });
    bar.classList.toggle('hidden', !on);
    bar.classList.toggle('flex', on);
    refresh();
  });
  document.getElementById('items-list').addEventListener('change', function (e) {
    if (e.target.classList && e.target.classList.contains('bulk-select')) refresh();
  });
})();
</script>
```

Note: htmx card swaps re-render cards (checkboxes reset) — acceptable; the
bar stays authoritative via `refresh()` on change.

- [ ] **Step 5: Run to verify**

Run: `python3 -m pytest tests/test_tag_suggest_bulk.py tests/test_endpoints.py -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add keepfor/models/items.py keepfor/app.py templates/library.html templates/partials/item_card.html tests/test_tag_suggest_bulk.py
git commit -m "feat(tags): bulk add/remove with selection bar"
```

---

### Task 10: Rules (model, ingest hook, miner, endpoints)

**Files:**
- Modify: `keepfor/models/items.py` (rules CRUD + match_rules + suggest_rules)
- Modify: `keepfor/consumer/processor.py` (hook)
- Modify: `keepfor/app.py` (3 endpoints + rules section context in `tags_page`)
- Modify: `templates/tags.html` (rules + suggested-rules sections)
- Test: `tests/test_tag_rules.py` (append; Task 1 smoke test already there)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_tag_rules.py`)

```python
RULES_MAX = 50  # mirror of the model cap; test pins it


@pytest.mark.asyncio
async def test_rule_crud_roundtrip(db):
    from keepfor.models.items import create_rule, delete_rule, list_rules

    user = await register_user(db, "rc@keepfor.me", "password123")
    assert await create_rule(db, user["id"], "domain", "ArXiv.ORG ", "research")
    assert await create_rule(db, user["id"], "bogus", "x", "y") is None
    assert await create_rule(db, user["id"], "domain", "x", "bad/name") is None
    rules = await list_rules(db, user["id"])
    assert [(r["field"], r["substr"], r["tag"]) for r in rules] == [
        ("domain", "arxiv.org", "research")
    ]
    assert await delete_rule(db, user["id"], rules[0]["id"]) is True
    assert await list_rules(db, user["id"]) == []


def test_match_rules_fields():
    from keepfor.models.items import match_rules

    rules = [
        {"field": "domain", "substr": "arxiv.org", "tag": "research"},
        {"field": "title", "substr": "postgres", "tag": "db"},
        {"field": "url", "substr": "utm_source", "tag": "promo"},
    ]
    got = match_rules(rules, "https://arxiv.org/abs/123?utm_source=x", "Postgres 16 notes")
    assert got == ["research", "db", "promo"]
    assert match_rules(rules, "https://example.com/", "Nothing here") == []


@pytest.mark.asyncio
async def test_miner_proposes_high_precision_domain(db):
    from keepfor.models.items import add_tags_to_item, suggest_rules

    user = await register_user(db, "mn@keepfor.me", "password123")
    for i in range(5):
        url = f"https://arxiv.org/abs/{i}"
        await db.execute(
            "INSERT INTO items (id, user_id, url, canonical_url, status)"
            " VALUES (?, ?, ?, ?, 'ok');",
            (f"mn{i}", user["id"], url, url),
        )
        await add_tags_to_item(db, user["id"], f"mn{i}", ["research"])
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status)"
        " VALUES (?, ?, ?, ?, 'ok');",
        ("mn5", user["id"], "https://example.com/x", "https://example.com/x"),
    )
    got = await suggest_rules(db, user["id"])
    assert {"domain": "arxiv.org", "tag": "research"} == {
        k: got[0][k] for k in ("domain", "tag")
    }
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_tag_rules.py -q`
Expected: FAIL with ImportError on `create_rule`

- [ ] **Step 3: Implement the model** (append in `keepfor/models/items.py`)

```python
RULE_FIELDS = ("domain", "title", "url")
RULES_PER_USER_MAX = 50


async def create_rule(
    db: Database, user_id: str, field: str, substr: str, tag: str
) -> str | None:
    """Creates a tagging rule; returns id or None when invalid/capped."""
    from keepfor.utils.tagger import validate_tag_name

    field = (field or "").strip().lower()
    clean_sub = (substr or "").strip().lower()
    clean_tag = validate_tag_name(tag or "")
    if field not in RULE_FIELDS or not (2 <= len(clean_sub) <= 64):
        return None
    if not clean_tag or clean_tag == "__untagged__":
        return None
    existing = await db.query_all(
        "SELECT id FROM tag_rules WHERE user_id = ?;", (user_id,)
    )
    if len(existing) >= RULES_PER_USER_MAX:
        return None
    rule_id = str(uuid.uuid4())
    await db.execute(
        "INSERT OR IGNORE INTO tag_rules (id, user_id, field, substr, tag)"
        " VALUES (?, ?, ?, ?, ?);",
        (rule_id, user_id, field, clean_sub, clean_tag),
    )
    return rule_id


async def list_rules(db: Database, user_id: str) -> list[dict[str, Any]]:
    return await db.query_all(
        "SELECT id, field, substr, tag FROM tag_rules"
        " WHERE user_id = ? ORDER BY created_at ASC;",
        (user_id,),
    )


async def delete_rule(db: Database, user_id: str, rule_id: str) -> bool:
    row = await db.query_first(
        "SELECT id FROM tag_rules WHERE id = ? AND user_id = ?;",
        (rule_id, user_id),
    )
    if not row:
        return False
    await db.execute("DELETE FROM tag_rules WHERE id = ?;", (rule_id,))
    return True


def match_rules(rules: list[dict], url: str, title: str) -> list[str]:
    """Pure matcher: domain/title/url substring rules. No I/O, no raises."""
    from urllib.parse import urlparse

    try:
        host = urlparse(url or "").netloc.lower()
    except Exception:
        host = ""
    lowered_title = (title or "").lower()
    lowered_url = (url or "").lower()
    out: list[str] = []
    for rule in rules:
        field, sub = rule.get("field"), rule.get("substr") or ""
        hit = (
            (field == "domain" and sub in host)
            or (field == "title" and sub in lowered_title)
            or (field == "url" and sub in lowered_url)
        )
        if hit and rule.get("tag") not in out:
            out.append(rule["tag"])
    return out


async def suggest_rules(
    db: Database,
    user_id: str,
    min_precision: float = 0.8,
    min_support: int = 5,
) -> list[dict[str, Any]]:
    """Mine domain->tag pairs worth turning into rules.

    Skips existing rules and dismissed keys (`domain:<d>:<t>`).
    """
    rows = await db.query_all(
        "SELECT i.canonical_url, t.name FROM item_tags it"
        " JOIN items i ON i.id = it.item_id"
        " JOIN tags t ON t.id = it.tag_id"
        " WHERE i.user_id = ?;",
        (user_id,),
    )
    from collections import Counter
    from urllib.parse import urlparse

    pair: Counter[tuple[str, str]] = Counter()
    pair_urls: dict[tuple[str, str], set[str]] = {}
    per_host_urls: dict[str, set[str]] = {}
    for row in rows:
        try:
            host = urlparse(row["canonical_url"] or "").netloc.lower()
        except Exception:
            continue
        if not host or not row["canonical_url"]:
            continue
        per_host_urls.setdefault(host, set()).add(row["canonical_url"])
        key = (host, row["name"])
        if row["canonical_url"] not in pair_urls.setdefault(key, set()):
            pair_urls[key].add(row["canonical_url"])
            pair[key] += 1
    existing = {
        (r["field"], r["substr"], r["tag"]) for r in await list_rules(db, user_id)
    }
    dismissed = {
        r["key"]
        for r in await db.query_all(
            "SELECT key FROM rule_suggestion_dismissals WHERE user_id = ?;",
            (user_id,),
        )
    }
    out = []
    for (host, tag), support in sorted(pair.items()):
        total = len(per_host_urls.get(host, set()))
        precision = (support / total) if total else 0.0
        key = f"domain:{host}:{tag}"
        if (
            support >= min_support
            and precision >= min_precision
            and ("domain", host, tag) not in existing
            and key not in dismissed
        ):
            out.append(
                {
                    "domain": host,
                    "tag": tag,
                    "precision": round(precision, 3),
                    "support": support,
                    "key": key,
                }
            )
    return out
```

- [ ] **Step 4: Ingest hook in `keepfor/consumer/processor.py`**

Inside the existing fail-open `try` (after the `add_suggestions` call, before
`except Exception`), add:

```python
            rules_rows = await db.query_all(
                "SELECT field, substr, tag FROM tag_rules WHERE user_id = ?;",
                (user_id,),
            )
            from keepfor.models.items import match_rules as _match_rules

            rule_tags = _match_rules(
                [dict(r) for r in rules_rows],
                url,
                extracted.get("title") or "",
            )
            if rule_tags:
                await add_tags_to_item(db, user_id, item_id, rule_tags)
```

No new failure mode: any defect lands in the existing `except` which logs
and continues extraction.

- [ ] **Step 5: Endpoints + `tags_page` context in `keepfor/app.py`**

```python
@app.post("/tags/rules/create")
async def tag_rule_create(
    request: Request,
    field: str = Form(""),
    substr: str = Form(""),
    tag: str = Form(""),
):
    user = await require_user(request)
    db = get_db(request)
    await create_rule(db, user["id"], field, substr, tag)
    return RedirectResponse(url="/tags", status_code=303)


@app.post("/tags/rules/delete")
async def tag_rule_delete(request: Request, rule_id: str = Form("")):
    user = await require_user(request)
    db = get_db(request)
    await delete_rule(db, user["id"], rule_id)
    return RedirectResponse(url="/tags", status_code=303)


@app.post("/tags/rules/suggestions/dismiss")
async def tag_rule_suggestion_dismiss(request: Request, key: str = Form("")):
    user = await require_user(request)
    db = get_db(request)
    clean = (key or "").strip()[:128]
    if clean:
        await db.execute(
            "INSERT OR IGNORE INTO rule_suggestion_dismissals (user_id, key)"
            " VALUES (?, ?);",
            (user["id"], clean),
        )
    return RedirectResponse(url="/tags", status_code=303)
```

In `tags_page`, add `rules=await list_rules(db, user["id"])` and
`suggested_rules=await suggest_rules(db, user["id"])` to the render context;
in `templates/tags.html` add two compact sections (rule rows with per-rule
`x` delete form; suggestion rows with Create (posts field/domain/tag to
`/tags/rules/create`) and Dismiss buttons).

- [ ] **Step 6: Run to verify**

Run: `python3 -m pytest tests/test_tag_rules.py tests/test_auto_tag.py -q`
Expected: all pass (processor hook covered by existing auto-tag tests)

- [ ] **Step 7: Commit**

```bash
git add keepfor/models/items.py keepfor/consumer/processor.py keepfor/app.py templates/tags.html tests/test_tag_rules.py
git commit -m "feat(tags): tagging rules, ingest hook, rule miner"
```

---

### Task 11: Full verification (lint, format, suite, bundle note)

**Files:** none (verification only)

- [ ] **Step 1: Run the CI lint form exactly**

Run: `ruff check keepfor/ tests/ --select=E,W,F,I,N`
Expected: clean (the `--select` matters: bare `ruff check .` hides `I001`)

- [ ] **Step 2: Run the CI format check**

Run: `ruff format --check keepfor/ tests/`
Expected: clean; if not, run `ruff format keepfor/ tests/` then re-check

- [ ] **Step 3: Run the full suite from the repo root**

Run: `python3 -m pytest tests/ -q`
Expected: ~90+ passed, 1 skipped (baseline before this plan: 90 passed, 1 skipped)

- [ ] **Step 4: Record the migration ops note (do NOT run; deploy.yml never applies migrations)**

Remote D1 still needs, by hand after merge:

```bash
npx wrangler d1 migrations apply keepfor-me-db --remote
```

`tests/conftest.py` already applies `0004` locally, so the suite is green
without it.

- [ ] **Step 5: Commit any lint/format fixes**

```bash
git add -A
git commit -m "chore(tags): lint and format"
```

---

## Self-review

**Spec coverage:** S1 manager → Tasks 2–4 (merge/prune models, endpoints,
pill-row UI, unused group). S2 finding → Tasks 5–6 (engine + wiring + empty
state). S3 in-flow → Tasks 7–9 (suggest endpoint, datalists/recents/card
input, bulk). S4 rules → Tasks 1 + 10 (migration, CRUD, hook, miner,
endpoints). Reserved-name, 10-tag cap, 100-id bulk cap, 50-rule cap all
pinned by tests. Suggest endpoint 401 uses `HTTPException` (already
imported in `keepfor/app.py`).

**Placeholder scan:** no TBD/TODO; every code step shows complete code;
template/JS blocks are complete; commands state exact invocations and
expected output.

**Type consistency:** `merge_tags`/`prune_unused_tags`/`suggest_tags`/
`bulk_update_tags`/rules signatures are identical between test steps and
implementation steps. `parse_tag_filter` returns `tuple[list[str], bool]`
everywhere. `match_rules` takes `list[dict]` rows in both test and hook
(`[dict(r) for r in rules_rows]` converts sqlite Rows). `suggest_rules`
dict keys (`domain`, `tag`, `key`) match the template buttons and the
miner test.
