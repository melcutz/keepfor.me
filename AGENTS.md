# AGENTS.md

FastAPI read-it-later app deployed as a Cloudflare **Python Worker** (`compatibility_flags: ["python_workers"]`, Pyodide). Bindings: D1 (SQLite + FTS5), R2, Vectorize, Workers AI, Queues.

## Commands

```bash
python3 -m pytest tests/ -q                          # 90 passed, 1 skipped, ~16s
python3 -m pytest tests/test_crypto.py::test_password_hashing   # single test
python3 -m pytest tests/ -k crypto -q                # filter

# These must match CI EXACTLY (see trap below)
ruff check src/ tests/ --select=E,W,F,I,N
ruff format --check src/ tests/
```

- **Run pytest from the repo root.** From any subdirectory it dies with `ModuleNotFoundError: No module named 'src'` — `src` has no `__init__.py` and resolves as a namespace package only when the root is on `sys.path`. `pythonpath = src` in `pytest.ini` is not what makes it work.
- All tests are **module-level functions**; there are no `Test*` classes. Node IDs are `tests/test_x.py::test_name`.
- Deploy locally / to prod: `uvx --from workers-py pywrangler dev` / `... deploy`.

## What CI actually gates

Only the `lint` (ruff check **and** format) and `test` (pytest) jobs can fail a build. `mypy`, `bandit`, and `coverage-check` all end in `|| true` — advisory only. There is no ruff/mypy config section in `pyproject.toml`, so bare `ruff check .` uses different rules than CI.

**Trap:** `ruff check .` **passes** on unsorted imports that CI's `--select=...,I` rejects (verified: `I001`). Always run the CI form locally.

**Trap:** `coverage-check` is named "80%+" but runs `--cov-fail-under=75` *and* is `|| true`. Coverage never blocks anything.

**`ruff format` is load-bearing** — format your code before pushing or CI fails.

## Pushing to main deploys to production

`deploy.yml` runs on every push to `main` and publishes to `app.keepfor.me/*` (also reachable at `keepfor-me.<subdomain>.workers.dev`; `workers_dev` stays on). Don't push casually. Needs `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` secrets.

**The post-deploy smoke test is useless as written — read this before trusting a green deploy.** It is `continue-on-error: true`, *and* it curls `https://keepfor.me/` (`deploy.yml:72`), the bare apex, which has no DNS record and returns `000`. Verified 2026-10-03: `app.keepfor.me` → 200, `*.workers.dev` → 200, `keepfor.me` → no response. So it cannot fail and it tests a dead URL. Check the deploy job log yourself, and repoint line 72 at `app.keepfor.me` next time you touch the workflow.

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

Residual known waste: ~1725 `.py` files (~19 MiB) from `.venv-workers`, plus `tests/*.py` and `browser-extension/`, still ship because `PythonModule` can't be shadowed. Removing them requires narrowing `base_dir` so it doesn't contain the venv, which means moving `worker.py` + `src/` + `templates/` into a subdirectory (this changes the packaged import path from `src.*` to `worker.src.*`). **Untested**: the payoff would be faster bundle *decompress* at cold start, not import time — those files are never imported.

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
| `DB` (`src/app.py:154`, `src/consumer/processor.py:245` and `:304`) | `keepfor_me_db` — **mismatch** |
| `BUCKET`, `VECTORIZE`, `AI`, `QUEUE` | match |

Both call sites fall back through `DB` → `keepfor_me_db` → `D1`, so this mismatch is
currently harmless — but keep all three names in the chain if you touch it.

`get_db()` looks up `env.DB`, but wrangler binds the D1 database as `keepfor_me_db`, so `d1_binding` resolves to `None` in production and `Database` raises `RuntimeError("No database connection available")`. **The test suite cannot catch this** — `tests/test_endpoints.py` monkeypatches `src.app.get_db` with a sqlite fixture, so no real `env` is ever built.

R2 is also declared twice (`BUCKET` and `keepfor_me_bucket`); only `BUCKET` is referenced by code.

`wrangler.jsonc` has no `database_id` for D1 (only `"remote": true`) — you must paste the id from `npx wrangler d1 create` before deploying.

## Configuration: `src/config.py` is dead code

Nothing imports it. `AppConfig`, its `.env` loading, `rate_limit_*`, and `max_import_*` are all unused. To add a setting, follow the existing pattern instead: declare it under `vars` in `wrangler.jsonc` and read it with `getattr(env, "NAME", default)` in the request handler. Wiring a new value into `AppConfig` will silently do nothing.

`SESSION_SECRET` in `wrangler.jsonc` is **never read**. Sessions are opaque random tokens (`secrets.token_hex(32)`) stored in the `sessions` table — not signed cookies.

## Auth invariants

- The **first** user to register becomes `admin`; afterwards registration raises `RegistrationClosedError` unless the `ALLOW_PUBLIC_SIGNUPS` var is the *string* `"true"` (`src/app.py:719` does a literal string compare).
- `GET /auth/login` and `GET /auth/register` **redirect (303) to `_safe_next(next)` when already signed in**, so the forms never render for an authenticated visitor. Any new auth page should do the same.
- Passwords: PBKDF2-HMAC-SHA256, 100k iterations, stored as `salt$hex`.
- PATs are prefixed `kfm_live_` / `rk_live_` and stored as a SHA-256 hash — never the raw token.

## Data layer

`src/models/db.py` is a 71-line dual-backend adapter: D1 in production, `sqlite3` in tests. It exposes only `query_all`, `query_first`, `execute`, `execute_batch`. **If you add a method, implement both branches** — the sqlite branch will otherwise be dead in prod and untested in CI.

## Testing quirks

