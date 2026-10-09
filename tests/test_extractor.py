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


def test_youtube_extracts_title_and_description():
    """YouTube pages are JS-rendered; meta tags carry the real content."""
    html = (
        "<html><head><title>We Bought the World's Cheapest Car - YouTube</title>"
        '<meta property="og:title" content="We Bought the World\'s Cheapest Car">'
        '<meta property="og:description"'
        ' content="We traveled to China to buy the cheapest car possible.'
        " Here's what we got.\">"
        '<meta property="og:image"'
        ' content="https://i.ytimg.com/vi/abc123/maxresdefault.jpg">'
        '<meta name="author" content="Car Adventures">'
        '<meta itemprop="datePublished" content="2024-03-15">'
        "</head><body></body></html>"
    )
    out = extract_article(html, "https://www.youtube.com/watch?v=abc123")
    assert out["title"] == "We Bought the World's Cheapest Car"
    assert "cheapest car" in out["content_text"]
    assert out["byline"] == "Car Adventures"
    assert out["published_date"] == "2024-03-15"
    assert out["image_url"] == "https://i.ytimg.com/vi/abc123/maxresdefault.jpg"
    assert out["word_count"] > 15
    assert "About" not in out["content_text"]
    assert "Press" not in out["content_text"]


def test_youtube_uses_full_short_description():
    """Full description lives in the embedded player response, not og:description."""
    html = (
        "<html><head><title>Video - YouTube</title>"
        '<meta property="og:description" content="Short truncated blurb.">'
        "</head><body><script>var x = {"
        '"shortDescription":"This is the full video description. '
        "It has multiple sentences and much more detail than the "
        'truncated OpenGraph description that YouTube also emits."'
        "};</script></body></html>"
    )
    out = extract_article(html, "https://www.youtube.com/watch?v=abc")
    assert "full video description" in out["content_text"]
    assert "multiple sentences" in out["content_text"]
    assert out["is_fallback"] == 0


def test_github_extracts_readme_not_session_banner():
    """GitHub HTML has session banners that trafilatura picks up instead of README."""
    html = (
        "<html><head><title>awesome-selfhosted: A list of Free Software"
        " · GitHub</title>"
        '<meta property="og:title"'
        ' content="awesome-selfhosted: A list of Free Software">'
        '<meta property="og:description"'
        ' content="A list of Free Software network services and web applications.">'
        '</head><body><div class="js-session-flash">'
        "You signed in with another tab or window. "
        "Reload to refresh your session.</div>"
        '<article class="markdown-body"><h1>Awesome Selfhosted</h1>'
        "<p>A list of Free Software network services and web applications"
        " which can be hosted on your own servers.</p>"
        "<p>This is a curated list of open source alternatives to"
        " popular SaaS products.</p>"
        "<p>All entries are free to use and modify under their"
        " respective licenses.</p>"
        "</article></body></html>"
    )
    out = extract_article(
        html, "https://github.com/awesome-selfhosted/awesome-selfhosted"
    )
    assert "You signed in with another tab" not in out["content_text"]
    assert "Free Software network services" in out["content_text"]
    assert "curated list" in out["content_text"]
    assert out["is_fallback"] == 0


@pytest.fixture
def force_bs4_fallback(monkeypatch):
    """Force extract_article down the BeautifulSoup fallback path.

    Trafilatura's minimum-length threshold varies by version, so tests of
    the fallback must not depend on it failing on their fixture.
    """
    from src.consumer import extractor

    extractor._load_parsers()  # ensure _soup_cls is populated
    monkeypatch.setattr(extractor, "_trafilatura", None)
    monkeypatch.setattr(extractor, "_parsers_loaded", True)
    return extractor


