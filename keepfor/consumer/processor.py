# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from keepfor.spi import Action, TenantScope

from keepfor.consumer.extractor import article_from_reader_markdown, extract_article
from keepfor.models.db import Database
from keepfor.utils.chunker import recursive_character_split
from keepfor.utils.egress import (
    _get_env_val,
    fetch_limited,
    policy_from_env,
    validate_url,
)
from keepfor.utils.logging import logger

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# Full browser header set: naive bot checks reject requests that look like
# scripts (missing Accept-Language / Sec-Fetch-*), which was a chunk of
# our 403s. No Accept-Encoding: avoids gzip/br decode handling.
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
MAX_HTML_BYTES = 5 * 1024 * 1024  # 5MB
FETCH_TIMEOUT = 20
SUMMARY_TIMEOUT = 4.0
SUMMARY_MODEL = "@cf/meta/llama-3.2-3b-instruct"


def get_reader_proxy_base(env: Any = None) -> str:
    """Return configured reader proxy base URL, normalized with trailing slash.

    Returns an empty string if unset or invalid.
    """
    base = _get_env_val(env, "READER_PROXY_BASE")
    if not base or not isinstance(base, str):
        return ""
    base = base.strip()
    if not base:
        return ""
    if not base.endswith("/"):
        base += "/"
    return base


class OriginHttpError(RuntimeError):
    """Origin returned an HTTP error status (carries the status code)."""

    def __init__(self, status_code: int, url: str):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code} returned by origin server ({url})")


class BlockedPageError(RuntimeError):
    """Fetch succeeded but returned a login wall / soft 404 / redirect stub.

    Not retryable: the origin consistently refuses anonymous fetches, so the
    item is recorded as failed with the reason and acknowledged (no queue
    retries). Stored as-is it would look like a saved 1-word article.
    """


async def fetch_page_html(
    url: str,
    headers: dict | None = None,
    *,
    action: Action = "fetch",
    tenant_id: str | None = None,
    env: Any = None,
) -> str:
    """Fetch URL HTML via pyfetch or urllib with egress limits and SSRF policy."""
    policy = policy_from_env(env)
    try:
        from keepfor.runtime import get_providers

        budget = get_providers().fetch_budget
    except Exception:
        budget = None

    res = await fetch_limited(
        url,
        headers=headers or BROWSER_HEADERS,
        policy=policy,
        budget=budget,
        tenant_id=tenant_id,
        action=action,
    )
    if res.status >= 400:
        raise OriginHttpError(res.status, res.url or url)
    return res.text


async def fetch_jina_reader(
    url: str,
    *,
    env: Any = None,
    tenant_id: str | None = None,
) -> str:
    """Fetch reader-proxy markdown for a URL blocked to direct fetching."""
    proxy_base = get_reader_proxy_base(env)
    if not proxy_base:
        raise RuntimeError("READER_PROXY_BASE is not configured")
    policy = policy_from_env(env)
    validate_url(url, policy)
    proxy_url = proxy_base + url
    validate_url(proxy_url, policy)
    return await fetch_page_html(
        proxy_url,
        headers={**BROWSER_HEADERS, "Accept": "text/markdown"},
        action="reader_proxy",
        tenant_id=tenant_id,
        env=env,
    )


async def generate_triage_summary(env: Any, plain_text: str) -> str | None:
    """Generate a 2-bullet triage summary with Workers AI (fail-open).

    Returns None when AI is unavailable, text is too short, or inference
    fails/times out - extraction must never fail because of summarization.
    """
    try:
        from keepfor.runtime import get_providers

        return await get_providers().ai.summarize(env, plain_text, max_words=40)
    except Exception:
        return None


MAX_REDIRECT_HOPS = 2
PROXY_FALLBACK_STATUSES = frozenset({403, 429, 530, 522, 520})


