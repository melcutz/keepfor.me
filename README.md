# Keepfor.me 📚

A thin, fast read-it-later and personal library application running entirely on Cloudflare Python Workers, D1 SQLite with FTS5, Vectorize, R2, and the Model Context Protocol (MCP).

---

## Features

- **Four Core Jobs**: Capture, Store, Search, and Share with AI Agents.
- **Python Edge Runtime**: Powered by FastAPI on Cloudflare Python Workers (Pyodide).
- **Distraction-Free Reader**: Customizable themes (Light, Sepia, Dark), fonts (Sans, Serif, Mono), and font sizes.
- **Hybrid Search**: Reciprocal Rank Fusion (RRF) combining D1 FTS5 BM25 keyword matching and Vectorize semantic embeddings (`bge-base-en-v1.5`).
- **Resilient Content Extraction**: Automated body parsing via `trafilatura` with OpenGraph metadata fallback so bookmarks are never lost.
- **Dual Snapshots in R2**: Raw original HTML snapshot (`raw.html`) and sanitized reader HTML (`clean.html`).
- **Zero-Config Single-Tenant Lock**: First registration automatically claims admin ownership and locks out external signups.
- **Model Context Protocol (MCP)**: Native `/api/mcp` endpoint and standalone `uvx keepfor-me-mcp` CLI for Claude Desktop and Cursor.
- **Instant Browser Capture**: Drag-and-drop JavaScript bookmarklet and unpacked Manifest V3 extension.
- **Import & Export**: Ingest Pocket, Instapaper, Omnivore, and browser bookmark exports (Netscape HTML & CSV).

---

## Architecture Overview

```mermaid
flowchart LR
    Browser["Web UI / Bookmarklet / Extension"] -->|FastAPI| Worker["Cloudflare Python Worker"]
    Claude["Claude Desktop / MCP Agents"] -->|uvx keepfor-me-mcp| Worker

    Worker -->|Queries & FTS5| D1[("D1 Database")]
    Worker -->|Enqueue job| Queue["Cloudflare Queue"]
    Queue -->|Process batch| Worker
    Worker -->|Raw & Clean HTML| R2[("R2 Storage")]
    Worker -->|Embeddings| WAI["Workers AI"]
    Worker -->|Upsert vectors| Vec[("Vectorize")]
```

---

## Deployment

### 1. Prerequisites
- Node.js 20 or newer and npm
- uv 0.12.3 or newer
- Cloudflare account with Workers, D1, R2, and Vectorize enabled
- Custom domain: `keepfor.me` (or standard `*.workers.dev` subdomain)

### 2. Provision Cloudflare Resources

Run the following commands once to create your D1 database, R2 bucket, Vectorize index, and Queue:

```bash
# 1. Create D1 Database
npx wrangler d1 create keepfor-me-db
# Copy the returned database_id into wrangler.jsonc

# 2. Create R2 Bucket
npx wrangler r2 bucket create keepfor-me-bucket

# 3. Create Vectorize Index (768 dimensions for bge-base-en-v1.5)
npx wrangler vectorize create keepfor-me-index --dimensions=768 --metric=cosine

# 4. Create Queue
npx wrangler queues create keepfor-me-queue
```

### 3. Apply Database Migrations

Apply the initial schema (tables, FTS5 virtual table, indexes):

```bash
npx wrangler d1 migrations apply keepfor-me-db --remote
```

For local testing:
```bash
npx wrangler d1 migrations apply keepfor-me-db --local
```

### 4. Deploy

PyWrangler bundles the Python dependencies declared in `pyproject.toml`.

```bash
uvx --from workers-py pywrangler deploy
```

Once deployed, visit your domain (e.g., `https://keepfor.me` or `https://keepfor-me.<your-subdomain>.workers.dev`).
The first user to register automatically becomes the admin and locks public registration.

---

## Connecting Claude Desktop & AI Agents (MCP)

Keepfor.me provides a full Model Context Protocol server exposing 6 tools:
1. `save_url`: Save any webpage to your library.
2. `search_library`: Hybrid search across text and meaning.
3. `get_item`: Read full clean text/markdown and metadata.
4. `list_items`: Paginated browse of recent items.
5. `tag_item`: Add or remove tags.
6. `delete_item`: Remove items and clean up vectors.

### Setup Claude Desktop

1. Go to **Settings & API** in your Keepfor.me UI.
2. Click **Generate Token** to create a Personal Access Token (`kfm_live_...`).
3. Add the server to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "keepfor-me": {
      "command": "uvx",
      "args": [
        "keepfor-me-mcp",
        "--url", "https://keepfor.me",
        "--token", "kfm_live_YOUR_TOKEN_HERE"
      ]
    }
  }
}
```

---

## Browser Capture

### 1. Drag-and-Drop Bookmarklet
Go to **Settings** in the Keepfor.me UI and drag the **📚 Save to Keepfor.me** button to your browser's bookmarks bar. Click it on any page to open a quick-save dialog.

### 2. Browser Extension (Manifest V3)
1. Open Chrome/Brave/Edge and navigate to `chrome://extensions/`.
2. Enable **Developer mode** (top right).
3. Click **Load unpacked** and select the `keepfor.me/browser-extension` folder.
4. Click the extension icon, enter your Worker URL (`https://keepfor.me`) and PAT token once in Settings (`⚙️`), then save any tab with `Alt+S`.

---

## Local Development & Testing

Install the project and its test dependencies, then run the pytest suite:

```bash
python3 -m pip install -e . pytest pytest-asyncio pytest-cov httpx
python3 -m pytest tests/
```

To run the local Worker preview with its Python dependencies:

```bash
uvx --from workers-py pywrangler dev
```

---

## License

MIT License. Open source and built for speed.
