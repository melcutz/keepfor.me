# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

import csv
import hashlib
import html
import io
import json
import re
import time
import zipfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import iterparse

from keepfor.models.db import Database
from keepfor.models.items import save_item, save_note
from keepfor.utils.url import canonicalize_url


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
        if (
            not href
            or not isinstance(href, str)
            or not href.startswith(("http://", "https://"))
        ):
            continue
        title = a.get_text().strip() or href
        tags_val = a.get("tags") or ""
        tags_raw = tags_val if isinstance(tags_val, str) else str(tags_val)
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


# ===== Google Keep Takeout Parser =====

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _extract_urls_from_text(text: str, title: str) -> list[dict[str, Any]]:
    """Regex-extract http(s) URLs embedded in a note body."""
    urls = []
    for match in _URL_RE.finditer(text):
        urls.append(
            {
                "url": match.group(0).rstrip(".,;:!?\"')]"),
                "title": title,
                "description": "",
            }
        )
    return urls


class ZipBombError(ValueError):
    """Raised when an upload breaks the zip decompression caps.

    The message is shown to the user in the import summary; when this is
    raised nothing is imported.
    """


def _zip_guard(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Validate a zip against the decompression caps and return its members.

    Everything is checked from ZipInfo metadata alone, before a single
    member is opened: capped member count and capped total declared
    uncompressed size.
    """
    infos = zf.infolist()
    if len(infos) > ZIP_MAX_MEMBERS:
        raise ZipBombError(
            f"Zip has too many files ({len(infos)} > {ZIP_MAX_MEMBERS}); "
            "nothing was imported."
        )
    total_declared = sum(info.file_size for info in infos)
    if total_declared > ZIP_MAX_TOTAL_BYTES:
        raise ZipBombError(
            f"Zip expands to {total_declared // (1024 * 1024)} MB uncompressed "
            f"(limit {ZIP_MAX_TOTAL_BYTES // (1024 * 1024)} MB); "
            "nothing was imported."
        )
    return infos


def _zip_member_read(
    zf: zipfile.ZipFile, info: zipfile.ZipInfo, stats: dict[str, int] | None
) -> bytes | None:
    """Bounded read of one zip member; None if it is over the per-member cap.

    Reads at most ZIP_MAX_MEMBER_BYTES + 1 bytes: a member whose declared
    file_size lies below the cap still cannot balloon memory.
    """
    if info.file_size > ZIP_MAX_MEMBER_BYTES:
        if stats is not None:
            stats["parse_errors"] = stats.get("parse_errors", 0) + 1
        return None
    try:
        with zf.open(info) as f:
            raw = f.read(ZIP_MAX_MEMBER_BYTES + 1)
    except (zipfile.BadZipFile, EOFError, OSError):
        # A member whose declared size lies: zipfile stops at the declared
        # size and the CRC check fails. Skip it instead of failing the import.
        if stats is not None:
            stats["parse_errors"] = stats.get("parse_errors", 0) + 1
        return None
    if len(raw) > ZIP_MAX_MEMBER_BYTES:
        # The declared size lied: the member is over the cap anyway.
        if stats is not None:
            stats["parse_errors"] = stats.get("parse_errors", 0) + 1
        return None
    return raw


def parse_keep_takeout(
    zip_bytes: bytes, stats: dict[str, int] | None = None
) -> list[dict[str, Any]]:
    """Parse Google Keep Takeout zip export.

    Expects a zip with Takeout/Keep/*.json files. Each JSON has:
    - title, textContent (or listContent for checklists)
    - labels[{name}], isPinned, isArchived, isTrashed
    - createdTimestampUsec, userEditedTimestampUsec (microseconds)
    - attachments[{filePath, mimetype}]
    - annotations[{description, source, title, url}] for links

    `stats` (optional) is filled with `trashed_skipped` so the caller can
    report how many trashed notes were dropped.
    """
    notes = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in _zip_guard(zf):
            name = info.filename
            # Only process JSON files under a Keep/ folder (Takeout nests
            # them at Takeout/Keep/, but re-zipped exports often do not).
            if not name.endswith(".json") or "Keep/" not in name:
                continue
            raw = _zip_member_read(zf, info, stats)
            if raw is None:
                continue
            try:
                data = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile):
                if stats is not None:
                    stats["parse_errors"] = stats.get("parse_errors", 0) + 1
                continue

            # Skip trashed notes (counted in the summary)
            if data.get("isTrashed"):
                if stats is not None:
                    stats["trashed_skipped"] = stats.get("trashed_skipped", 0) + 1
                continue

            # Build note body
            body_parts = []
            if data.get("textContent"):
                body_parts.append(data["textContent"])
            elif data.get("listContent"):
                # Render checklist as Markdown task list
                for item in data["listContent"]:
                    checked = "x" if item.get("isChecked") else " "
                    body_parts.append(f"- [{checked}] {item.get('text', '')}")

            body = "\n\n".join(body_parts).strip()

            # Title from title field, or first line of body
            title = data.get("title", "").strip()
            if not title and body:
                title = body.split("\n")[0][:120]
            if not title:
                title = "Untitled note"

            # Tags from labels + source tag
            tags = [
                label["name"].strip().lower()
                for label in data.get("labels", [])
                if label.get("name")
            ]
            tags.append("keep")

            # Timestamps (microseconds to ISO string)
            created_usec = data.get("createdTimestampUsec")
            created_at = None
            if created_usec:
                created_at = (
                    datetime.fromtimestamp(created_usec / 1_000_000, tz=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )

            # Source ID: use the filename as unique identifier
            source_id = Path(name).stem

            # Attachments - list them in body
            attachments = data.get("attachments", [])
            if attachments:
                for att in attachments:
                    file_path = att.get("filePath", "attachment")
                    body += f"\n\n📎 {file_path} (not imported)"

            # Extract URLs from body text
            urls = _extract_urls_from_text(body, title)

            # URL annotations - these become separate URL saves
            for ann in data.get("annotations", []):
                if ann.get("source") == "WEBLINK" and ann.get("url"):
                    urls.append(
                        {
                            "url": ann["url"],
                            "title": ann.get("title", ann["url"]),
                            "description": ann.get("description", ""),
                        }
                    )

            notes.append(
                {
                    "title": title,
                    "body": body,
                    "tags": tags,
                    "is_pinned": bool(data.get("isPinned")),
                    "read_state": "archived" if data.get("isArchived") else "unread",
                    "created_at": created_at,
                    "source": "keep",
                    "source_id": source_id,
                    "urls": urls,
                    "attachments_count": len(attachments),
                }
            )

    return notes


# ===== Evernote ENEX Parser =====

# Shown to the user when an ENEX file carries a DTD or entity definitions.
ENEX_REJECTED_MESSAGE = (
    "The ENEX file contains a DTD or entity definition and was rejected for safety."
)


def _safe_iterparse(enex_bytes: bytes) -> Iterator[tuple[str, Any]]:
    """iterparse that turns a DTD/entity payload into a clear error.

    defusedxml refuses to expand entity definitions (billion laughs); the
    stdlib parser would happily expand them into gigabytes of text.
    """
    try:
        yield from iterparse(io.BytesIO(enex_bytes), events=("start", "end"))
    except DefusedXmlException as exc:
        raise ValueError(ENEX_REJECTED_MESSAGE) from exc


def _strip_enml(enml: str) -> str:
    """Convert ENML (Evernote Markup Language) to plain text/Markdown."""
    if not enml:
        return ""
    # Remove ENML wrapper
    enml = re.sub(r"^<\?xml[^>]*\?>", "", enml)
    enml = re.sub(r"^<!DOCTYPE[^>]*>", "", enml)
    enml = re.sub(r"</?en-note>", "", enml)

    # Convert common ENML tags to Markdown
    enml = re.sub(r"<en-todo[^>]*checked=\"true\"[^>]*/>", "- [x] ", enml)
    enml = re.sub(r"<en-todo[^>]*/>", "- [ ] ", enml)
    enml = re.sub(r"</?div>", "\n", enml)
    enml = re.sub(r"</?p>", "\n", enml)
    enml = re.sub(r"<br\s*/?>", "\n", enml)
    enml = re.sub(r"<b>(.*?)</b>", r"**\1**", enml)
    enml = re.sub(r"<i>(.*?)</i>", r"*\1*", enml)
    enml = re.sub(r"<u>(.*?)</u>", r"_\1_", enml)
    enml = re.sub(r"<a\s+href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"[\2](\1)", enml)
    # Remove remaining tags
    enml = re.sub(r"<[^>]+>", "", enml)
    # Decode HTML entities
    enml = html.unescape(enml)
    # Normalize whitespace
    enml = re.sub(r"\n{3,}", "\n\n", enml)
    return enml.strip()


def parse_enex(enex_bytes: bytes) -> list[dict[str, Any]]:
    """Parse Evernote ENEX export (XML format).

    Uses streaming parser to handle large files. Each <note> has:
    - title, content (ENML in CDATA), created, updated
    - tag (repeated), note-attributes/source-url (for web clips)
    - resource (base64 data, mime, file-name) - listed but not stored
    """
    notes = []
    # Use iterparse for streaming; defusedxml forbids DTD entity expansion
    # (billion laughs), a stdlib ElementTree would happily expand.
    context = _safe_iterparse(enex_bytes)
    try:
        event, root = next(context)
    except StopIteration as exc:
        raise ValueError("Empty or malformed ENEX file") from exc

    current_note: dict[str, Any] = {}
    resource_count = 0

    for event, elem in context:
        if event == "start":
            if elem.tag == "note":
                current_note = {
                    "title": "",
                    "content": "",
                    "created": None,
                    "updated": None,
                    "tags": [],
                    "source_url": None,
                }
            elif elem.tag == "title":
                current_note["title"] = elem.text or ""
            elif elem.tag == "created":
                current_note["created"] = elem.text
            elif elem.tag == "updated":
                current_note["updated"] = elem.text
            elif elem.tag == "tag":
                if elem.text:
                    current_note["tags"].append(elem.text.strip().lower())
            elif elem.tag == "source-url":
                current_note["source_url"] = elem.text
            elif elem.tag == "content":
                # CDATA text is populated on the element by the time the
                # parser yields it; verified against multi-note ENEX files.
                current_note["content"] = elem.text or ""
            elif elem.tag == "resource":
                resource_count += 1

        elif event == "end" and elem.tag == "note":
            # Process the complete note
            body = _strip_enml(current_note["content"])

            # Title fallback
            title = current_note["title"].strip()
            if not title and body:
                title = body.split("\n")[0][:120]
            if not title:
                title = "Untitled note"

            # Tags + source tag
            tags = current_note["tags"]
            tags.append("evernote")

            # Created timestamp
            created_at = None
            if current_note["created"]:
                try:
                    # ENEX uses format like "20240115T103000Z"
                    dt = datetime.strptime(current_note["created"], "%Y%m%dT%H%M%SZ")
                    created_at = dt.isoformat()
                except ValueError:
                    pass

            # Source ID: hash of title + created + content for dedupe
            source_id_raw = f"{title}|{current_note['created']}|{body[:500]}"
            source_id = hashlib.sha256(source_id_raw.encode()).hexdigest()[:32]

            # Attachments mention
            if resource_count > 0:
                body += f"\n\n📎 {resource_count} attachment(s) (not imported)"

            # URLs: web-clip source-url first, then any embedded in the body
            urls = []
            if current_note["source_url"]:
                urls.append(
                    {
                        "url": current_note["source_url"],
                        "title": title,
                        "description": "Web clip source",
                    }
                )
            urls.extend(_extract_urls_from_text(body, title))

            notes.append(
                {
                    "title": title,
                    "body": body,
                    "tags": tags,
                    "is_pinned": False,
                    "read_state": "unread",
                    "created_at": created_at,
                    "source": "evernote",
                    "source_id": source_id,
                    "urls": urls,
                    "attachments_count": resource_count,
                }
            )

            resource_count = 0
            # Clear element to free memory
            root.clear()

    return notes


# ===== Markdown/Text Zip Parser =====


def _parse_front_matter(content: str) -> tuple[dict[str, Any], str]:
    """Extract the `title` and `tags` keys from YAML front matter.

    shortcut: line-based parsing of two known keys only (PyYAML is not a
    dependency and would not ship in the Workers bundle); upgrade to a real
    YAML parser if more front-matter keys are ever imported.
    """
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    front_matter: dict[str, Any] = {}
    for raw_line in parts[1].splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Block-list continuation: "  - tag" under a "tags:" key
        if line.startswith("- "):
            if "tags" in front_matter and isinstance(front_matter["tags"], list):
                front_matter["tags"].append(line[2:].strip().strip("\"'"))
            continue
        match = re.match(r"^([\w-]+):\s*(.*)$", line)
        if not match:
            continue
        key, value = match.group(1).lower(), match.group(2).strip()
        if key == "title":
            front_matter["title"] = value.strip("\"'")
        elif key == "tags":
            # "tags: [a, b]", "tags: a, b" or the start of a block list
            items = [
                t.strip().strip("\"'")
                for t in re.split(r"[;,]", value.strip("[]"))
                if t.strip()
            ]
            front_matter["tags"] = items
    return front_matter, parts[2].lstrip("\n")


def _extract_inline_tags(text: str) -> list[str]:
    """Extract #hashtag style tags from text."""
    return [tag.lower() for tag in re.findall(r"#(\w+)", text)]


def parse_markdown_zip(
    zip_bytes: bytes, stats: dict[str, int] | None = None
) -> list[dict[str, Any]]:
    """Parse a zip of Markdown (.md) and text (.txt) files.

    - Title from front-matter 'title', then first # H1, then filename
    - Tags from front-matter 'tags'/'tags:' and inline #hashtags
    - Folder path becomes a tag
    - Skips .obsidian/, __MACOSX/, hidden files, non-text files
    """
    notes = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in _zip_guard(zf):
            name = info.filename
            # Skip unwanted files
            if any(
                skip in name for skip in [".obsidian/", "__MACOSX/", "/.", ".DS_Store"]
            ):
                continue
            if name.endswith("/"):
                continue  # Skip directories

            # Only process .md and .txt files
            if not (name.endswith(".md") or name.endswith(".txt")):
                continue

            raw = _zip_member_read(zf, info, stats)
            if raw is None:
                continue
            content = raw.decode("utf-8", errors="replace")

            # Extract front matter
            front_matter, body = _parse_front_matter(content)

            # Title: front-matter > first H1 > filename
            title = front_matter.get("title", "").strip()
            if not title:
                # Find first # H1
                for line in body.split("\n"):
                    line = line.strip()
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
            if not title:
                title = Path(name).stem

            # Tags: front-matter tags + inline #tags + folder tag
            tags = []
            fm_tags = front_matter.get("tags")
            if isinstance(fm_tags, list):
                tags.extend([t.strip().lower() for t in fm_tags if t.strip()])
            elif isinstance(fm_tags, str):
                tags.extend(
                    [
                        t.strip().lower()
                        for t in fm_tags.replace(";", ",").split(",")
                        if t.strip()
                    ]
                )

            tags.extend(_extract_inline_tags(body))

            # Folder path as tag (parent directory)
            parent_dir = Path(name).parent
            if parent_dir != Path("."):
                folder_tag = parent_dir.name.lower()
                if folder_tag and folder_tag not in tags:
                    tags.append(folder_tag)

            tags.append("markdown")

            # Source ID: relative path in zip
            source_id = name

            # Created time - try to get from file metadata in zip
            created_at = None
            if info.date_time:
                try:
                    created_at = datetime(*info.date_time).isoformat()
                except ValueError:
                    pass

            # Extract URLs from body
            urls = _extract_urls_from_text(body, title)

            notes.append(
                {
                    "title": title,
                    "body": body,
                    "tags": tags,
                    "is_pinned": False,
                    "read_state": "unread",
                    "created_at": created_at,
                    "source": "markdown",
                    "source_id": source_id,
                    "urls": urls,
                    "attachments_count": 0,
                }
            )

    return notes


# ===== High-level Import Functions =====

MAX_URLS_PER_IMPORT = 500
ZIP_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
ENEX_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
HTML_CSV_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
ZIP_MAX_MEMBER_BYTES = 2 * 1024 * 1024  # per-member cap inside a notes zip
ZIP_MAX_MEMBERS = 20_000  # hard member-count cap
ZIP_MAX_TOTAL_BYTES = 200 * 1024 * 1024  # declared total uncompressed size cap


def detect_file_type(filename: str, content: bytes) -> str:
    """Detect file type by extension and magic bytes."""
    filename_lower = filename.lower()

    # Check magic bytes first. ENEX is detected by its root element, not the
    # XML declaration: XHTML bookmark exports also start with `<?xml`.
    if (
        content[:4] == b"PK\x03\x04"
        or content[:4] == b"PK\x05\x06"
        or content[:4] == b"PK\x07\x08"
    ):
        return "zip"
    if b"<en-export" in content[:2048]:
        return "enex"

    # Fallback to extension
    if filename_lower.endswith(".enex"):
        return "enex"
    if filename_lower.endswith(".zip"):
        return "zip"
    if filename_lower.endswith(".html") or filename_lower.endswith(".htm"):
        return "html"
    if filename_lower.endswith(".csv"):
        return "csv"

    return "unknown"


async def _check_duplicate(
    db: Database, user_id: str, source: str, source_id: str
) -> bool:
    """Check if a note with this source+source_id already exists."""
    if not source_id:
        return False
    row = await db.query_first(
        "SELECT 1 FROM items WHERE user_id = ? AND source = ? AND source_id = ?;",
        (user_id, source, source_id),
    )
    return row is not None


async def import_notes(
    db: Database,
    env: Any,
    user_id: str,
    notes: list[dict[str, Any]],
    providers: Any,
    scope: Any,
    source: str,
) -> dict[str, Any]:
    """Import parsed notes, deduplicate, extract URLs, and return summary."""
    from keepfor.spi import EntitlementDenied
    from keepfor.utils.logging import logger

    logger.info(f"import_notes_started source={source} notes={len(notes)}")
    imported = 0
    duplicates = 0
    trashed_skipped = 0  # Not used here but kept for API consistency
    urls_queued = 0
    urls_skipped = 0
    parse_errors = 0
    plan_skipped = 0  # notes and URLs refused because the plan item cap was hit
    quota_exhausted = False
    total_urls_in_notes = 0
    urls_seen: set[str] = set()

    for note in notes:
        # Plan limit already hit for this tenant: everything outstanding is
        # refused and counted, never silently aborted with an exception.
        if quota_exhausted:
            plan_skipped += 1 + len(note.get("urls", []))
            continue

        # Dedupe on (source, source_id); fall back to a content hash when the
        # source has no stable per-note identifier (spec item 7).
        source_id = (
            note.get("source_id")
            or hashlib.sha256(
                f"{note['title']}|{note.get('created_at')}|{note['body']}".encode()
            ).hexdigest()[:32]
        )
        if await _check_duplicate(db, user_id, source, source_id):
            duplicates += 1
            continue

        # Check entitlement for the note: same "import" pool as bookmarks.
        decision = await providers.entitlements.check(scope.tenant_id, "import", qty=1)
        if not decision.allowed:
            plan_skipped += 1 + len(note.get("urls", []))
            quota_exhausted = True
            continue

        # Save the note
        try:
            await save_note(
                db=db,
                env=env,
                user_id=user_id,
                title=note["title"],
                content_text=note["body"],
                tags=note["tags"],
                is_pinned=note.get("is_pinned", False),
                read_state=note.get("read_state", "unread"),
                created_at=note.get("created_at"),
                source=source,
                source_id=source_id,
            )
            imported += 1

            # Record entitlement
            try:
                await providers.entitlements.record(scope.tenant_id, "import", qty=1)
            except Exception:
                pass

            # Extract and queue URLs from note body and annotations
            note_urls = note.get("urls", [])
            total_urls_in_notes += len(note_urls)

            for url_info in note_urls:
                if urls_queued >= MAX_URLS_PER_IMPORT:
                    break

                url = url_info.get("url")
                if not url:
                    continue

                canonical = canonicalize_url(url)
                if canonical in urls_seen:
                    urls_skipped += 1
                    continue
                urls_seen.add(canonical)

                # Check entitlement for the URL: it costs the same unit as
                # the note it came from.
                if quota_exhausted:
                    plan_skipped += 1
                    continue
                url_decision = await providers.entitlements.check(
                    scope.tenant_id, "import", qty=1
                )
                if not url_decision.allowed:
                    plan_skipped += 1
                    quota_exhausted = True
                    continue

                # Save as URL item (will dedupe on canonical_url)
                try:
                    url_tags = note["tags"] + ["from-notes"]
                    _, is_new = await save_item(
                        db=db, env=env, user_id=user_id, url=url, tags=url_tags
                    )
                    if is_new:
                        urls_queued += 1
                        try:
                            await providers.entitlements.record(
                                scope.tenant_id, "import", qty=1
                            )
                        except Exception:
                            pass
                    else:
                        urls_skipped += 1
                except EntitlementDenied:
                    # The plan's item limit refused this URL: count it as a
                    # plan skip, not as a parse error.
                    plan_skipped += 1
                except Exception:
                    urls_skipped += 1

        except Exception:
            parse_errors += 1
            continue

    return {
        "notes_imported": imported,
        "duplicates_skipped": duplicates,
        "trashed_skipped": trashed_skipped,
        "urls_queued": urls_queued,
        "urls_skipped": urls_skipped,
        "total_urls_found": total_urls_in_notes,
        "parse_errors": parse_errors,
        "plan_skipped": plan_skipped,
        "attachments_not_imported": sum(n.get("attachments_count", 0) for n in notes),
    }


async def plan_quota(entitlements: Any, tenant_id: str, total: int) -> int:
    """How many of `total` items the plan still allows for this tenant.

    `check` is monotonic (allowed iff the remaining quota covers qty), so the
    largest allowed quantity is found by bisection: ~11 checks for a
    1,000-note export, and only when the whole batch does not fit. The
    per-item check in `import_notes` stays authoritative, so a provider with
    a non-monotonic `check` can only skew this estimate, never over-quota.
    """
    if total <= 0:
        return 0
    low, high = 0, total  # `high` is known to be refused by the caller
    while low < high:
        mid = (low + high + 1) // 2
        decision = await entitlements.check(tenant_id, "import", qty=mid)
        if decision.allowed:
            low = mid
        else:
            high = mid - 1
    return low


async def export_library_json(db: Database, user_id: str) -> list[dict[str, Any]]:
    """Exports entire user library as structured JSON."""
    rows = await db.query_all(
        """
        SELECT id, url, canonical_url, title, byline, site_name,
               published_date, excerpt, status, word_count, created_at,
               user_notes, item_type, is_pinned, image_url, summary, read_state
        FROM items
        WHERE user_id = ?
        ORDER BY created_at DESC;
        """,
        (user_id,),
    )
    if not rows:
        return []

    all_tags = await db.query_all(
        """
        SELECT it.item_id, t.name
        FROM item_tags it
        JOIN items i ON i.id = it.item_id AND i.user_id = ?
        JOIN tags t ON it.tag_id = t.id AND t.user_id = i.user_id
        ORDER BY it.item_id, t.name;
        """,
        (user_id,),
    )
    tags_by_item: dict[str, list[str]] = {}
    for tr in all_tags:
        tags_by_item.setdefault(tr["item_id"], []).append(tr["name"])

    items = []
    for r in rows:
        item = dict(r)
        item["tags"] = tags_by_item.get(r["id"], [])
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
        safe_url = html.escape(it["url"], quote=True)
        safe_tags = html.escape(tags_str, quote=True)
        safe_title = html.escape(title)
        html_lines.append(
            f'    <DT><A HREF="{safe_url}" ADD_DATE="{epoch}" '
            f'TAGS="{safe_tags}">{safe_title}</A>'
        )

    html_lines.append("</DL><p>")
    return "\n".join(html_lines)
