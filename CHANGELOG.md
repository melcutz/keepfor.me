# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Reader highlighting UI (GitHub issue #45):
  - `POST /items/{id}/highlights` — create a highlight with quoted text, optional note, and character offsets
  - `POST /items/{id}/highlights/{hid}/delete` — delete a highlight
  - Text selection in the reader shows a floating "Highlight" button
  - Highlights render inline in the article as `<mark>` elements
  - Highlights list appears under the article with delete controls
  - Highlights created via MCP `create_highlight` tool appear in the reader
  - htmx support for seamless create/delete without page reload
  - No-JS fallback redirects back to reader
  - Tenant-scoped: users only see and manage their own highlights

### Fixed
- **Reader highlights work on multi-paragraph articles and use an inline popover** (GitHub issue #94): selecting text in the reader now maps DOM ranges to `content_text` offsets using the same block-boundary, entity, and whitespace normalization rules as the extractor, clamping triple-click and cross-paragraph selections and trimming trailing newlines. On the server, `POST /items/{id}/highlights` uses `resolve_highlight_offsets` to anchor on the nearest quote occurrence (including whitespace-normalized matches) before rejecting an offset mismatch, and accepts a `color` field. The native `prompt()` and `alert()` dialogs in `templates/reader.html` are replaced with an inline popover (note input and color picker) and inline error feedback that never exposes raw JSON.
- **A keyword search finds the items that hold only some of the words of the query** (GitHub issue #87): searching "local LLM" missed an open-webui save whose text holds "local" but not "LLM". The keyword side matched the words of a query as a whole, and the union of the words was only a fallback that ran when *no* item held every word — so an item that held both words (the charmbracelet/crush one) hid the item that held one of them. The keyword side now matches the content words one by one (in the text, the title and the tags), counts the words an item holds and ranks the results by that count: the items that hold every content word come first, the items that hold only some of them follow them, the ones that hold more words first and the score of the rest after them. The bm25 scores of the words add up, so the score of an item is the score of the whole query — the order inside a tier is unchanged. The filler words ("my", "saved", "library") are no partial match: they are dropped from the query as before, a query that names no content ("my saved") matches as a whole, and a nonsense query ("zxqvbnm", "my saved zxqvbnm") still returns nothing — Ask still refuses it with 0 sources. A short word still matches exactly ("ai" does not match "air"). No migration is needed: the rule lives in the queries (`_word_hits()` and `search_fts()` in `keepfor/search/engine.py`).
- **Delete stale vectors and chunks on textless re-extraction** (keepfor-cloud#160): when `should_index_text` returns `False` during extraction or re-extraction, the processor now deletes the item's existing Vectorize vectors (using the chunk IDs from the `chunks` table and the tenant namespace) and then removes the `chunks` rows. If no chunks exist, no vector call is made. Vector-delete errors are logged and do not fail extraction; chunks are only deleted after the vector delete succeeds.

### Breaking
MCP Server 0.2.0 changes three tool behaviours. The old behaviour is one
argument away in the first two cases.

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
- **MCP `delete_item` no longer destroys anything** (GitHub issue #30): it
  moves the item to the Trash, where the user can restore it for 30 days. The
  R2 snapshots and the vector embeddings are kept, the row is kept, and only
  `items.deleted_at` is set. The tool result changed with it: it now returns
  `{"id", "trashed", "message"}` instead of `{"id", "deleted"}`. `trashed` is
  `false` when the item does not exist (or belongs to somebody else).
- `DELETE /api/items/{id}` is a soft delete too, for the same reason: the
  agent-reachable REST path may not destroy library data. The row, the
  snapshots and the vectors stay until the user deletes the item for good.

### Added
- **Web "Clean up" view** (GitHub issue #75): `GET /cleanup?kind=duplicates|failed_extraction|dead_links|untagged` lists cleanup candidates as ordinary item cards with tabs and counts, linked from /stats and Settings. `find_cleanup` now splits the failed items into two disjoint categories: dead links (a recorded origin HTTP error) and failed extractions (every other recorded failure), so the MCP `find_cleanup` tool gains the `failed_extraction` category and its `dead_links` narrows to origin HTTP errors. No live link checking: the view reads only what is already recorded.
- **Indexer hooks for textless items** (keepfor-cloud#120): the new public
  predicates in `keepfor.search.indexable` let an indexer skip an item that
  holds no text instead of writing it to the index and deleting it after
  extraction. `should_index_text(item)` returns `False` for a failed
  extraction, a binary URL, a mostly unprintable body, an empty body, a bare
  URL and a URL-only body. `should_index_title(item)` exposes title-only
  indexing separately, so an excluded item stays title-indexable (the title
  row of keepfor-cloud#119). The extraction processor consults the hook
  before chunking and embedding, and keeps a title-only row for a textless
  item: a saved image or a bare-URL bookmark has no text to index, but its
  title still finds it in the library search. See
  `docs/extension-points.md`, section 8.
- **Scoped Trash purge** (GitHub issue #37): `purge_trash()` takes the tenant
  `scope` now, so a multi-tenant host destroys the R2 snapshots under the
  tenant prefix (`t/<tenant_id>/...`) and the vectors in the tenant
  namespace instead of orphaning them. An unscoped call is **refused** in
  multi-tenant mode (a single-tenant self-host keeps working unchanged),
  the purge is then limited to the scope's own user (a shared database holds
  the rows of every tenant), and the new `limit` caps how many items one
  call destroys. `list_purgeable_items(db, user_id, older_than_days, limit)`
  exposes the 30-day selection, so a host never copies the SQL.
- **MCP `library_digest`** (GitHub issue #22): a read-only recap of the library for a period — how many items were **saved** and **opened**, how many are still **unread**, the **top tags**, and a short list of **unread highlights**. Takes optional `since`, `until` and `tag` (child tags count). Inputs are bounded (32-character dates, 64-character tag; unparsable or reversed ranges are rejected) and the response is capped (10 tags, 5 highlights, 5 tags per highlight, 120-character titles, 200-character urls) and stays under 4 KB.
- **Read-only MCP tokens** (GitHub issue #29, part of keepfor-cloud#61): a
  Personal Access Token can now be limited to read-only. Over MCP a read-only
  token sees only the read tools in `tools/list`, and every write or
  destructive tool is refused with a JSON-RPC error
  (`error.data.type` = `insufficient_scope`) **before it runs**, so nothing
  is written.
  - Scopes: `read`, `write` and `*` (all). `*` is the default and is what a
    token without scope information gets, so existing tokens keep their full
    access. `write` implies `read`.
  - The token form on the settings page offers **Read-only** next to **Full
    access**, and the token table shows the scope of every token.
  - Migration `0012_pat_scopes.sql` (additive): the nullable `scopes` column
    on `personal_access_tokens`. Apply it **before** deploying:
    `npx wrangler d1 migrations apply keepfor-me-db --remote`. Without the
    column PAT authentication fails; with it, NULL means "all scopes", so
    tokens created before the migration are unaffected.
  - Scopes are enforced over MCP (per tool) and over REST: a read-only token
    gets `403` on every write route. `/api/search` and `/search` are the
    exception — a search is a read — and PATs are refused on `/settings/*`
    entirely, so a token can never mint a broader one.
- **Trash** (GitHub issue #30): a deleted item is never lost. MCP
  `delete_item` and `DELETE /api/items/{id}` set `items.deleted_at` and stop
  there — the row, the raw/clean R2 snapshots and the vector embeddings stay,
  so a restore is instant.
  - A **Trash** view (nav → Trash, or `/trash`) lists the trashed items with
    two actions: **Restore** (back into the library) and **Delete forever**
    (the only permanent path; it reuses the existing hard delete, so the row,
    the snapshots and the vectors go). It pages, so nothing hides behind the
    first 50 rows.
  - Trashed items are excluded everywhere: library, pinned shelf, search (FTS
    and vector results), MCP `list_items`/`search_library`/`search_passages`/
    `find_item`/`read_item`/`get_item`/`related_items`/`reading_history`/
    `library_digest`, the
    MCP resources and prompts, the status pills, the tag counts and
    co-occurrence, the tag cleanup report, the `find_cleanup` report,
    `bulk_update` matching, the pending tag suggestions, the stats page and
    the library export (JSON and HTML). The highlights and the collections
    too: a trashed item takes no new highlight, its highlights are not
    listed, and it is neither counted nor listed in a collection.
  - The trash keeps the annotations of an item: trashing and restoring never
    touch its highlights or its collection links, so a restore gives them
    back. Only the permanent delete (Trash → Delete forever, the 30-day
    purge, the cleanup of the failed extractions) drops them.
  - A trashed item is also immutable: it cannot be pinned, annotated,
    archived or re-tagged while it waits in the Trash.
  - `purge_trash(db, env, user_id=None, older_than_days=30)` hard-deletes
    trashed items older than the window, so a host can schedule the 30-day
    purge. Without a `user_id` it purges every user of the database. A
    multi-tenant host passes the tenant `scope` too (see the scoped purge
    above): the R2 prefix and the Vectorize namespace are per tenant.
  - Migration `0014_trash.sql` (additive): the nullable `items.deleted_at`
    column and its `(user_id, deleted_at)` index. Apply it before deploying:
    `npx wrangler d1 migrations apply keepfor-me-db --remote`.
  - Saving a url that sits in the Trash brings it back: the canonical url is
    unique per user, so re-saving restores the existing row.
  - **Settings → Cleanup failed items** only touches live failed extractions.
    A trashed one stays in the Trash until the user deletes it for good or the
    30-day purge takes it.
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
- **MCP `find_cleanup`** (GitHub issue #21): a read-only tool that suggests what could be cleaned up in the library, without changing anything.
  - Categories: `duplicates` (items whose urls normalize to the same value — lowercase scheme/host, no `www.`, no tracking parameters such as `utm_*`/`fbclid`/`gclid`, no trailing slash, no fragment), `dead_links`, `never_opened`, `untagged` and `near_duplicate_tags`.
  - Dead links come from data already stored on the item: `items.status = 'failed'` plus the recorded `fail_reason`. **No network requests are made**, so a link that was never fetched cannot be detected and the category stays empty for a library without failed extractions.
  - `never_opened` covers items older than `older_than_days` (default 90) that were never opened in the reader; archived items are skipped.
  - Inputs are validated: unknown categories and out-of-range `limit` (1–100, default 20) or `older_than_days` return an `invalid_arguments` tool error.
  - Each category reports the candidates, the total number of candidates and a `truncated` flag. The item categories list one id with a reason; the group categories list one entry per group — `duplicates` gives the group's ids (capped at 10), its size and whether the id list was truncated, `near_duplicate_tags` gives the group's tag names — so `limit` caps groups and a url saved many times stays a small response.
  - Every query is scoped to the caller's `user_id`, and the tool never writes.
- **Notes import** (GitHub issue #15): upload a **Google Keep Takeout zip**, an **Evernote `.enex`** file, or a **zip of Markdown/text files** (Obsidian vault, Apple Notes via Exporter, Simplenote) from Settings → Import. Notes become searchable Keepfor notes with tags and original creation dates; Keep checklists render as Markdown tasks, archived Keep notes land in the archive, and trashed Keep notes are skipped. Links inside notes (body URLs, Keep `WEBLINK` annotations, ENEX `source-url` web clips) become regular URL saves tagged `from-notes`.
  - Dedupe on `(user_id, source, source_id)` with a content-hash fallback: re-importing the same export inserts nothing new.
  - Parsed notes fan out through the queue in chunks (same as bookmark imports), so a 1,000-note export never times out the request.
  - Upload caps: 50 MB for `.zip`/`.enex`, 10 MB for HTML/CSV. Attachments are listed in the note body (`📎 file (not imported)`) but never stored.
  - Each imported note and each queued URL costs 1 `import` entitlement unit (the same pool as bookmarks); the import stops at the remaining quota and reports the refused items as "N skipped: plan limit".
  - Import results (and any error) show as a one-shot toast on the library page; a failed upload also shows its error on the settings page it lands on. Logs contain counts only, never note content.
  - Migration `0008_import_sources.sql` (additive): `items.source`, `items.source_id` and a unique index backing the dedupe.
  - MCP `search_library` now surfaces imported notes alongside articles.
- **MCP highlights and collections** (GitHub issue #24): nine new tools for the two curation features the library was missing.
  - `create_highlight`, `list_highlights`, `delete_highlight`: a highlight quotes a passage of an item and anchors it with the character offsets it covers inside `content_text`. `start_offset` defaults to `0`, `end_offset` to `start_offset + len(text)`; an end offset past the end of the item's content is refused.
  - `create_collection`, `list_collections`, `add_to_collection`, `remove_from_collection`, `rename_collection`, `delete_collection`: a collection is a named bucket of library items, addressed by name or id. Creating one with an existing name returns it unchanged; renaming onto a taken name is refused, never merged. Deleting a collection drops the links only — the items it held are never deleted.
  - `add_to_collection` and `remove_from_collection` take at most 100 item ids per call and report `truncated`; ids that are not yours, that do not exist, or that are already in the collection are skipped and counted.
  - Every query is scoped to the caller's `user_id`: another user's item, highlight or collection is not found — never read and never changed.
  - Input caps: 2,000 characters of quoted text, 500 per note, 32 per color, 64 per collection name, 200 per description, 100 highlights and 50 collections per list call, 20 item ids listed per collection.
  - Migration `0013_highlights_and_collections.sql` (additive): the `highlights`, `collections` and `collection_items` tables. **Apply it before deploying** (`npx wrangler d1 migrations apply keepfor-me-db --remote`): until it is applied the new tools report an error, the rest of the app is unaffected.
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
- **MCP `bulk_update`** (GitHub issue #20): updates the status, the pin state and the tags of every item matching a filter — status group (`saved`/`extracting`/`failed`, the same groups the library pills use), saved date range (`since`/`until`), `domain`, `pinned`, `type` (`note`/`url`) and the usual tag filters. It defaults to `dry_run=true` and reports `total_matched`, `affected` and `truncated` without writing anything; `dry_run=false` applies the change. At most 200 items are updated per call, an empty filter (which would match the whole library) and an empty change are rejected with an `invalid_arguments` error, and the tool is annotated destructive.
- All new tools include MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`). They are advisory hints for the client, and the server now uses them to enforce the scopes of a token (see *Read-only MCP tokens* above). A token is never rejected for missing scope information: no scope information means full access.
- `search_library` and `list_items` now return structured responses with `items`, `total`, `limit`, `offset` (and `cursor`/`fields` support).
- Tenant isolation verified for all new MCP tools via isolation test suite.
- **MCP resources and prompts** (GitHub issue #23): the MCP server now also speaks the resources and prompts parts of the spec, with the same auth and tenant scoping as the tools.
  - Resources: `keepfor://item/{item_id}` (one saved article or note) and `keepfor://tag/{name}` (one tag with the items carrying it). `resources/list` returns the most recent items (at most 50) and the most used tags (at most 50) as concrete resources, `resources/templates/list` returns the two URI templates, and `resources/read` returns the payload as JSON.
  - Prompts: `research_brief` (arguments `topic`, `max_items`) briefs the assistant on what the user already saved about a topic; `weekly_review` (arguments `days`, `tag`) reviews what the user saved over the last days.
  - Inputs are capped: a resource URI is limited to 512 characters, one resource carries at most 20,000 characters of text (and reports `truncated`), a tag resource lists at most 50 items (and reports `items_truncated`), a prompt argument is limited to 500 characters, `max_items` to 20 and `days` to 90.
  - Resources and resource templates carry MCP annotations (`audience`, `priority`); the tools keep their `readOnlyHint`, `destructiveHint` and `idempotentHint` annotations.
  - Failures use the codes the spec names: `-32002` for a resource that does not exist, `-32602` for invalid arguments or an unknown prompt, `-32603` for internal errors. `error.data.type` keeps the `invalid_arguments` / `not_found` / `internal_error` discriminator the tools use.
  - Every query is scoped to the caller's `user_id`: reading another tenant's item or tag fails with `not_found`, and neither listing nor a prompt ever quotes another tenant's data. Covered by the isolation test suite.

### Fixed
- **A keyword search matches plural and inflected forms** (GitHub issue #76): searching "local LLM" missed an item whose text only says "LLMs" — the multi-word keyword query required every word in the same item (`local* AND llm*`), so an item that names only one of the words had no keyword hit and was dropped (the semantic hit fell under the hybrid floor). When no item holds every content word of a query, the keyword side now falls back to the union of its words, so a natural-language query finds the content it names; the strict query still wins when it matches anything. A content word of at least three letters also matches every word that starts with it, in the text, in the tags and in the whole-word search of the semantic-only mode — the tag "llms" answers "llm" now — and a shorter word matches exactly, so "ai" no longer matches "air". Plurals in the indexed text already folded with the Porter stemmer ("model" matched "models" all along); the prefix rule is what covers tag names, which are matched live and never stemmed. The stopwords and the library words stay no keyword hit ("my saved zxqvbnm" is still empty) and a nonsense query ("zxqvbnm") still returns nothing. **Keyword parity with cloud Ask** (keepfor-cloud#142 `question_tokens`): the keyword selection is unchanged — the same content words — and for plurals the two sides now effectively agree (cloud folds them with Porter plus its `_word_forms` expansion, the core with Porter plus the prefix match); the core is looser in that any word starting with a content word answers it, and it keeps 1-2 letter words as exact hits where cloud drops words under three letters. No migration is needed: the index already tokenizes with `porter unicode61` (`migrations/0001_initial_schema.sql`), the prefix rule lives in the queries (`_keyword_match()` in `keepfor/search/engine.py`).
- **Favicon cache misses are rate limited, and an anonymous visitor always gets the fallback** (keepfor-cloud#154): a signed-in request could still make the Worker fetch any number of arbitrary domains and write an R2 object for each, and an anonymous request was answered with the real icon on a cache hit and the fallback on a miss — which revealed whether *some* user's library holds that domain (the cache is global). Cache **misses** are now capped per user and per IP (60 misses per 10 minutes, counted in the same `rate_limits` table as the auth throttle); over the cap the route serves the transparent fallback with no upstream fetch and no cache write. A cache **hit** costs nothing, so browsing a library is unaffected. An anonymous request now always gets the fallback — same status, body, content type and `Cache-Control` — so the response never reveals the cache state.
- **`BlobStore.delete_prefix` refuses an empty prefix** (GitHub issue #79): an empty store prefix combined with an empty `rel_prefix` matched *every* key of the bucket, so one call could wipe the whole bucket — the snapshots of every tenant of a shared R2. The call is now refused with a `ValueError` before the bucket is listed or deleted, so it has to name a scope (the store prefix or a `rel_prefix`). A prefixed store (the tenant prefix `t/<tenant_id>/` of the cloud, or a named `rel_prefix`) deletes exactly as before, and a missing bucket still returns `0`.
- **Unicode letters in tag names and NFC normalisation** (GitHub issue #74): `validate_tag_name` only accepted ASCII `[a-z0-9]`, so creating, renaming or merging tags with umlauts, `ß` or accented characters (`konsumgüterbranche`, `straße`, `café`) on `/tags` or over MCP failed, even though the tagger already produced German suggestions with those characters. Tag validation now accepts any Unicode letter or digit (`[^\W_]`) with the same single-separator and 2-64 character rules, and `normalize_tag` applies Unicode NFC normalisation before lowercasing across validation and save paths so composed (`ü`) and decomposed (`u` + combining diaeresis) inputs resolve to one tag without folding distinct letters (`ü` to `u` or `ß` to `ss`). Existing ASCII tags are unchanged and no migration is required; pre-existing tags that differ only by NFC form are not auto-merged and can be merged manually on `/tags` if needed.
- **The pager keeps the page size you picked** (GitHub issue #62): every prev/next/page link pinned `per_page=20`, so a page size other than the default was overridden the moment the feed moved to another page. The links now carry the chosen size, and carry no `per_page` at all when it is the default (20). The rest of the query (`?view=`, the search text, the filters) travels as before.
- **The library words are no keyword hits** (GitHub issue #63): every word of a query counted as a keyword, including the words of the library ("saved", "my", "library", "bookmark"), which are in the text of every item of a library. So "my saved zxqvbnm" found a keyword hit on "saved" and kept unrelated semantic results instead of showing the empty state. Only the content words of a query are a keyword hit now: the stopwords and the library words are dropped from the FTS terms, from the tag match and from the whole-word search of the semantic-only mode (ported from keepfor-cloud#142). A query of these words only ("library") falls back to its raw words, so it still finds the item titled with it, and a real search is unchanged ("local LLM" still matches).
- **Saving a link keeps the library layout** (GitHub issue #64): the Save Link form posted no layout, so `/save` redirected to `/` and the library came back in the saved preference (`localStorage`) instead of the layout the page was in. The form now carries the layout of the page it was rendered on (a hidden `view` field, empty without an explicit `?view=`), the switcher updates it, and `/save` redirects to `/?view=<layout>` for the three known layouts only.
- **The library layout survives the navigation**: `?view=cards|list|compact` only won on the very first load. The pager, the category tabs, the status chips, the tag filters and the search all dropped the parameter, so the next request rendered the default layout and the client fell back to the saved preference (`localStorage`) instead of what the URL asked for. Every URL the library page builds now carries the layout, the switcher marks the rendered layout server-side (`aria-pressed` on the matching button), and picking a layout rewrites the page's links, the URL and the hidden `view` field the htmx requests send. Without an explicit `?view=` the links stay layout-free and the client keeps applying the saved preference.
- **The reader shows the headline once** (GitHub issue #57): a generic article that repeats its title in its own leading `<h1>` (a Cloudflare blog post, for instance) was rendered with two identical headings: the reader's own title and the article's. The generic extractor now drops that leading `<h1>` when it matches the item title — case, quotes and whitespace are ignored. A different H1 stays, and an H1 further down is a section heading, not a duplicate.
- **The search state is not sticky any more** (GitHub issue #56): the query lived only in the search box, so the URL said nothing about it and the browser brought the last query back — plain `/` could show the previous search instead of the whole library. The URL is the source of truth now: a live search is mirrored into `?q=` (so it can be shared, bookmarked and survives a reload), and a value the browser restored on its own is dropped when the URL asks for no search. Plain `/` is the unfiltered library with an empty search box, and a page with an active search shows a **Clear search** control that returns to `/` without the query, keeping the layout the page is in (`?view=cards` stays in Cards).
- **A keyword search also matches the tags and the summary** (GitHub issue #55): searching "local LLM" returned Crush but not open-webui, although open-webui was the closest vector (cosine similarity ~0.63). The hybrid rule keeps a semantic-only hit only when it is strong (>= 0.80) and near the top, so an item that names the concept nowhere in its title or its text had no keyword hit and was dropped. The keyword side now covers the tags (matched on word boundaries and case-insensitively, like the FTS tokenizer) and the summary and the page description are indexed with the text. The 0.80 floor is unchanged: a nonsense query ("zxqvbnm") still returns nothing.
- **Pinned items lead the browse feed** (GitHub issues #39, #40): the feed was ordered by `created_at` alone, so a save pinned last year sank below the newer saves and page 1 opened with unpinned items. The browse query now orders by `is_pinned DESC, created_at DESC`, in every view (Cards, List, Compact) and in every branch of the query (plain, tag-filtered, untagged-filtered) — the index `idx_items_user_pinned_created` already served that order without a temp b-tree. A search stays relevance-ordered.
- **List and Compact views no longer drop items** (GitHub issue #39): those two views hid pinned items with a CSS rule, so the feed showed fewer items than the category and status counts promised — the Archive category looked empty while its count said otherwise. Cards, List and Compact now list exactly the items the counts report, pinned and archived ones included. The layout is a URL parameter now (`?view=cards|list|compact`), so the server renders the requested layout and a shared link keeps it.
- **Auto-suggested tags are noun phrases, not clause fragments** (GitHub issue #44): the suggested tags of a saved article were arbitrary word runs ("vacuum changed", "carriers around", "supply chain ocean"), because the keyphrase extractor chunked the text into fixed three-word runs and scored them by word degree. Candidates are now runs of *significant* words (repeated in the document, or named by the title, the URL slug or a Title Case entity), cut at sentence, stopword and rare-word boundaries, trimmed back to their noun core and kept only when they are a repeated collocation or anchored in the title/slug. A word that only ever appears inside a collocation is not proposed on its own, a word the user already tags is never re-proposed, a match from the user's own vocabulary is proposed first and the list is capped at 5 suggestions.
- **Tag suggestions follow the page language** (GitHub issue #58): a German page was cut at English stopwords only, so its suggestions were "abgerufen" (citation boilerplate), "hamburg und" (a conjunction ending a phrase) and "hamburg" — the German function words never ended a phrase. The tagger now uses the stopword list of the page's language: the page's own `<html lang>` when the extractor saw it, a stopword-ratio guess otherwise. German articles, pronouns, prepositions, conjunctions and auxiliaries are never a phrase edge, and the German citation boilerplate ("Abgerufen am ...", "Archiviert vom Original ...") is never a topic. English pages are unaffected.
- **A nonsense search no longer returns unrelated items** (GitHub issue #41): a result now needs a keyword hit, or a semantic-only hit that is strong (>= 0.80) and near the top of the vector results. In semantic-only mode a whole query term in the title or the text counts as the keyword hit. Searching "zxqvbnm" shows the "No results" empty state instead of the closest vectors, in both modes, and the result count follows the filtered set. The embedding model is `@cf/baai/bge-base-en-v1.5` and Vectorize scores are cosine similarities: measured Oct 2026, a nonsense query scored 0.587 on the 7-item QA library but 0.727 on a real prod library, where the real hits score 0.63-0.67. The baseline climbs with the library size, so no fixed floor separates noise from a match.
- **Pinned items are not rendered twice** (GitHub issue #41): the pinned shelf is gone, so a pinned item is listed once, in the feed of every view. The browse query no longer excludes the pinned items (the shelf they used to be duplicated by does not exist anymore), so the header count, the pager and the rendered cards agree. A search keeps the pinned match too.
- **The sidebar "All Items" count stays the library total during a search** (GitHub issue #41): it counts the non-trashed saves in scope again instead of the number of search hits.
- **Import errors no longer leak internal detail**: a failed note or link used to store the raw exception text (`note: OperationalError: ...`) in `import_status.errors`, which is rendered in the library and settings panels. The stored message is now generic ("note: could not be imported", "link: could not be queued"); the exception goes to the server log.
- **MCP `dry_run` fails closed**: `dry_run` is now accepted only as a JSON boolean. A `null`, `0` or `"false"` used to be coerced with `bool(...)` and silently *applied* the change; it is now rejected with an `invalid_arguments` error. The same check covers `bulk_update`, `delete_tag`, `tag_items` and `retag_by_query`.
- **MCP `bulk_update` tag names**: `add_tags`/`remove_tags` now go through the same normalization and validation as `create_tag` (2-64 characters of `a-z`, `0-9`, space, `_` or `-`). A name the tag API would reject is refused with an `invalid_arguments` error instead of creating a junk tag.
- **MCP `bulk_update` is atomic**: the status, pin and tag statements now run in a single batch (one transaction), so a failure in the middle of an apply leaves the library unchanged instead of half-updated. The SQLite test backend rolls back a failed batch like D1's transactional `batch()`.
- **Zip bombs in notes import**: both zip parsers (Google Keep Takeout and Markdown/text) now check the archive metadata before opening a single member — at most 20,000 members and 200 MB of declared uncompressed size — and read every member with a bounded read (2 MB per file, bigger ones are skipped and counted). Breaking a cap gives a visible error and imports nothing.
- **XML entity expansion in `.enex`**: ENEX files are parsed with `defusedxml`, so a DTD/entity payload (billion laughs) is rejected with a visible error instead of being expanded.
- **Plan limits on notes import**: imported notes and the URLs inside them count against the plan's item limit like bookmarks. The import stops at the remaining quota and reports "N skipped: plan limit" instead of aborting the whole upload (or refusing a file that only partly fits).
- `reading_history` with `event="opened"` plus a tag or query filter raised a SQLite parameter mismatch instead of returning results.
- The FTS5 re-index after extraction read the item's provenance fields by `id` only; it is now scoped to the owning `user_id` as well.
- **Note bodies lost on read**: the MCP 0.2.0 content cap was applied inside `get_item`, so every caller (the reader page, the Copy-for-AI action, `GET /api/items/{id}`) got a truncated body — and notes, which have no excerpt, lost their text entirely. The cap now lives in the MCP `get_item` tool only; `get_item` returns the full body by default.
- **Wikipedia reader output** (GitHub issue #43): a Wikipedia article is extracted with its own rule. The `[edit]` fragments next to the headings, the duplicated H1, the site chrome and the junk byline ("Contributors to Wikimedia projects") are gone, the title loses the " - Wikipedia" suffix, and the date is the **last modification** of the article instead of its creation date.
- **Published dates are normalised** (GitHub issue #43): every extractor stores the published date as `YYYY-MM-DD`. The generic extractor used to store the raw `article:published_time` (`2024-03-01T12:00:00+00:00`), which the library card then rendered verbatim. The card shows the date the item was saved and, when the article has one, a labelled `Published YYYY-MM-DD` next to it. A stored date that is not a date is not rendered at all.

### Changed
- The pinned shelf ("Pinned References & Notes") is gone. Pinned items are listed in the feed of every view, marked with the existing "Pinned" badge and an amber border, so a card is never listed twice and never missing.
- The core UI (Tags, Trash, Stats, Reader, save popup and the browser extension) uses the homepage brand blue tokens instead of green accents; `tests/test_brand_tokens.py` keeps green and hard-coded blue classes out of the templates.
- FTS5 index now includes the provenance fields (`agent_summary`, `why_saved`, `source_context`, `saved_by`) in `content_text` for searchability.
- Migration 0009 adds `agent_summary`, `why_saved`, `source_context`, `saved_by` columns to `items` table.
- Migration 0010 adds `description`, `color` and `parent_id` columns to the `tags` table for tag metadata and hierarchy.
- Migration 0013 adds the `highlights`, `collections` and `collection_items` tables for the MCP highlights and collections tools.
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
