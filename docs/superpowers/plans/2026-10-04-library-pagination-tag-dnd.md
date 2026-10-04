# Library Pagination + Tag Drag-and-Drop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Paginated library with per-page selector and drag/tap tag assignment, PWA-safe.

**Architecture:** Engine gains `offset` + `(items, total)` returns; `/search` returns cards plus an out-of-band pager fragment; one HTML-returning tag-mutation endpoint serves desktop DnD, pill buttons, and touch tap-to-assign.

**Tech Stack:** FastAPI + Jinja + htmx 2.0.3 (CDN, already loaded) + vanilla inline JS. No new dependencies, no JS bundle.

---

## File structure

| File | Responsibility |
|---|---|
| `src/search/engine.py` | `count_recent_items()` helper; `hybrid_search(..., offset)` returns `(items, total)` |
| `src/app.py` | `library_page` + `search_htmx` pagination params; new `POST /items/{item_id}/tags` |
| `src/mcp/server.py` | Unwrap `hybrid_search` tuple (one line) |
| `templates/partials/pager.html` | New: pager bar + result count + per-page select (create) |
| `templates/library.html` | Hidden page/per_page inputs, pager include, draggable tags, tag-mode toggle, inline script |
| `templates/partials/item_card.html` | Card data attrs, pill restructure with `×` button |
| `tests/test_pagination.py` | New: engine offset/total tests |
| `tests/test_library_paging.py` | New: endpoint tests (pager, clamping, tag mutation) |
| `tests/test_items_and_search.py:89`, `tests/test_mcp.py:176,197` | Unwrap tuple in 3 existing assertions |

Pager state rule (applies everywhere): changing per-page, tag, status, or query resets to page 1. Out-of-range page renders the last page, never empty.

---

### Task 1: Engine pagination + totals

**Files:**
- Modify: `src/search/engine.py:137-250` (`hybrid_search`), append `count_recent_items` after `get_recent_items` (ends at line 314)
- Test: `tests/test_pagination.py` (create)

- [ ] **Step 1: Write the failing tests.** Create `tests/test_pagination.py` with the full fixture copy below (do not import from other test modules):

```python
"""Engine offset/total tests for library pagination."""

import pytest

from src.auth.service import register_user
from src.models.items import save_item
from src.search.engine import count_recent_items, hybrid_search


class FakeQueue:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


class MockEnv:
    def __init__(self):
        self.DB = None
        self.QUEUE = FakeQueue()
        self.AI = None
        self.VECTORIZE = None
        self.BUCKET = None


@pytest.fixture
async def user_with_items(db):
    user = await register_user(db, "pager@keepfor.me", "password123")
    env = MockEnv()
    for n in range(7):
        await save_item(db, env, user["id"], f"https://example.com/p{n}")
    return user, env, db


async def test_browse_offset_and_total(user_with_items):
    user, env, db = user_with_items
    items, total = await hybrid_search(db, env, user["id"], query="", limit=3, offset=0)
    assert total == 7
    assert len(items) == 3
    items2, total2 = await hybrid_search(db, env, user["id"], query="", limit=3, offset=3)
    assert total2 == 7
    assert len(items2) == 3
    assert items2[0]["id"] != items[0]["id"]


async def test_count_recent_items_matches(user_with_items):
    from src.models.items import add_tags_to_item

    user, env, db = user_with_items
    assert await count_recent_items(db, user["id"]) == 7
    items, _ = await hybrid_search(db, env, user["id"], query="", limit=7, offset=0)
    await add_tags_to_item(db, user["id"], items[0]["id"], ["triage"])
    assert await count_recent_items(db, user["id"], tag="triage") == 1
```

- [ ] **Step 2: Run to verify they fail.**

Run: `python3 -m pytest tests/test_pagination.py -q` (from repo root)
Expected: FAIL — `hybrid_search` takes no `offset`, returns list (unpack error), `count_recent_items` undefined.

- [ ] **Step 3: Implement.** In `src/search/engine.py`, change the signature and empty-query path:

```python
async def hybrid_search(
    db: Database,
    env: Any,
    user_id: str,
    query: str,
    mode: str = "hybrid",
    tag: str | None = None,
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Execute hybrid search with RRF fusion, filters, and pagination.

    Returns (page_items, total_matches). total is exact for browse, and
    the ranked-candidate count (~100 max) for text queries.
    """
    query = query.strip()
    if not query:
        # Return recent items
        items = await get_recent_items(
            db, user_id, tag=tag, limit=limit, offset=offset, status=status
        )
        total = await count_recent_items(db, user_id, tag=tag, status=status)
        return items, total
```

