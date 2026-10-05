# Phase 3 Specification: Reference vs. Reading Triage & Edge AI Summaries

**Date**: 2026-10-05  
**Status**: Draft / Proposed  
**Scope**: Triage Workflow, Content Categorization, Workers AI Summaries, Long-form Reader

---

## 1. Overview & Objectives

Phase 3 cures the "digital graveyard" backlog by distinguishing **permanent reference assets** (tools, GitHub repos, cheat sheets, docs) from **transient reading articles** (essays, news, newsletters).

Key deliverables:
1. **Category & Triage Tabs**: `All Saves` | `📖 Reading Inbox` | `🛠 Reference & Tools` | `📝 Notes` | `✅ Archive`.
2. **1-Click Article Archiving (`e` key)**: Fast triage specifically for reading queue articles without ever nagging or removing permanent reference bookmarks.
3. **Workers AI 2-Bullet Triage Summaries**: Edge LLM inference on saved articles for 10-second card skimming.
4. **Reading Time Filters & Reader Polish**: Filtering by length (*"⚡ Quick Reads (< 5 min)"*), Table of Contents (ToC) drawer, and scroll position memory.

---

## 2. Reference vs. Reading Triage

### A. The Core Distinction
* **Permanent Reference & Tools (`#tools`, `#docs`, GitHub repos, APIs, SaaS)**:
  * These are assets to **KEEP FOREVER**.
  * They should **never** be marked as "unread" tasks waiting to be cleared.
  * Direct actions: Visit Original (`↗`), Copy URL, Copy for AI.
* **Reading Articles (Essays, blog posts, news, newsletters)**:
  * These are materials to **READ & TRIAGE**.
  * Tracked in **Reading Inbox** with reading time estimate and Reader Mode button.
  * When finished, 1-click **Archive** moves them out of the Inbox into the searchable Archive.

### B. Category Tabs Specification
In `templates/library.html`, above the feed:
* `All Saves` (Full knowledge vault)
* `📖 Reading Inbox` (`item_type='url' AND read_state='unread' AND tag NOT IN ('tools','docs','reference')`)
* `🛠 Reference & Tools` (`item_type='url' AND (tag IN ('tools','docs','reference') OR domain IN ('github.com','docs.*','api.*'))`)
* `📝 Notes` (`item_type='note'`)
* `✅ Archive` (`read_state='archived'`)

### C. 1-Click Archiving
* Endpoint: `POST /items/{id}/archive` (and `POST /items/{id}/unarchive`).
* Keyboard shortcut: Pressing `e` while hovering or selecting a card archives it instantly with an undo toast.
* In Reader header: Prominent "✓ Mark as Read & Archive" button.

---

## 3. Workers AI 2-Bullet Triage Summaries

### Execution Pipeline
* Bound via `env.AI`.
* During queue extraction in `src/consumer/processor.py`:
  ```python
  if hasattr(env, "AI") and env.AI is not None and len(plain_text) > 300:
      prompt = (
          "Extract the core thesis and 2 key takeaways from this text in under 40 words total:\n\n"
          f"{plain_text[:2000]}"
      )
      res = await env.AI.run("@cf/meta/llama-3.2-3b-instruct", {"prompt": prompt, "max_tokens": 80})
      summary = res.get("response", "").strip()
  ```
* Stored in `items.summary`.
* Non-blocking: Bounded by a 4-second timeout; if AI fails or times out, extraction completes successfully without summary.

### Card UI
* Expandable badge on card: `✨ Key Takeaway`
* Clicking expands an inline 2-bullet summary box, allowing users to absorb the article's core insight in 10 seconds.

---

## 4. Long-Form Reader Enhancements

### A. Table of Contents (ToC)
* Extracted client-side or during sanitization from `<h2>` and `<h3>` tags in `clean_html`.
* Renders a floating, collapsible sidebar or drawer on wider screens with active scroll highlighting.

### B. Reading Time Filter
* Quick filter pill: *"⚡ Quick Reads (< 5 min)"* (`word_count < 1000`).
* Enables users to find quick break-time reads on mobile.

### C. Scroll Position Restoration
* When reading an article, `window.addEventListener('scroll')` debounces `scrollTop` ratio to `localStorage.setItem('kfm_scroll_' + itemId, ratio)`.
* On article open, smoothly scrolls back to the saved ratio, curing the mobile scroll loss complaint.

---

## 5. Verification & Testing

* **Triage State Transitions**: `test_archive_item` and `test_unarchive_item` verify state transitions.
* **Category Filtering**: `test_category_tabs` tests query filtering across inbox, reference, notes, and archive.
* **Workers AI Mock**: Test queue processor handles summary generation with mocked AI binding.
* **Keyboard Shortcut**: TestClient / JS verification for `e` key dispatch.
