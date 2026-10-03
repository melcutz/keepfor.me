import asyncio
import json
from typing import Any

from src.consumer.extractor import article_from_reader_markdown, extract_article
from src.models.db import Database
from src.utils.chunker import recursive_character_split
from src.utils.logging import logger

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
JINA_READER_BASE = "https://r.jina.ai/"
MAX_HTML_BYTES = 5 * 1024 * 1024  # 5MB
FETCH_TIMEOUT = 20


class OriginHttpError(RuntimeError):
    """Origin returned an HTTP error status (carries the status code)."""

    def __init__(self, status_code: int, url: str):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code} returned by origin server ({url})")


async def fetch_page_html(url: str, headers: dict | None = None) -> str:
    """Fetch URL HTML via pyfetch or urllib with a 20s timeout and 5MB cap."""
    try:
        import pyodide.http

        response = await pyodide.http.pyfetch(
            url,
            headers=headers or BROWSER_HEADERS,
            timeout=FETCH_TIMEOUT,
        )
        if response.status >= 400:
            raise OriginHttpError(response.status, url)
        # Read text with length check
        text = await response.text()
        return text[:MAX_HTML_BYTES]
    except ImportError:
        # Local development / standard Python runtime fallback
        import urllib.error
        import urllib.request

        req = urllib.request.Request(url, headers=headers or BROWSER_HEADERS)
        loop = asyncio.get_running_loop()

        def _sync_fetch():
            try:
                with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
                    if resp.status >= 400:
                        raise OriginHttpError(resp.status, url)
                    raw = resp.read(MAX_HTML_BYTES)
                    return raw.decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                # urlopen raises (instead of returning) for 4xx/5xx.
                raise OriginHttpError(exc.code, url) from None

        return await loop.run_in_executor(None, _sync_fetch)


async def fetch_jina_reader(url: str) -> str:
    """Fetch reader-proxy markdown for a URL blocked to direct fetching."""
    return await fetch_page_html(
        JINA_READER_BASE + url, headers={**BROWSER_HEADERS, "Accept": "text/markdown"}
    )


async def extract_and_store(db: Database, env: Any, item_id: str, url: str) -> None:
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
    user_id = item_row["user_id"]

    try:
        logger.info(f"Starting extraction: item={item_id}, url={url}")
        # 1. Fetch raw HTML, falling back to a reader proxy when the
        # origin forbids datacenter fetches (403). Other statuses and a
        # failed proxy keep the original error as the recorded reason.
        raw_html: str | None = None
        try:
            raw_html = await fetch_page_html(url)
            extracted = extract_article(raw_html, url)
        except OriginHttpError as direct_err:
            if direct_err.status_code != 403:
                raise
            logger.info(f"Direct fetch forbidden, trying reader proxy: item={item_id}")
            try:
                proxy_md = await fetch_jina_reader(url)
            except Exception:
                raise direct_err from None
            extracted = article_from_reader_markdown(url, proxy_md)
            raw_html = None

        # 2. Store raw snapshot in R2 (skipped for proxied markdown)
        if raw_html is not None and hasattr(env, "BUCKET") and env.BUCKET is not None:
            await env.BUCKET.put(
                f"items/{item_id}/raw.html",
                raw_html,
                {"httpMetadata": {"contentType": "text/html; charset=utf-8"}},
            )

        # 3. Store clean reader HTML in R2
        if hasattr(env, "BUCKET") and env.BUCKET is not None:
            await env.BUCKET.put(
                f"items/{item_id}/clean.html",
                extracted["clean_html"],
                {"httpMetadata": {"contentType": "text/html; charset=utf-8"}},
            )

        # 4. Update items in D1
        await db.execute(
            """
            UPDATE items
            SET title = ?, byline = ?, site_name = ?, published_date = ?,
                excerpt = ?, content_text = ?, word_count = ?, is_fallback = ?,
                status = 'ok', fail_reason = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?;
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
                item_id,
            ),
        )

        # 5. Update FTS5 virtual table
        await db.execute("DELETE FROM items_fts WHERE item_id = ?;", (item_id,))
        await db.execute(
            "INSERT INTO items_fts (item_id, user_id, title, content_text) "
            "VALUES (?, ?, ?, ?);",
            (item_id, user_id, extracted["title"], extracted["content_text"]),
        )

        # 6. Semantic chunking & Vectorize indexing
        chunks = recursive_character_split(
            extracted["content_text"], target_tokens=400, overlap_tokens=50
        )

        if (
            chunks
            and hasattr(env, "AI")
            and hasattr(env, "VECTORIZE")
            and env.AI is not None
            and env.VECTORIZE is not None
        ):
            # Batch embeddings in groups of 10
            vectors_to_upsert = []
            chunk_records = []

            for i in range(0, len(chunks), 10):
                batch_chunks = chunks[i : i + 10]
                texts = [c.text for c in batch_chunks]
                ai_res = await env.AI.run("@cf/baai/bge-base-en-v1.5", {"text": texts})
                raw_data = getattr(ai_res, "data", ai_res)
                if hasattr(raw_data, "to_py"):
                    raw_data = raw_data.to_py()

                embeddings = (
                    raw_data.get("data", raw_data)
                    if isinstance(raw_data, dict)
                    else raw_data
                )

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
                await env.VECTORIZE.upsert(vectors_to_upsert)

            # Update D1 chunks tracking table
            await db.execute("DELETE FROM chunks WHERE item_id = ?;", (item_id,))
            for rec in chunk_records:
                await db.execute(
                    "INSERT INTO chunks (id, item_id, user_id, chunk_index, "
                    "token_count) VALUES (?, ?, ?, ?, ?);",
                    rec,
                )

    except Exception as exc:
        # Mark failure in D1
        await db.execute(
            "UPDATE items SET status = 'failed', fail_reason = ? WHERE id = ?;",
            (str(exc)[:500], item_id),
        )
        logger.error(
            f"Extraction failed: item={item_id}, error={str(exc)}", exc_info=exc
        )
        raise exc


async def process_single_item(item_id: str, url: str, env: Any) -> None:
    """Queue-consumer entry point: builds Database from the D1 binding."""
    d1 = (
        getattr(env, "DB", None)
        or getattr(env, "keepfor_me_db", None)
        or getattr(env, "D1", None)
    )
    db = Database(d1_binding=d1)
    await extract_and_store(db, env, item_id, url)


async def process_queue_batch(batch: Any, env: Any) -> None:
    """Processes Cloudflare Queue batch with retry handling."""
    messages = getattr(batch, "messages", [])
    # Support len() on real batches; be defensive about unexpected shapes.
    try:
        batch_size = len(messages)
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
            if item_id and url:
                await process_single_item(item_id, url, env)
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
    # Lazy import: src.models.items lazily imports this module in save_item.
    from src.models.items import save_item

    d1 = (
        getattr(env, "DB", None)
        or getattr(env, "keepfor_me_db", None)
        or getattr(env, "D1", None)
    )
    if d1 is None and getattr(env, "sqlite_conn", None) is None:
        logger.warning("Import batch dropped: no database binding available")
        return
    db = Database(d1_binding=d1, sqlite_conn=getattr(env, "sqlite_conn", None))
    for b in bookmarks:
        try:
            url = b.get("url") if isinstance(b, dict) else None
            if not url:
                continue
            tags = b.get("tags", []) if isinstance(b, dict) else []
            await save_item(db, env, user_id, url, tags)
        except Exception:
            continue