Replace `top_ids = sorted_item_ids[: limit * 2]  # Fetch extra ...` with:

```python
    top_ids = sorted_item_ids  # Full ranked window; sliced per page below
```

Delete the early stop:

```python
            if len(final_items) >= limit:
                break
```

Replace `return final_items` with:

```python
    return final_items[offset : offset + limit], len(final_items)
```

Also change `if not rrf_scores: return []` to `return [], 0`.

Append after `get_recent_items` (end of file):

```python
async def count_recent_items(
    db: Database,
    user_id: str,
    tag: str | None = None,
    status: str | None = None,
) -> int:
    """Count items matching the browse filters (same WHERE as get_recent_items)."""
    status_values = _status_values(status)
    status_clause = ""
    status_params: tuple = ()
    if status_values:
        status_clause = f" AND status IN ({','.join('?' for _ in status_values)})"
        status_params = tuple(status_values)
    if tag:
        tag_clause = status_clause.replace("status", "i.status")
        rows = await db.query_all(
            "SELECT COUNT(*) as count FROM items i "
            "JOIN item_tags it ON i.id = it.item_id "
            "JOIN tags t ON it.tag_id = t.id "
            f"WHERE i.user_id = ? AND LOWER(t.name) = LOWER(?){tag_clause};",
            (user_id, tag, *status_params),
        )
    else:
        rows = await db.query_all(
            f"SELECT COUNT(*) as count FROM items WHERE user_id = ?{status_clause};",
            (user_id, *status_params),
        )
    return rows[0]["count"] if rows else 0
```

- [ ] **Step 4: Run tests.**

Run: `python3 -m pytest tests/test_pagination.py -q`
Expected: PASS (2 passed). Other suites still fail on tuple unpack — fixed in Task 2.

- [ ] **Step 5: Commit.**

```bash
git add src/search/engine.py tests/test_pagination.py
git commit -m "feat: hybrid_search offset pagination and totals"
```

---

### Task 2: Update existing `hybrid_search` callers

**Files:**
- Modify: `src/mcp/server.py:193`, `src/app.py:1191` (`api_search`), `tests/test_items_and_search.py:89`, `tests/test_mcp.py:176,197`

No new tests (existing tests cover behavior; they just need unwrapping).

- [ ] **Step 1: Unwrap the tuple at all 5 sites.** Exact edits:

`src/mcp/server.py:193`:
```python
        items, _ = await hybrid_search(
            db, env, user_id, query, mode=mode, tag=tag, limit=limit
        )
```

`src/app.py` `api_search` body:
```python
    results, _ = await hybrid_search(
        db,
        env,
        user["id"],
        query=body.query,
        mode=body.mode,
        tag=body.tag,
        limit=body.limit,
    )
    return JSONResponse(content=results)
```
(JSON shape stays a bare list — no extension/API break.)

`tests/test_items_and_search.py:89`:
```python
    results, total = await hybrid_search(
        db, env, user["id"], query="Python Workers", mode="keyword"
    )
    assert total == 1
```

`tests/test_mcp.py` (~line 176 and ~197): `results = await engine.hybrid_search(` → `results, _ = await engine.hybrid_search(` (both occurrences, keep all other lines identical).

- [ ] **Step 2: Run affected suites.**

Run: `python3 -m pytest tests/test_items_and_search.py tests/test_mcp.py tests/test_status_filters.py -q` (from repo root)
Expected: all PASS.

- [ ] **Step 3: Commit.**

```bash
git add src/mcp/server.py src/app.py tests/test_items_and_search.py tests/test_mcp.py
git commit -m "feat: unwrap hybrid_search items/total tuple at callers"
```

---

### Task 3: Pager partial template

**Files:**
- Create: `templates/partials/pager.html`
- Test: none yet (covered by Task 6 endpoint tests)

- [ ] **Step 1: Create the partial.** Context provided by routes: `page`, `total_pages`, `pages` (windowed list), `total`, `per_page`, `per_page_options`, `push_base` (e.g. `"/?tag=ai&status=saved&"` or `"/?"`), `shown_from`, `shown_to`. Every button POSTs to `/search` (included hidden inputs carry q/tag/status/page/per_page) and pushes the matching GET URL so back-button/bookmarks work:

