# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for URL canonicalization and normalization."""

from keepfor.utils.url import canonicalize_url, extract_url


def test_strip_tracking_params():
    """Test removal of common tracking parameters."""
    url = "https://Example.COM:443/article/?utm_source=twitter&utm_medium=social&id=123&fbclid=xyz#section"
    clean = canonicalize_url(url)
    assert clean == "https://example.com/article?id=123"


def test_trailing_slash_removal():
    """Test trailing slash normalization."""
    url = "http://example.org/path/to/page/"
    clean = canonicalize_url(url)
    assert clean == "http://example.org/path/to/page"


def test_auto_https_scheme():
    """Test scheme is added when missing."""
    url = "blog.cloudflare.com/python-workers"
    clean = canonicalize_url(url)
    assert clean == "https://blog.cloudflare.com/python-workers"


def test_extract_url_pure_urls():
    """Test extract_url preserves valid pure URLs."""
    assert extract_url("https://example.com/article") == "https://example.com/article"
    assert (
        extract_url("http://example.org/test?a=1&b=2#frag")
        == "http://example.org/test?a=1&b=2#frag"
    )
    assert (
        extract_url("   https://example.com/spaced   ") == "https://example.com/spaced"
    )


def test_extract_url_without_scheme():
    """Test extract_url prepends https to valid domain strings."""
    assert extract_url("example.com") == "https://example.com"
    assert extract_url("sub.example.com/path") == "https://sub.example.com/path"
    assert (
        extract_url("github.com/fastapi/fastapi")
        == "https://github.com/fastapi/fastapi"
    )


def test_extract_url_embedded_in_share_text():
    """Test extracting URL from iOS/Android app share strings."""
    text1 = "Interesting piece: https://theverge.com/2026/10/apple-news via @verge"
    assert extract_url(text1) == "https://theverge.com/2026/10/apple-news"

    text2 = "Check this out https://nytimes.com/article.html."
    assert extract_url(text2) == "https://nytimes.com/article.html"

    text3 = "Here is the paper (https://arxiv.org/abs/2301.00001)"
    assert extract_url(text3) == "https://arxiv.org/abs/2301.00001"

    # Balanced parens inside URL should be preserved
    wiki = "https://en.wikipedia.org/wiki/Rust_(programming_language)"
    assert extract_url(wiki) == wiki


def test_extract_url_invalid_inputs():
    """Test extract_url returns None for invalid or non-URL strings."""
    assert extract_url("") is None
    assert extract_url("   ") is None
    assert extract_url(None) is None
    assert extract_url("Just some random text with no links") is None
    assert extract_url("javascript:alert(1)") is None
    assert extract_url("file:///etc/passwd") is None