- `tests/conftest.py` hardcodes `migrations/0001_initial_schema.sql`. It will **not** pick up a new `0002_*.sql`, so schema changes made for D1 won't reach tests. Update the fixture or the initial schema when you change tables.
- The fixture loads the schema by splitting the file on `;` — no `;` inside string literals or trigger bodies.
- Vectorize, Workers AI, R2, and Queue branches are **never executed in tests** (there is no Cloudflare `env`); they are guarded by `hasattr(env, ...)` / `is not None` checks. `src/consumer/processor.py` and `src/models/items.py` are effectively untested — review those by hand.
- Entrypoint signatures in `src/worker.py` must accept the runtime's full dispatch: `fetch(self, request, env=None, ctx=None)`, `queue(self, batch, env=None, ctx=None)`. A narrower `queue(self, batch)` crashed **every** prod delivery with `TypeError: ... takes 2 positional arguments but 4 were given` (2026-10-03): 998 ingested, ~808 acked-and-dropped, zero items processed, zero `failed` rows. The `workers` package (and the failure) exists only on the runtime — `tests/test_worker_entrypoint.py` pins the contract but skips everywhere except prod.
- `search_fts` and `search_vectorize` wrap their bodies in bare `except Exception` (`src/search/engine.py:37` and `:102`). Search **degrades silently to empty results** instead of raising. When search returns nothing, read the logs rather than expecting a traceback.

## Repo hygiene traps

- 16 `*.pyc` files and `.DS_Store` are still **tracked** despite `.gitignore` listing them. `.gitignore` does not untrack anything — use `git rm --cached`.- `pylock.toml`, `python_modules/`, and `.venv-workers/` are gitignored Workers build artifacts (~200 MB on disk). The deployed dependency set is therefore **not locked in git**; `pyproject.toml` ranges are the only constraint.
- `.wrangler/` (local dev-server state) is **not** gitignored, so `git status` stays dirty after any `pywrangler dev`. Don't commit it.
- `mcp-cli/keepforme_mcp.py` is a legacy 12-line shim that duplicates the `keepfor-me-mcp` / `keepforme-mcp` console scripts (`src.mcp.cli:main`). Prefer `pyproject.toml` entrypoints.
- CI runs Python 3.11; local venvs are 3.12/3.14. `src/utils/logging.py:39` uses `datetime.utcnow()`, deprecated on 3.12+ and noisy in test output.

## Layout

`worker.py` (root, the deploy entrypoint) → `src/worker.py` → `src/app.py` (FastAPI, 30 routes, owns HTML + JSON endpoints). Supporting packages: `src/auth`, `src/consumer` (extraction + queue processing), `src/models` (data access), `src/search` (RRF hybrid search), `src/utils`, `src/mcp` (JSON-RPC server + CLI). Jinja templates in `templates/`, static assets in `static/`, Manifest V3 extension in `browser-extension/`.

Icons, PWA icons, and the web manifest are served by **dynamic Python routes** (`/favicon.ico`, `/icon-192.png`, `/icon-512.png`, `/icon-maskable.png`, `/manifest.webmanifest`) with bytes embedded in `src/pwa_icons.py` / `src/app.py` — deliberately no static files, so there is zero bundle impact and it works identically in the Worker, local dev, and TestClient.

## Performance: cold starts dominate latency

Measured on prod: page loads are **~250-300ms warm but spike to 1.5-3.5s on ~15% of requests** (isolate cold starts). Compression is fine — a 200KB library page is 14KB on the wire. D1 queries are fast individually. Do not chase payload or query tuning first; look at what a cold start loads.

- **Heavy parsers must stay out of the module-scope import path.** `src/worker.py` imports the consumer chain at module scope, so an eager `import trafilatura` / `from bs4 import BeautifulSoup` in `src/consumer/extractor.py` **or `src/utils/importer.py`** (imported by `src/app.py`) makes *every* request pay for the parsing stack — including `/auth/login`. Both now import lazily via `_load_parsers()` / inside `parse_netscape_bookmarks()`. This was worth **0.46s → 0.35s** of import time in the Workers venv. `tests/test_mcp.py::test_heavy_extraction_libs_are_not_imported_at_module_load` pins it (verified it fails if an eager import returns).
- Measure import cost in the real venv, not the local one: `.venv-workers/bin/python -c "import src.worker"`.
- Hybrid search runs FTS and the Workers AI embedding **concurrently** (`asyncio.gather`), and both the embedding and the vector query are bounded by `VECTOR_SEARCH_TIMEOUT` (5s) so a hanging AI binding degrades to keyword-only instead of hanging the request.
- Untested lever: the 1725 `.venv-workers` modules still ship. Trimming them requires narrowing `base_dir` (see bundle section) — the win is faster bundle decompress at cold start, **not** import time, since those files are never imported.

## Form endpoints must return HTML, not JSON

Several endpoints are submitted by HTML forms or swapped by htmx. Returning `JSONResponse` from one produces a raw-JSON page in the browser. Each of these bit us once already:

- `POST /settings/tokens` → re-renders `settings.html` with `new_token` (was `201` JSON, dumped the secret token as a page).
- `POST /import` → `303` redirect (was expecting `content`/`format` form fields that no real browser sends; the file upload 422'd). Now reads the uploaded `file`, picks CSV vs Netscape by extension, and fans out via queue messages of 25 bookmarks — a 900-item import inside one request froze the tab and would blow the Worker request limit.
- `DELETE /api/items/{id}` → empty HTML body (JSON was rendered as the htmx `outerHTML` replacement text).
- `GET /items/{id}` on a missing item → styled HTML 404, not `{"detail":"Item not found"}`.

Keep the hidden `source` field on `save_popup.html`: share-target/PWA pages **cannot** `window.close()`, so they render a static done panel and let the user swipe back, while the bookmarklet popup still auto-closes.