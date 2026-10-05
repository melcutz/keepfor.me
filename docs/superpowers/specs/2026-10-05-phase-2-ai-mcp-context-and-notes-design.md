# Phase 2 Specification: AI & MCP Context Enhancements + Quick Notes

**Date**: 2026-10-05  
**Status**: Draft / Proposed  
**Scope**: Model Context Protocol (MCP), Personal Notes, Prompt Copying & AI Transparency

---

## 1. Overview & Objectives

Phase 2 elevates Keepfor.me into a primary **AI Context Vault**, enabling bidirectional knowledge sharing with **Claude Desktop**, **Cursor**, **opencode**, and custom agents via the Model Context Protocol (MCP).

Key deliverables:
1. **1-Click "Copy for AI" (Markdown)**: Instant clipboard action generating prompt-ready Markdown with source metadata.
2. **User Notes ("Why I Kept This")**: Intent annotations attached to bookmarks, indexed in FTS5 and passed to AI agents via MCP.
3. **"AI Indexed" Transparency Badges**: Visual indicators demonstrating 768d vector embeddings, R2 clean text, and FTS readiness.
4. **Quick Notes & Code Snippets**: Standalone Markdown text notes saved into the same vector & FTS5 knowledge vault.
5. **MCP Server Tools Expansion**: Exposing `save_note`, `pin_item`, and enriched retrieval for AI assistants.

---

## 2. Quick Notes & Code Snippets Architecture

### A. Data Representation
In the `items` table:
* `item_type = 'note'`
* `url`: Set to a synthetic permalink (`urn:note:<uuid>`) or user-provided source.
* `title`: Note title or first line of Markdown text.
* `content_text`: Raw user-authored Markdown text.
* `status = 'saved'`: Notes are saved immediately without queue fetching.
* `word_count`: Calculated from Markdown text.

### B. Indexing & Embedding Pipeline
* **FTS5 Virtual Table**: Automatically indexed into `items_fts` on insertion/update.
* **Vectorize Embedding**: When `env.AI` is present, generate a 768-dim embedding using `@cf/baai/bge-base-en-v1.5` and upsert into Vectorize with a matching `chunks` tracking row.
* **Unified Retrieval**: Claude/Cursor calling `search_library` or user searching in the UI matches both web bookmarks and personal notes in a single query.

---

## 3. User Notes ("Why I Kept This")

### Purpose
When saving documentation or technical links, users frequently have a specific rationale (e.g. *"Use this approach for our D1 migration in sprint 3"*).
* **For the User**: Immediate context when revisiting months later.
* **For AI Agents**: Grounding context passed in MCP tool responses (`search_library`, `get_item`), significantly boosting agent accuracy and relevance.

### UI & Editing
* In Quick Save modal: optional input *"Why I saved this / Notes"*.
* On Card & Reader: subtle editable note pill with inline htmx save (`POST /items/{id}/notes`).

---

## 4. 1-Click "Copy for AI" (Markdown)

### Clipboard Payload Format
Clicking "Copy for AI" on any card or in the reader generates a formatted Markdown block:

```markdown
# [Title of Article or Note]
Source: [URL or "Personal Knowledge Note"]
Date: [YYYY-MM-DD]
Tags: #tag1 #tag2
Notes: [User Notes if present]

[Clean Sanitized Article Body or Note Markdown]
```

### UX Feedback
* Uses `navigator.clipboard.writeText()`.
* Triggers an instant floating toast: *"✓ Copied clean Markdown for AI prompt"*.

---

## 5. "AI Indexed" Status Badges

In the card metadata header:
* `✨ AI Indexed`: Item has 768d vector embeddings in Vectorize + clean Markdown in R2 + FTS5 indexed.
* `⏳ Indexing`: Extraction / vectorization in progress.
* `🔗 Link Only`: Fallback mode (no body extracted).

In **Settings & API**:
* Enhanced MCP connection guides for **Claude Desktop**, **Cursor**, and **opencode** with one-click copyable config snippets.

---

## 6. Remote MCP Server Enhancements (`/api/mcp`)

### New MCP Tools
1. `save_note`:
   ```json
   {
     "name": "save_note",
     "description": "Saves a personal note, code snippet, prompt template, or idea to the user's personal knowledge vault and indexes it for hybrid search.",
     "inputSchema": {
       "type": "object",
       "properties": {
         "title": {"type": "string", "description": "Title of the note"},
         "content": {"type": "string", "description": "Markdown body content"},
         "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags"},
         "is_pinned": {"type": "boolean", "default": false, "description": "Pin note to top"}
       },
       "required": ["title", "content"]
     }
   }
   ```
2. `pin_item`:
   ```json
   {
     "name": "pin_item",
     "description": "Pins or unpins an item to/from the top shelf of the library.",
     "inputSchema": {
       "type": "object",
       "properties": {
         "item_id": {"type": "string"},
         "pinned": {"type": "boolean"}
       },
       "required": ["item_id"]
     }
   }
   ```

### Enriched Returns
* `get_item`: Returns `item_type`, `is_pinned`, `user_notes`, `summary`, `content_text`.
* `search_library`: Returns `item_type`, `is_pinned`, `user_notes`, `rrf_score`.

---

## 7. Verification & Testing

* **Note Creation**: `test_save_note` asserts insertion, FTS5 presence, and `item_type='note'`.
* **User Notes Update**: `test_update_user_notes` verifies persistence and FTS5 update.
* **MCP Tools**: `test_mcp_save_note` and `test_mcp_pin_item` assert tool execution via JSON-RPC.
* **Markdown Endpoint**: `test_copy_markdown_endpoint` verifies clean Markdown payload generation.
