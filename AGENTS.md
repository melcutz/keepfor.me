# AGENTS.md

FastAPI read-it-later app deployed as a Cloudflare **Python Worker** (`compatibility_flags: ["python_workers"]`, Pyodide). Bindings: D1 (SQLite + FTS5), R2, Vectorize, Workers AI, Queues.

## Commands

```bash
python3 -m pytest tests/ -q                          # 319 passed, 9 skipped, ~100s
python3 -m pytest tests/isolation -q                 # tenant-isolation suite (59 passed)
python3 -m pytest tests/test_crypto.py::test_password_hashing   # single test
python3 -m pytest tests/ -k crypto -q                # filter

# These must match CI EXACTLY (see trap below)
ruff check keepfor/ tests/ --select=E,W,F,I,N
ruff format --check keepfor/ tests/
```

- **Run pytest from the repo root.** All tests are **module-level functions**; there are no `Test*` classes. Node IDs are `tests/test_x.py::test_name`.
- Local dev / deploy to prod: `uvx --from workers-py pywrangler dev --config wrangler.local.jsonc` (never run dev against `wrangler.jsonc` without `--config`) / manual deploy via `workflow_dispatch` or `pywrangler deploy`.

## What CI actually gates

Only the `lint` (ruff check **and** format) and `test` (pytest) jobs can fail a build. `mypy`, `bandit`, and `coverage-check` all end in `|| true` — advisory only. There is no ruff/mypy config section in `pyproject.toml`, so bare `ruff check .` uses different rules than CI.

**Trap:** `ruff check .` **passes** on unsorted imports that CI's `--select=...,I` rejects (verified: `I001`). Always run the CI form locally.

**Trap:** `coverage-check` is named "80%+" but runs `--cov-fail-under=75` *and* is `|| true`. Coverage never blocks anything.

**`ruff format` is load-bearing** — format your code before pushing or CI fails.

## Deploys are manual (no auto-deploy on push)

`deploy.yml` requires manual triggering via `workflow_dispatch` with input `confirm: "deploy"` (or local `pywrangler deploy`). Deploys are manual: self-hosters deploy to their own Cloudflare account; the owner deploys by running the workflow by hand. Pushing to `main` does NOT auto-deploy. Needs `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` secrets.

**Warning about `"remote": true`:** `wrangler.jsonc` has `"remote": true` on D1, R2, and Vectorize bindings with the real production `database_id`. Never run local dev against `wrangler.jsonc` as it reads and writes remote production resources! For local simulation, copy `wrangler.local.example.jsonc` to `wrangler.local.jsonc` and use `pywrangler dev --config wrangler.local.jsonc`.

## Open-core rules

- core never imports from the private cloud repo
- extension happens only through `keepfor/spi.py`
- every SQL touching a tenant table must be scoped by `user_id` (CI enforces)
- every change to `spi.py` bumps `CORE_API_VERSION` when it is not backward compatible
- deploys are manual

## Bundle size: the deploy will fail with "exceeded 64 MiB" if you aren't careful

**Always measure before deploying.** Dry run costs nothing and uploads nothing:

```bash
uvx --from workers-py pywrangler deploy --dry-run   # prints the module table + Total
```

Look at the `Total (N modules)` row. Healthy is roughly **51,000 KiB / 4475 modules** (currently 4488 / ~52,600 KiB). If `.venv-workers` paths appear in the module table, the build venv is being bundled and the deploy is one dependency bump away from failing.

### Why this happens

`main` is `worker.py`, so wrangler force-appends a catch-all bundling rule:

```js
if (mainModule.endsWith(".py")) config.rules.push({ type: "PythonModule", globs: ["**/*.py"] })
```

It then walks every file under `base_dir` and keeps whatever matches a rule. `getFiles()` is a blind recursive walk with **no `.gitignore` awareness** (its only hardcoded skip is `.wrangler/`). Because `base_dir` is `"."`, that walk covers the whole repo, and pywrangler hardcodes the build venv to `<project_root>/.venv-workers` (`sync.py`). So every `.py` in the 200 MB build venv ships to Cloudflare.

### What you can and cannot exclude via `rules`

Rules are **include-only** (see `matchFiles`); they exclude by *shadowing* a later rule of the same `type`, which requires `fallthrough: false`.

