# Phase 1 Specification: Multi-View Layouts, Cover Thumbnails & Pinned Items

**Date**: 2026-10-05  
**Status**: Draft / Proposed  
**Scope**: UI / UX Layouts, Visual Polish, Schema Foundation, and Pinned Shelf

---

## 1. Overview & Objectives

Phase 1 provides the foundational visual flexibility and high-density browsing capabilities inspired by **Raindrop.io** (multi-view density), **Karakeep** (visual card covers), and **Google Keep** (pinned shelf).

Key deliverables:
1. **Database Foundation (Migration 0006)**: Adding `item_type`, `is_pinned`, `image_url`, `user_notes`, `summary`, and `read_state` to support all three roadmap phases.
2. **3-Way Layout Switcher**: Instant switching between **Cards (Grid)**, **List**, and **Compact (Headlines)**, persisted in `localStorage`.
3. **Cover Thumbnail Extraction & Display**: Extracting `og:image` / `top_image` with 16:9 responsive display and branded fallbacks.
4. **Pinned Shelf (📌)**: Google Keep-style anchored shelf at the top of the library for high-priority references, tools, or scratchpads.

---

## 2. Database Schema (Migration 0006)

File: `migrations/0006_notes_pins_and_metadata.sql`

```sql
-- Migration 0006: Notes, Pinning, User Notes, Cover Images, Summaries, Read State

ALTER TABLE items ADD COLUMN item_type TEXT NOT NULL DEFAULT 'url';
ALTER TABLE items ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE items ADD COLUMN user_notes TEXT;
ALTER TABLE items ADD COLUMN image_url TEXT;
ALTER TABLE items ADD COLUMN summary TEXT;
ALTER TABLE items ADD COLUMN read_state TEXT NOT NULL DEFAULT 'unread';

-- Composite index to serve sorted pinned and unpinned feeds without temp b-trees
CREATE INDEX IF NOT EXISTS idx_items_user_pinned_created ON items(user_id, is_pinned DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_user_type_state ON items(user_id, item_type, read_state, created_at DESC);
```

---

## 3. Visual Layout Specifications

### A. 3-Way Layout Switcher
The library toolbar (`templates/library.html`) gains a segmented control in the top-right corner:
* `⊞ Cards` (Grid View)
* `≡ List` (Standard List View)
* `☷ Compact` (Headlines View)

#### State & DOM Mechanics
* Stored in `localStorage.getItem('kfm_view') || 'list'`.
* On page load or toggle, a single CSS class is toggled on `#items-list`:
  * `.view-cards`: CSS grid layout `grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4`.
  * `.view-list`: Vertical stack `space-y-4` (existing layout).
  * `.view-compact`: High-density single-line table/list with minimized padding `space-y-1`.
* Instant, client-side, zero server roundtrips.

### B. Cover Thumbnail Pipeline
* In `src/consumer/extractor.py`, extract `image_url`:
  ```python
  image_url = None
  if metadata and getattr(metadata, "image", None):
      image_url = metadata.image
  if not image_url:
      og_img = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
      image_url = og_img["content"].strip() if og_img and og_img.get("content") else None
  ```
* In `src/consumer/processor.py`, write `image_url` to D1 during extraction.
* Card rendering:
  * When `image_url` is present: render a 16:9 rounded cover image with `object-cover` and `loading="lazy"`.
  * When `image_url` is absent: render an elegant pastel gradient placeholder with the domain favicon centered.

### C. Pinned Items Shelf (📌)
* Endpoints:
  * `POST /items/{id}/pin`: Toggles `is_pinned = 1 - is_pinned`. Returns updated card or 200.
* Query:
  * `get_pinned_items(db, user_id)`: Fetches items where `user_id = ? AND is_pinned = 1 ORDER BY created_at DESC`.
  * Parallelized with `asyncio.gather` alongside feed queries in `index_page`.
* UI Rendering:
  * If `pinned_items` exist, display a distinct shelf above the main stream:
    ```html
    <div id="pinned-shelf" class="mb-6 pb-6 border-b border-slate-200">
      <div class="flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider text-slate-400 mb-3">
        <span>📌 Pinned References & Notes</span>
      </div>
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        <!-- Pinned cards -->
      </div>
    </div>
    ```
  * Every card has a pin toggle button (active filled 📌 or outline 📌 on hover).

---

## 4. Verification & Testing

* **Migration Integrity**: `python3 -m pytest tests/test_db_and_auth.py`
* **Pin Toggle**: `test_toggle_pin_item` in `tests/test_notes_and_pins.py`
* **Extractor Cover Image**: Unit test asserting `image_url` extraction from OpenGraph tags.
* **Layout Switching**: TestClient test asserting layout classes render in HTML.