```html
<div id="pager" hx-swap-oob="true" class="flex flex-wrap items-center gap-x-3 gap-y-2 py-2">
  <p class="text-xs text-slate-500">Showing {{ shown_from }}–{{ shown_to }} of {{ total }} saves</p>
  <label class="inline-flex items-center gap-1.5 text-xs text-slate-500">Per page
    <select id="pager-perpage" name="per_page"
            hx-post="/search" hx-target="#items-list"
            hx-include="#search-input, #active-tag-input, #active-status-input, #active-page-input, #pager-perpage"
            hx-on:change="document.getElementById('active-page-input').value='1'"
            class="text-base md:text-xs border border-slate-200 rounded-lg px-2 py-2 md:py-1 bg-white">
      {% for n in per_page_options %}
      <option value="{{ n }}" {% if n == per_page %}selected{% endif %}>{{ n }}</option>
      {% endfor %}
    </select>
  </label>
  <nav class="inline-flex items-center gap-1" aria-label="Pages">
    <button {% if page <= 1 %}disabled{% endif %}
            hx-post="/search" hx-target="#items-list"
            hx-include="#search-input, #active-tag-input, #active-status-input, #active-page-input, #active-perpage-input"
            hx-push-url="{{ push_base }}page={{ page - 1 }}&per_page={{ per_page }}"
            onclick="document.getElementById('active-page-input').value='{{ page - 1 }}'"
            class="min-w-11 min-h-11 md:min-w-0 md:min-h-0 px-3 py-2 md:py-1 rounded-lg text-xs font-medium border border-slate-200 bg-white text-slate-600 disabled:opacity-40" aria-label="Previous page">‹ Prev</button>
    {% for p in pages %}
    <button hx-post="/search" hx-target="#items-list"
            hx-include="#search-input, #active-tag-input, #active-status-input, #active-page-input, #active-perpage-input"
            hx-push-url="{{ push_base }}page={{ p }}&per_page={{ per_page }}"
            onclick="document.getElementById('active-page-input').value='{{ p }}'"
            aria-current="{% if p == page %}page{% else %}false{% endif %}"
            class="min-w-11 min-h-11 md:min-w-0 md:min-h-0 px-3 py-2 md:py-1 rounded-lg text-xs font-medium {% if p == page %}bg-slate-900 text-white{% else %}border border-slate-200 bg-white text-slate-600{% endif %}">{{ p }}</button>
    {% endfor %}
    <button {% if page >= total_pages %}disabled{% endif %}
            hx-post="/search" hx-target="#items-list"
            hx-include="#search-input, #active-tag-input, #active-status-input, #active-page-input, #active-perpage-input"
            hx-push-url="{{ push_base }}page={{ page + 1 }}&per_page={{ per_page }}"
            onclick="document.getElementById('active-page-input').value='{{ page + 1 }}'"
            class="min-w-11 min-h-11 md:min-w-0 md:min-h-0 px-3 py-2 md:py-1 rounded-lg text-xs font-medium border border-slate-200 bg-white text-slate-600 disabled:opacity-40" aria-label="Next page">Next ›</button>
  </nav>
</div>
```

Notes for the worker: `hx-swap-oob="true"` on the full-page render is inert (htmx only processes OOB on swaps) and active on `/search` responses. `urlencode` is already imported in `src/app.py:8`. Touch targets use `min-w-11 min-h-11` (44px) collapsing to natural size on desktop.

- [ ] **Step 2: Verify template parses.**

Run: `python3 -c "from src.app import jinja_env; jinja_env.get_template('partials/pager.html'); print('pager ok')"` (from repo root)
Expected: `pager ok`, no exception.

- [ ] **Step 3: Commit.**

```bash
git add templates/partials/pager.html
git commit -m "feat: pager partial with OOB swap and push-url buttons"
```

---

### Task 4: Route pagination (`library_page` + `search_htmx`)

**Files:**
- Modify: `src/app.py:322-406`
- Modify: `templates/library.html:60-78` (hidden inputs, search reset, pager include)

- [ ] **Step 1: Add a shared pagination helper** above `library_page` in `src/app.py`:

```python
PER_PAGE_OPTIONS = [10, 20, 30, 50]


def _pager_context(
    page: int, per_page: int, total: int
) -> dict[str, int | list[int]]:
    """Clamp page/per_page and build pager template context."""
    per_page = per_page if per_page in PER_PAGE_OPTIONS else 20
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(page, 1), total_pages)
    start = max(1, min(page - 2, max(1, total_pages - 4)))
    pages = list(range(start, min(total_pages, start + 4) + 1))
    return {
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "pages": pages,
        "shown_from": (page - 1) * per_page + 1 if total else 0,
        "shown_to": min(page * per_page, total),
    }
```

