"""Tests for HTML extraction and sanitization."""

from src.consumer.extractor import extract_article, sanitize_clean_html


def test_sanitize_html():
    """Test HTML sanitization removes scripts and dangerous attributes."""
    dirty = """
    <div>
        <h1>Clean Header</h1>
        <script>alert('xss');</script>
        <p>Clean paragraph with <a href="https://example.com"
        onclick="steal()">link</a>.</p>
        <iframe src="https://evil.com"></iframe>
    </div>
    """
    clean = sanitize_clean_html(dirty)
    assert "<script>" not in clean
    assert "<iframe>" not in clean
    assert "onclick" not in clean
    assert "<h1>Clean Header</h1>" in clean
    assert 'href="https://example.com"' in clean


def test_fallback_when_empty_body():
    """Test extraction uses OpenGraph metadata when body is empty."""
    raw_html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>My Cool Homepage</title>
        <meta property="og:description" content="A personal showcase of web projects.">
        <meta property="og:site_name" content="CoolSite">
    </head>
    <body>
        <div id="app"></div>
    </body>
    </html>
    """
    extracted = extract_article(raw_html, "https://example.com")
    assert extracted["title"] == "My Cool Homepage"
    assert extracted["site_name"] == "CoolSite"
    assert extracted["excerpt"] == "A personal showcase of web projects."
    assert extracted["is_fallback"] == 1