- **`Text` is excludable.** The built-in default is `{type:"Text", globs:["**/*.txt","**/*.html","**/*.sql"]}`. The current config shadows it with `globs:["**/*.html","**/*.sql"]`, which is what keeps ~6.4 MiB of venv `.txt` files (`tld/res/*.dat.txt`) out of the bundle. Keep `fallthrough: false` on that rule or the shadowing silently stops working.
- **Wrangler version sensitivity (read before touching `deploy.yml`).** Wrangler 4.86.0 turns any file matching a *shadowed* rule into a hard deploy error (`matched a module rule ... not marked as fallthrough = true`), which breaks this exact setup on the first venv `.txt` it walks (e.g. `annotated_doc-*.dist-info/entry_points.txt`). Upstream `workers-sdk#14257`, fixed in later 4.x. CI pins `wrangler@4.147.0` (verified locally: dry-run passes, 4475 modules / ~52,500 KiB, under the 58,000 KiB budget gate). Do not float the pin or regress it to 4.86.0. `fallthrough: true` is NOT an alternative — it bundles the venv `.txt` (+~6.4 MiB) and blows the budget.
- **`PythonModule` is NOT excludable.** `config-schema.json` lists it as a valid rule type, but wrangler's runtime `validateRule` hardcodes only `ESModule, CommonJS, CompiledWasm, Text, Data`. Putting `PythonModule` in `rules` is rejected at config-parse time with a message that misleadingly says *"bindings should have a string type field..."*. That is schema/runtime drift in wrangler, not a mistake in your config.
- `python_modules/` is **not** affected by `rules` at all. It is added separately with its own `Data **/*` ruleset and filtered only by `python_modules.exclude`. So trimming `rules` never breaks vendored dependencies, and trimming `python_modules.exclude` never affects the repo scan.

CI's `deploy.yml` runs this same dry run and hard-fails above a **58,000 KiB** budget (a warning if `.venv-workers` still appears). Current headroom is ~5,400 KiB.

Residual known waste: ~1725 `.py` files (~19 MiB) from `.venv-workers`, plus `tests/*.py` and `browser-extension/`, still ship because `PythonModule` can't be shadowed. Removing them requires narrowing `base_dir` so it doesn't contain the venv, which means moving `worker.py` + `keepfor/` + `templates/` into a subdirectory (this changes the packaged import path from `keepfor.*` to `worker.keepfor.*`). **Untested**: the payoff would be faster bundle *decompress* at cold start, not import time — those files are never imported.

**Trap:** never put a git worktree under the repo root. `getFiles()` walks `.worktrees/` too, so a worktree with its own venv ships thousands of extra modules — verified 2026-10-04: a stale merged `ui-pwa-redesign` worktree took the bundle from ~52,700 KiB to 81,778 KiB and failed the deploy with `10021 multipart: message too large`. Create worktrees outside the repo (`git worktree add ../<name>`) and remove them once merged (`git worktree remove <path>`).

### Reading a failed deploy

`wrangler` writes a full module table to `~/.config/.wrangler/logs/wrangler-<ts>.log`. To find the culprit fast:

```bash
L=$(ls -t ~/.config/.wrangler/logs/*.log | head -1)
grep -oE '│ [a-zA-Z0-9_.-]+/' "$L" | sort | uniq -c | sort -rn   # which top-level dirs shipped
grep -c '\.venv-workers' "$L"                                   # is the build venv in there?
```

The table is **truncated**, so counts there are a lower bound; trust the `Total (N modules)` row for the real number. Note wrangler's reported size excludes per-module multipart overhead, so a total that looks under 64 MiB can still be rejected by Cloudflare.

## Cloudflare binding names (mismatch — read this before touching DB code)

Code reads bindings by name via `env.X` / `getattr(env, "X", None)`:

| Name used in code | Declared in `wrangler.jsonc` |
|---|---|
| `DB` (`keepfor/app.py:194`, `keepfor/consumer/processor.py:393` and `:452`) | `keepfor_me_db` — **mismatch** |
| `BUCKET`, `VECTORIZE`, `AI`, `QUEUE` | match |

Both call sites fall back through `DB` → `keepfor_me_db` → `D1`, so this mismatch is
currently harmless — but keep all three names in the chain if you touch it.

