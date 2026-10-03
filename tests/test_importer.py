"""Tests for bookmark import functionality."""

import pytest
from src.utils.importer import parse_netscape_bookmarks, parse_csv_bookmarks

def test_parse_netscape_html():
    """Test Netscape bookmark HTML format parsing."""
    html = """
    <!DOCTYPE NETSCAPE-Bookmark-file-1>
    <META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
    <TITLE>Bookmarks</TITLE>
    <H1>Bookmarks</H1>
    <DL><p>
        <DT><A HREF="https://example.com/1" ADD_DATE="1700000000" TAGS="tech,news">Example One</A>
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
