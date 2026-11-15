# Keepfor.me Repository Map

Open-core read-it-later and personal library built with FastAPI on Cloudflare Python Workers (D1 SQLite + FTS5, R2, Vectorize, Workers AI, Queues) with Streamable HTTP MCP (`/api/mcp`).

## Core Package (`keepfor/`)
- `spi.py`, `defaults.py`, `runtime.py`: Public Service Provider Interface (`CORE_API_VERSION`) consumed by multi-tenant hosts (`keepfor-cloud`); bump `CORE_API_VERSION` on any non-backward-compatible `spi.py` change.
- `worker.py` (and root `worker.py`), `app.py`, `deps.py`, `middleware.py`, `schemas.py`, `templating.py`, `paths.py`, `pwa_icons.py`: Cloudflare Worker entrypoint (`fetch`, `queue`), FastAPI factory, dependency wiring, and dynamic PWA icon/manifest routes.
- `routes/` (`ui.py`, `api.py`, `auth.py`, `settings.py`, `_shared.py`): HTML views/htmx endpoints, REST JSON API, sign-in/register flows, settings/PAT/import/cleanup routes.
- `models/` (`db.py`, `items.py`, `tags.py`, `highlights.py`, `collections.py`, `imports.py`, `cleanup.py`, `stats.py`, `tenant.py`, `tenant_tables.py`): Dual D1/SQLite database adapter (`query_all`, `query_first`, `execute`, `execute_batch`) and data access functions.
- `auth/` (`crypto.py`, `service.py`): PBKDF2-HMAC-SHA256 password hashing, opaque sessions, `kfm_live_`/`rk_live_` PAT SHA-256 verification.
- `consumer/` (`extractor.py`, `processor.py`): Lazy-loaded content extraction (`trafilatura`/`bs4` must NEVER be imported at module scope) and queue batch consumer.
- `search/` (`engine.py`, `indexable.py`, `vectors.py`): FTS5 + Vectorize RRF hybrid search, content-word/prefix/tag matching, soft-delete (`deleted_at IS NULL`) filtering.
- `storage/blobs.py`: Scoped R2 snapshot storage.
- `mcp/` (`server.py`, `resources.py`): Stateless Streamable HTTP JSON-RPC MCP tools, resources, and prompts.
- `utils/` (`egress.py`, `favicon.py`, `importer.py`, `rate_limit.py`, `tagger.py`, `url.py`, `chunker.py`, `dates.py`, `logging.py`): SSRF-safe egress, same-origin favicon proxy/cache, Netscape/CSV importer, D1 rate limiting, URL normalization.

## Templates, Migrations & Extension
- `templates/`, `static/`: Jinja2 HTML templates (`library.html`, `base.html`, `settings.html`, `save_popup.html`) and CSS/JS assets.
- `migrations/0001_initial_schema.sql` .. `0016_smart_collections.sql`: D1/SQLite schema migrations (loaded in order by `tests/conftest.py`).
- `browser-extension/`: Manifest V3 browser extension.

## Hard Invariants (Enforced by Gate)
1. **Ruff CI parity:** `ruff check keepfor/ tests/ --select=E,W,F,I,N` and `ruff format --check keepfor/ tests/` must both pass cleanly.
2. **User-scoped SQL:** Every SQL query touching a tenant table must scope by `user_id` (`scripts/audit_sql.py`).
3. **Auth route order & cold-start imports:** `scripts/check_auth_order.py` must pass, and `trafilatura` / `BeautifulSoup` must remain lazy-imported off the module load path.
4. **Protected paths:** Never modify `factory/`, `.agents/`, or `.github/` inside job branches.

## Tests Layout (`tests/`)
- Module-level `test_*` functions in `tests/test_*.py` and `tests/isolation/` (shared fakes in `tests/fakes.py` and fixtures in `tests/conftest.py`).
- Run targeted tests fast with `uv run --extra dev pytest -q -o addopts="" tests/<target>.py`.
