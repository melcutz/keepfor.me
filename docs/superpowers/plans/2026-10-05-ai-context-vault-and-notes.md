# AI Context Vault, Quick Notes & Multi-View Knowledge Base Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform Keepfor.me into a premier edge-native personal intelligence hub: an AI Context Vault powering Claude Desktop/Cursor via MCP, a lifelong reference knowledge base with quick notes and pinning (Google Keep style), and a distraction-free reader with multi-view layout switching (Cards, List, Compact).

**Architecture:**
- **Edge Data Foundation**: D1 Migration `0006` adds `item_type`, `is_pinned`, `user_notes`, `image_url`, `summary`, and `read_state` with composite indexes.
- **Unified Retrieval Pipeline**: Notes and bookmarks share the same FTS5 BM25 index and 768d Vectorize cosine embeddings.
- **Remote MCP Server (`/api/mcp`)**: Native tools `save_note`, `pin_item`, enriched `search_library` and `get_item`.
- **UI & Layout Engine**: Client-persisted 3-way layout switcher (Cards/List/Compact), Google Keep-style Pinned Shelf (📌), 1-click "Copy for AI" Markdown action, and category triage tabs.

---

## File Structure & Plan Map

| Component | Target File | Actions & Responsibility |
|---|---|---|
| **Database** | `migrations/0006_notes_pins_and_metadata.sql` | [NEW] Migration for notes, pins, cover images, summaries, and read_state |
| **Models** | `src/models/items.py` | [MODIFY] Add `save_note`, `toggle_pin_item`, `update_user_notes`, `archive_item`, update `save_item`, add `get_pinned_items` |
| **Search Engine** | `src/search/engine.py` | [MODIFY] Include `user_notes`, `item_type`, `is_pinned`, `image_url` in hybrid & FTS5 search queries |
| **Extractor** | `src/consumer/extractor.py` | [MODIFY] Extract `image_url` from trafilatura metadata and OpenGraph tags |
| **Queue Processor** | `src/consumer/processor.py` | [MODIFY] Store `image_url` and run Workers AI 2-bullet summary generation |
| **MCP Server** | `src/mcp/server.py` | [MODIFY] Expose `save_note`, `pin_item`, return `user_notes` in `get_item` / `search_library` |
| **App Routing** | `src/app.py` | [MODIFY] Add routes for note creation, pin toggling, markdown retrieval, pass pinned items + layout context to templates |
| **Base Template** | `templates/base.html` | [MODIFY] Quick Save modal Link/Note tabs + Copy for AI toast handler |
| **Library View** | `templates/library.html` | [MODIFY] View switcher (Cards/List/Compact), Pinned shelf (📌), category tabs |
| **Card Partial** | `templates/partials/item_card.html` | [MODIFY] Render 3 layout modes, cover thumbnails, AI badges, Copy for AI button, Pin button |
| **Reader View** | `templates/reader.html` | [MODIFY] Copy for AI button, User Notes editor, Pin button, Table of Contents drawer |
| **Test Suite** | `tests/test_notes_and_pins.py` | [NEW] Comprehensive unit and integration tests for notes, pinning, and metadata |

---

## Phase 1: Multi-View Layouts, Cover Thumbnails & Pinned Shelf

### Task 1.1: Migration 0006 & Database Foundation
- [ ] Create `migrations/0006_notes_pins_and_metadata.sql`:
  ```sql
  ALTER TABLE items ADD COLUMN item_type TEXT NOT NULL DEFAULT 'url';
  ALTER TABLE items ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0;
  ALTER TABLE items ADD COLUMN user_notes TEXT;
  ALTER TABLE items ADD COLUMN image_url TEXT;
  ALTER TABLE items ADD COLUMN summary TEXT;
  ALTER TABLE items ADD COLUMN read_state TEXT NOT NULL DEFAULT 'unread';

  CREATE INDEX IF NOT EXISTS idx_items_user_pinned_created ON items(user_id, is_pinned DESC, created_at DESC);
  CREATE INDEX IF NOT EXISTS idx_items_user_type_state ON items(user_id, item_type, read_state, created_at DESC);
  ```
- [ ] Run `python3 -m pytest tests/test_db_and_auth.py -q` to verify migration applies cleanly in SQLite test suite.

### Task 1.2: Cover Thumbnail Extraction
- [ ] In `src/consumer/extractor.py`, extract `image_url` from `metadata.image` or OpenGraph tags (`og:image`, `twitter:image`).
- [ ] In `src/consumer/processor.py`, update `extract_and_store` to insert `image_url` into D1.
- [ ] Write unit test in `tests/test_extractor.py` asserting `image_url` is extracted when present.

### Task 1.3: Pinned Items Backend & Model
- [ ] In `src/models/items.py`:
  - Implement `toggle_pin_item(db: Database, user_id: str, item_id: str) -> bool`.
  - Implement `get_pinned_items(db: Database, user_id: str) -> list[dict]`.
  - Update `get_recent_items` to return `is_pinned`, `item_type`, `user_notes`, `image_url`.
