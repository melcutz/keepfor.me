# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

import io
import json
import zipfile

import pytest

from keepfor.auth.service import register_user
from keepfor.models.items import (
    save_item,
    save_note,
    toggle_pin_item,
    update_user_notes,
)
from keepfor.utils.importer import (
    ZipBombError,
    detect_file_type,
    export_library_html,
    export_library_json,
    import_notes,
    parse_csv_bookmarks,
    parse_enex,
    parse_keep_takeout,
    parse_markdown_zip,
    parse_netscape_bookmarks,
)


def test_parse_netscape_html():
    """Test Netscape bookmark HTML format parsing."""
    html = """
    <!DOCTYPE NETSCAPE-Bookmark-file-1>
    <META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
    <TITLE>Bookmarks</TITLE>
    <H1>Bookmarks</H1>
    <DL><p>
        <DT><A HREF="https://example.com/1" ADD_DATE="1700000000"
        TAGS="tech,news">Example One</A>
        <DT><A HREF="https://example.com/2" ADD_DATE="1700000001">Example Two</A>
    </DL><p>
    """
    bookmarks = parse_netscape_bookmarks(html)
    assert len(bookmarks) == 2
    assert bookmarks[0]["url"] == "https://example.com/1"
    assert bookmarks[0]["title"] == "Example One"
    assert bookmarks[0]["tags"] == ["tech", "news"]
    assert bookmarks[1]["title"] == "Example Two"


def test_parse_csv():
    """Test CSV bookmark format parsing."""
    csv_content = """url,title,tags
https://blog.cloudflare.com/workers,Cloudflare Workers Blog,"cloudflare,serverless"
https://fastapi.tiangolo.com,FastAPI Framework,python
"""
    bookmarks = parse_csv_bookmarks(csv_content)
    assert len(bookmarks) == 2
    assert bookmarks[0]["url"] == "https://blog.cloudflare.com/workers"
    assert bookmarks[0]["tags"] == ["cloudflare", "serverless"]


def test_parse_chrome_folders_become_tags():
    """Chrome exports group links under H3 folders; folders become tags."""
    html = """
    <!DOCTYPE NETSCAPE-Bookmark-file-1>
    <TITLE>Bookmarks</TITLE>
    <H1>Bookmarks</H1>
    <DL><p>
        <DT><H3 ADD_DATE="1700000000">News</H3>
        <DL><p>
            <DT><A HREF="https://example.com/a">Article A</A>
            <DT><A HREF="https://example.com/b">Article B</A>
        </DL><p>
        <DT><H3 ADD_DATE="1700000001">Recipes</H3>
        <DL><p>
            <DT><A HREF="https://example.com/c">Cake</A>
        </DL><p>
    </DL><p>
    """
    bookmarks = parse_netscape_bookmarks(html)
    assert len(bookmarks) == 3
    assert bookmarks[0]["tags"] == ["news"]
    assert bookmarks[1]["tags"] == ["news"]
    assert bookmarks[2]["tags"] == ["recipes"]


@pytest.mark.asyncio
async def test_export_library_json_includes_metadata_and_batches_tags(db):
    user = await register_user(db, "exporter@test.local", "password123")
    user_id = user["id"]

    # Empty export test (Review Focus 3)
    empty_export = await export_library_json(db, user_id)
    assert empty_export == []
    empty_html = await export_library_html(db, user_id)
    assert "<!DOCTYPE NETSCAPE-Bookmark-file-1>" in empty_html
    assert "<DT><A" not in empty_html

    # Save item with tags and note
    item, _ = await save_item(
        db, None, user_id, "https://example.com/article", ["news", "tech"]
    )
    await update_user_notes(db, user_id, item["id"], "My custom note")
    await toggle_pin_item(db, user_id, item["id"])

    # Save a quick note
    note = await save_note(db, None, user_id, "Quick Note", "Some note text", ["ideas"])

    exported = await export_library_json(db, user_id)
    assert len(exported) == 2
    by_id = {it["id"]: it for it in exported}

    # Verify migration 0006 metadata retained
    exp_item = by_id[item["id"]]
    assert set(exp_item["tags"]) == {"news", "tech"}
    assert exp_item["user_notes"] == "My custom note"
    assert exp_item["is_pinned"] == 1
    assert exp_item["read_state"] == "unread"
    assert "image_url" in exp_item
    assert "summary" in exp_item
    assert exp_item["item_type"] == "url"

    exp_note = by_id[note["id"]]
    assert exp_note["item_type"] == "note"
    assert set(exp_note["tags"]) == {"ideas"}


@pytest.mark.asyncio
async def test_export_library_html_escapes_entities(db):
    user = await register_user(db, "html_esc@test.local", "password123")
    user_id = user["id"]

    # Insert item with malicious / special characters in title and url
    dangerous_url = 'https://example.com/test?a=1&b=2"<script>alert(1)</script>'
    item, _ = await save_item(db, None, user_id, dangerous_url, ["tag&1", 'tag"2'])
    await db.execute(
        "UPDATE items SET title = ? WHERE id = ?;",
        ('Dangerous <Title> & "Quotes"', item["id"]),
    )

    exported_html = await export_library_html(db, user_id)
    assert "<script>" not in exported_html
    assert "&lt;script&gt;" in exported_html or "%3Cscript%3E" in exported_html
    assert "Dangerous &lt;Title&gt; &amp; &quot;Quotes&quot;" in exported_html
    assert "tag&amp;1" in exported_html
    assert "tag&quot;2" in exported_html


# ===== Google Keep Takeout Parser Tests =====