async def _fetch_and_extract(
    url: str,
    item_id: str,
    *,
    env: Any = None,
    tenant_id: str | None = None,
) -> tuple[dict, str | None]:
    """Fetch + extract, following bounded redirect stubs and reader proxy.

    Returns (extracted, raw_html). raw_html is None for proxy-sourced items.
    Raises BlockedPageError when the payload is a wall rather than an article.
    """
    policy = policy_from_env(env)
    current_url = url
    raw_html: str | None = None
    for hop in range(MAX_REDIRECT_HOPS + 1):
        try:
            try:
                raw_html = await fetch_page_html(
                    current_url, tenant_id=tenant_id, env=env
                )
            except TypeError:
                raw_html = await fetch_page_html(current_url)
            extracted = extract_article(raw_html, current_url)
        except OriginHttpError as direct_err:
            reader_proxy_base = get_reader_proxy_base(env)
            if (
                not reader_proxy_base
                or direct_err.status_code not in PROXY_FALLBACK_STATUSES
            ):
                raise
            # SSRF check: ensure target URL is not pointing to private/internal IPs
            validate_url(current_url, policy)
            logger.info(
                f"Direct fetch got {direct_err.status_code}, "
                f"trying reader proxy: item={item_id}"
            )
            try:
                try:
                    proxy_md = await fetch_jina_reader(
                        current_url, env=env, tenant_id=tenant_id
                    )
                except TypeError:
                    proxy_md = await fetch_jina_reader(current_url)
            except Exception:
                raise direct_err from None
            extracted = article_from_reader_markdown(current_url, proxy_md)
            raw_html = None

        redirect_url = extracted.get("redirect_url")
        blocked = extracted.get("blocked_reason")
        # A redirect stub is worth one more hop; a wall is not.
        if redirect_url and hop < MAX_REDIRECT_HOPS:
            normalized_redirect = validate_url(redirect_url, policy)
            logger.info(
                f"Following redirect stub: item={item_id} "
                f"{current_url} -> {normalized_redirect}"
            )
            current_url = normalized_redirect
            continue
        if blocked:
            raise BlockedPageError(blocked)
        return extracted, raw_html

    raise BlockedPageError("redirect loop did not resolve to an article")


