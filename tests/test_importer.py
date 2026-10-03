"""Tests for bookmark import functionality."""

from src.utils.importer import parse_csv_bookmarks, parse_netscape_bookmarks


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