def _make_keep_takeout_zip(notes: list[dict]) -> bytes:
    """Create a Google Keep Takeout zip in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, note in enumerate(notes):
            # Write JSON file
            json_name = f"Takeout/Keep/note-{i}.json"
            zf.writestr(json_name, json.dumps(note))
            # Write HTML file (ignored by parser when JSON exists)
            html_name = f"Takeout/Keep/note-{i}.html"
            zf.writestr(
                html_name, f"<html><body>{note.get('textContent', '')}</body></html>"
            )
    return buf.getvalue()


def test_parse_keep_takeout_text_note():
    """Test parsing a Keep text note."""
    notes = [
        {
            "title": "My Note",
            "textContent": "This is the body text.",
            "labels": [{"name": "personal"}, {"name": "work"}],
            "isPinned": True,
            "isArchived": False,
            "isTrashed": False,
            "createdTimestampUsec": 1717378714000000,
            "userEditedTimestampUsec": 1717378894000000,
            "color": "BLUE",
        }
    ]
    zip_bytes = _make_keep_takeout_zip(notes)
    parsed = parse_keep_takeout(zip_bytes)

    assert len(parsed) == 1
    note = parsed[0]
    assert note["title"] == "My Note"
    assert note["body"] == "This is the body text."
    assert "personal" in note["tags"]
    assert "work" in note["tags"]
    assert "keep" in note["tags"]
    assert note["is_pinned"] is True
    assert note["read_state"] == "unread"
    assert note["created_at"] == "2024-06-03T01:38:34Z"
    assert note["source_id"] == "note-0"
    assert note["attachments_count"] == 0


def test_parse_keep_takeout_checklist_note():
    """Test parsing a Keep checklist note renders as Markdown tasks."""
    notes = [
        {
            "title": "Shopping List",
            "listContent": [
                {"text": "Milk", "isChecked": False},
                {"text": "Eggs", "isChecked": True},
                {"text": "Bread", "isChecked": False},
            ],
            "labels": [{"name": "shopping"}],
            "isPinned": False,
            "isArchived": False,
            "isTrashed": False,
            "createdTimestampUsec": 1690372692755000,
        }
    ]
    zip_bytes = _make_keep_takeout_zip(notes)
    parsed = parse_keep_takeout(zip_bytes)

    assert len(parsed) == 1
    note = parsed[0]
    assert note["title"] == "Shopping List"
    assert "- [ ] Milk" in note["body"]
    assert "- [x] Eggs" in note["body"]
    assert "- [ ] Bread" in note["body"]
    assert "shopping" in note["tags"]
    assert "keep" in note["tags"]


def test_parse_keep_takeout_trashed_skipped():
    """Test that trashed notes are not imported."""
    notes = [
        {
            "title": "Active",
            "textContent": "Active note",
            "isTrashed": False,
            "createdTimestampUsec": 1000000,
        },
        {
            "title": "Trashed",
            "textContent": "Trashed note",
            "isTrashed": True,
            "createdTimestampUsec": 2000000,
        },
    ]
    zip_bytes = _make_keep_takeout_zip(notes)
    parsed = parse_keep_takeout(zip_bytes)

    assert len(parsed) == 1
    assert parsed[0]["title"] == "Active"


def test_parse_keep_takeout_archived_read_state():
    """Test that archived notes get read_state='archived'."""
    notes = [
        {
            "title": "Archived Note",
            "textContent": "This was archived",
            "isArchived": True,
            "isTrashed": False,
            "createdTimestampUsec": 1000000,
        }
    ]
    zip_bytes = _make_keep_takeout_zip(notes)
    parsed = parse_keep_takeout(zip_bytes)

    assert len(parsed) == 1
    assert parsed[0]["read_state"] == "archived"


def test_parse_keep_takeout_with_attachments():
    """Test that attachments are listed in body but not stored."""
    notes = [
        {
            "title": "Note with Image",
            "textContent": "Body text",
            "attachments": [
                {"filePath": "image.png", "mimetype": "image/png"},
                {"filePath": "doc.pdf", "mimetype": "application/pdf"},
            ],
            "isTrashed": False,
            "createdTimestampUsec": 1000000,
        }
    ]
    zip_bytes = _make_keep_takeout_zip(notes)
    parsed = parse_keep_takeout(zip_bytes)

    assert len(parsed) == 1
    note = parsed[0]
    assert "📎 image.png (not imported)" in note["body"]
    assert "📎 doc.pdf (not imported)" in note["body"]
    assert note["attachments_count"] == 2


def test_parse_keep_takeout_with_annotations():
    """Test that URL annotations are extracted as separate URL saves."""
    notes = [
        {
            "title": "Annotated Note",
            "textContent": "Links I saved from Keep.",
            "annotations": [
                {
                    "source": "WEBLINK",
                    "title": "Example",
                    "url": "https://example.com/page1",
                    "description": "",
                },
                {
                    "source": "WEBLINK",
                    "title": "Cloudflare",
                    "url": "https://cloudflare.com",
                    "description": "Web performance",
                },
            ],
            "isTrashed": False,
            "createdTimestampUsec": 1717378714000000,
        }
    ]
    zip_bytes = _make_keep_takeout_zip(notes)
    parsed = parse_keep_takeout(zip_bytes)

    assert len(parsed) == 1
    note = parsed[0]
    assert len(note["urls"]) == 2
    assert note["urls"][0]["url"] == "https://example.com/page1"
    assert note["urls"][1]["url"] == "https://cloudflare.com"


def test_parse_keep_takeout_no_title_uses_first_line():
    """Test that title falls back to first line of body."""
    notes = [
        {
            "textContent": "First line of body\nSecond line",
            "isTrashed": False,
            "createdTimestampUsec": 1000000,
        }
    ]
    zip_bytes = _make_keep_takeout_zip(notes)
    parsed = parse_keep_takeout(zip_bytes)

    assert len(parsed) == 1
    assert parsed[0]["title"] == "First line of body"


# ===== Evernote ENEX Parser Tests =====


def _make_enex(notes: list[dict]) -> bytes:
    """Create an ENEX file in memory."""
    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE en-export SYSTEM "http://xml.evernote.com/pub/evernote-export3.dtd">',
        "<en-export>",
    ]
    for note in notes:
        xml_parts.append("<note>")
        xml_parts.append(f"<title>{note.get('title', '')}</title>")
        xml_parts.append(f"<content><![CDATA[{note.get('content', '')}]]></content>")
        if note.get("created"):
            xml_parts.append(f"<created>{note['created']}</created>")
        if note.get("updated"):
            xml_parts.append(f"<updated>{note['updated']}</updated>")
        for tag in note.get("tags", []):
            xml_parts.append(f"<tag>{tag}</tag>")
        if note.get("source_url"):
            xml_parts.append(
                f"<note-attributes><source-url>{note['source_url']}</source-url></note-attributes>"
            )
        if note.get("resources"):
            for res in note["resources"]:
                xml_parts.append(
                    f'<resource><data encoding="base64">{res["data"]}</data><mime>{res["mime"]}</mime><resource-attributes><file-name>{res["file_name"]}</file-name></resource-attributes></resource>'  # noqa: E501
                )
        xml_parts.append("</note>")
    xml_parts.append("</en-export>")
    return "".join(xml_parts).encode("utf-8")


def test_parse_enex_basic_note():
    """Test parsing a basic ENEX note."""
    notes = [
        {
            "title": "Evernote Note",
            "content": '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE en-note SYSTEM "http://xml.evernote.com/pub/enml2.dtd"><en-note>This is the body text.</en-note>',  # noqa: E501
            "created": "20240115T103000Z",
            "tags": ["personal", "work"],
        }
    ]
    enex_bytes = _make_enex(notes)
    parsed = parse_enex(enex_bytes)

    assert len(parsed) == 1
    note = parsed[0]
    assert note["title"] == "Evernote Note"
    assert "This is the body text" in note["body"]
    assert "personal" in note["tags"]
    assert "work" in note["tags"]
    assert "evernote" in note["tags"]
    assert note["created_at"] == "2024-01-15T10:30:00"
    assert len(note["source_id"]) == 32  # SHA256 hex


def test_parse_enex_web_clip():
    """Test that web clips create a URL save for source-url."""
    notes = [
        {
            "title": "Web Clip",
            "content": '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE en-note SYSTEM "http://xml.evernote.com/pub/enml2.dtd"><en-note>Clipped content</en-note>',  # noqa: E501
            "created": "20240115T103000Z",
            "source_url": "https://example.com/original-article",
        }
    ]
    enex_bytes = _make_enex(notes)
    parsed = parse_enex(enex_bytes)

    assert len(parsed) == 1
    note = parsed[0]
    assert len(note["urls"]) == 1
    assert note["urls"][0]["url"] == "https://example.com/original-article"
    assert note["urls"][0]["title"] == "Web Clip"


def test_parse_enex_with_resources():
    """Test that resources (attachments) are counted but not stored."""
    notes = [
        {
            "title": "Note with Attachment",
            "content": "<en-note>Body</en-note>",
            "resources": [
                {"data": "base64data1", "mime": "image/png", "file_name": "photo.png"},
                {
                    "data": "base64data2",
                    "mime": "application/pdf",
                    "file_name": "doc.pdf",
                },
            ],
        }
    ]
    enex_bytes = _make_enex(notes)
    parsed = parse_enex(enex_bytes)

    assert len(parsed) == 1
    note = parsed[0]
    assert note["attachments_count"] == 2
    assert "📎 2 attachment(s) (not imported)" in note["body"]


def test_parse_enex_enml_to_markdown():
    """Test ENML to Markdown conversion."""
    notes = [
        {
            "title": "Formatted",
            "content": '<en-note><div>Paragraph 1</div><div>Paragraph 2</div><b>Bold</b> <i>Italic</i> <a href="https://example.com">Link</a><br/>Line break</en-note>',  # noqa: E501
        }
    ]
    enex_bytes = _make_enex(notes)
    parsed = parse_enex(enex_bytes)

    assert len(parsed) == 1
    body = parsed[0]["body"]
    assert "Paragraph 1" in body
    assert "Paragraph 2" in body
    assert "**Bold**" in body
    assert "*Italic*" in body
    assert "[Link](https://example.com)" in body


# ===== Markdown/Text Zip Parser Tests =====


def _make_markdown_zip(files: dict[str, str]) -> bytes:
    """Create a Markdown zip in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_parse_markdown_zip_front_matter():
    """Test parsing Markdown with YAML front matter."""
    files = {
        "note1.md": "---\ntitle: My Note\ntags: [tag1, tag2]\n---"
        "\n\nBody content here.",
        "note2.md": "# Heading Title\n\nNo front matter.",
    }
    zip_bytes = _make_markdown_zip(files)
    parsed = parse_markdown_zip(zip_bytes)

    assert len(parsed) == 2
    note1 = parsed[0]
    assert note1["title"] == "My Note"
    assert "tag1" in note1["tags"]
    assert "tag2" in note1["tags"]
    assert "markdown" in note1["tags"]

    note2 = parsed[1]
    assert note2["title"] == "Heading Title"


