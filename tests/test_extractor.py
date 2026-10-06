"""Tests for HTML extraction, sanitization, and blocked/redirect handling."""

import pytest

from src.auth.service import register_user
from src.consumer.extractor import extract_article, sanitize_clean_html


class MockEnv:
    """Minimal env: no R2, no AI, no Vectorize (extraction must still work)."""

    DB = None
    BUCKET = None
    AI = None
    VECTORIZE = None
    QUEUE = None


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


def test_sanitize_drops_script_and_style_text():
    """Non-content tags must be decomposed, not unwrapped: unwrap leaks their
    source into the reader (found in prod: CSS/JS inside article prose)."""
    dirty = (
        "<p>before</p><script>alert('xss')</script>"
        "<style>body{display:none}</style><p>after</p>"
    )
    clean = sanitize_clean_html(dirty)
    assert "alert" not in clean
    assert "display:none" not in clean
    assert "<p>before</p>" in clean and "<p>after</p>" in clean


def test_sanitize_drops_svg_and_form_text():
    """Inline SVG/canvas/form controls are chrome, not prose."""
    dirty = (
        "<p>x</p><svg><text>chart label</text></svg><button>Subscribe</button><p>y</p>"
    )
    clean = sanitize_clean_html(dirty)
    assert "chart label" not in clean
    assert "Subscribe" not in clean
    assert "<p>x</p>" in clean and "<p>y</p>" in clean


def test_sanitize_flattens_nested_pre():
    """Nested <pre> (seen in prod) breaks monospace block styling."""
    clean = sanitize_clean_html("<pre><pre>code line</pre></pre>")
    assert clean.count("<pre>") == 1
    assert "code line" in clean


def test_sanitize_normalizes_whitespace_entities():
    """&nbsp; runs and zero-width chars poison copy-for-AI and FTS."""
    clean = sanitize_clean_html("<p>hello&nbsp;&nbsp;&nbsp;world</p><p>he\u200bllo</p>")
    assert "\u00a0" not in clean
    assert "\u200b" not in clean
    assert "hello world" in clean


def test_extract_flags_login_wall():
    """Login walls extract to a stub; they must not be stored as saved."""
    html = (
        "<html><head><title>Sign in to continue</title></head>"
        "<body><form>Sign in to continue to access this page.</form></body></html>"
    )
    out = extract_article(html, "https://intranet.example.com/page")
    assert out["blocked_reason"]


def test_extract_flags_redirect_stub_and_exposes_target():
    """JS/meta redirect pages carry the real URL: report it so the caller
    can follow one hop instead of keeping the stub."""
    html = (
        "<html><head><title>Redirecting…</title>"
        '<meta http-equiv="refresh" content="0;url=https://example.com/real"></head>'
        "<body></body></html>"
    )
    out = extract_article(html, "https://t.co/abc")
    assert out["redirect_url"] == "https://example.com/real"
    assert out["blocked_reason"]


def test_extract_flags_soft_404():
    html = (
        "<html><head><title>Page not found</title></head>"
        "<body><p>We are sorry, the page you requested cannot be found.</p></body>"
        "</html>"
    )
    out = extract_article(html, "https://example.com/missing")
    assert out["blocked_reason"]


def test_extract_keeps_real_short_article():
    """A genuinely short page is not a blocked page: no false positives."""
    body = (
        "<html><head><title>Release notes</title></head><body><article>"
        "<p>Version 2.1 shipped today with a faster indexer and fewer retries "
        "when upstream servers rate limit us during bulk imports of large "
        "bookmark files that users have accumulated over the years.</p>"
        "<p>Upgrade with pip install -U keepforme and rerun the reindex command "
        "to rebuild vectors for your existing library of saved documents.</p>"
        "</article></body></html>"
    )
    out = extract_article(body, "https://example.com/releases")
    assert out["blocked_reason"] is None
    assert out["redirect_url"] is None


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
    assert extracted["redirect_url"] is None


# ==========================================
# Processor-level behavior (fetch -> store)
# ==========================================


def _article_html(title: str, body: str = "x" * 400) -> str:
    return (
        f"<html><head><title>{title}</title></head>"
        f"<body><article><p>{body}</p></article></body></html>"
    )