async def extract_and_store(
    db: Database,
    env: Any,
    item_id: str,
    url: str,
    *,
    scope: TenantScope | None = None,
    user_id: str | None = None,
) -> None:
    """Fetch, extract, upload snapshots to R2, and update D1 and Vectorize.

    Shared core used by both the queue consumer (via process_single_item,
    which builds Database from the D1 binding) and the synchronous inline
    fallback in save_item (which reuses the caller's Database handle).
    On failure the item is marked failed in D1 and the error re-raised.
    """
    item_row = await db.query_first(
        "SELECT user_id, title FROM items WHERE id = ?;", (item_id,)
    )
    if not item_row:
        logger.warning(f"Item not found for processing: {item_id}")
        return
    row_user_id = item_row["user_id"]
    if user_id is not None and row_user_id != user_id:
        logger.warning(
            "Mismatched user_id for item %s: message has %s, row has %s",
            item_id,
            user_id,
            row_user_id,
        )
        return
    user_id = row_user_id

    if scope is None:
        from keepfor.defaults import default_scope

        scope = default_scope(db, env, user_id)

    from keepfor.runtime import get_providers

    providers = get_providers()

    try:
        logger.info(f"Starting extraction: item={item_id}, url={url}")
        # 1. Fetch raw HTML, following redirect stubs and falling back to a
        # reader proxy when the origin forbids datacenter fetches (403).
        # Other statuses and a failed proxy keep the original error.
        try:
            extracted, raw_html = await _fetch_and_extract(
                url,
                item_id,
                env=env,
                tenant_id=scope.tenant_id if scope else user_id,
            )
        except TypeError:
            extracted, raw_html = await _fetch_and_extract(url, item_id)

        # 2. Store raw snapshot in R2 (skipped for proxied markdown)
        blobs = scope.blobs(env)
        if raw_html is not None:
            await blobs.put_html(blobs.key("items", item_id, "raw.html"), raw_html)

        # 3. Store clean reader HTML in R2
        await blobs.put_html(
            blobs.key("items", item_id, "clean.html"),
            extracted["clean_html"],
        )

        # 4. Triage summary via Workers AI (fail-open, bounded).
        summary = None
        summarize_allowed = False
        try:
            decision = await providers.entitlements.check(
                scope.tenant_id, "summarize", qty=1
            )
            summarize_allowed = decision.allowed
        except Exception as exc:
            logger.warning("Entitlement check for summarize failed: %s", exc)

        if summarize_allowed:
            summary = await generate_triage_summary(
                env, extracted.get("content_text") or ""
            )
            if summary:
                try:
                    await providers.entitlements.record(
                        scope.tenant_id, "summarize", qty=1
                    )
                except Exception as exc:
                    logger.warning("Entitlement record for summarize failed: %s", exc)

        # 4b. Update items in D1
        await db.execute(
            """
            UPDATE items
            SET title = ?, byline = ?, site_name = ?, published_date = ?,
                excerpt = ?, content_text = ?, word_count = ?, is_fallback = ?,
                image_url = ?, summary = ?,
                status = 'ok', fail_reason = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND user_id = ?;
            """,
            (
                extracted["title"],
                extracted["byline"],
                extracted["site_name"],
                extracted["published_date"],
                extracted["excerpt"],
                extracted["content_text"],
                extracted["word_count"],
                extracted["is_fallback"],
                extracted.get("image_url"),
                summary,
                item_id,
                user_id,
            ),
        )

        # 5. Update FTS5 virtual table
        await db.execute(
            "DELETE FROM items_fts WHERE item_id = ? AND user_id = ?;",
            (item_id, user_id),
        )
        await db.execute(
            "INSERT INTO items_fts (item_id, user_id, title, content_text) "
            "VALUES (?, ?, ?, ?);",
            (item_id, user_id, extracted["title"], extracted["content_text"]),
        )

        # 5b. Automatic tagging (fail-open: a tagger defect must never
        # flip an extracted item to failed).
        try:
            from keepfor.models.items import (
                add_suggestions,
                add_tags_to_item,
                get_item_tags,
            )
            from keepfor.utils import tagger as _tagger

            vocab_rows = await db.query_all(
                "SELECT name FROM tags WHERE user_id = ?;", (user_id,)
            )
            vocab = [r["name"] for r in vocab_rows]
            matched = _tagger.match_existing_tags(
                title=extracted.get("title") or "",
                excerpt=extracted.get("excerpt") or "",
                url=url,
                body=extracted.get("content_text") or "",
                user_tags=vocab,
            )
            if matched.auto_apply:
                await add_tags_to_item(db, user_id, item_id, matched.auto_apply)
            already = set(await get_item_tags(db, user_id, item_id)) | set(
                matched.auto_apply
            )
            novel = _tagger.suggest_new_tags(
                title=extracted.get("title") or "",
                excerpt=extracted.get("excerpt") or "",
                url=url,
                body=extracted.get("content_text") or "",
                user_tags=vocab,
                limit=5,
            )
            phrases = [(s.phrase, s.score) for s in novel if s.phrase not in already]
            for tag in matched.suggest_only:
                if tag not in already and tag not in [p for p, _ in phrases]:
                    phrases.append((tag, 1.0))
            if phrases:
                await add_suggestions(db, user_id, item_id, phrases)
            rules_rows = await db.query_all(
                "SELECT field, substr, tag FROM tag_rules WHERE user_id = ?;",
                (user_id,),
            )
            from keepfor.models.items import match_rules as _match_rules

            rule_tags = _match_rules(
                [dict(r) for r in rules_rows],
                url,
                extracted.get("title") or "",
            )
            if rule_tags:
                await add_tags_to_item(db, user_id, item_id, rule_tags)
        except Exception as exc:
            logger.warning(f"Auto-tagging failed: item={item_id}: {exc}")

        # 6. Semantic chunking & Vectorize indexing
        chunks = recursive_character_split(
            extracted["content_text"], target_tokens=400, overlap_tokens=50
        )

        vectors = scope.vectors(env)
        can_embed = False
        if chunks and vectors.index is not None:
            try:
                decision = await providers.entitlements.check(
                    scope.tenant_id, "embed", qty=len(chunks)
                )
                can_embed = decision.allowed
            except Exception as exc:
                logger.warning("Entitlement check for embed failed: %s", exc)

        if can_embed:
            # Batch embeddings in groups of 10
            vectors_to_upsert = []
            chunk_records = []

            for i in range(0, len(chunks), 10):
                batch_chunks = chunks[i : i + 10]
                texts = [c.text for c in batch_chunks]
                embeddings = await providers.ai.embed(env, texts)

                for chunk, vector in zip(batch_chunks, embeddings):
                    chunk_id = f"item_{item_id}_chunk_{chunk.index}"
                    vectors_to_upsert.append(
                        {
                            "id": chunk_id,
                            "values": vector,
                            "metadata": {
                                "item_id": item_id,
                                "user_id": user_id,
                                "chunk_index": chunk.index,
                            },
                        }
                    )
                    chunk_records.append(
                        (chunk_id, item_id, user_id, chunk.index, chunk.token_count)
                    )

            # Upsert into Vectorize
            if vectors_to_upsert:
                await vectors.upsert(vectors_to_upsert)
                try:
                    await providers.entitlements.record(
                        scope.tenant_id, "embed", qty=len(vectors_to_upsert)
                    )
                except Exception as exc:
                    logger.warning("Entitlement record for embed failed: %s", exc)

            # Update D1 chunks tracking table
            await db.execute(
                "DELETE FROM chunks WHERE item_id = ? AND user_id = ?;",
                (item_id, user_id),
            )
            for rec in chunk_records:
                await db.execute(
                    "INSERT INTO chunks (id, item_id, user_id, chunk_index, "
                    "token_count) VALUES (?, ?, ?, ?, ?);",
                    rec,
                )

        # 7. Publish item.extracted event
        try:
            await providers.events.publish(
                "item.extracted",
                {
                    "tenant_id": scope.tenant_id,
                    "user_id": user_id,
                    "item_id": item_id,
                },
            )
        except Exception as exc:
            logger.warning("Failed to publish item.extracted event: %s", exc)

    except BlockedPageError as blocked:
        # Recorded, not raised: a wall is a permanent condition, so retrying
        # the queue message would burn attempts on an unauthenticated fetch.
        reason = f"Blocked, no article content: {blocked} ({url})"
        await db.execute(
            "UPDATE items SET status = 'failed', fail_reason = ? "
            "WHERE id = ? AND user_id = ?;",
            (reason[:500], item_id, user_id),
        )
        logger.info(f"Blocked page, marked failed: item={item_id}: {blocked}")
        try:
            await providers.events.publish(
                "item.failed",
                {
                    "tenant_id": scope.tenant_id,
                    "user_id": user_id,
                    "item_id": item_id,
                },
            )
        except Exception as exc:
            logger.warning("Failed to publish item.failed event: %s", exc)
    except Exception as exc:
        # Mark failure in D1
        await db.execute(
            "UPDATE items SET status = 'failed', fail_reason = ? "
            "WHERE id = ? AND user_id = ?;",
            (str(exc)[:500], item_id, user_id),
        )
        logger.error(
            f"Extraction failed: item={item_id}, error={str(exc)}", exc_info=exc
        )
        try:
            await providers.events.publish(
                "item.failed",
                {
                    "tenant_id": scope.tenant_id,
                    "user_id": user_id,
                    "item_id": item_id,
                },
            )
        except Exception as pub_exc:
            logger.warning("Failed to publish item.failed event: %s", pub_exc)
        raise exc