def test_parse_markdown_zip_folder_tag():
    """Test that folder path becomes a tag."""
    files = {
        "folder/subfolder/note.md": "# Note in Folder\n\nContent.",
    }
    zip_bytes = _make_markdown_zip(files)
    parsed = parse_markdown_zip(zip_bytes)

    assert len(parsed) == 1
    assert "subfolder" in parsed[0]["tags"]
    assert "markdown" in parsed[0]["tags"]


def test_parse_markdown_zip_skips_obsidian():
    """Test that .obsidian/ folder is skipped."""
    files = {
        "note.md": "# Note\n\nContent.",
        ".obsidian/config.json": '{"key": "value"}',
        "__MACOSX/._note.md": "junk",
    }
    zip_bytes = _make_markdown_zip(files)
    parsed = parse_markdown_zip(zip_bytes)

    assert len(parsed) == 1
    assert parsed[0]["title"] == "Note"


def test_parse_markdown_zip_inline_tags():
    """Test that inline #hashtags are extracted as tags."""
    files = {
        "note.md": "# Note\n\nThis has #inline #tags in the body.",
    }
    zip_bytes = _make_markdown_zip(files)
    parsed = parse_markdown_zip(zip_bytes)

    assert len(parsed) == 1
    assert "inline" in parsed[0]["tags"]
    assert "tags" in parsed[0]["tags"]
    assert "markdown" in parsed[0]["tags"]


def test_parse_markdown_zip_extracts_urls():
    """Test that URLs in Markdown body are extracted."""
    files = {
        "note.md": "# Note\n\nCheck out https://example.com and https://another.com/page.",
    }
    zip_bytes = _make_markdown_zip(files)
    parsed = parse_markdown_zip(zip_bytes)

    assert len(parsed) == 1
    assert len(parsed[0]["urls"]) == 2
    assert parsed[0]["urls"][0]["url"] == "https://example.com"
    assert parsed[0]["urls"][1]["url"] == "https://another.com/page"


# ===== File Type Detection Tests =====


def test_detect_file_type_zip():
    """Test zip detection by magic bytes."""
    zip_bytes = _make_keep_takeout_zip([{"title": "Test", "textContent": "Body"}])
    assert detect_file_type("test.zip", zip_bytes) == "zip"


def test_detect_file_type_enex():
    """Test enex detection by magic bytes."""
    enex_bytes = _make_enex([{"title": "Test", "content": "<en-note>Body</en-note>"}])
    assert detect_file_type("test.enex", enex_bytes) == "enex"


def test_detect_file_type_html():
    """Test HTML detection by extension."""
    html_bytes = b"<html><body>Test</body></html>"
    assert detect_file_type("bookmarks.html", html_bytes) == "html"


def test_detect_file_type_csv():
    """Test CSV detection by extension."""
    csv_bytes = b"url,title\nhttps://example.com,Test"
    assert detect_file_type("bookmarks.csv", csv_bytes) == "csv"


# ===== Integration Tests =====


class MockProviders:
    def __init__(self):
        self.entitlements = MockEntitlements()


class MockEntitlements:
    async def check(self, tenant_id, feature, qty=1):
        class Decision:
            allowed = True
            reason = ""
            upgrade_url = None

        return Decision()

    async def record(self, tenant_id, feature, qty=1):
        pass


class MockScope:
    def __init__(self, tenant_id):
        self.tenant_id = tenant_id


class MockEnv:
    def __init__(self, queue=None):
        self.QUEUE = queue


@pytest.mark.asyncio
async def test_import_keep_takeout_integration(db):
    """Integration test: import Keep Takeout zip through import_notes."""
    from keepfor.utils.importer import import_notes

    user = await register_user(db, "keep@test.local", "password123")
    user_id = user["id"]

    providers = MockProviders()
    scope = MockScope(user_id)
    env = MockEnv(queue=MockQueue())

    notes = [
        {
            "title": "Keep Note",
            "textContent": "Imported from Keep",
            "labels": [{"name": "keep"}],
            "isPinned": True,
            "isArchived": False,
            "isTrashed": False,
            "createdTimestampUsec": 1717378714000000,
            "annotations": [
                {"source": "WEBLINK", "title": "Example", "url": "https://example.com"}
            ],
        }
    ]
    zip_bytes = _make_keep_takeout_zip(notes)
    parsed = parse_keep_takeout(zip_bytes)

    summary = await import_notes(db, env, user_id, parsed, providers, scope, "keep")

    assert summary["notes_imported"] == 1
    assert summary["duplicates_skipped"] == 0
    assert summary["urls_queued"] == 1
    assert summary["attachments_not_imported"] == 0

    # Verify note was saved with source/source_id
    rows = await db.query_all(
        "SELECT * FROM items WHERE user_id = ? AND source = 'keep';", (user_id,)
    )
    assert len(rows) == 1
    assert rows[0]["item_type"] == "note"
    assert rows[0]["is_pinned"] == 1
    assert rows[0]["source"] == "keep"
    assert rows[0]["source_id"] == "note-0"