- [ ] In `src/app.py`:
  - Add route `POST /items/{item_id}/pin` which toggles pinned state and re-renders the card.
  - In `index_page`, fetch `pinned_items` via `asyncio.gather` and pass to template context.

### Task 1.4: 3-Way Layout Switcher & Pinned Shelf UI
- [ ] In `templates/library.html`:
  - Add view switcher control: `⊞ Cards`, `≡ List`, `☷ Compact`.
  - Add `#pinned-shelf` container rendering pinned items when `pinned_items` is non-empty.
  - Add client script to manage `localStorage.getItem('kfm_view')` and toggle CSS classes.
- [ ] In `templates/partials/item_card.html`:
  - Add pin toggle button `📌` (active blue/filled if pinned).
  - Add cover image thumbnail rendering in Cards mode.
  - Add high-density single-line layout for Compact mode.
- [ ] Verify test suite: `python3 -m pytest tests/ -q`.

---

## Phase 2: AI & MCP Context Enhancements + Quick Notes

### Task 2.1: Quick Notes & Code Snippets Backend
- [ ] In `src/models/items.py`:
  - Implement `save_note(db, env, user_id, title, content_text, tags=None, is_pinned=False)`.
  - Inserts row with `item_type='note'`, `status='saved'`, `content_text=content_text`.
  - Indexes into `items_fts`.
  - Generates 768d Vectorize embedding chunk when `env.AI` is present.
- [ ] In `src/app.py`:
  - Add `POST /notes` endpoint to handle note creation from the Quick Save modal.

### Task 2.2: User Notes ("Why I Kept This")
- [ ] In `src/models/items.py`:
  - Implement `update_user_notes(db, user_id, item_id, user_notes)`.
- [ ] In `src/app.py`:
  - Add `POST /items/{item_id}/notes` endpoint.
- [ ] In `templates/partials/item_card.html` and `templates/reader.html`:
  - Add editable user note section with htmx save.

### Task 2.3: 1-Click "Copy for AI" & Transparency Badges
- [ ] In `templates/partials/item_card.html` and `templates/reader.html`:
  - Add **"📋 Copy for AI"** button.
  - Adds JS copy handler writing prompt-ready Markdown with source, tags, notes, and clean body to clipboard.
  - Show floating toast feedback: *"✓ Copied clean Markdown for AI prompt"*.
- [ ] Add `✨ AI Indexed` status badge on cards when item has embeddings & clean R2 text.

### Task 2.4: MCP Server Expansion
- [ ] In `src/mcp/server.py`:
  - Add `save_note` tool definition and handler.
  - Add `pin_item` tool definition and handler.
  - Update `get_item` to return `user_notes`, `item_type`, `is_pinned`.
  - Update `search_library` to return `user_notes` and `item_type`.
- [ ] Add MCP tests in `tests/test_mcp.py` verifying tool calls.

---

## Phase 3: Reference vs. Reading Triage & Edge AI

### Task 3.1: Category & Triage Tabs
- [ ] In `templates/library.html`:
  - Add top category tabs: `All Saves` | `📖 Reading Inbox` | `🛠 Reference & Tools` | `📝 Notes` | `✅ Archive`.
- [ ] In `src/search/engine.py` & `src/app.py`:
  - Support filtering by `category`:
    - `inbox`: `item_type='url' AND read_state='unread'`
    - `reference`: `tag IN ('tools','docs','reference') OR domain IN ('github.com','docs.*','api.*')`
    - `notes`: `item_type='note'`
    - `archive`: `read_state='archived'`

### Task 3.2: 1-Click Article Archiving
- [ ] In `src/models/items.py`:
  - Implement `archive_item(db, user_id, item_id)` and `unarchive_item(db, user_id, item_id)`.
- [ ] In `src/app.py`:
  - Add `POST /items/{item_id}/archive`.
  - Add keyboard shortcut `e` to archive selected card.

### Task 3.3: Workers AI 2-Bullet Summaries
- [ ] In `src/consumer/processor.py`:
  - Call `@cf/meta/llama-3.2-3b-instruct` when `env.AI` is present for articles with >300 words.
  - Store 2-bullet summary in `items.summary`.
- [ ] In `templates/partials/item_card.html`:
  - Render expandable `✨ Key Takeaway` badge.

### Task 3.4: Reading Time Filters & Reader Polish
- [ ] Add length filter: `⚡ Quick Reads (< 5 min)` (`word_count < 1000`).
- [ ] In `templates/reader.html`:
  - Add Table of Contents (ToC) drawer parsing `<h2>`/`<h3>` headings.
  - Add scroll position restoration via `localStorage`.

---

## Verification & Budget Gates

- [ ] Run full test suite: `python3 -m pytest tests/ -q`
- [ ] Run CI lint: `ruff check src/ tests/ --select=E,W,F,I,N`
- [ ] Run CI format check: `ruff format --check src/ tests/`
- [ ] Enforce bundle size: `uvx --from workers-py pywrangler deploy --dry-run` (verify under 58,000 KiB budget gate).
