import csv
import io
import time
from typing import Any

from src.models.db import Database
from src.models.items import save_item


def parse_netscape_bookmarks(html_content: str) -> list[dict[str, Any]]:
    """Parse Netscape Bookmark HTML exported by Pocket, Omnivore, or browsers.

    Browser exports group links under <H3> folder headers; the enclosing
    folder name is kept as a tag so Chrome categories survive the import.
    """
    # Imported here, not at module scope: this module is imported by the FastAPI
    # app, and bs4 pulls lxml into every request's cold start (~0.1s measured).
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_content, "html.parser")
    bookmarks = []

    for a in soup.find_all("a"):
        href = a.get("href")
        if not href or not href.startswith(("http://", "https://")):
            continue
        title = a.get_text().strip() or href
        tags_raw = a.get("tags") or ""
        tags = [t.strip().lower() for t in tags_raw.split(",") if t.strip()]
        folder_header = a.find_previous("h3")
        if folder_header:
            folder = folder_header.get_text().strip().lower()
            if folder and folder not in tags:
                tags.append(folder)
        bookmarks.append({"url": href, "title": title, "tags": tags})
    return bookmarks


def parse_csv_bookmarks(csv_content: str) -> list[dict[str, Any]]:
    """Parses CSV exports from Omnivore, Instapaper, or custom sheets."""
    reader = csv.DictReader(io.StringIO(csv_content))
    bookmarks = []

    for row in reader:
        # Check potential URL column names
        url = row.get("url") or row.get("URL") or row.get("Link") or row.get("href")
        if not url or not url.strip().startswith(("http://", "https://")):
            continue
        title = row.get("title") or row.get("Title") or url
        tags_raw = row.get("tags") or row.get("Tags") or row.get("folder") or ""
        tags = [
            t.strip().lower()
            for t in tags_raw.replace(";", ",").split(",")
            if t.strip()
        ]
        bookmarks.append({"url": url.strip(), "title": title.strip(), "tags": tags})
    return bookmarks


async def import_bookmarks(
    db: Database, env: Any, user_id: str, bookmarks: list[dict[str, Any]]
) -> dict[str, int]:
    """Bulk imports bookmarks into D1 and enqueues them for background extraction."""
    imported = 0
    duplicates = 0

    for b in bookmarks:
        try:
            _, is_new = await save_item(
                db=db, env=env, user_id=user_id, url=b["url"], tags=b.get("tags", [])
            )
            if is_new:
                imported += 1
            else:
                duplicates += 1
        except Exception:
            continue

    return {"imported": imported, "duplicates": duplicates, "total": len(bookmarks)}


async def export_library_json(db: Database, user_id: str) -> list[dict[str, Any]]:
    """Exports entire user library as structured JSON."""
    rows = await db.query_all(
        """
        SELECT id, url, canonical_url, title, byline, site_name,
               published_date, excerpt, status, word_count, created_at
        FROM items
        WHERE user_id = ?
        ORDER BY created_at DESC;
        """,
        (user_id,),
    )
    items = []
    for r in rows:
        item = dict(r)
        # Fetch tags
        tag_rows = await db.query_all(
            """
            SELECT t.name FROM tags t
            JOIN item_tags it ON t.id = it.tag_id
            WHERE it.item_id = ?;
            """,
            (r["id"],),
        )
        item["tags"] = [tr["name"] for tr in tag_rows]
        items.append(item)
    return items


async def export_library_html(db: Database, user_id: str) -> str:
    """Exports library as standard Netscape Bookmark HTML."""
    items = await export_library_json(db, user_id)
    html_lines = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        "<!-- This is an automatically generated file. -->",
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        "<TITLE>Keepfor.me Library Export</TITLE>",
        "<H1>Bookmarks</H1>",
        "<DL><p>",
    ]
    for it in items:
        tags_str = ",".join(it["tags"])
        epoch = int(time.time())
        title = it.get("title") or it["url"]
        html_lines.append(
            f'    <DT><A HREF="{it["url"]}" ADD_DATE="{epoch}" '
            f'TAGS="{tags_str}">{title}</A>'
        )
    html_lines.append("</DL><p>")
    return "\n".join(html_lines)