@pytest.mark.asyncio
async def test_import_enex_integration(db):
    """Integration test: import ENEX through import_notes."""
    from keepfor.utils.importer import import_notes

    user = await register_user(db, "enex@test.local", "password123")
    user_id = user["id"]

    providers = MockProviders()
    scope = MockScope(user_id)
    env = MockEnv(queue=MockQueue())

    notes = [
        {
            "title": "Evernote Note",
            "content": "<en-note>Body text</en-note>",
            "created": "20240115T103000Z",
            "tags": ["tag1"],
            "source_url": "https://clipped.com/article",
        }
    ]
    enex_bytes = _make_enex(notes)
    parsed = parse_enex(enex_bytes)

    summary = await import_notes(db, env, user_id, parsed, providers, scope, "evernote")

    assert summary["notes_imported"] == 1
    assert summary["urls_queued"] == 1

    rows = await db.query_all(
        "SELECT * FROM items WHERE user_id = ? AND source = 'evernote';", (user_id,)
    )
    assert len(rows) == 1
    assert rows[0]["item_type"] == "note"
    assert rows[0]["source"] == "evernote"


@pytest.mark.asyncio
async def test_import_markdown_zip_integration(db):
    """Integration test: import Markdown zip through import_notes."""
    from keepfor.utils.importer import import_notes

    user = await register_user(db, "md@test.local", "password123")
    user_id = user["id"]

    providers = MockProviders()
    scope = MockScope(user_id)
    env = MockEnv(queue=MockQueue())

    files = {"notes/note.md": "---\ntitle: Markdown Note\ntags: [mdtag]\n---\n\nBody."}
    zip_bytes = _make_markdown_zip(files)
    parsed = parse_markdown_zip(zip_bytes)

    summary = await import_notes(db, env, user_id, parsed, providers, scope, "markdown")

    assert summary["notes_imported"] == 1

    rows = await db.query_all(
        "SELECT * FROM items WHERE user_id = ? AND source = 'markdown';", (user_id,)
    )
    assert len(rows) == 1
    assert rows[0]["item_type"] == "note"
    assert rows[0]["source"] == "markdown"


@pytest.mark.asyncio
async def test_import_dedupe_on_source_id(db):
    """Test that re-importing the same export creates no duplicates."""
    from keepfor.utils.importer import import_notes

    user = await register_user(db, "dupe@test.local", "password123")
    user_id = user["id"]

    providers = MockProviders()
    scope = MockScope(user_id)
    env = MockEnv(queue=MockQueue())

    notes = [
        {
            "title": "Note",
            "textContent": "Body",
            "isTrashed": False,
            "createdTimestampUsec": 1000000,
        }
    ]
    zip_bytes = _make_keep_takeout_zip(notes)
    parsed = parse_keep_takeout(zip_bytes)

    # First import
    summary1 = await import_notes(db, env, user_id, parsed, providers, scope, "keep")
    assert summary1["notes_imported"] == 1
    assert summary1["duplicates_skipped"] == 0

    # Second import - should skip as duplicate
    summary2 = await import_notes(db, env, user_id, parsed, providers, scope, "keep")
    assert summary2["notes_imported"] == 0
    assert summary2["duplicates_skipped"] == 1


@pytest.mark.asyncio
async def test_import_enex_dedupe_on_hash(db):
    """Test that ENEX dedupes on content hash when no stable source_id."""
    from keepfor.utils.importer import import_notes

    user = await register_user(db, "enexdupe@test.local", "password123")
    user_id = user["id"]

    providers = MockProviders()
    scope = MockScope(user_id)
    env = MockEnv(queue=MockQueue())

    notes = [
        {
            "title": "Same Note",
            "content": "<en-note>Body</en-note>",
            "created": "20240115T103000Z",
        }
    ]
    enex_bytes = _make_enex(notes)
    parsed = parse_enex(enex_bytes)

    summary1 = await import_notes(
        db, env, user_id, parsed, providers, scope, "evernote"
    )
    assert summary1["notes_imported"] == 1

    summary2 = await import_notes(
        db, env, user_id, parsed, providers, scope, "evernote"
    )
    assert summary2["notes_imported"] == 0
    assert summary2["duplicates_skipped"] == 1


@pytest.mark.asyncio
async def test_import_url_cap_enforced(db):
    """Test that URL cap per import is enforced (500)."""
    from keepfor.utils.importer import MAX_URLS_PER_IMPORT, import_notes

    user = await register_user(db, "urlcap@test.local", "password123")
    user_id = user["id"]

    providers = MockProviders()
    scope = MockScope(user_id)
    env = MockEnv(queue=MockQueue())

    # Create a note with 600 URLs
    urls_text = "\n".join([f"https://example.com/{i}" for i in range(600)])
    notes = [
        {
            "title": "Many URLs",
            "textContent": urls_text,
            "isTrashed": False,
            "createdTimestampUsec": 1000000,
        }
    ]
    zip_bytes = _make_keep_takeout_zip(notes)
    parsed = parse_keep_takeout(zip_bytes)

    summary = await import_notes(db, env, user_id, parsed, providers, scope, "keep")

    # Should only queue up to MAX_URLS_PER_IMPORT
    assert summary["urls_queued"] == MAX_URLS_PER_IMPORT
    assert summary["total_urls_found"] == 600


@pytest.mark.asyncio
async def test_import_existing_urls_not_duplicated(db):
    """Test that existing URLs in library are not duplicated."""
    from keepfor.models.items import save_item
    from keepfor.utils.importer import import_notes

    user = await register_user(db, "existingurl@test.local", "password123")
    user_id = user["id"]

    providers = MockProviders()
    scope = MockScope(user_id)
    env = MockEnv(queue=MockQueue())

    # Pre-save a URL
    await save_item(db, env, user_id, "https://example.com/existing", ["existing"])

    # Import a note with the same URL
    notes = [
        {
            "title": "Note with URL",
            "textContent": "Check https://example.com/existing",
            "isTrashed": False,
            "createdTimestampUsec": 1000000,
        }
    ]
    zip_bytes = _make_keep_takeout_zip(notes)
    parsed = parse_keep_takeout(zip_bytes)

    summary = await import_notes(db, env, user_id, parsed, providers, scope, "keep")

    # URL should be skipped as duplicate
    assert summary["urls_queued"] == 0
    assert summary["urls_skipped"] == 1


