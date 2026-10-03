# AGENTS.md

FastAPI read-it-later app deployed as a Cloudflare **Python Worker** (`compatibility_flags: ["python_workers"]`, Pyodide). Bindings: D1 (SQLite + FTS5), R2, Vectorize, Workers AI, Queues.

## Commands

```bash
python3 -m pytest tests/ -q                          # 45 tests, ~8s
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

`deploy.yml` runs on every push to `main` and publishes to `https://keepfor.me`, then curls the live site. Don't push casually. Needs `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` secrets. The post-deploy smoke test is `continue-on-error: true`, so it reports green even when the site is broken — check the deploy job log yourself.

## Bundle size: the deploy will fail with "exceeded 64 MiB" if you aren't careful

**Always measure before deploying.** Dry run costs nothing and uploads nothing:

```bash
uvx --from workers-py pywrangler deploy --dry-run   # prints the module table + Total
```

Look at the `Total (N modules)` row. Healthy is roughly **51,000 KiB / 4475 modules**. If `.venv-workers` paths appear in the module table, the build venv is being bundled and the deploy is one dependency bump away from failing.

### Why this happens

`main` is `worker.py`, so wrangler force-appends a catch-all bundling rule:

```js
if (mainModule.endsWith(".py")) config.rules.push({ type: "PythonModule", globs: ["**/*.py"] })
```

It then walks every file under `base_dir` and keeps whatever matches a rule. `getFiles()` is a blind recursive walk with **no `.gitignore` awareness** (its only hardcoded skip is `.wrangler/`). Because `base_dir` is `"."`, that walk covers the whole repo, and pywrangler hardcodes the build venv to `<project_root>/.venv-workers` (`sync.py`). So every `.py` in the 200 MB build venv ships to Cloudflare.

### What you can and cannot exclude via `rules`

Rules are **include-only** (see `matchFiles`); they exclude by *shadowing* a later rule of the same `type`, which requires `fallthrough: false`.

- **`Text` is excludable.** The built-in default is `{type:"Text", globs:["**/*.txt","**/*.html","**/*.sql"]}`. The current config shadows it with `globs:["**/*.html","**/*.sql"]`, which is what keeps ~6.4 MiB of venv `.txt` files (`tld/res/*.dat.txt`) out of the bundle. Keep `fallthrough: false` on that rule or the shadowing silently stops working.
- **`PythonModule` is NOT excludable.** `config-schema.json` lists it as a valid rule type, but wrangler's runtime `validateRule` hardcodes only `ESModule, CommonJS, CompiledWasm, Text, Data`. Putting `PythonModule` in `rules` is rejected at config-parse time with a message that misleadingly says *"bindings should have a string type field..."*. That is schema/runtime drift in wrangler, not a mistake in your config.
- `python_modules/` is **not** affected by `rules` at all. It is added separately with its own `Data **/*` ruleset and filtered only by `python_modules.exclude`. So trimming `rules` never breaks vendored dependencies, and trimming `python_modules.exclude` never affects the repo scan.

Residual known waste: ~1725 `.py` files (~19 MiB) from `.venv-workers`, plus `tests/*.py` and `browser-extension/`, still ship because `PythonModule` can't be shadowed. Removing them requires narrowing `base_dir` so it doesn't contain the venv, which means moving `worker.py` + `src/` + `templates/` into a subdirectory (this changes the packaged import path from `src.*` to `worker.src.*`).

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
| `DB` (`src/app.py:137`, `src/consumer/processor.py:60`) | `keepfor_me_db` — **mismatch** |
| `BUCKET`, `VECTORIZE`, `AI`, `QUEUE` | match |

`get_db()` looks up `env.DB`, but wrangler binds the D1 database as `keepfor_me_db`, so `d1_binding` resolves to `None` in production and `Database` raises `RuntimeError("No database connection available")`. **The test suite cannot catch this** — `tests/test_endpoints.py` monkeypatches `src.app.get_db` with a sqlite fixture, so no real `env` is ever built.