async def process_single_item(
    item_id: str, url: str, env: Any, user_id: str | None = None
) -> None:
    """Queue-consumer entry point: builds Database from scope or D1 binding."""
    from keepfor.runtime import get_providers

    providers = get_providers()
    if user_id:
        scope = await providers.scope.scope_for_tenant(user_id, env)
        db = scope.db
    else:
        d1 = (
            getattr(env, "DB", None)
            or getattr(env, "keepfor_me_db", None)
            or getattr(env, "D1", None)
        )
        sqlite_conn = getattr(env, "sqlite_conn", None)
        db = Database(d1_binding=d1, sqlite_conn=sqlite_conn)
        scope = None

    await extract_and_store(db, env, item_id, url, scope=scope, user_id=user_id)


async def process_queue_batch(batch: Any, env: Any) -> None:
    """Processes Cloudflare Queue batch with retry handling."""
    messages = getattr(batch, "messages", [])
    # Support len() on real batches; be defensive about unexpected shapes.
    try:
        batch_size: int | str = len(messages)
    except TypeError:
        batch_size = "?"
    logger.info(f"Processing queue batch: size={batch_size}")
    for msg in messages:
        try:
            body = getattr(msg, "body", msg)
            if hasattr(body, "to_py"):
                body = body.to_py()
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except ValueError:
                    body = {}
            if isinstance(body, dict) and body.get("import_batch"):
                await _process_import_batch(
                    env, body.get("user_id"), body.get("import_batch") or []
                )
                if hasattr(msg, "ack"):
                    msg.ack()
                continue
            item_id = body.get("item_id") if isinstance(body, dict) else None
            url = body.get("url") if isinstance(body, dict) else None
            user_id = body.get("user_id") if isinstance(body, dict) else None
            if item_id and url:
                await process_single_item(item_id, url, env, user_id=user_id)
            else:
                # Never silently swallow: an acked skip is a lost message.
                logger.warning(f"Skipping queue message with no item_id/url: {body!r}")
            if hasattr(msg, "ack"):
                msg.ack()
        except Exception as exc:
            logger.warning(f"Queue message failed, retrying: {exc}")
            if hasattr(msg, "retry"):
                msg.retry()


async def _process_import_batch(
    env: Any, user_id: str | None, bookmarks: list[dict]
) -> None:
    """Saves one bulk-import chunk; each save re-enqueues for extraction."""
    if not user_id or not bookmarks:
        return
    # Lazy import: keepfor.models.items lazily imports this module in save_item.
    from keepfor.models.items import save_item
    from keepfor.runtime import get_providers

    providers = get_providers()
    scope = await providers.scope.scope_for_tenant(user_id, env)
    db = scope.db

    for b in bookmarks:
        try:
            url = b.get("url") if isinstance(b, dict) else None
            if not url:
                continue
            tags = b.get("tags", []) if isinstance(b, dict) else []
            await save_item(db, env, user_id, url, tags, scope=scope)
        except Exception:
            continue