@pytest.mark.asyncio
async def test_import_logs_no_note_content(db, caplog):
    """Test that logs contain no note content (privacy)."""
    from keepfor.utils.importer import import_notes, parse_keep_takeout

    user = await register_user(db, "privacy@test.local", "password123")
    user_id = user["id"]

    providers = MockProviders()
    scope = MockScope(user_id)
    env = MockEnv(queue=MockQueue())

    notes = [
        {
            "title": "Secret Note Title",
            "textContent": "Secret body content with password123",
            "labels": [{"name": "secret"}],
            "isTrashed": False,
            "createdTimestampUsec": 1000000,
        }
    ]
    zip_bytes = _make_keep_takeout_zip(notes)
    parsed = parse_keep_takeout(zip_bytes)

    # Capture logs
    import logging

    caplog.set_level(logging.DEBUG)

    await import_notes(db, env, user_id, parsed, providers, scope, "keep")

    # Check logs don't contain note content
    log_text = caplog.text
    assert "Secret Note Title" not in log_text
    assert "Secret body content" not in log_text
    assert "password123" not in log_text
    # But counts and IDs are OK
    assert "import" in log_text.lower()


# ===== 1000-note generated export test =====


def _make_large_keep_zip(num_notes: int) -> bytes:
    """Create a Keep Takeout zip with many notes."""
    notes = []
    for i in range(num_notes):
        notes.append(
            {
                "title": f"Note {i}",
                "textContent": f"Body of note {i} with https://example.com/{i}",
                "labels": [{"name": f"tag{i % 10}"}],
                "isPinned": i % 10 == 0,
                "isArchived": i % 20 == 0,
                "isTrashed": False,
                "createdTimestampUsec": 1000000 + i * 1000,
            }
        )
    return _make_keep_takeout_zip(notes)


@pytest.mark.asyncio
async def test_import_1000_notes_no_timeout(db):
    """Test importing 1000 notes works (queue fan-out)."""
    from keepfor.utils.importer import import_notes

    user = await register_user(db, "large@test.local", "password123")
    user_id = user["id"]

    providers = MockProviders()
    scope = MockScope(user_id)
    env = MockEnv(queue=MockQueue())

    zip_bytes = _make_large_keep_zip(1000)
    parsed = parse_keep_takeout(zip_bytes)

    summary = await import_notes(db, env, user_id, parsed, providers, scope, "keep")

    assert summary["notes_imported"] == 1000
    assert summary["urls_queued"] == 500  # capped at MAX_URLS_PER_IMPORT


class MockQueue:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


# ===== Malformed file test =====