- [ ] **Step 2: Rewire `library_page`.** Change signature to accept `page: int = 1, per_page: int = 20`, and replace the `hybrid_search` call and render context:

```python
async def library_page(
    request: Request,
    tag: str | None = None,
    q: str | None = None,
    status: str | None = None,
    page: int = 1,
    per_page: int = 20,
):
    ... (auth unchanged) ...
    clean_status = status.strip() if status and status.strip() else None
    items, total = await hybrid_search(
        db,
        env,
        user["id"],
        query=q or "",
        tag=tag,
        limit=per_page if per_page in PER_PAGE_OPTIONS else 20,
        offset=0,
        status=clean_status,
    )
    pager = _pager_context(page, per_page, total)
    if pager["page"] != page:
        items, total = await hybrid_search(
            db,
            env,
            user["id"],
            query=q or "",
            tag=tag,
            limit=pager["per_page"],
            offset=(pager["page"] - 1) * pager["per_page"],
            status=clean_status,
        )
        pager = _pager_context(pager["page"], pager["per_page"], total)
    ... (tags, tag_styles, total_count, status_counts unchanged) ...
    pager_qs = urlencode(
        {
            k: v
            for k, v in {
                "q": q or "",
                "tag": tag or "",
                "status": clean_status or "",
            }.items()
            if v
        }
    )
    push_base = ("/?" + pager_qs + "&") if pager_qs else "/?"
    html = template.render(
        ... (existing keys unchanged) ...
        total=total,
        per_page_options=PER_PAGE_OPTIONS,
        push_base=push_base,
        **pager,
    )
```

(The first fetch gets the total; the conditional refetch handles out-of-range pages. `total_count` stays as the unfiltered library size for the sidebar.)

- [ ] **Step 3: Rewire `search_htmx`.** Accept `page: int = Form(1), per_page: int = Form(20)`, same fetch-then-clamp pattern, and return cards plus the OOB pager:

```python
    items, total = await hybrid_search(
        db, env, user["id"], query=query, mode="hybrid", tag=clean_tag,
        limit=per_page if per_page in PER_PAGE_OPTIONS else 20,
        offset=0, status=clean_status,
    )
    pager = _pager_context(page, per_page, total)
    if pager["page"] != page:
        items, total = await hybrid_search(
            db, env, user["id"], query=query, mode="hybrid", tag=clean_tag,
            limit=pager["per_page"],
            offset=(pager["page"] - 1) * pager["per_page"],
            status=clean_status,
        )
        pager = _pager_context(pager["page"], pager["per_page"], total)
    ... (tag_styles unchanged) ...
    pager_qs = urlencode(
        {
            k: v
            for k, v in {
                "q": query or "",
                "tag": clean_tag or "",
                "status": clean_status or "",
            }.items()
            if v
        }
    )
    push_base = ("/?" + pager_qs + "&") if pager_qs else "/?"
    pager_html = jinja_env.get_template("partials/pager.html").render(
        total=total, per_page_options=PER_PAGE_OPTIONS, push_base=push_base, **pager
    )
    if not items:
        return HTMLResponse(
            '<div class="text-center py-12 text-slate-400 text-xs">'
            "No matching articles found.</div>" + pager_html
        )
    cards = [template.render(item=it, tag_styles=tag_styles) for it in items]
    return HTMLResponse(content="".join(cards) + pager_html)
```

- [ ] **Step 4: Wire the template.** In `templates/library.html`: add the two hidden inputs next to `#active-status-input`, extend every `hx-include` on the page with `, #active-page-input, #active-perpage-input`, add `id="pager-wrap"` include of the partial after `#items-list`, and reset page on new searches:

```html
<input type="hidden" id="active-page-input" name="page" value="{{ page }}">
<input type="hidden" id="active-perpage-input" name="per_page" value="{{ per_page }}">
```

```html
<div id="pager-wrap" class="pt-1">
  {% include "partials/pager.html" %}
</div>
```

Status-pill `onclick` handlers gain `document.getElementById('active-page-input').value='1';` first. The search input keeps its htmx attrs; page reset on typing is handled by the Task 7 script listener.

- [ ] **Step 5: Manual smoke via tests in Task 6** (no separate test step here).

- [ ] **Step 6: Commit.**

```bash
git add src/app.py templates/library.html
git commit -m "feat: paginated library routes and template wiring"
```

---

### Task 5: Tag mutation endpoint

