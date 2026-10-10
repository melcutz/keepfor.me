# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

import pytest

from src.auth.service import register_user
from src.models.items import save_item, save_note, toggle_pin_item, update_user_notes
from src.utils.importer import (
    export_library_html,
    export_library_json,
    parse_csv_bookmarks,
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
    assert 'TAGS="tag&amp;1,tag&quot;2"' in exported_html
