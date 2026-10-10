# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-10-10

### Added
- SPI abstractions in `keepfor.spi`:
  - `TenantScope`: per-tenant database, blob store, and configuration context.
  - `BlobStore`: storage backend abstraction for HTML snapshots, icons, and reader artifacts.
  - `Database`: async database interface supporting parameterized queries, transactions, and batch execution.
  - `EventBus`: event dispatch protocol for item lifecycle hooks (`item.created`, `item.processed`, `item.deleted`).
  - `FetchBudget`: outbound HTTP egress quota and rate tracking SPI with `action` support (`"fetch"`, `"reader_proxy"`).
  - `Entitlements`: feature gating and capability checks per tenant.
  - `AIProvider`: swappable AI backend abstraction for summarization and embeddings.
- Extension hook registry in `keepfor.hooks`: `register_hook` and `invoke_hook` supporting sync and async lifecycle interceptors.
- Multi-tenant request scoping via `request.state.scope` in FastAPI request pipeline.
- Outbound egress policy and rate limiting in `keepfor.utils.egress`: size-capping (5MB max), timeouts (20s), and SSRF validation against private/loopback/link-local IP addresses and internal hostnames.
- Per-tenant isolate routing support via incoming `X-Tenant-Id` header.
- Single-tenant defaults in `keepfor.defaults` preserving 100% of 1.0.0 standalone behavior without configuration changes.

### Changed
- Reader proxy fallback is now opt-in via `READER_PROXY_BASE`. Default is empty (no external proxy fallback). Set `vars.READER_PROXY_BASE = "https://r.jina.ai/"` in `wrangler.jsonc` or `.dev.vars` to restore previous behavior.
- Egress requests strictly enforce SSRF validation before outbound dispatch and before routing to external reader proxies.

### Fixed
- Vectorize index isolation: queries and mutations are isolated by tenant scope or user namespace, and vector batching operations are bounded.

### Upgrade Notes
- **Existing Self-Hosters**: Upgrading from 1.0.0 requires no schema migrations and no configuration changes. All existing single-tenant defaults are preserved.
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