**Files:**
- Modify: `src/app.py` (append after `tags_delete`, ~line 553)
- Test: `tests/test_library_paging.py` (create; fixture code included in Task 6 — write the endpoint first, tests next task)

- [ ] **Step 1: Add the endpoint** after `tags_delete` in `src/app.py`:

```python
@app.post("/items/{item_id}/tags", response_class=HTMLResponse)
async def item_tags_update(
    request: Request,
    item_id: str,
    add: str = Form(""),
    remove: str = Form(""),
):
    # htmx tag assignment: returns the re-rendered card (HTML, not JSON).
    user = await require_user(request)
    db = get_db(request)
    if add.strip():
        await add_tags_to_item(
            db, user["id"], item_id, [t for t in add.split(",") if t.strip()]
        )
    if remove.strip():
        await remove_tags_from_item(
            db, user["id"], item_id, [t for t in remove.split(",") if t.strip()]
        )
    item = await get_item(db, user["id"], item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    tag_styles = tag_styles_for(item.get("tags") or [])
    template = jinja_env.get_template("partials/item_card.html")
    return HTMLResponse(content=template.render(item=item, tag_styles=tag_styles))
```

`add_tags_to_item`, `remove_tags_from_item`, `get_item` are already imported in `src/app.py` (lines 38-50). `Form` is imported (line 14).

- [ ] **Step 2: Quick manual check.**

Run: `python3 -m pytest tests/test_endpoints.py -q -k "tags or save" 2>&1 | tail -2` (from repo root)
Expected: PASS (endpoint added, nothing broken).

- [ ] **Step 3: Commit.**

```bash
git add src/app.py
git commit -m "feat: POST /items/{id}/tags returns re-rendered card"
```

---

### Task 6: Endpoint tests (pager + clamping + tag mutation)

**Files:**
- Test: `tests/test_library_paging.py` (create, self-contained fixtures)

- [ ] **Step 1: Write the tests.**

```python
"""Endpoint tests for library pagination and card tag mutation."""

import pytest
from fastapi.testclient import TestClient

from src import app as app_module
from src.auth.service import create_pat, register_user
from src.models.items import save_item


class FakeQueue:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


class MockEnv:
    def __init__(self, sqlite_conn=None):
        self.DB = None
        self.QUEUE = FakeQueue()
        self.AI = None
        self.VECTORIZE = None
        self.BUCKET = None
        self.sqlite_conn = sqlite_conn


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(app_module, "get_db", lambda request: db)
    monkeypatch.setattr(
        app_module, "get_env_from_request", lambda request: MockEnv()
    )
    return TestClient(app_module.app)


@pytest.fixture
async def paging_setup(db):
    user = await register_user(db, "paging@keepfor.me", "password123")
    env = MockEnv()
    for n in range(25):
        await save_item(db, env, user["id"], f"https://example.com/pg{n}")
    pat = await create_pat(db, user["id"], "Paging PAT")
    return {"user": user, "headers": {"Authorization": f"Bearer {pat['token']}"}}


async def _login(client, db, email="paging@keepfor.me"):
    from src.auth.service import login_user

    _, session_id = await login_user(db, email, "password123")
    client.cookies["kfm_session"] = session_id


async def test_library_page_two_per_page(client, db, paging_setup):
    await _login(client, db)
    res = client.get("/", params={"per_page": 10})
    assert res.status_code == 200
    assert res.text.count('id="item-card-') == 10
    assert "Showing 1–10 of 25 saves" in res.text


async def test_library_clamps_bad_per_page(client, db, paging_setup):
    await _login(client, db)
    res = client.get("/", params={"per_page": 999})
    assert res.status_code == 200
    assert 'value="20"' in res.text


async def test_library_out_of_range_page_shows_last(client, db, paging_setup):
    await _login(client, db)
    res = client.get("/", params={"per_page": 10, "page": 99})
    assert res.status_code == 200
    assert res.text.count('id="item-card-') == 5
    assert "Showing 21–25 of 25 saves" in res.text


async def test_search_returns_oob_pager(client, db, paging_setup):
    await _login(client, db)
    res = client.post(
        "/search",
        data={"query": "", "tag": "", "status": "", "page": 2, "per_page": 10},
    )
    assert res.status_code == 200
    assert 'hx-swap-oob="true"' in res.text
    assert "Showing 11–20 of 25 saves" in res.text


async def test_tag_add_and_remove_roundtrip(client, db, paging_setup):
    await _login(client, db)
    from src.models.items import get_item

    page = client.get("/", params={"per_page": 10})
    item_id = page.text.split('id="item-card-')[1].split('"')[0]

    added = client.post(f"/items/{item_id}/tags", data={"add": "triage"})
    assert added.status_code == 200
    assert "#triage" in added.text
    assert (await get_item(db, paging_setup["user"]["id"], item_id))["tags"] == ["triage"]

    removed = client.post(f"/items/{item_id}/tags", data={"remove": "triage"})
    assert removed.status_code == 200
    assert "#triage" not in removed.text


async def test_tag_mutation_requires_auth(client, db, paging_setup):
    res = client.post("/items/whatever/tags", data={"add": "x"})
    assert res.status_code == 401


async def test_tag_mutation_404_unknown_item(client, db, paging_setup):
    await _login(client, db)
    res = client.post("/items/does-not-exist/tags", data={"add": "x"})
    assert res.status_code == 404
```