`get_db()` looks up `env.DB`, but wrangler binds the D1 database as `keepfor_me_db`, so `d1_binding` resolves to `None` in production and `Database` raises `RuntimeError("No database connection available")`. **The test suite cannot catch this** — `tests/test_endpoints.py` monkeypatches `keepfor.app.get_db` with a sqlite fixture, so no real `env` is ever built.

R2 is also declared twice (`BUCKET` and `keepfor_me_bucket`); only `BUCKET` is referenced by code.

`wrangler.jsonc` binds the production D1 database ID (`46107ebb-e820-41e2-92f1-089c7eac4fbf`) with `"remote": true`.

## Configuration: `keepfor/config.py` is dead code

Nothing imports it. `AppConfig`, its `.env` loading, `rate_limit_*`, and `max_import_*` are all unused. To add a setting, follow the existing pattern instead: declare it under `vars` in `wrangler.jsonc` and read it with `getattr(env, "NAME", default)` in the request handler. Wiring a new value into `AppConfig` will silently do nothing.

`SESSION_SECRET` in `wrangler.jsonc` is **never read**. Sessions are opaque random tokens (`secrets.token_hex(32)`) stored in the `sessions` table — not signed cookies.

## Auth invariants

- The **first** user to register becomes `admin`; afterwards registration raises `RegistrationClosedError` unless the `ALLOW_PUBLIC_SIGNUPS` var is the *string* `"true"` (`keepfor/app.py:1611` does a literal string compare).
- `GET /auth/login` and `GET /auth/register` **redirect (303) to `_safe_next(next)` when already signed in**, so the forms never render for an authenticated visitor. Any new auth page should do the same.
- Passwords: PBKDF2-HMAC-SHA256, 100k iterations, stored as `salt$hex`.
- PATs are prefixed `kfm_live_` / `rk_live_` and stored as a SHA-256 hash — never the raw token.

## Data layer

`keepfor/models/db.py` is a 71-line dual-backend adapter: D1 in production, `sqlite3` in tests. It exposes only `query_all`, `query_first`, `execute`, `execute_batch`. **If you add a method, implement both branches** — the sqlite branch will otherwise be dead in prod and untested in CI.

- **Migrations (`migrations/*.sql`)**: Fifteen schema files exist:
  - `0001_initial_schema.sql` (core tables, FTS5 virtual table, triggers)
  - `0002_rate_limits.sql` (rate limiting table)
  - `0003_suggested_tags.sql` (suggested tags table)
  - `0004_tag_rules.sql` (auto-tagging rules table)
  - `0005_performance_indexes.sql` (composite indexes for library feeds and lookups)
  - `0006_notes_pins_and_metadata.sql`, `0007_item_opens.sql` (note columns, read tracking)
  - `0008_import_sources.sql` (`items.source`/`source_id` + the dedupe index)
  - `0009_mcp_v2_metadata.sql` (provenance columns), `0010_tag_metadata.sql` (tag columns)
  - `0011_import_status.sql` (`import_status`: one row per notes upload with the counters and a capped error list)
  - `0012_pat_scopes.sql` (`personal_access_tokens.scopes`: the nullable scope column of the read-only tokens)
  - `0013_highlights_and_collections.sql` (`highlights`, `collections`, `collection_items`: the MCP highlights and collections tools)
  - `0014_trash.sql` (`items.deleted_at` + the `(user_id, deleted_at)` index: the Trash)
  - `0015_search_metadata.sql` (backfills the summary and the page description into the FTS rows: the keyword side of the search)
