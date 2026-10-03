"""Tests for URL canonicalization and normalization."""

import pytest
from src.utils.url import canonicalize_url

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