@pytest.mark.asyncio
async def test_import_malformed_file_graceful(db):
    """Test that malformed files give visible error, no partial crash."""
    from keepfor.utils.importer import import_notes

    user = await register_user(db, "malformed@test.local", "password123")
    user_id = user["id"]

    providers = MockProviders()
    scope = MockScope(user_id)
    env = MockEnv(queue=MockQueue())

    # Malformed JSON in zip
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Takeout/Keep/note.json", "not valid json {")
    malformed_zip = buf.getvalue()

    parsed = parse_keep_takeout(malformed_zip)
    # Parser should skip the bad file, not crash
    assert parsed == []

    summary = await import_notes(db, env, user_id, parsed, providers, scope, "keep")
    assert summary["notes_imported"] == 0
    assert summary["parse_errors"] == 0


# ===== Search tests =====


@pytest.mark.asyncio
async def test_imported_notes_searchable(db):
    """Test that imported notes are found by FTS and semantic search."""
    from keepfor.search.engine import hybrid_search
    from keepfor.utils.importer import import_notes

    user = await register_user(db, "search@test.local", "password123")
    user_id = user["id"]

    providers = MockProviders()
    scope = MockScope(user_id)
    env = MockEnv(queue=MockQueue())

    notes = [
        {
            "title": "Searchable Note",
            "textContent": "This note contains unique searchable content about quantum computing",  # noqa: E501
            "labels": [{"name": "science"}],
            "isTrashed": False,
            "createdTimestampUsec": 1000000,
        }
    ]
    zip_bytes = _make_keep_takeout_zip(notes)
    parsed = parse_keep_takeout(zip_bytes)

    await import_notes(db, env, user_id, parsed, providers, scope, "keep")

    # Test FTS search (hybrid with mode=keyword)
    items, _ = await hybrid_search(
        db, env, user_id, "quantum", mode="keyword", limit=10
    )
    assert len(items) == 1
    assert items[0]["title"] == "Searchable Note"

    # Test category filter for notes
    items, _ = await hybrid_search(db, env, user_id, "", category="notes", limit=10)
    assert len(items) == 1
    assert items[0]["item_type"] == "note"


# ===== save_note with created_at/source/source_id tests =====


@pytest.mark.asyncio
async def test_save_note_with_created_at_and_source(db):
    """Test save_note accepts created_at, source, source_id."""
    from keepfor.models.items import get_item, save_note

    user = await register_user(db, "savenote@test.local", "password123")
    user_id = user["id"]

    env = MockEnv()

    note = await save_note(
        db,
        env,
        user_id,
        title="Imported Note",
        content_text="Body text",
        tags=["imported"],
        is_pinned=False,
        created_at="2024-01-15T10:30:00",
        source="keep",
        source_id="keep-123",
    )

    assert note["source"] == "keep"
    assert note["source_id"] == "keep-123"
    assert note["created_at"] == "2024-01-15T10:30:00"

    fetched = await get_item(db, user_id, note["id"])
    assert fetched["source"] == "keep"
    assert fetched["source_id"] == "keep-123"
    assert fetched["created_at"] == "2024-01-15T10:30:00"


# ===== Gap tests (issue #15 finish) =====


def test_parse_keep_takeout_counts_trashed():
    """Trashed notes are counted in stats, not silently dropped."""
    notes = [
        {
            "title": "Keep",
            "textContent": "x",
            "isTrashed": False,
            "createdTimestampUsec": 1,
        },
        {
            "title": "Bin1",
            "textContent": "x",
            "isTrashed": True,
            "createdTimestampUsec": 2,
        },
        {
            "title": "Bin2",
            "textContent": "x",
            "isTrashed": True,
            "createdTimestampUsec": 3,
        },
    ]
    stats = {}
    parsed = parse_keep_takeout(_make_keep_takeout_zip(notes), stats=stats)
    assert len(parsed) == 1
    assert stats["trashed_skipped"] == 2


def test_parse_keep_takeout_accepts_non_takeout_path():
    """A re-zipped Keep folder (Keep/*.json without the Takeout/ prefix) parses."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "Keep/note.json",
            json.dumps(
                {
                    "title": "Rezip",
                    "textContent": "Body",
                    "isTrashed": False,
                    "createdTimestampUsec": 1000000,
                }
            ),
        )
    parsed = parse_keep_takeout(buf.getvalue())
    assert len(parsed) == 1
    assert parsed[0]["title"] == "Rezip"
    assert parsed[0]["source"] == "keep"


def test_parse_enex_body_urls_extracted():
    """URLs embedded in the ENML body become candidate URL saves too."""
    notes = [
        {
            "title": "Linked",
            "content": '<en-note>See <a href="https://example.com/a">this</a> and https://example.com/b</en-note>',
            "created": "20240115T103000Z",
        }
    ]
    parsed = parse_enex(_make_enex(notes))
    assert len(parsed) == 1
    urls = {u["url"] for u in parsed[0]["urls"]}
    assert urls == {"https://example.com/a", "https://example.com/b"}


def test_parse_markdown_zip_oversized_file_skipped():
    """A single zip member over the per-file cap is skipped, not loaded."""
    big = "x" * (2 * 1024 * 1024 + 10)
    stats = {}
    parsed = parse_markdown_zip(
        _make_markdown_zip({"big.md": f"# Big\n\n{big}", "ok.md": "# Fine\n\nBody"}),
        stats=stats,
    )
    assert [n["title"] for n in parsed] == ["Fine"]
    assert stats["parse_errors"] == 1


def test_front_matter_block_list_and_inline_tags():
    """Front matter parses without PyYAML (not a shipped dependency)."""
    from keepfor.utils.importer import _parse_front_matter

    fm, body = _parse_front_matter(
        "---\ntitle: T\ntags:\n  - one\n  - two\n---\n\nBody"
    )
    assert fm["title"] == "T"
    assert fm["tags"] == ["one", "two"]
    assert body == "Body"

    fm2, _ = _parse_front_matter("---\ntags: a, b\ntitle: X\n---\nBody")
    assert fm2["tags"] == ["a", "b"]
    assert fm2["title"] == "X"


def test_detect_file_type_xhtml_bookmarks_are_html():
    """XHTML bookmark exports start with <?xml but are not ENEX files."""
    xhtml = b'<?xml version="1.0"?><!DOCTYPE NETSCAPE-Bookmark-file-1><html></html>'
    assert detect_file_type("bookmarks.html", xhtml) == "html"
    assert detect_file_type("whatever.xml", xhtml) == "unknown"


@pytest.mark.asyncio
async def test_import_archived_note_keeps_read_state(db):
    """An archived Keep note lands with read_state='archived' and its date."""
    user = await register_user(db, "archived@test.local", "password123")
    user_id = user["id"]
    env = MockEnv(queue=MockQueue())

    notes = [
        {
            "title": "Old Archived",
            "textContent": "from long ago",
            "isArchived": True,
            "isTrashed": False,
            "createdTimestampUsec": 1690514572858000,
        }
    ]
    parsed = parse_keep_takeout(_make_keep_takeout_zip(notes))
    summary = await import_notes(
        db, env, user_id, parsed, MockProviders(), MockScope(user_id), "keep"
    )
    assert summary["notes_imported"] == 1

    row = await db.query_first(
        "SELECT read_state, created_at FROM items WHERE user_id = ? AND source = 'keep';",  # noqa: E501
        (user_id,),
    )
    assert row["read_state"] == "archived"
    assert row["created_at"].startswith("2023-07-28")


@pytest.mark.asyncio
async def test_import_dedupe_fallback_content_hash(db):
    """Notes without a source_id dedupe on a title+body+date hash."""
    user = await register_user(db, "hashdupe@test.local", "password123")
    user_id = user["id"]
    env = MockEnv(queue=MockQueue())

    note = {
        "title": "Hashed",
        "body": "Body text",
        "tags": ["x"],
        "is_pinned": False,
        "read_state": "unread",
        "created_at": "2024-01-01T00:00:00",
        "source_id": None,
        "urls": [],
        "attachments_count": 0,
    }
    summary1 = await import_notes(
        db, env, user_id, [note], MockProviders(), MockScope(user_id), "keep"
    )
    summary2 = await import_notes(
        db, env, user_id, [dict(note)], MockProviders(), MockScope(user_id), "keep"
    )
    assert summary1["notes_imported"] == 1
    assert summary2["notes_imported"] == 0
    assert summary2["duplicates_skipped"] == 1

    row = await db.query_first(
        "SELECT source_id FROM items WHERE user_id = ? AND source = 'keep';",
        (user_id,),
    )
    assert row["source_id"]  # the hash was stored


@pytest.mark.asyncio
async def test_save_note_defaults_created_at_and_read_state(db):
    """created_at falls back to CURRENT_TIMESTAMP: the column default must
    not be bypassed by an explicit NULL (regression guard)."""
    user = await register_user(db, "savenote_default@test.local", "password123")
    note = await save_note(db, None, user["id"], "Quick", "Body text")
    row = await db.query_first(
        "SELECT created_at, read_state FROM items WHERE id = ?;", (note["id"],)
    )
    assert row["created_at"]
    assert row["read_state"] == "unread"


@pytest.mark.asyncio
async def test_imported_notes_findable_via_mcp_search_library(db):
    """MCP search_library finds an imported note (keyword mode)."""
    from keepfor.mcp.server import process_mcp_request

    user = await register_user(db, "mcpsearch@test.local", "password123")
    user_id = user["id"]
    env = MockEnv(queue=MockQueue())

    notes = [
        {
            "title": "MCP Searchable Note",
            "textContent": "contains zephyrgist content unique to this note",
            "isTrashed": False,
            "createdTimestampUsec": 1000000,
        }
    ]
    parsed = parse_keep_takeout(_make_keep_takeout_zip(notes))
    await import_notes(
        db, env, user_id, parsed, MockProviders(), MockScope(user_id), "keep"
    )

    req = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {
            "name": "search_library",
            "arguments": {"query": "zephyrgist", "mode": "keyword"},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    payload = json.loads(res["result"]["content"][0]["text"])
    assert [p["title"] for p in payload] == ["MCP Searchable Note"]
    assert payload[0]["item_type"] == "note"


@pytest.mark.asyncio
async def test_imported_notes_findable_via_semantic_search(db, monkeypatch):
    """Semantic search returns an imported note when the vector index has it."""
    from keepfor.search.engine import hybrid_search

    user = await register_user(db, "vecsearch@test.local", "password123")
    user_id = user["id"]
    env = MockEnv(queue=MockQueue())

    notes = [
        {
            "title": "Semantic Note",
            "textContent": "all about wibbletron machines",
            "isTrashed": False,
            "createdTimestampUsec": 1000000,
        }
    ]
    parsed = parse_keep_takeout(_make_keep_takeout_zip(notes))
    await import_notes(
        db, env, user_id, parsed, MockProviders(), MockScope(user_id), "keep"
    )
    row = await db.query_first(
        "SELECT id FROM items WHERE user_id = ? AND source = 'keep';", (user_id,)
    )

    async def fake_search_vectorize(env, user_id, query, limit=50, *, scope=None):
        return [{"item_id": row["id"], "score": 0.9}]

    monkeypatch.setattr("keepfor.search.engine.search_vectorize", fake_search_vectorize)
    items, _ = await hybrid_search(db, env, user_id, "wibbletron", mode="semantic")
    assert [i["title"] for i in items] == ["Semantic Note"]


@pytest.mark.asyncio
async def test_import_route_queues_notes_in_chunks(db, monkeypatch):
    """POST /import enqueues chunked note batches instead of importing inline
    (a 1,000-note export must not time out the request)."""
    from fastapi.testclient import TestClient

    from keepfor.app import app
    from keepfor.auth.service import login_user

    env = MockEnv(queue=MockQueue())
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    client = TestClient(app)

    await register_user(db, "fanout@test.local", "password123")
    _, session_id = await login_user(db, "fanout@test.local", "password123")
    client.cookies.set("kfm_session", session_id)

    zip_bytes = _make_large_keep_zip(60)
    resp = client.post(
        "/import",
        files={"file": ("takeout.zip", zip_bytes, "application/zip")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert len(env.QUEUE.sent) == 3  # ceil(60 / IMPORT_QUEUE_CHUNK)
    assert all("import_notes_batch" in m for m in env.QUEUE.sent)
    assert sum(len(m["import_notes_batch"]) for m in env.QUEUE.sent) == 60

    # Nothing was imported inline in the request; the consumer does that.
    row = await db.query_first("SELECT COUNT(*) AS c FROM items;")
    assert row["c"] == 0


@pytest.mark.asyncio
async def test_import_route_rejects_unknown_file_type(db, monkeypatch):
    """An unrecognized upload redirects back to settings with a visible error."""
    from fastapi.testclient import TestClient

    from keepfor.app import app
    from keepfor.auth.service import login_user

    env = MockEnv(queue=MockQueue())
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    client = TestClient(app)

    await register_user(db, "unknownfile@test.local", "password123")
    _, session_id = await login_user(db, "unknownfile@test.local", "password123")
    client.cookies.set("kfm_session", session_id)

    resp = client.post(
        "/import",
        files={
            "file": (
                "random.bin",
                b"\x00\x01\x02binary junk",
                "application/octet-stream",
            )
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings"
    # The cookie carries the parse-stage summary; read it from the raw
    # header (the JSON round-trips through cookie quoting).
    set_cookie = resp.headers.get("set-cookie", "")
    assert "import_summary=" in set_cookie
    # Starlette quotes the cookie with http.cookies escaping (surrounding
    # quotes, backslash-escaped quotes and \054 for commas); undo it just
    # far enough to check the error text is really in there.
    raw = set_cookie.split("import_summary=", 1)[1].split(";", 1)[0]
    decoded = raw.strip('"').replace("\\054", ",").replace('\\"', '"')
    assert '"error"' in decoded
    assert "Unrecognized file type" in decoded


# ===== Zip bomb caps (Pete review #1) =====


def _zip_with_declared_size(name: str, content: str, declared_size: int) -> bytes:
    """Zip whose central directory lies about a member's uncompressed size."""
    import struct

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, content)
    data = bytearray(buf.getvalue())
    # Central directory signature PK\\x01\\x02; uncompressed size at offset 24.
    cd = data.rfind(b"PK\x01\x02")
    assert cd != -1
    struct.pack_into("<I", data, cd + 24, declared_size)
    return bytes(data)