(Delete the `rows = await save_item` placeholder lines — the final test uses the page-scraped id.)

- [ ] **Step 2: Run to verify.**

Run: `python3 -m pytest tests/test_library_paging.py -q` (from repo root)
Expected: 7 passed. If `_login` misbehaves under asyncio_mode=auto (event loop juggling), replace it with the cookie pattern from `test_endpoints.py:auth_headers` (login via `await login_user` in an async test and set `client.cookies` directly — all tests here are already async).

- [ ] **Step 3: Commit.**

```bash
git add tests/test_library_paging.py
git commit -m "test: library pager and card tag mutation endpoints"
```

---

### Task 7: Card markup — data attrs and removable pills

**Files:**
- Modify: `templates/partials/item_card.html:1-6` (both root divs), `:57-66` (pills)

- [ ] **Step 1: Tag the card roots.** Add `data-item-id="{{ item.id }}"` and `data-tags="{{ (item.tags or [])|join(',') }}"` to both root `<div>`s (queued-polling and normal). Everything else on those lines stays identical.

- [ ] **Step 2: Restructure pills.** Replace lines 57–66 with a span pill (filter link preserved) plus a remove button that needs no JS:

```html
      {% if item.tags %}
      <div class="flex flex-wrap gap-1.5 mt-3">
        {% for tag in item.tags %}
        {% set ts = tag_styles.get(tag, ('bg-slate-100 text-slate-600', 'bg-slate-400')) %}
        <span class="inline-flex items-center gap-1 pl-2.5 pr-1 py-1 {{ ts[0] }} text-xs font-medium rounded-full">
          <a href="/?tag={{ tag }}" class="hover:opacity-80">#{{ tag }}</a>
          <button hx-post="/items/{{ item.id }}/tags" hx-vals='{"remove": "{{ tag }}"}'
                  hx-target="#item-card-{{ item.id }}" hx-swap="outerHTML"
                  class="min-w-8 min-h-8 md:min-w-0 md:min-h-0 px-2 py-1.5 md:py-0.5 rounded-full hover:bg-black/10 font-bold"
                  aria-label="Remove tag {{ tag }}">×</button>
        </span>
        {% endfor %}
      </div>
      {% endif %}
```

- [ ] **Step 3: Re-run card-rendering suites.**

Run: `python3 -m pytest tests/test_library_paging.py tests/test_endpoints.py -q` (from repo root)
Expected: all PASS (pill test asserts `#triage` presence/absence — still true).

- [ ] **Step 4: Commit.**

```bash
git add templates/partials/item_card.html
git commit -m "feat: card data attrs and pill remove buttons"
```

---

### Task 8: Desktop drag-and-drop script

**Files:**
- Modify: `templates/library.html` (sidebar tag rows, trash chip, inline script)

- [ ] **Step 1: Make sidebar rows draggable.** Add to the sidebar tag `<a>` (line 21): `draggable="true" data-tag-source="{{ t.name }}"`. Add to mobile strip chips (line 53): `data-tag-source="{{ t.name }}"` (no `draggable` — touch uses tap mode from Task 9).

- [ ] **Step 2: Add the trash chip.** Insert before the closing of the main content column (after `#pager-wrap`):

```html
<div id="tag-trash" class="hidden fixed bottom-24 left-1/2 -translate-x-1/2 z-50 px-5 py-3 rounded-full bg-red-600 text-white text-sm font-semibold shadow-xl">
  Drop here to remove tag
</div>
```

- [ ] **Step 3: Add the inline script** at the end of `{% block content %}` (after the closing `</div>` of the max-w container):

