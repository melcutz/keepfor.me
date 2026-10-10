# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.1] - 2026-10-10

### Fixed
- Fix `NameError` importing `keepfor.consumer.processor` (`TenantScope` was only imported under `TYPE_CHECKING` without `from __future__ import annotations`).

## [1.1.0] - 2026-10-10

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