def test_zip_caps_are_the_reviewed_values():
    """The shipped caps match the reviewed values (2 MB / 200 MB / 20k)."""
    from keepfor.utils import importer

    assert importer.ZIP_MAX_MEMBER_BYTES == 2 * 1024 * 1024
    assert importer.ZIP_MAX_TOTAL_BYTES == 200 * 1024 * 1024
    assert importer.ZIP_MAX_MEMBERS == 20000


def test_parse_keep_takeout_oversized_member_skipped():
    """A Keep JSON member over the 2 MB per-file cap is skipped and counted."""
    big = "x" * (2 * 1024 * 1024 + 10)
    notes = [
        {"title": "Big", "textContent": big},
        {"title": "Small", "textContent": "ok"},
    ]
    stats = {}
    parsed = parse_keep_takeout(_make_keep_takeout_zip(notes), stats=stats)
    assert [n["title"] for n in parsed] == ["Small"]
    assert stats["parse_errors"] == 1


def test_parse_keep_takeout_lying_file_size_bounded_read():
    """A member whose declared size lies below the cap is still bounded."""
    big_json = json.dumps({"title": "Big", "textContent": "x" * (2 * 1024 * 1024 + 10)})
    zip_bytes = _zip_with_declared_size("Takeout/Keep/lie.json", big_json, 100)
    stats = {}
    assert parse_keep_takeout(zip_bytes, stats=stats) == []
    assert stats["parse_errors"] == 1


def test_parse_keep_takeout_total_size_cap(monkeypatch):
    """Declared uncompressed total over the cap is rejected before any read."""
    from keepfor.utils import importer

    monkeypatch.setattr(importer, "ZIP_MAX_TOTAL_BYTES", 1000)
    zip_bytes = _make_keep_takeout_zip([{"title": "T", "textContent": "y" * 600}])
    with pytest.raises(ZipBombError, match="uncompressed"):
        parse_keep_takeout(zip_bytes)


def test_parse_markdown_zip_total_size_cap(monkeypatch):
    from keepfor.utils import importer

    monkeypatch.setattr(importer, "ZIP_MAX_TOTAL_BYTES", 1000)
    zip_bytes = _make_markdown_zip({"a.md": "y" * 600, "b.md": "z" * 600})
    with pytest.raises(ZipBombError, match="uncompressed"):
        parse_markdown_zip(zip_bytes)


def test_parse_zip_member_count_cap(monkeypatch):
    """Over the member-count cap both parsers refuse before opening anything."""
    from keepfor.utils import importer

    monkeypatch.setattr(importer, "ZIP_MAX_MEMBERS", 3)
    zip_bytes = _make_markdown_zip({f"n{i}.md": "x" for i in range(4)})
    with pytest.raises(ZipBombError, match="too many files"):
        parse_markdown_zip(zip_bytes)
    with pytest.raises(ZipBombError, match="too many files"):
        parse_keep_takeout(zip_bytes)


