# Self-Hosting Guide

This guide covers running Keepfor.me on Cloudflare Workers, configuring privacy and egress controls, operating with or without cloud AI services, and running locally.

---

## 1. Architecture & Cloudflare Bindings

Keepfor.me is designed as a serverless application running on Cloudflare Python Workers (Pyodide). A complete deployment uses five Cloudflare primitives:

1. **D1 Database**: Stores users, items, tags, and FTS5 search index.
2. **R2 Bucket**: Stores immutable HTML snapshots (`raw.html` original source and `clean.html` extracted reader view).
3. **Queues**: Asynchronous ingestion worker for bookmark extraction, parsing, and vector indexing.
4. **Vectorize**: Vector database index for hybrid semantic search (`bge-base-en-v1.5`, 768 dimensions).
5. **Workers AI**: On-device edge models for text embedding generation and 2-bullet triage summarization.

---

## 2. Quickstart Deployment

### Prerequisites
- Node.js 20+ and npm
- `uv` 0.12.3+
- Cloudflare account with Workers paid plan (or free plan within quota limits)

### Provisioning Resources

```bash
# 1. D1 Database
npx wrangler d1 create keepfor-me-db
# Note the database_id returned and update wrangler.jsonc

# 2. R2 Storage
npx wrangler r2 bucket create keepfor-me-bucket

# 3. Vectorize Index (768 dimensions for bge-base-en-v1.5)
npx wrangler vectorize create keepfor-me-index --dimensions=768 --metric=cosine

# 4. Queues
npx wrangler queues create keepfor-me-queue

# 5. Apply D1 Migrations
npx wrangler d1 migrations apply keepfor-me-db --remote
```

### Deploying the Worker

Deploy the application using Wrangler:

```bash
npx wrangler deploy
```

On first load, visit your deployed worker URL to register the administrator account. Registration automatically closes after the initial user account is created.

---

## 3. Privacy & Egress Hardening

Keepfor.me is hardened against Server-Side Request Forgery (SSRF) and data leakage.

### Reader Proxy Configuration (`READER_PROXY_BASE`)

When fetching web pages to archive, target origins may reject automated datacenter IP addresses (HTTP 403, 429, or 530).

- **Default Behavior**: Off. Failed requests directly fail the item processing and record the HTTP status. No URL is shared with any external proxy.
- **Enabling Proxy Fallback**: To forward blocked URLs to an extraction proxy, configure `READER_PROXY_BASE` in `wrangler.jsonc` or `.dev.vars`:

```jsonc
{
  "vars": {
    "READER_PROXY_BASE": "https://r.jina.ai/"
  }
}
```

Or point it to a self-hosted readability instance (e.g., `https://readability.yourdomain.com/`).

> **Privacy Note**: Enabling `READER_PROXY_BASE` causes article URLs that receive HTTP 403/429/530 to be sent to that proxy. Internal or private network IP addresses (such as `127.0.0.1`, `10.0.0.0/8`, `192.168.0.0/16`) are blocked by SSRF policy and are never forwarded to any proxy.

### Egress Limits & Policy Variables

Outbound HTTP egress is governed by `keepfor/utils/egress.py` and can be adjusted with environment variables:

| Variable | Default | Description |
|---|---|---|
| `EGRESS_MAX_BYTES` | `5242880` (5 MB) | Maximum allowable payload size for outbound HTML fetches |
| `EGRESS_TIMEOUT_S` | `20.0` | Timeout in seconds for outbound HTTP requests |
| `EGRESS_DENY_HOSTS` | Empty | Comma-separated list of additional hostnames or domains to deny |

Internal IP addresses, loopback addresses, link-local addresses, and cloud metadata endpoints (`169.254.169.254`) are permanently blocked.

---

## 4. Operating Without Cloud AI (Private / Offline Mode)

Keepfor.me functions completely without Workers AI or Vectorize bindings. When these bindings are absent:

1. **Hybrid Search Degrades Gracefully**:
   - Without `AI` or `VECTORIZE`, search falls back transparently to D1 SQLite FTS5 (BM25 keyword search).
   - All title, body text, and tag queries continue to work with instant response times.
2. **Summaries Skip Gracefully**:
   - Article triage summarization fails open (items are stored with `summary = None`).
   - Body extraction and reader view generation are completely unaffected.

To operate in zero-AI mode, simply omit the `ai` and `vectorize` bindings from your `wrangler.jsonc`.

---

## 5. Local Development via Pywrangler

For local development without deploying to Cloudflare:

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest tests/ -q

# Run local preview server
uvx --from workers-py pywrangler dev
```

During local execution:
- Queue messages are processed inline synchronously by `save_item`.
- Outbound requests use Python's standard `urllib.request` with full SSRF and egress validation.
- SQLite runs in local temporary storage or local D1 emulation.