def test_fallback_uses_readability_for_content_pages(force_bs4_fallback):
    """When trafilatura fails, readability finds content in common containers."""
    html = """
    <html><head><title>My Blog Post</title></head>
    <body>
    <nav>Home | About | Contact</nav>
    <article>
    <h1>My Blog Post</h1>
    <p>This is the first paragraph of a real article with enough content
    to be useful for extraction testing purposes.</p>
    <p>This is the second paragraph with more substance and detail about
    the topic being discussed here today.</p>
    <p>A third paragraph ensures we have enough text to pass the
    readability threshold for fallback extraction.</p>
    </article>
    <footer>Copyright 2024</footer>
    </body></html>
    """
    out = extract_article(html, "https://blog.example.com/post")
    assert out["is_fallback"] == 1
    assert "first paragraph" in out["content_text"]
    assert "second paragraph" in out["content_text"]
    assert "Copyright" not in out["content_text"]


def test_substantial_readability_recovery_clears_fallback(force_bs4_fallback):
    """A large readability recovery is a real article, not a link-only stub."""
    paras = "".join(
        f"<p>Paragraph {i} of a long recovered article. " + ("word " * 60) + "</p>"
        for i in range(4)
    )
    html = f"<html><head><title>Recovered</title></head><body>{paras}</body></html>"
    out = extract_article(html, "https://example.com/recovered")
    assert out["word_count"] >= 200
    assert out["is_fallback"] == 0


def test_fallback_falls_back_to_url_when_nothing_found(force_bs4_fallback):
    """When no content can be extracted, title falls back to URL."""
    html = """
    <html><head><title>Empty Page</title></head>
    <body><div id="app"></div></body></html>
    """
    out = extract_article(html, "https://example.com/empty")
    assert out["title"] == "Empty Page"
    assert out["is_fallback"] == 1


def test_nav_junk_marked_as_fallback():
    """Content that is mostly navigation words should be marked as fallback."""
    html = """
    <html><head><title>Some Page</title></head>
    <body><article>
    <p>Sign In Log In Cart Menu Home About Contact Copyright Privacy Terms
    Search Register Login Logout Subscribe Newsletter Policy</p>
    </article></body></html>
    """
    out = extract_article(html, "https://example.com/page")
    assert out["is_fallback"] == 1


def test_genuine_content_not_flagged_as_nav_junk():
    """Normal article content should not be flagged as nav junk."""
    from src.consumer.extractor import _is_nav_junk

    text = (
        "The quick brown fox jumps over the lazy dog near the river bank"
        " on a sunny afternoon. "
        "Machine learning models require large amounts of training data to"
        " achieve good performance "
        "across many different tasks and domains. Cloudflare Workers provide"
        " a serverless execution "
        "environment for edge computing applications that need low latency."
    )
    assert _is_nav_junk(text) is False


def test_soft_404_with_error_body():
    """Pages with 'ERROR 404' body text should be detected as blocked."""
    from src.consumer.extractor import detect_blocked_page

    reason = detect_blocked_page(
        "Videolectures",
        "ERROR 404\nUnfortunately, the page you are trying to reach"
        " has been moved or does not exist.",
    )
    assert reason


def test_is_youtube_url_domain_boundaries():
    """Ensure YouTube URL detection checks proper domain boundaries."""
    from src.consumer.extractor import _is_youtube_url

    assert _is_youtube_url("https://youtube.com/watch?v=123") is True
    assert _is_youtube_url("https://www.youtube.com/watch?v=123") is True
    assert _is_youtube_url("https://m.youtube.com/watch?v=123") is True
    assert _is_youtube_url("https://youtu.be/123") is True

    # Hostnames ending with 'youtube.com' or 'youtu.be' without dot boundary
    # must be rejected
    assert _is_youtube_url("https://notyoutube.com/watch?v=123") is False
    assert _is_youtube_url("https://fakeyoutube.com") is False
    assert _is_youtube_url("https://fakeyoutu.be") is False
    assert _is_youtube_url("https://youtube.com.attacker.com") is False
    assert _is_youtube_url("https://youtu.be.attacker.com") is False