```html
<script>
(function () {
  var dragged = null; // {tag, itemId} for pill drags, {tag} for sidebar drags
  function cards() { return document.querySelectorAll('#items-list [data-item-id]'); }
  function showTrash(show, label) {
    var t = document.getElementById('tag-trash');
    if (!t) return;
    t.classList.toggle('hidden', !show);
    if (label) t.textContent = label;
  }
  document.querySelectorAll('[data-tag-source]').forEach(function (el) {
    el.addEventListener('dragstart', function (e) {
      dragged = { tag: el.getAttribute('data-tag-source'), itemId: null };
      e.dataTransfer.effectAllowed = 'copy';
      try { e.dataTransfer.setData('text/plain', dragged.tag); } catch (_) {}
    });
    el.addEventListener('dragend', function () { dragged = null; clearHints(); showTrash(false); });
  });
  function clearHints() { cards().forEach(function (c) { c.classList.remove('ring-2', 'ring-blue-500'); }); }
  document.addEventListener('dragover', function (e) {
    var card = e.target.closest && e.target.closest('#items-list [data-item-id]');
    if (dragged && !dragged.itemId && card) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
      card.classList.add('ring-2', 'ring-blue-500');
    }
    var trash = e.target.closest && e.target.closest('#tag-trash');
    if (dragged && dragged.itemId && trash) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }
  });
  document.addEventListener('dragleave', function (e) {
    var card = e.target.closest && e.target.closest('#items-list [data-item-id]');
    if (card) card.classList.remove('ring-2', 'ring-blue-500');
  });
  document.addEventListener('drop', function (e) {
    var card = e.target.closest && e.target.closest('#items-list [data-item-id]');
    if (dragged && !dragged.itemId && card) {
      e.preventDefault();
      htmx.ajax('POST', '/items/' + card.getAttribute('data-item-id') + '/tags',
        { target: '#' + card.id, swap: 'outerHTML', values: { add: dragged.tag } });
    }
    var trash = e.target.closest && e.target.closest('#tag-trash');
    if (dragged && dragged.itemId && trash) {
      e.preventDefault();
      htmx.ajax('POST', '/items/' + dragged.itemId + '/tags',
        { target: '#item-card-' + dragged.itemId, swap: 'outerHTML', values: { remove: dragged.tag } });
    }
    dragged = null; clearHints(); showTrash(false);
  });
  // Pills are draggable removal sources (desktop): trash appears while dragging.
  document.addEventListener('dragstart', function (e) {
    var pill = e.target.closest && e.target.closest('#items-list [data-pill]');
    if (pill) {
      dragged = { tag: pill.getAttribute('data-pill'), itemId: pill.getAttribute('data-item-id') };
      showTrash(true, 'Drop here to remove #' + dragged.tag);
    }
  });
})();
</script>
```

And add `draggable="true" data-pill="{{ tag }}" data-item-id="{{ item.id }}"` to the pill `<span>` from Task 7.

- [ ] **Step 4: Run template suites.**

Run: `python3 -m pytest tests/test_library_paging.py tests/test_endpoints.py -q` (from repo root)
Expected: all PASS (script is client-side; server output unchanged except attributes).

- [ ] **Step 5: Commit.**

```bash
git add templates/library.html templates/partials/item_card.html
git commit -m "feat: desktop drag-and-drop tag assignment"
```

---

### Task 9: Touch tap-to-assign mode

**Files:**
- Modify: `templates/library.html` (mode toggle + hint bar + tap logic, same script block)

Design note: chips stay plain filter links normally. A "🏷 Tag" toggle next to the search bar enters tagging mode; in that mode chip taps arm a tag instead of navigating, and card taps toggle it. This preserves filter navigation and needs no hover.

- [ ] **Step 1: Add toggle + hint bar.** Insert after the search bar block:

```html
<div class="flex items-center gap-2">
  <button id="tag-mode-toggle" aria-pressed="false"
          class="shrink-0 px-3 py-2 rounded-full text-xs font-medium bg-white border border-slate-200 text-slate-600">🏷 Tag</button>
  <p id="tag-mode-hint" class="hidden text-xs text-slate-500">Tap a tag, then tap articles to add it. Tap again to stop.</p>
</div>
```

- [ ] **Step 2: Extend the Task 8 script** (same IIFE, append before `})();`):