@pytest.mark.asyncio
async def test_import_route_rejects_zip_bomb(db, monkeypatch):
    """A zip over the caps gives a visible error and imports nothing."""
    from fastapi.testclient import TestClient

    from keepfor.app import app
    from keepfor.auth.service import login_user
    from keepfor.utils import importer

    monkeypatch.setattr(importer, "ZIP_MAX_MEMBERS", 3)
    env = MockEnv(queue=MockQueue())
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    client = TestClient(app)

    await register_user(db, "zipbomb@test.local", "password123")
    _, session_id = await login_user(db, "zipbomb@test.local", "password123")
    client.cookies.set("kfm_session", session_id)

    zip_bytes = _make_markdown_zip({f"n{i}.md": "x" for i in range(4)})
    resp = client.post(
        "/import",
        files={"file": ("bomb.zip", zip_bytes, "application/zip")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings"
    set_cookie = resp.headers.get("set-cookie", "")
    raw = set_cookie.split("import_summary=", 1)[1].split(";", 1)[0]
    decoded = raw.strip('"').replace("\\054", ",").replace('\\"', '"')
    assert "too many files" in decoded
    assert env.QUEUE.sent == []  # nothing queued
    row = await db.query_first("SELECT COUNT(*) AS c FROM items;")
    assert row["c"] == 0

    # The error is visible where the user lands (the settings page).
    page = client.get("/settings")
    assert "Import failed" in page.text
    assert "too many files" in page.text


def _import_summary(resp) -> dict:
    """Parse the import summary cookie of an /import redirect."""
    set_cookie = resp.headers.get("set-cookie", "")
    raw = set_cookie.split("import_summary=", 1)[1].split(";", 1)[0]
    decoded = raw.strip('"').replace("\\054", ",").replace('\\"', '"')
    return json.loads(decoded)


# ===== ENEX entity expansion (Pete review #2) =====

BILLION_LAUGHS_ENEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE en-export [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<en-export><note><title>&lol3;</title><content><![CDATA[x]]></content>
<created>20240115T103000Z</created></note></en-export>"""


def test_parse_enex_rejects_entity_expansion():
    """A billion-laughs payload is refused, not expanded."""
    from keepfor.utils.importer import parse_enex

    with pytest.raises(ValueError, match="DTD"):
        parse_enex(BILLION_LAUGHS_ENEX)


@pytest.mark.asyncio
async def test_import_route_rejects_enex_entity_expansion(db, monkeypatch):
    """The upload gives a visible error and imports nothing."""
    from fastapi.testclient import TestClient

    from keepfor.app import app
    from keepfor.auth.service import login_user

    env = MockEnv(queue=MockQueue())
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    client = TestClient(app)

    await register_user(db, "enexbomb@test.local", "password123")
    _, session_id = await login_user(db, "enexbomb@test.local", "password123")
    client.cookies.set("kfm_session", session_id)

    resp = client.post(
        "/import",
        files={"file": ("bomb.enex", BILLION_LAUGHS_ENEX, "application/xml")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings"
    summary = _import_summary(resp)
    assert "rejected for safety" in summary["error"]
    assert env.QUEUE.sent == []  # nothing queued
    row = await db.query_first("SELECT COUNT(*) AS c FROM items;")
    assert row["c"] == 0

    page = client.get("/settings")
    assert "rejected for safety" in page.text


# ===== Plan limits (Pete review #3) =====


class LimitedEntitlements:
    """Allows a fixed number of `import` units, then refuses (plan limit)."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    async def check(self, tenant_id, action, qty=1):
        from keepfor.spi import Decision

        if action != "import":
            return Decision(allowed=True)
        return Decision(allowed=self.used + qty <= self.limit, reason="limit")

    async def record(self, tenant_id, action, qty=1):
        if action == "import":
            self.used += qty


@pytest.mark.asyncio
async def test_import_notes_stops_at_plan_limit(db):
    """Notes count against the plan limit; the rest is skipped, not imported."""
    from keepfor.utils.importer import import_notes

    user = await register_user(db, "planlimit@test.local", "password123")
    user_id = user["id"]

    providers = MockProviders()
    providers.entitlements = LimitedEntitlements(limit=2)
    scope = MockScope(user_id)
    env = MockEnv(queue=MockQueue())

    notes = [
        {"title": f"Note {i}", "body": "body", "tags": [], "urls": []} for i in range(3)
    ]
    summary = await import_notes(db, env, user_id, notes, providers, scope, "keep")

    assert summary["notes_imported"] == 2
    assert summary["plan_skipped"] == 1
    rows = await db.query_all(
        "SELECT title FROM items WHERE user_id = ? ORDER BY title;", (user_id,)
    )
    assert [r["title"] for r in rows] == ["Note 0", "Note 1"]


@pytest.mark.asyncio
async def test_import_notes_urls_count_against_plan_limit(db):
    """A note's extracted URLs cost plan units too."""
    from keepfor.utils.importer import import_notes

    user = await register_user(db, "planurl@test.local", "password123")
    user_id = user["id"]

    providers = MockProviders()
    providers.entitlements = LimitedEntitlements(limit=2)
    scope = MockScope(user_id)
    env = MockEnv(queue=MockQueue())

    notes = [
        {
            "title": "Note",
            "body": "body",
            "tags": [],
            "urls": [
                {"url": "https://example.com/a", "title": "A", "description": ""},
                {"url": "https://example.com/b", "title": "B", "description": ""},
            ],
        }
    ]
    summary = await import_notes(db, env, user_id, notes, providers, scope, "keep")

    assert summary["notes_imported"] == 1
    assert summary["urls_queued"] == 1
    assert summary["plan_skipped"] == 1


@pytest.mark.asyncio
async def test_import_route_reports_plan_limit(db, monkeypatch):
    """A plan that cannot fit the whole upload imports what fits and says so."""
    from fastapi.testclient import TestClient

    from keepfor.app import app
    from keepfor.auth.service import login_user
    from keepfor.runtime import set_providers
    from keepfor.spi import Providers

    limited = Providers(entitlements=LimitedEntitlements(limit=2)).resolved()
    monkeypatch.setattr(app.state, "providers", limited)
    set_providers(limited)
    env = MockEnv(queue=MockQueue())
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    client = TestClient(app)

    await register_user(db, "planroute@test.local", "password123")
    _, session_id = await login_user(db, "planroute@test.local", "password123")
    client.cookies.set("kfm_session", session_id)

    zip_bytes = _make_keep_takeout_zip(
        [{"title": f"Note {i}", "textContent": "b"} for i in range(3)]
    )
    resp = client.post(
        "/import",
        files={"file": ("keep.zip", zip_bytes, "application/zip")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    summary = _import_summary(resp)
    assert summary["notes_queued"] == 3
    assert summary["plan_skipped"] == 1

    # The toast tells the user how many items the plan limit refused.
    page = client.get("/")
    assert "1 skipped: plan limit" in page.text


# ===== Upload size cap (Pete review #4) =====


@pytest.mark.asyncio
async def test_import_route_rejects_oversize_upload(db, monkeypatch):
    """An upload over the 50 MB cap is rejected server-side."""
    from fastapi.testclient import TestClient

    from keepfor.app import app
    from keepfor.auth.service import login_user
    from keepfor.utils.importer import ZIP_MAX_BYTES

    assert ZIP_MAX_BYTES == 50 * 1024 * 1024

    env = MockEnv(queue=MockQueue())
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    client = TestClient(app)

    await register_user(db, "toobig@test.local", "password123")
    _, session_id = await login_user(db, "toobig@test.local", "password123")
    client.cookies.set("kfm_session", session_id)

    resp = client.post(
        "/import",
        files={"file": ("big.zip", b"x" * (ZIP_MAX_BYTES + 1), "application/zip")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings"
    summary = _import_summary(resp)
    assert "too large" in summary["error"]
    assert env.QUEUE.sent == []  # nothing queued
    row = await db.query_first("SELECT COUNT(*) AS c FROM items;")
    assert row["c"] == 0