@pytest.mark.asyncio
async def test_processor_follows_redirect_stub(db, monkeypatch):
    """A JS/meta redirect stub must be followed, not stored as the article."""
    from src.consumer import processor

    user = await register_user(db, "u@test.local", "password123")
    item_id = "11111111-1111-1111-1111-111111111111"
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status, created_at) "
        "VALUES (?,?,?,?, 'queued', '2026-01-01 00:00:00');",
        (item_id, user["id"], "https://t.co/abc", "https://t.co/abc"),
    )

    stub = (
        "<html><head><title>Redirecting</title>"
        '<meta http-equiv="refresh" content="0;url=https://example.com/real">'
        "</head><body></body></html>"
    )

    async def fake_fetch(url, headers=None):
        return stub if "t.co" in url else _article_html("Real Article Title")

    monkeypatch.setattr(processor, "fetch_page_html", fake_fetch)
    await processor.extract_and_store(db, MockEnv(), item_id, "https://t.co/abc")

    row = await db.query_first(
        "SELECT status, title FROM items WHERE id=?;", (item_id,)
    )
    assert row["status"] == "ok"
    # Stub title ("Redirecting") must be gone: we stored the hop target.
    assert row["title"] == "Real Article Title"


@pytest.mark.asyncio
async def test_processor_marks_login_wall_failed_without_raising(db, monkeypatch):
    """Walls are recorded as failed and swallowed: retrying cannot help."""
    from src.consumer import processor

    user = await register_user(db, "u2@test.local", "password123")
    item_id = "22222222-2222-2222-2222-222222222222"
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status, created_at) "
        "VALUES (?,?,?,?, 'queued', '2026-01-01 00:00:00');",
        (item_id, user["id"], "https://intranet/page", "https://intranet/page"),
    )

    async def fake_fetch(url, headers=None):
        return (
            "<html><head><title>SSO_login</title></head><body>"
            "<form>Sign in to continue to access this page.</form></body></html>"
        )

    monkeypatch.setattr(processor, "fetch_page_html", fake_fetch)
    await processor.extract_and_store(db, MockEnv(), item_id, "https://intranet/page")

    row = await db.query_first(
        "SELECT status, fail_reason FROM items WHERE id=?;", (item_id,)
    )
    assert row["status"] == "failed"
    assert "Blocked" in row["fail_reason"]


@pytest.mark.asyncio
async def test_processor_still_raises_on_origin_error(db, monkeypatch):
    """Real origin errors keep raising so the queue retries them."""
    from src.consumer import processor

    user = await register_user(db, "u3@test.local", "password123")
    item_id = "33333333-3333-3333-3333-333333333333"
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status, created_at) "
        "VALUES (?,?,?,?, 'queued', '2026-01-01 00:00:00');",
        (item_id, user["id"], "https://example.com/gone", "https://example.com/gone"),
    )

    async def fake_fetch(url, headers=None):
        raise processor.OriginHttpError(404, url)

    monkeypatch.setattr(processor, "fetch_page_html", fake_fetch)
    with pytest.raises(processor.OriginHttpError):
        await processor.extract_and_store(
            db, MockEnv(), item_id, "https://example.com/gone"
        )
    row = await db.query_first("SELECT status FROM items WHERE id=?;", (item_id,))
    assert row["status"] == "failed"


def test_extract_flags_javascript_app_shell():
    """Empty SPA mount points produced 1-word 'saved' items in prod."""
    html = (
        "<html><head><title>ServiceNow</title></head>"
        '<body><div id="app"></div><script>window.__NEXT_DATA__={}</script>'
        "</body></html>"
    )
    out = extract_article(html, "https://vendor.service-now.com/now")
    assert out["blocked_reason"]


def test_extract_flags_rate_limited_title():
    html = (
        "<html><head><title>429 Too Many Requests</title></head>"
        "<body>Sorry, you have been blocked</body></html>"
    )
    out = extract_article(html, "https://example.com/x")
    assert out["blocked_reason"]


def test_sanitize_keeps_article_prose_intact():
    """The hardening must not eat real content (regression guard)."""
    html = (
        "<h2>Heading</h2><p>Real <strong>prose</strong> with a "
        '<a href="https://example.com/x">link</a>.</p>'
        "<ul><li>one</li><li>two</li></ul><pre><code>print('hi')</code></pre>"
        "<table><thead><tr><th>K</th></tr></thead>"
        "<tbody><tr><td>v</td></tr></tbody></table>"
    )
    clean = sanitize_clean_html(html)
    for needle in (
        "<h2>Heading</h2>",
        "<strong>prose</strong>",
        'href="https://example.com/x"',
        "<li>one</li>",
        "print('hi')",
        "<th>K</th>",
        "<td>v</td>",
    ):
        assert needle in clean, needle
