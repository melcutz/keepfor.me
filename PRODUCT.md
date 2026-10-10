# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
Developers, researchers, and power users who actively read, save, and curate knowledge from across the web and want a private, single-tenant, self-hosted vault without subscription fees or heavy server maintenance.

## Product Purpose
Keepfor.me is a thin, ultra-fast read-it-later and personal library application running entirely serverless at the Cloudflare edge. It turns ephemeral web bookmarks into a permanent personal library that is distraction-free to read, instantly searchable via hybrid keyword and semantic search, and directly accessible by AI agents (Claude, Cursor, etc.) via remote MCP. Success is zero-friction capture across devices, instant sub-second access, and complete data ownership.

## Positioning
An edge-native, single-tenant personal knowledge vault with dual HTML snapshots and first-class Model Context Protocol (MCP) integration. Unlike bloated hosted services (Pocket, Instapaper, Omnivore) or heavy Docker self-hosted setups (Wallabag), Keepfor.me runs serverless at the edge on Cloudflare Python Workers, costs near zero to operate, guarantees data permanence with raw and clean R2 snapshots, and serves both human readers and AI agents natively.

## Operating Context
- **Capture**: Drag-and-drop bookmarklet, unpacked Manifest V3 browser extension, mobile PWA Web Share Target on Android, 1-tap Apple Shortcuts on iOS with Safari session auth, and smart clipboard detection.
- **Reading & Triage**: Distraction-free clean reader view with customizable typography (Sans, Serif, Mono), themes (Light, Sepia, Dark), adjustable font sizing, tag chips, and status tracking (unread, archived, starred).
- **Search & Retrieval**: Hybrid search combining D1 FTS5 BM25 keyword matching with Vectorize semantic vector embeddings (`bge-base-en-v1.5`), plus tag filtering.
- **AI Agent Interaction**: Remote Streamable HTTP MCP server (`/api/mcp`) authenticating with Bearer Personal Access Tokens (PATs) for Claude Desktop, opencode, Cursor, etc.
- **Environment**: Cloudflare Python Workers (Pyodide), D1 SQLite, R2 object storage, Vectorize, Cloudflare Queues, and Workers AI.

## Capabilities and Constraints
- **Zero-Config Single-Tenant**: First user to register becomes admin; public registration is locked by default (`ALLOW_PUBLIC_SIGNUPS="false"`).
- **Edge Architecture Limits**: Strict Cloudflare Python Worker limits (64 MiB bundle budget, no background daemon threads, asynchronous worker queue for heavy parsing and imports, remote RPC D1 batching).
- **Frictionless Capture**: Immediate URL ingestion returning `queued`, resilient extraction via `trafilatura` and OpenGraph fallback with visible extraction status/failure reasons.
- **FastAPI + Jinja + htmx**: UI server-rendered via Jinja2 templates and interactive htmx partial swaps; form endpoints return HTML responses; strictly same-origin (no CORS middleware needed).
- **Data Sovereignty**: Preserves raw original HTML (`raw.html`) and sanitized reader HTML (`clean.html`) in R2 storage.

## Brand Commitments
- **Name**: Keepfor.me
- **Tone**: Clean, utility-first, calm, developer-friendly, and distraction-free.
- **Identity Assets**: Minimalist mark, dynamically generated SVG/PNG icons and PWA manifest (in-code, zero static bundle overhead).

## Evidence on Hand
- Full working codebase running FastAPI on Cloudflare Python Workers.
- Production deployment at `app.keepfor.me`.
- Jinja2 templates in `templates/` (`base.html`, `library.html`, `reader.html`, `save_popup.html`, `settings.html`, `stats.html`, `tags.html`).
- Working Manifest V3 browser extension in `browser-extension/`.
- 6 MCP tools implemented and documented in `keepfor/mcp/`.
- Test suite with 238+ unit and integration tests.

## Product Principles
1. **Speed & Distraction-Free Utility**: Sub-second edge performance, keyboard-first navigation, responsive typography, and zero visual clutter.
2. **Archival Fidelity**: Preserve knowledge permanently with dual R2 snapshots (raw and clean) and resilient content extraction that never loses an ingested bookmark.
3. **Frictionless Multi-Device Capture**: Seamless entry points from desktop browsers, iOS share sheets, Android Web Share targets, and smart clipboard sync.
4. **Developer & AI Synergy**: Treat AI agents as first-class citizens alongside human readers via native MCP, transparent APIs, and open data.

## Accessibility & Inclusion
- Mobile-first responsive UI with minimum 32px touch targets and no horizontal overflow down to 360px viewport width.
- Semantic HTML and WCAG AA contrast across light, dark, and sepia reader themes.
- Reader accessibility controls: adjustable font size, line height, font family (sans, serif, mono), and high-contrast themes.