R2 is also declared twice (`BUCKET` and `keepfor_me_bucket`); only `BUCKET` is referenced by code.

`wrangler.jsonc` has no `database_id` for D1 (only `"remote": true`) — you must paste the id from `npx wrangler d1 create` before deploying.

## Configuration: `src/config.py` is dead code

Nothing imports it. `AppConfig`, its `.env` loading, `rate_limit_*`, and `max_import_*` are all unused. To add a setting, follow the existing pattern instead: declare it under `vars` in `wrangler.jsonc` and read it with `getattr(env, "NAME", default)` in the request handler. Wiring a new value into `AppConfig` will silently do nothing.

`SESSION_SECRET` in `wrangler.jsonc` is **never read**. Sessions are opaque random tokens (`secrets.token_hex(32)`) stored in the `sessions` table — not signed cookies.

## Auth invariants

- The **first** user to register becomes `admin`; afterwards registration raises `RegistrationClosedError` unless the `ALLOW_PUBLIC_SIGNUPS` var is the *string* `"true"` (`src/app.py:404` does a literal string compare).
- Passwords: PBKDF2-HMAC-SHA256, 100k iterations, stored as `salt$hex`.
- PATs are prefixed `kfm_live_` / `rk_live_` and stored as a SHA-256 hash — never the raw token.

## Data layer

`src/models/db.py` is a 71-line dual-backend adapter: D1 in production, `sqlite3` in tests. It exposes only `query_all`, `query_first`, `execute`, `execute_batch`. **If you add a method, implement both branches** — the sqlite branch will otherwise be dead in prod and untested in CI.

## Testing quirks

- `tests/conftest.py` hardcodes `migrations/0001_initial_schema.sql`. It will **not** pick up a new `0002_*.sql`, so schema changes made for D1 won't reach tests. Update the fixture or the initial schema when you change tables.
- The fixture loads the schema by splitting the file on `;` — no `;` inside string literals or trigger bodies.
- Vectorize, Workers AI, R2, and Queue branches are **never executed in tests** (there is no Cloudflare `env`); they are guarded by `hasattr(env, ...)` / `is not None` checks. `src/consumer/processor.py` and `src/models/items.py` are effectively untested — review those by hand.
- `search_fts` and `search_vectorize` wrap their bodies in bare `except Exception` (`src/search/engine.py:32` and `:89`). Search **degrades silently to empty results** instead of raising. When search returns nothing, read the logs rather than expecting a traceback.

## Repo hygiene traps

- 16 `*.pyc` files and `.DS_Store` are still **tracked** despite `.gitignore` listing them. `.gitignore` does not untrack anything — use `git rm --cached`.
- `pylock.toml`, `python_modules/`, and `.venv-workers/` are gitignored Workers build artifacts (~200 MB on disk). The deployed dependency set is therefore **not locked in git**; `pyproject.toml` ranges are the only constraint.
- `.wrangler/` (local dev-server state) is **not** gitignored, so `git status` stays dirty after any `pywrangler dev`. Don't commit it.
- `mcp-cli/keepforme_mcp.py` is a legacy 12-line shim that duplicates the `keepfor-me-mcp` / `keepforme-mcp` console scripts (`src.mcp.cli:main`). Prefer `pyproject.toml` entrypoints.
- CI runs Python 3.11; local venvs are 3.12/3.14. `src/utils/logging.py:39` uses `datetime.utcnow()`, deprecated on 3.12+ and noisy in test output.

## Layout

`worker.py` (root, the deploy entrypoint) → `src/worker.py` → `src/app.py` (FastAPI, 23 routes, owns HTML + JSON endpoints). Supporting packages: `src/auth`, `src/consumer` (extraction + queue processing), `src/models` (data access), `src/search` (RRF hybrid search), `src/utils`, `src/mcp` (JSON-RPC server + CLI). Jinja templates in `templates/`, static assets in `static/`, Manifest V3 extension in `browser-extension/`.