```js
  var armedTag = null, tagMode = false;
  var toggle = document.getElementById('tag-mode-toggle');
  var hint = document.getElementById('tag-mode-hint');
  function setMode(on) {
    tagMode = on; if (!on) armedTag = null;
    toggle.setAttribute('aria-pressed', on ? 'true' : 'false');
    toggle.className = 'shrink-0 px-3 py-2 rounded-full text-xs font-medium ' +
      (on ? 'bg-slate-900 text-white' : 'bg-white border border-slate-200 text-slate-600');
    hint.classList.toggle('hidden', !on);
    document.querySelectorAll('[data-tag-source]').forEach(function (el) {
      el.classList.toggle('ring-2', on && el.getAttribute('data-tag-source') === armedTag);
      el.classList.toggle('ring-slate-900', on && el.getAttribute('data-tag-source') === armedTag);
    });
  }
  if (toggle) toggle.addEventListener('click', function () { setMode(!tagMode); });
  document.querySelectorAll('[data-tag-source]').forEach(function (el) {
    el.addEventListener('click', function (e) {
      if (!tagMode) return; // normal filter navigation
      e.preventDefault();
      var t = el.getAttribute('data-tag-source');
      armedTag = (armedTag === t) ? null : t;
      hint.textContent = armedTag
        ? 'Tap articles to add #' + armedTag + '. Tap #' + armedTag + ' again to stop.'
        : 'Tap a tag, then tap articles to add it. Tap again to stop.';
      setMode(true);
    });
  });
  document.getElementById('items-list').addEventListener('click', function (e) {
    if (!tagMode || !armedTag) return;
    if (e.target.closest('a, button')) return; // links/buttons keep working
    var card = e.target.closest('[data-item-id]');
    if (!card) return;
    var has = (card.getAttribute('data-tags') || '').split(',').indexOf(armedTag) !== -1;
    var vals = {};
    vals[has ? 'remove' : 'add'] = armedTag;
    htmx.ajax('POST', '/items/' + card.getAttribute('data-item-id') + '/tags',
      { target: '#' + card.id, swap: 'outerHTML', values: vals });
  });
  // New searches start on page 1.
  var searchInput = document.getElementById('search-input');
  if (searchInput) searchInput.addEventListener('input', function () {
    document.getElementById('active-page-input').value = '1';
  });
```

- [ ] **Step 3: Run suites.**

Run: `python3 -m pytest tests/test_library_paging.py tests/test_endpoints.py -q` (from repo root)
Expected: all PASS.

- [ ] **Step 4: Commit.**

```bash
git add templates/library.html
git commit -m "feat: touch tap-to-assign tagging mode"
```

---

### Task 10: PWA verification, full suite, lint

**Files:** none (verification only unless issues surface)

- [ ] **Step 1: Full suite.**

Run: `python3 -m pytest tests/ -q` (from repo root)
Expected: all pass (baseline before this plan: 193 passed, 1 skipped).

- [ ] **Step 2: CI lint, exact form.**

Run: `ruff check src/ tests/ --select=E,W,F,I,N` then `ruff format --check src/ tests/`
Expected: `All checks passed!` and no `Would reformat` lines. If reformatting is needed, run `ruff format` on the listed files and re-run Step 1 for the touched areas.

- [ ] **Step 3: PWA spot-checks** (by hand, in a 360px viewport + installed PWA if available):
  - Pager never overflows horizontally; page buttons collapse to Prev/numbers/Next.
  - All pager buttons, per-page select, tag toggle, and pill × meet 44px touch targets.
  - Pager sits above the bottom tab bar with daylight between them; safe-area inset intact.
  - Focusing the per-page select does not auto-zoom iOS (16px floor kept).
  - Turning pages preserves tag/status/search filters in the pushed URL.

- [ ] **Step 4: Push.** Only when Steps 1–3 are green (pushing `main` deploys to production):

```bash
git push origin main
```

---

## Self-review

- Spec coverage: pagination engine (§1) → Tasks 1–4; tag endpoint (§2) → Task 5; desktop DnD → Task 8; touch mode → Task 9; PWA rules (§3) → pager markup (Task 3), Task 10 step 3; testing (§4) → Tasks 1, 2, 6, 10. Non-goals respected (no persistence, no new deps).
- Placeholders: none — every step has exact code, paths, commands, expected output. The one expositional branch (Task 6 `_login` fallback) states the exact replacement.
- Type consistency: `hybrid_search(...) -> tuple[list, int]` used identically in Tasks 1, 2, 4; pager context keys (`page`, `per_page`, `total_pages`, `pages`, `shown_from`, `shown_to`, `push_base`, `total`, `per_page_options`) identical in Tasks 3 and 4; card ids `item-card-{id}` and `data-item-id`/`data-tags`/`data-pill` identical in Tasks 5–9.
