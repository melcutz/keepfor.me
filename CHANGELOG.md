# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Breaking
MCP Server 0.2.0 changes two tool defaults. The old behaviour is one argument
away in both cases.

- `get_item` no longer returns unbounded `content_text`. It is capped to the
  excerpt by default; pass `include_content=true` for the full text. The
  result now carries `truncated` (`true` when the returned text is shorter
  than the stored content) and `content_length` (the full content length in
  characters), so a client knows when to opt in.
- `list_items` returns the **compact** shape by default (`id`, `title`, `url`,
  `saved_at`, `tags` — no `excerpt`, no `status`, no `item_type`). Pass
  `fields=[...]` to opt into the extra fields. Tool results are serialized as
  compact JSON instead of indented JSON, which keeps the default `list_items`
  page under 4 KB.

### Added
- **Persisted notes-import status** (GitHub issue #18): every notes upload now
  writes one `import_status` record per user. The record holds the running
  counters (notes imported, duplicates skipped, links queued, attachments not
  imported, parse errors, items skipped by the plan limit) plus a capped list
  of error messages, and it is updated chunk by chunk while the queue consumer
  works through the uploaded notes. The import panel on the library and
  settings pages shows the live progress ("12 of 60 notes processed") and the
  final result, and it is still there after a refresh — the one-shot toast
  only ever said how many notes were queued. A rejected upload (zip bomb,
  ENEX entity payload, unreadable file, plan limit) leaves a failed record
  with the reason.
  - Migration `0011_import_status.sql` (additive): the `import_status` table
    and its `(user_id, created_at)` index. Apply it before deploying:
    `npx wrangler d1 migrations apply keepfor-me-db --remote`. Without it the
    import still works, only the status panel stays empty.
  - The status record is a tenant table: every query is scoped by `user_id`
    and the tenant-isolation suite covers it.
- **Notes import** (GitHub issue #15): upload a **Google Keep Takeout zip**, an **Evernote `.enex`** file, or a **zip of Markdown/text files** (Obsidian vault, Apple Notes via Exporter, Simplenote) from Settings → Import. Notes become searchable Keepfor notes with tags and original creation dates; Keep checklists render as Markdown tasks, archived Keep notes land in the archive, and trashed Keep notes are skipped. Links inside notes (body URLs, Keep `WEBLINK` annotations, ENEX `source-url` web clips) become regular URL saves tagged `from-notes`.
  - Dedupe on `(user_id, source, source_id)` with a content-hash fallback: re-importing the same export inserts nothing new.
  - Parsed notes fan out through the queue in chunks (same as bookmark imports), so a 1,000-note export never times out the request.
  - Upload caps: 50 MB for `.zip`/`.enex`, 10 MB for HTML/CSV. Attachments are listed in the note body (`📎 file (not imported)`) but never stored.
  - Each imported note and each queued URL costs 1 `import` entitlement unit (the same pool as bookmarks); the import stops at the remaining quota and reports the refused items as "N skipped: plan limit".
  - Import results (and any error) show as a one-shot toast on the library page; a failed upload also shows its error on the settings page it lands on. Logs contain counts only, never note content.
  - Migration `0008_import_sources.sql` (additive): `items.source`, `items.source_id` and a unique index backing the dedupe.
  - MCP `search_library` now surfaces imported notes alongside articles.
- **MCP Server 0.2.0** — direct lookup, full-text reading and full tag management:
  - `find_item`: Finds item ids by exact/prefix title or exact/normalized url. No search and no embeddings, so an agent goes from a title to the full text in two calls.
  - `get_item` now accepts `item_id`, `url` or an exact `title`.
  - `list_items`: Compact by default, with `sort` (`saved`/`opened`/`title`), filters (`since`, `until`, `domain`, `pinned`, `type`=`note`|`url`), `cursor`/`offset` pagination and a `fields` projection.
  - `list_tags`, `create_tag`, `describe_tag`, `update_tag`, `rename_tag`, `merge_tags`, `delete_tag`: full tag management, including hierarchy (`parent`, e.g. `work/research`) via the new `tags.description`, `tags.color` and `tags.parent_id` columns.
  - `tag_item` (existing) is joined by `tag_items`, `retag_by_query` and `set_item_tags` for bulk and whole-set tagging.
  - `suggest_tags` (existing vocabulary first), `search_tags` (fuzzy over names and descriptions), `tag_stats`, `related_tags` (co-occurrence) and `tag_cleanup` (near-duplicate tags, unused tags, untagged items).
  - Tag filters `tags_all`, `tags_any`, `tags_none` and `untagged` on `list_items`, `search_library`, `search_passages`, `find_item` and `reading_history`. Child tags count: filtering on `work` also matches `work/research`.
  - `search_passages`: Passage-level hybrid search over semantic chunks with `max_chars` budget, returning `item_id`, `chunk_id`, char offsets, `snapshot_url` (permanent R2 archive), `saved_at`, and score. Supports `mode` (hybrid/keyword/semantic), tag filtering, and cursor pagination.
  - `read_item`: Paginated full-text reading with token budget (`max_tokens`), returning `text`, `next_cursor`, `total_chunks`, and `outline` for navigation. Each chunk ≤ `max_tokens`.
  - `reading_history`: Date-filtered reading history using `created_at` (saved) and `item_opens` (opened) tables. Supports `since`/`until` date range, `event` filter (saved/opened/all), query, tag filtering, and pagination.
  - `related_items`: Vector similarity search for related items by `item_id` or raw `text`. Excludes source item. Includes one-line reasons (shared tags or semantic similarity).
  - `save_url`/`save_note` accept and store provenance metadata fields `agent_summary`, `why_saved`, `source_context`, `saved_by`. These fields are indexed in FTS5 and searchable.
  - Every bulk or destructive tag operation (`delete_tag`, `tag_items`, `retag_by_query`) defaults to `dry_run=true` and returns the affected count plus a sample; applying requires `dry_run=false`. `tag_items` and `retag_by_query` cap at 100 items per call and report `truncated` and `total_matched`; `merge_tags` accepts at most 20 source tags.
  - Tool errors are structured: `error.data.type` is one of `invalid_arguments`, `not_found` or `internal_error`.
- All new tools include MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`). The annotations are advisory hints for the client — the server does not enforce them, and a token is never rejected for missing scope information.
- `search_library` and `list_items` now return structured responses with `items`, `total`, `limit`, `offset` (and `cursor`/`fields` support).
- Tenant isolation verified for all new MCP tools via isolation test suite.

### Fixed
- **Import errors no longer leak internal detail**: a failed note or link used to store the raw exception text (`note: OperationalError: ...`) in `import_status.errors`, which is rendered in the library and settings panels. The stored message is now generic ("note: could not be imported", "link: could not be queued"); the exception goes to the server log.
- **Zip bombs in notes import**: both zip parsers (Google Keep Takeout and Markdown/text) now check the archive metadata before opening a single member — at most 20,000 members and 200 MB of declared uncompressed size — and read every member with a bounded read (2 MB per file, bigger ones are skipped and counted). Breaking a cap gives a visible error and imports nothing.
- **XML entity expansion in `.enex`**: ENEX files are parsed with `defusedxml`, so a DTD/entity payload (billion laughs) is rejected with a visible error instead of being expanded.
- **Plan limits on notes import**: imported notes and the URLs inside them count against the plan's item limit like bookmarks. The import stops at the remaining quota and reports "N skipped: plan limit" instead of aborting the whole upload (or refusing a file that only partly fits).
- `reading_history` with `event="opened"` plus a tag or query filter raised a SQLite parameter mismatch instead of returning results.
- The FTS5 re-index after extraction read the item's provenance fields by `id` only; it is now scoped to the owning `user_id` as well.
- **Note bodies lost on read**: the MCP 0.2.0 content cap was applied inside `get_item`, so every caller (the reader page, the Copy-for-AI action, `GET /api/items/{id}`) got a truncated body — and notes, which have no excerpt, lost their text entirely. The cap now lives in the MCP `get_item` tool only; `get_item` returns the full body by default.

### Changed
- FTS5 index now includes the provenance fields (`agent_summary`, `why_saved`, `source_context`, `saved_by`) in `content_text` for searchability.
- Migration 0009 adds `agent_summary`, `why_saved`, `source_context`, `saved_by` columns to `items` table.
- Migration 0010 adds `description`, `color` and `parent_id` columns to the `tags` table for tag metadata and hierarchy.
- The library export orders tags deterministically (`ORDER BY item_id, tag name`).

## [1.1.2] - 2026-10-10

### Fixed
- Unauthenticated `GET /` no longer returns HTTP 500 when the app runs with a multi-tenant scope provider. `library_page` now resolves the user first and redirects to `/auth/login` without opening a database. The "no users yet, go to register" redirect is kept only for the single-tenant default scope.

## [1.1.1] - 2026-10-10

### Fixed
- Fix `NameError` importing `keepfor.consumer.processor` (`TenantScope` was only imported under `TYPE_CHECKING` without `from __future__ import annotations`). This resolves a critical issue in v1.1.0 that prevented background extraction tasks from running.

## [1.1.0] - 2026-10-10

> [!WARNING]
> Release v1.1.0 is broken and should not be used. A missing runtime import of `TenantScope` in `keepfor/consumer/processor.py` caused background extraction tasks to crash with a `NameError`. Please use v1.1.1 or later.

### Breaking Changes
- Package rename from `src` to `keepfor`: any custom scripts or external tooling importing from `src.*` must now import from `keepfor.*`.

### Added
- Provider seams and Service Provider Interface (SPI) in `keepfor.spi` (`CORE_API_VERSION = 1`):
  - `TenantScope`: per-tenant database, blob store, and configuration context.
  - `BlobStore`: storage backend abstraction for HTML snapshots and assets.
  - `Database`: async database interface supporting parameterized queries, transactions, and batch execution.
  - `EventBus`: event dispatch protocol for item lifecycle events (`item.saved`, `item.extracted`, `item.failed`, `item.deleted`).
  - `FetchBudget`: outbound HTTP egress quota and rate tracking SPI with action differentiation (`"fetch"`, `"reader_proxy"`).
  - `Entitlements`: feature gating and capability checks per tenant.
  - `AIProvider`: swappable AI backend abstraction for summarization and embeddings.
- Single-tenant defaults in `keepfor.defaults` preserving 100% of 1.0.0 behavior out of the box without configuration changes.
- Global provider resolution in `keepfor.runtime` for request-less background execution (queue consumers).
- Application factory `create_app()` in `keepfor.app`:
  - Pluggable providers (`auth`, `scope`, `entitlements`, `fetch_budget`, `events`, `ai`).
  - Customizable template directories (`template_dirs`), template globals (`template_globals`), extra middleware, and extra routers.
  - Pluggable template blocks in `base.html` (`head_extra`, `banner`, `nav_extra`, `footer_extra`) and `settings.html` (`settings_extra`).
- Tenant isolation across all storage layers:
  - Strict tenant and `user_id` scoping verified across all SQL queries in `keepfor/models`.
  - Partitioned R2 storage via `TenantScope.blobs()` with custom key prefixes.
  - Vectorize index isolation via namespace partitioning.
- Outbound egress policy and SSRF hardening in `keepfor.utils.egress`:
  - Size capping (5MB max body size), streaming limits, and 20s timeouts.
  - Lexical and socket SSRF validation rejecting private, loopback, link-local, multicast IP addresses, and intranet hostnames.
- Per-tenant isolate routing support via incoming `X-Tenant-Id` header.
- Contributor License Agreement (CLA) in `CLA.md` with successor clause, and trademark guidelines in `TRADEMARK.md`.
- Explicit maintainer assignments in `.github/CODEOWNERS`.

### Changed
- Reader proxy fallback is now opt-in via `READER_PROXY_BASE`. Default is empty (no external proxy fallback). Set `vars.READER_PROXY_BASE = "https://r.jina.ai/"` in `wrangler.jsonc` or `.dev.vars` to restore previous behavior.
- Egress requests strictly enforce SSRF validation before outbound dispatch and before routing to external reader proxies.
- Deployment workflow (`.github/workflows/deploy.yml`) is now manual via `workflow_dispatch` only (zero automatic push deployments).

### Fixed
- Vectorize index queries and mutations are isolated by tenant scope or user namespace, and vector batching operations are bounded.
- Ingestion queue consumer gracefully handles both legacy 2-element messages and modern 3-element (`user_id` aware) messages during upgrades.

### Upgrade Notes for Self-Hosters
- **Schema & Migrations**: No database migrations are required when upgrading from 1.0.0 to 1.1.0. The existing D1 schema is 100% compatible.
- **In-Flight Queue Messages**: Queue messages already in flight during deployment are automatically processed without data loss.
- **Reader Proxy Fallback**: If you relied on automatic fallback to Jina Reader on 403 errors, add `vars.READER_PROXY_BASE = "https://r.jina.ai/"` to your `wrangler.jsonc`.

## [1.0.0] - 2026-09-01

### Added
- Initial release of Keepfor.me: read-it-later application on Cloudflare Python Workers.
- FastAPI backend running on Cloudflare Workers with Pyodide.
- SQLite D1 database with FTS5 full-text search.
- Cloudflare R2 storage for raw and reader HTML snapshots.
- Vectorize semantic search using Workers AI (`bge-base-en-v1.5`).
- Model Context Protocol (MCP) streamable HTTP server.
- Installable PWA with reader themes and mobile share targets.