- **`deploy.yml` does NOT apply migrations**: Schema migrations must be applied manually to production via `npx wrangler d1 migrations apply keepfor-me-db --remote`.
- **Composite indexes are mandatory for sorted feeds**: SQLite's query planner requires composite indexes matching the `WHERE` + `ORDER BY` pattern (e.g. `items(user_id, created_at DESC)` and `items(user_id, status, created_at DESC)`). Without them, SQLite performs `USE TEMP B-TREE FOR ORDER BY`, incurring noticeable CPU and memory overhead on library page loads. Check `EXPLAIN QUERY PLAN` on any new list query.
- **The keyword side of the search covers the tags and the summary** (issue #55): FTS5 indexes the title and the extracted text, so an item that names the concept only in a tag or in its summary had no keyword hit and the hybrid rule dropped it. The tags are matched live (`search_tag_hits` in `keepfor/search/engine.py`: word boundaries, case-insensitive, like the FTS tokenizer) and the summary and the page description are indexed with the text. The hybrid rule itself is unchanged: a semantic-only hit still needs >= 0.80 and a near-top score.
- **Only the content words of a query are a keyword hit** (issue #63): the words of the library ("saved", "my", "library", "bookmark") and the stopwords are in the text of every item of a library, so a keyword hit on one of them proves nothing. `_content_terms()` in `keepfor/search/engine.py` drops them from the FTS terms, from the tag match and from the whole-word search of the semantic-only mode (ported from keepfor-cloud#142). A query of these words only ("library") falls back to its raw words, so it still finds the item titled with it. The embedding still uses the raw query.
- **A content word matches its inflected forms** (issue #76): a query word of at least `MIN_PREFIX_LEN` (3) letters matches every word that starts with it, in the text, in the tags and in the whole-word search — "llm" finds "LLMs", "model" finds "models". A shorter word matches exactly ("ai" does not match "air"). `_keyword_match()` in `keepfor/search/engine.py` builds the FTS match, `search_tag_hits` does the same for the tag words. The index already stems with the `porter unicode61` tokenizer (`migrations/0001_initial_schema.sql`), so no migration is needed — but the tag names are not indexed, hence the rule in Python.
- **An item that holds only some of the words of a query is a partial match** (issue #87): "local LLM" finds the item that holds "local" but not "LLM" (the open-webui save) after the item that holds both of them. `search_fts()` in `keepfor/search/engine.py` matches the content words one by one (`_word_hits()`: the text, the title and the tags of one word), counts the words an item holds and sorts the candidates by that count — the full matches first, the partial ones after them, the ones that hold more words first. The bm25 score of the words adds up, so the score of an item is the score of the whole query. Before #87 the union of the words was a fallback that only ran when no item held every word, so an item that held both words hid the one that held only one. The filler words are no partial match: a query that names no content ("my saved") matches as a whole, and a nonsense query ("zxqvbnm") still finds nothing.
- **The Trash is invisible to the rest of the app**: `items.deleted_at` marks a trashed item (soft delete, issue #30). Every read path must filter `deleted_at IS NULL` — library, search (FTS *and* vector results), MCP tools, MCP resources/prompts, the `find_cleanup` report, counts, tag stats, exports, highlights and collections. Only `list_trash`, the restore path and `purge_trash` may see trashed rows. A trashed item keeps its row, its R2 snapshots and its vectors, so the vector search must filter them out explicitly.
- **The purge needs the tenant scope** (issue #37): `purge_trash(db, env, user_id=None, *, older_than_days=30, scope=None, limit=1000)` refuses an unscoped call when the app runs with a multi-tenant scope provider (`keepfor.runtime.is_multi_tenant()`) — the default scope would delete the wrong R2 prefix and the wrong Vectorize namespace, orphaning the tenant's files. With a scope the purge is limited to the scope's own user, because a shared database holds the rows of every tenant. `list_purgeable_items(db, user_id, older_than_days, limit)` holds the selection SQL, so a host never copies it.
- **The permanent delete is one function**: `delete_item` in `keepfor/models/items.py` is the only path that destroys item data (the Trash's "Delete forever", the 30-day purge and the settings cleanup all come through it). It calls `purge_item_children`, which drops the highlights and the collection links of the item. The trash and the restore never run it, so a restored item keeps them; `ITEM_EXTRA_DELETES` is the same extension point for any other per-item table.
- **Batching mutations**: D1 executes queries over remote RPC. Multi-row or cascading updates (such as updating tag associations or deleting an item with related tag mappings and vector embeddings) must use `db.execute_batch()` rather than looping individual `db.execute()` calls to prevent sequential RPC latency.

## Testing quirks

- `tests/conftest.py` now loads **every** `migrations/*.sql` in filename order (it previously hardcoded `0001`, which silently hid `0002_rate_limits.sql` from the entire suite). The fixture strips `--` comment lines before splitting on `;`, so a semicolon inside a SQL comment no longer splits mid-comment and produces unparseable SQL.
- Still true of the splitter: no `;` inside **string literals** or trigger bodies. Comment handling is safe; quoted text is not.
- Vectorize, Workers AI, R2, and Queue branches are **never executed in tests** (there is no Cloudflare `env`); they are guarded by `hasattr(env, ...)` / `is not None` checks. `keepfor/consumer/processor.py` and `keepfor/models/items.py` are effectively untested — review those by hand.
- Entrypoint signatures in `keepfor/worker.py` must accept the runtime's full dispatch: `fetch(self, request, env=None, ctx=None)`, `queue(self, batch, env=None, ctx=None)`. A narrower `queue(self, batch)` crashed **every** prod delivery with `TypeError: ... takes 2 positional arguments but 4 were given` (2026-10-03): 998 ingested, ~808 acked-and-dropped, zero items processed, zero `failed` rows. The `workers` package (and the failure) exists only on the runtime — `tests/test_worker_entrypoint.py` pins the contract but skips everywhere except prod.
- `search_fts` and `search_vectorize` wrap their bodies in bare `except Exception` (`keepfor/search/engine.py:169`, `:190`, `:288`). Search **degrades silently to empty results** instead of raising. When search returns nothing, read the logs rather than expecting a traceback.

## Repo hygiene traps

- `pylock.toml`, `python_modules/`, and `.venv-workers/` are gitignored Workers build artifacts (~200 MB on disk). The deployed dependency set is therefore **not locked in git**; `pyproject.toml` ranges are the only constraint.
- `/api/mcp` is a remote stateless Streamable HTTP MCP endpoint (no sessions, no SSE, Bearer PAT auth). There is deliberately no local stdio proxy/CLI anymore — `keepfor/mcp/cli.py`, the `pyproject.toml` console scripts, and the `mcp-cli/` shim were removed.

## Layout

`worker.py` (root, the deploy entrypoint) → `keepfor/worker.py` → `keepfor/app.py` (FastAPI, 32 routes, owns HTML + JSON endpoints). Supporting packages: `keepfor/auth`, `keepfor/consumer` (extraction + queue processing), `keepfor/models` (data access), `keepfor/search` (RRF hybrid search), `keepfor/utils`, `keepfor/mcp` (stateless Streamable HTTP JSON-RPC server: `process_mcp_request` returns a payload for requests, `None` for notifications → 202). Jinja templates in `templates/`, static assets in `static/`, Manifest V3 extension in `browser-extension/`.

Icons, PWA icons, and the web manifest are served by **dynamic Python routes** (`/favicon.ico`, `/icon-192.png`, `/icon-512.png`, `/icon-maskable.png`, `/manifest.webmanifest`) with bytes embedded in `keepfor/pwa_icons.py` / `keepfor/app.py` — deliberately no static files, so there is zero bundle impact and it works identically in the Worker, local dev, and TestClient.

## Library views: the feed is the only list of items

Cards, List and Compact are **one list of items in three layouts** (issue #39). The layout is a URL parameter (`?view=cards|list|compact`): the server renders the requested layout, the client keeps the choice in `localStorage` and mirrors it into the URL.

- **No view may hide an item.** The feed shows every item the category/status/tag filters match — pinned and archived ones included — so the counts and the list always agree. A CSS rule that hides a card (`display: none`) breaks that silently; `tests/test_library_views.py` pins it.
- **There is no pinned shelf.** Pinned items are ordinary feed items, marked in the card (amber border + "Pinned" badge). A second copy of an item (the old cards-only shelf) breaks the count, and an unscoped shelf shows items the current filter excludes.
- **Pinned items lead the feed.** The browse feed is ordered by `is_pinned DESC, created_at DESC` — in all three branches of `get_recent_items` — so a pinned save opens page 1 in every view. Search stays relevance-ordered; `tests/test_library_pinned_order.py` pins both.
- **The layout travels with the URL.** An explicit `?view=` is carried into every URL the library page builds (pager, category tabs, status chips, tag filters, search form, clear search), so the layout survives a filter, a search or a page change; without one the links stay layout-free and the client applies `localStorage`. The switcher marks the rendered layout server-side (`aria-pressed`), and picking one rewrites the page's links and the hidden `view` field the htmx requests send; `tests/test_library_view_param.py` pins both.
- **A save comes back in the same layout** (issue #64). The Save Link form (`templates/base.html`, rendered on every page) carries a hidden `view` field: the library page fills it with its `view_param` (empty without an explicit `?view=`), and `syncViewLinks` in `templates/library.html` rewrites it when the switcher picks a layout. `/save` accepts that field and redirects to `/?view=<layout>` — only the three `LIBRARY_VIEW_PARAMS` values, so the URL never carries request data. A save from a page without a layout still redirects to `/`; `tests/test_save_keeps_view.py` pins both.

## The search state lives in the URL, not in the box

The library search is a URL parameter (`?q=`), like the view (issue #56). The server renders the query it is given; the client mirrors a live search into the URL and drops a value the browser restored on its own when the URL asks for no search.

- **Plain `/` is always the unfiltered library** with an empty search box. Nothing persists the last query: no cookie, no `localStorage`, no session. `tests/test_search_results.py` pins it.
- **A page with an active search shows a "Clear search" control** that returns to `/` without the query — keeping the layout, so `?view=cards` stays in Cards (`#clear-search` in `templates/library.html`).
- The live search posts to `/search` (htmx) but does **not** change the URL by itself — the `input` listener mirrors the query into `?q=` (debounced: the browser throttles `history.replaceState`). Without that, the URL says nothing about the search and the browser brings the last query back on the next navigation.

## Favicons are served same-origin (`/favicons/<domain>`)

Item cards used to point their `<img>` at a third-party favicon service. Those answer 404 for every domain without an icon, so every library page filled the browser console with failed requests (issue #61). The card now asks `/favicons/<domain>` instead, and that route **always answers 200**.

- `keepfor/utils/favicon.py` owns it: `normalize_host()` keeps only public hostnames (IP literals, `localhost`, `*.local`, `*.internal` are refused), `_fetch_upstream()` goes through `keepfor/utils/egress.py` (so the SSRF policy, the size cap and the timeout apply), and the R2 cache stores `favicons/<domain>` as base64 text (R2's binary put differs between runtimes, its text put does not).
- A domain with no icon gets the **transparent 1×1 PNG fallback**, so the card's letter tile shows through it. Never serve an upstream body that is not a raster image: an HTML error page would fail to decode in the browser, and SVG is refused because a script inside it runs when the URL is opened as a document.
- The route is **public**, like `/favicon.ico`: a session that expires between the page and its images must not turn into a 401 in the console.
- **Only a signed-in request may read the cache, fetch or store.** The route calls `deps.get_current_user()` (no 401) and passes `may_fetch` to `resolve_favicon()`; an anonymous request gets the **fallback, always** (same status, body, content type and `Cache-Control`, cached domain or not) — the cache is global, so a cached icon would tell a visitor whether some user's library holds that domain. Without the gate the route is an open fetch-and-store proxy for anyone.
- **Cache misses are rate limited** (keepfor-cloud#154): a miss makes us fetch an arbitrary domain and write an R2 object, so misses are capped per user and per IP (`FAVICON_MAX_MISSES_PER_USER` / `FAVICON_MAX_MISSES_PER_IP`, 60 per `FAVICON_MISS_WINDOW_SECONDS` = 10 min) in the same `rate_limits` table as the auth throttle. Over the cap: the fallback, no upstream fetch, no R2 write. A cache **hit** costs nothing.
- **Only a real fetched icon is cached.** The fallback is never written to R2 (so there is no hourly re-fetch of iconless domains), and a failed fetch writes nothing either. A legacy `fallback: true` entry is treated as a miss and refetched.
- The card must not point at any external favicon service — `tests/test_favicons.py` pins that for all three views.
- **A site's CDN can refuse our whole network** (keepfor-cloud#168): python.org answers **403 with an empty body** to every request that leaves Cloudflare, for every user agent, header set, redirect handling and retry — the icon is public, the CDN just does not serve us. The request shape is *not* the cause; do not chase the UA. When a site answers a refusal status (`REFUSED_STATUSES` in `keepfor/utils/favicon.py`), the icon is fetched from a public icon service instead (`FAVICON_ICON_SERVICE`, `""` turns it off). A 404 still means "no icon" and keeps the placeholder.

## Performance: cold starts dominate latency

Measured on prod: page loads are **~250-300ms warm but spike to 1.5-3.5s on ~15% of requests** (isolate cold starts). Compression is fine — a 200KB library page is 14KB on the wire. D1 queries are fast individually. Do not chase payload or query tuning first; look at what a cold start loads.

- **Heavy parsers must stay out of the module-scope import path.** `keepfor/worker.py` imports the consumer chain at module scope, so an eager `import trafilatura` / `from bs4 import BeautifulSoup` in `keepfor/consumer/extractor.py` **or `keepfor/utils/importer.py`** (imported by `keepfor/app.py`) makes *every* request pay for the parsing stack — including `/auth/login`. Both now import lazily via `_load_parsers()` / inside `parse_netscape_bookmarks()`. This was worth **0.46s → 0.35s** of import time in the Workers venv. `tests/test_mcp.py::test_heavy_extraction_libs_are_not_imported_at_module_load` pins it (verified it fails if an eager import returns).
- Measure import cost in the real venv, not the local one: `.venv-workers/bin/python -c "import keepfor.worker"`.
- Hybrid search runs FTS and the Workers AI embedding **concurrently** (`asyncio.gather`), and both the embedding and the vector query are bounded by `VECTOR_SEARCH_TIMEOUT` (5s) so a hanging AI binding degrades to keyword-only instead of hanging the request.
- **D1 latency waterfalls**: Each D1 query from a Worker isolate incurs an internal RPC roundtrip. Running independent queries sequentially adds cumulative latency (7 sequential queries = 150-250ms of overhead). Independent feed reads (`get_recent_items`, `count_recent_items`, `get_status_counts`, `get_user_tags`) are parallelized using `asyncio.gather`. Avoid redundant queries — e.g. `status_counts["all"]` is reused instead of issuing an extra `COUNT(*)` query.
- Untested lever: the 1725 `.venv-workers` modules still ship. Trimming them requires narrowing `base_dir` (see bundle section) — the win is faster bundle decompress at cold start, **not** import time, since those files are never imported.

## Form endpoints must return HTML, not JSON

Several endpoints are submitted by HTML forms or swapped by htmx. Returning `JSONResponse` from one produces a raw-JSON page in the browser. Each of these bit us once already:

- `POST /settings/tokens` → re-renders `settings.html` with `new_token` (was `201` JSON, dumped the secret token as a page).
- `POST /import` → `303` redirect (was expecting `content`/`format` form fields that no real browser sends; the file upload 422'd). Now reads the uploaded `file`, picks CSV vs Netscape by extension, and fans out via queue messages of 25 bookmarks — a 900-item import inside one request froze the tab and would blow the Worker request limit.
- `DELETE /api/items/{id}` → empty HTML body (JSON was rendered as the htmx `outerHTML` replacement text).
- `GET /items/{id}` on a missing item → styled HTML 404, not `{"detail":"Item not found"}`.

Keep the hidden `source` field on `save_popup.html`: share-target/PWA pages **cannot** `window.close()`, so they render a static done panel and let the user swipe back, while the bookmarklet popup still auto-closes.
## Same-origin only: no CORS middleware, on purpose

There is **no `CORSMiddleware`** in `keepfor/app.py`, and that is intentional. The app is same-origin, so the browser's own policy is the correct policy and needs no help.

The previous `allow_origins=["*"]` + `allow_credentials=True` combination made Starlette **reflect any `Origin`** with credentials allowed (verified in prod: `Origin: https://attacker.test` came back in `Access-Control-Allow-Origin`). Session-cookie reads were blocked only by `SameSite=Lax`. **Do not reintroduce a CORS allowlist** — `tests/test_endpoints.py::test_no_cross_origin_cors_headers` asserts no `Access-Control-*` header is ever emitted.

If a separate frontend origin is ever needed, proxy through the same origin instead. The browser extension does not depend on server CORS: MV3 grants its fetch via `host_permissions` in `browser-extension/manifest.json`, so **adding a host there is what keeps the extension working** — not a relaxed server.

## Auth rate limiting

`keepfor/utils/rate_limit.py` throttles `/auth/login` and `/auth/register`. Read it before changing anything about sign-in.

- **Counters live in D1, not memory.** Isolates are short-lived and numerous, so an in-memory counter is bypassed by spreading requests across them. Requires the `rate_limits` table from `migrations/0002_rate_limits.sql` — applied to remote D1 on 2026-10-03, but **`deploy.yml` does not apply migrations**, so a future schema file must be applied by hand (`npx wrangler d1 migrations apply keepfor-me-db --remote`).
- **Only failures count**, and a successful login clears the *account* counter so ordinary typos never lock the owner out. The IP counter intentionally survives.
- **Both keys are needed.** Per-IP (20) stops one host spraying many accounts; per-account (10) stops many IPs targeting one account, and it is the tighter cap so it binds first.
- **The check runs before `login_user`,** i.e. before PBKDF2, so a throttled client costs no CPU. Keep it that way.
- `client_ip()` trusts only `CF-Connecting-IP` (edge-set). It must never trust `X-Forwarded-For` — that is client-controllable and would mint a fresh limit per request.
- The limiter **fails open** on error. Login needs D1 anyway, but hard-failing would lock the owner out of their own instance over a defect in this file.
- Fixed 15-minute windows, not a sliding log: one indexed row per key per window. Boundary bursts are absorbed by the conservative limits.
- The same table and helpers serve the **favicon miss limiter** (`check_limit()` / `record_hits()` with their own window and caps, and their own `fav-user:` / `fav-ip:` key prefixes). The row cleanup keeps a window that can still be current for *any* limiter (the longest window is the horizon), so a favicon miss never deletes a live auth counter.

## CodeQL: `py/url-redirection` false positives on `_safe_next()`

Four `py/url-redirection` alerts fire on `RedirectResponse(url=_safe_next(next))` in `/auth/login` and `/auth/register`. They are **false positives** and are dismissed with that justification.

The query recognises sanitizers in a specific inline shape (`urlparse(x).netloc` / `.scheme` checks plus backslash elimination), but it **cannot infer sanitization through a user-defined wrapper function** — reported upstream in `github/codeql#15178` and hit by other projects for the same reason. The taint therefore reaches the sink as far as the query is concerned.

**Do not "fix" this by weakening `_safe_next()` to the shape the query models.** It was genuinely exploitable before `d3f6051`: browsers normalise `\` to `/` inside a `Location` header, so the old `startswith("/") and not startswith("//")` check let `?next=/\evil.com` through. That was confirmed in Chromium with Playwright (a 302 carrying `Location: /\evil.example` produced a real request to `http://evil.example/`), and a literal tab before `//` is a second, independent bypass. The current implementation rejects rather than mangles, and handles control characters too.

Regression coverage: `tests/test_endpoints.py::test_safe_next_rejects_open_redirect_payloads` (19 payloads) and `test_safe_next_keeps_real_relative_paths`; both fail if the old implementation is restored.

Prefer dismissing verified false positives with a justification. Do **not** add a `query-filters` exclusion for `py/url-redirection` — that would hide genuine future open redirects, which is how this one nearly shipped.

## Mobile capture & URL extraction

- **iOS WebKit PWA vs Android PWA**:
  - Android supports the W3C Web Share Target API (`share_target` in `manifest.webmanifest`), dispatching directly to `POST /save-popup` from the Android system share sheet.
  - iOS WebKit deliberately omits Web Share Target for PWAs. iOS relies on two complementary paths:
    1. **1-Tap Apple Shortcut**: Dispatches to `/save-popup?url=...&source=shortcut` or `POST` directly from the iOS Share Sheet. Uses the user's active Safari cookie session so no PAT / API token is required. When `source=shortcut`, the save confirmation page provides an interactive close/back panel (since web pages cannot `window.close()` iOS Safari tabs opened from shortcuts).
    2. **Smart Clipboard in PWA**: When installed as a standalone PWA, switching to Keepfor.me checks the clipboard (`navigator.clipboard.readText()`) on `visibilitychange` and `window.focus`. If a URL is detected, a floating toast appears offering 1-tap save.
- **URL extraction resiliency (`keepfor/utils/url.py:extract_url`)**:
  - Native mobile share actions often share text blobs combining article titles and URLs (e.g., `"Article Title: https://example.com/path?utm=..."`).
  - All save entry points (`/save`, `/save-popup`, `/api/items`) use `extract_url()` to extract and normalize valid URLs, strip surrounding punctuation/text/markdown, and prepend `https://` for bare domain strings.

## Agent skills

### Issue tracker

Issues live in GitHub Issues. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage roles, each label string equal to its name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout (one `GLOSSARY.md` + `docs/adr/` at root). See `docs/agents/domain.md`.
