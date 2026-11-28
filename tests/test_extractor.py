# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for HTML extraction, sanitization, and blocked/redirect handling."""

import pytest

from keepfor.auth.service import register_user
from keepfor.consumer.extractor import extract_article, sanitize_clean_html


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


def _blog_html(title: str, h1: str) -> str:
    """A generic article page: og:title plus the article's own H1."""
    prose = "Cloudflare runs AI models on GPUs next to users all over the world. " * 12
    quoted = title.replace('"', "&quot;")
    return (
        "<html><head>"
        f"<title>{title}</title>"
        f'<meta property="og:title" content="{quoted}">'
        "</head><body><article>"
        f"<h1>{h1}</h1>"
        f"<p>{prose}</p>"
        "</article></body></html>"
    )


def test_leading_h1_that_repeats_the_title_is_dropped():
    """The reader renders the title as the page heading: the article's own
    copy of it is a duplicate (issue #57)."""
    out = extract_article(
        _blog_html(
            "Workers AI: serverless inference",
            "Workers AI: serverless inference",
        ),
        "https://blog.cloudflare.com/workers-ai/",
    )
    assert out["title"] == "Workers AI: serverless inference"
    assert "<h1" not in out["clean_html"]
    assert "serverless inference" not in out["clean_html"]


def test_leading_h1_that_differs_from_the_title_is_kept():
    """Only a heading that repeats the title is a duplicate: a different H1
    is the article's own headline and stays."""
    out = extract_article(
        _blog_html("Reading list: October", "How we cut our cold starts"),
        "https://example.com/post",
    )
    assert out["title"] == "Reading list: October"
    assert "<h1>How we cut our cold starts</h1>" in out["clean_html"]


def test_leading_h1_matches_the_title_despite_case_quotes_and_space():
    """The comparison folds case, quotes and whitespace: the page may write
    the headline differently in the title and in its H1."""
    out = extract_article(
        _blog_html('The "Best" Guide', "the “best”\nguide"),
        "https://example.com/guide",
    )
    assert out["title"] == 'The "Best" Guide'
    assert "<h1" not in out["clean_html"]


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


def test_extract_reads_the_page_language():
    """The tagger needs the page language to pick its stopword list (#58)."""
    german = (
        '<html lang="de"><head><title>Hamburg</title></head><body><article>'
        "<p>Hamburg ist ein Stadtstaat und eine Hansestadt mit einem grossen "
        "Hafen, der Elbe und vielen Bruecken in der Innenstadt.</p>"
        "</article></body></html>"
    )
    assert extract_article(german, "https://de.example.com/hamburg")["language"] == "de"

    english = (
        '<html lang="en"><head><title>Hamburg</title></head><body><article>'
        "<p>Hamburg is a city state and a free city with a large harbour, "
        "the Elbe river and many bridges in the inner city.</p>"
        "</article></body></html>"
    )
    assert extract_article(english, "https://example.com/hamburg")["language"] == "en"

    plain = (
        "<html><head><title>Hamburg</title></head><body><article>"
        "<p>Hamburg is a city state with a large harbour and many bridges.</p>"
        "</article></body></html>"
    )
    assert extract_article(plain, "https://example.com/hamburg")["language"] is None


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
    from keepfor.consumer import processor

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
    from keepfor.consumer import processor

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
    from keepfor.consumer import processor

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


@pytest.mark.asyncio
async def test_processor_scopes_provenance_read_to_the_item_owner(db, monkeypatch):
    """The FTS re-index reads the item's provenance fields scoped to its owner.

    The read used to select them by item id alone; every read of a tenant
    table must carry the owning user_id.
    """
    from keepfor.consumer import processor

    user = await register_user(db, "u4@test.local", "password123")
    item_id = "44444444-4444-4444-4444-444444444444"
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status, created_at) "
        "VALUES (?,?,?,?, 'queued', '2026-01-01 00:00:00');",
        (item_id, user["id"], "https://example.com/prov", "https://example.com/prov"),
    )

    queries: list[tuple[str, tuple]] = []
    original = db.query_first

    async def spy(sql, params=()):
        queries.append((sql, params))
        return await original(sql, params)

    monkeypatch.setattr(db, "query_first", spy)

    async def fake_fetch(url, headers=None):
        return _article_html("Provenance Article")

    monkeypatch.setattr(processor, "fetch_page_html", fake_fetch)
    await processor.extract_and_store(
        db, MockEnv(), item_id, "https://example.com/prov"
    )

    provenance = [q for q in queries if "agent_summary" in q[0]]
    assert provenance, f"provenance query not recorded: {queries}"
    for sql, params in provenance:
        assert "user_id = ?" in sql, sql
        assert user["id"] in params, params


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
    from keepfor.consumer import extractor

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


def test_fallback_keeps_leading_h1_that_differs_from_title(force_bs4_fallback):
    """Fallback extraction keeps an H1 that differs from title (issue #158)."""
    prose_a = "Cloudflare runs AI models on GPUs next to users all over the world."
    prose_b = (
        "Additional technical details about edge deployment and latency reduction."
    )
    html = f"""
    <html><head><title>Reading list: October</title></head>
    <body>
    <article>
    <h1>How we cut our cold starts</h1>
    <p>{prose_a}</p>
    <p>{prose_b}</p>
    </article>
    </body></html>
    """
    out = extract_article(html, "https://example.com/post")
    assert out["title"] == "Reading list: October"
    assert "<h1>How we cut our cold starts</h1>" in out["clean_html"]


def test_fallback_recovers_h1_outside_content_container(force_bs4_fallback):
    """Fallback extraction recovers an H1 before content container (issue #158)."""
    p1 = (
        "Building reliable distributed systems requires "
        "understanding consensus protocols."
    )
    p2 = (
        "We explore practical lessons learned from operating key-value stores at scale."
    )
    html = f"""
    <html><head><title>Blog Archives</title></head>
    <body>
    <header class="entry-header">
      <h1>Distributed Systems in Practice</h1>
    </header>
    <div class="entry-content">
      <p>{p1}</p>
      <p>{p2}</p>
    </div>
    </body></html>
    """
    out = extract_article(html, "https://example.com/distributed")
    assert out["title"] == "Blog Archives"
    assert "<h1>Distributed Systems in Practice</h1>" in out["clean_html"]
    assert "consensus protocols" in out["clean_html"]


def test_fallback_preserves_subheadings_and_structure(force_bs4_fallback):
    """Fallback extraction preserves headings and paragraph tags (issue #158)."""
    p1 = "The system consists of core components: ingest worker, DB, and search engine."
    p2 = "Data flows through Cloudflare Queues into D1 and Vectorize with batching."
    html = f"""
    <html><head><title>Architecture Overview</title></head>
    <body>
    <article>
    <h1>Architecture Overview</h1>
    <h2>Component Breakdown</h2>
    <p>{p1}</p>
    <h2>Data Pipeline</h2>
    <p>{p2}</p>
    </article>
    </body></html>
    """
    out = extract_article(html, "https://example.com/arch")
    assert out["title"] == "Architecture Overview"
    assert "<h1" not in out["clean_html"]
    assert "<h2>Component Breakdown</h2>" in out["clean_html"]
    assert "<h2>Data Pipeline</h2>" in out["clean_html"]


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
    from keepfor.consumer.extractor import _is_nav_junk

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
    from keepfor.consumer.extractor import detect_blocked_page

    reason = detect_blocked_page(
        "Videolectures",
        "ERROR 404\nUnfortunately, the page you are trying to reach"
        " has been moved or does not exist.",
    )
    assert reason


def test_is_youtube_url_domain_boundaries():
    """Ensure YouTube URL detection checks proper domain boundaries."""
    from keepfor.consumer.extractor import _is_youtube_url

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


def test_extractor_escapes_html_in_youtube_github_and_fallback(force_bs4_fallback):
    """YouTube, GitHub, and fallback extractors escape title, description, and URL."""
    yt_html = (
        "<html><head>"
        '<meta property="og:title" content=\'<img src="https://evil.example/t">Vid\'>'
        '<meta property="og:description"'
        " content='<a href=\"https://evil.example/p\">Click</a>'>"
        "</head><body></body></html>"
    )
    yt_out = extract_article(yt_html, "https://www.youtube.com/watch?v=abc123")
    assert "<img" not in yt_out["clean_html"]
    assert "<a " not in yt_out["clean_html"]
    assert "&lt;img" in yt_out["clean_html"]
    assert "&lt;a " in yt_out["clean_html"]

    gh_html = (
        "<html><head>"
        '<meta property="og:title" content=\'<img src="https://evil.example/t">Repo\'>'
        '<meta property="og:description"'
        " content='<a href=\"https://evil.example/p\">Repo desc</a>'>"
        "</head><body></body></html>"
    )
    gh_out = extract_article(gh_html, "https://github.com/org/repo")
    assert "<img" not in gh_out["clean_html"]
    assert "<a " not in gh_out["clean_html"]
    assert "&lt;img" in gh_out["clean_html"]
    assert "&lt;a " in gh_out["clean_html"]

    fb_html = (
        "<html><head><title> Safe </title>"
        '<meta name="description"'
        " content='<a href=\"https://evil.example/p\">Trap</a>'>"
        "</head><body></body></html>"
    )
    fb_url = 'https://example.com/path?a=\'><img src="https://evil.example/u">'
    fb_out = extract_article(fb_html, fb_url)
    assert "<img" not in fb_out["clean_html"]
    assert '<a href="https://evil.example/p"' not in fb_out["clean_html"]
    assert "&lt;a " in fb_out["clean_html"]


def test_extract_plain_text_and_rfc_document():
    """text/plain and .txt RFC documents extract full text with is_fallback=0."""
    rfc_text = (
        "RFC 9110: HTTP Semantics\n\n"
        "Abstract\n\n"
        "The Hypertext Transfer Protocol (HTTP) is a stateless application-level\n"
        "protocol for distributed, collaborative, hypertext information systems.\n"
        "Angle brackets like <https://example.com> must be escaped.\n"
    )
    out = extract_article(
        rfc_text,
        "https://www.rfc-editor.org/rfc/rfc9110.txt",
        content_type="text/plain; charset=utf-8",
    )
    assert out["is_fallback"] == 0
    assert out["title"] == "RFC 9110: HTTP Semantics"
    assert "stateless application-level" in out["content_text"]
    assert out["word_count"] >= 25
    assert "<p>" in out["clean_html"]
    assert "&lt;https://example.com&gt;" in out["clean_html"]
    assert out["blocked_reason"] is None


def test_extract_markdown_and_rst_documents():
    """Markdown and RST documents derive titles and extract with is_fallback=0."""
    md = (
        "# Designing Distributed Systems\n\n"
        "Distributed systems require careful handling of partial failures "
        "and network partitions across nodes.\n"
    )
    md_out = extract_article(
        md, "https://example.com/notes.md", content_type="text/markdown"
    )
    assert md_out["is_fallback"] == 0
    assert md_out["title"] == "Designing Distributed Systems"
    assert "partial failures" in md_out["content_text"]
    assert md_out["blocked_reason"] is None

    rst = (
        "Python Packaging Guide\n"
        "======================\n\n"
        "Use pyproject.toml to declare build-system dependencies and metadata "
        "for modern Python packages.\n"
    )
    rst_out = extract_article(
        rst, "https://example.com/guide.rst", content_type="text/x-rst"
    )
    assert rst_out["is_fallback"] == 0
    assert rst_out["title"] == "Python Packaging Guide"
    assert "pyproject.toml" in rst_out["content_text"]
    assert rst_out["blocked_reason"] is None


def test_extract_plain_text_sniffs_when_content_type_is_none():
    """Non-HTML plain text without content_type is sniffed and extracted cleanly."""
    text = (
        "# Local Notes\n\n"
        "When content_type is omitted, plain text without HTML structural tags "
        "should still extract as readable prose instead of a 1-word fallback.\n"
    )
    out = extract_article(text, "https://example.com/raw/readme.txt")
    assert out["is_fallback"] == 0
    assert out["title"] == "Local Notes"
    assert "readable prose" in out["content_text"]
    assert out["word_count"] >= 20


@pytest.mark.parametrize(
    ("payload", "content_type"),
    [
        ("%PDF-1.7 binary stub", "application/pdf"),
        ("PNG binary data", "image/png"),
        ("ZIP archive", "application/zip"),
        ("Octet stream", "application/octet-stream"),
        ("hello\x00world\x00binary", "text/plain"),
        ("hello\x00world\x00binary", None),
        ("abc" + ("\ufffd" * 20), "text/plain"),
        ("abc" + ("\x01\x02\x03\x04" * 5), None),
    ],
)
def test_extract_rejects_binary_payloads(payload: str, content_type: str | None):
    """Binary Content-Types and binary byte payloads are rejected cleanly."""
    out = extract_article(
        payload, "https://example.com/file.bin", content_type=content_type
    )
    assert out["is_fallback"] == 1
    assert out["word_count"] == 0
    assert out["content_text"] == ""
    assert out["clean_html"] == ""
    assert out["blocked_reason"]


@pytest.mark.asyncio
async def test_fetch_page_html_preserves_content_type_and_extracts(monkeypatch):
    """fetch_page_html keeps Content-Type and _fetch_and_extract passes it."""
    from keepfor.consumer import processor
    from keepfor.utils.egress import FetchResult

    async def fake_fetch_limited(url, **_kwargs):
        return FetchResult(
            text=(
                "# Plain Title\n\nThis is plain markdown served with mixed-case header."
            ),
            status=200,
            url=url,
            headers={"cOnTeNt-TyPe": "text/markdown; charset=utf-8"},
        )

    monkeypatch.setattr(processor, "fetch_limited", fake_fetch_limited)

    page = await processor.fetch_page_html("https://example.com/doc.md")
    assert isinstance(page, str)
    assert page.content_type == "text/markdown; charset=utf-8"

    extracted, raw_html = await processor._fetch_and_extract(
        "https://example.com/doc.md", "item-1"
    )
    assert raw_html == page
    assert extracted["is_fallback"] == 0
    assert extracted["title"] == "Plain Title"
    assert "mixed-case header" in extracted["content_text"]


@pytest.mark.asyncio
async def test_fetch_and_extract_rejects_binary_with_blocked_page_error(monkeypatch):
    """Binary payloads from fetch_limited raise BlockedPageError."""
    from keepfor.consumer import processor
    from keepfor.utils.egress import FetchResult

    async def fake_fetch_limited(url, **_kwargs):
        return FetchResult(
            text="%PDF-1.4 ...",
            status=200,
            url=url,
            headers={"Content-Type": "application/pdf"},
        )

    monkeypatch.setattr(processor, "fetch_limited", fake_fetch_limited)

    with pytest.raises(processor.BlockedPageError):
        await processor._fetch_and_extract("https://example.com/spec.pdf", "item-2")


def _synthetic_rfc_html(target_bytes: int = 1_600_000) -> str:
    """Build a synthetic multi-thousand-node RFC-like HTML specification."""
    head = (
        "<!DOCTYPE html><html><head>"
        "<title>RFC 9110: HTTP Semantics</title>"
        '<meta property="og:title" content="RFC 9110: HTTP Semantics">'
        '<meta name="description" content="HTTP Semantics specification.">'
        '<meta property="og:image" content="https://example.com/rfc-cover.png">'
        "<style>body { font-family: monospace; }</style>"
        "<script>console.log('head-script');</script>"
        "</head><body>"
        "<nav><a href='https://example.com/toc'>Table of Contents</a></nav>"
        "<article>"
        "<h1>RFC 9110: HTTP Semantics</h1>"
    )
    section = (
        "<section>"
        "<h2>Section: Message Framing and Semantics</h2>"
        "<p>The Hypertext Transfer Protocol (HTTP) is a stateless "
        "application-level protocol for distributed, collaborative, "
        "hypertext information systems. "
        '<a href="https://example.com/spec" onclick="steal()">Reference</a> '
        '<a href="javascript:alert(1)">BadLink</a> '
        '<img src="https://example.com/diagram.png" alt="Diagram" '
        'onerror="evil()">'
        "</p>"
        "<pre><pre><code>GET /index.html HTTP/1.1\nHost: example.com</code></pre></pre>"
        "<script>alert('body-xss')</script>"
        "</section>"
    )
    tail = "</article><footer>Copyright IETF</footer></body></html>"
    repeat = max(1, (target_bytes - len(head) - len(tail)) // len(section) + 1)
    return head + (section * repeat) + tail


def test_extract_image_url_bounds_head_scanning(monkeypatch):
    """extract_image_url only passes a <=65_536 char <head> window to BeautifulSoup."""
    from keepfor.consumer import extractor

    seen_lengths: list[int] = []
    real_load = extractor._load_parsers

    def spy_load_parsers():
        soup_cls, tf = real_load()

        def wrapped_soup(markup, *args, **kwargs):
            seen_lengths.append(len(markup))
            return soup_cls(markup, *args, **kwargs)

        return wrapped_soup, tf

    monkeypatch.setattr(extractor, "_load_parsers", spy_load_parsers)

    padding = "<p>body filler text</p>" * 80_000  # > 1.7 MB
    html_in_head = (
        "<html><head>"
        '<meta property="og:image" content="https://example.com/cover.png">'
        f"</head><body>{padding}"
        '<meta property="og:image" content="https://example.com/late.png">'
        "</body></html>"
    )
    assert len(html_in_head) > 1_500_000
    assert extractor.extract_image_url(html_in_head) == "https://example.com/cover.png"
    assert seen_lengths and all(n <= 65_536 for n in seen_lengths)

    seen_lengths.clear()
    html_late_only = (
        f"<html><head><title>No Image</title></head><body>{padding}"
        '<meta property="og:image" content="https://example.com/late.png">'
        "</body></html>"
    )
    assert extractor.extract_image_url(html_late_only) is None
    assert seen_lengths and all(n <= 65_536 for n in seen_lengths)


def test_detect_redirect_url_bounds_scanning(monkeypatch):
    """detect_redirect_url only passes a <=65_536 char prefix to BeautifulSoup."""
    from keepfor.consumer import extractor

    seen_lengths: list[int] = []
    real_load = extractor._load_parsers

    def spy_load_parsers():
        soup_cls, tf = real_load()

        def wrapped_soup(markup, *args, **kwargs):
            seen_lengths.append(len(markup))
            return soup_cls(markup, *args, **kwargs)

        return wrapped_soup, tf

    monkeypatch.setattr(extractor, "_load_parsers", spy_load_parsers)

    padding = "<p>body filler text</p>" * 80_000  # > 1.7 MB
    html = (
        "<html><head>"
        '<meta http-equiv="refresh" content="0;url=https://example.com/target">'
        f"</head><body>{padding}</body></html>"
    )
    assert len(html) > 1_500_000
    assert (
        extractor.detect_redirect_url(html, "https://example.com/start")
        == "https://example.com/target"
    )
    assert seen_lengths and all(n <= 65_536 for n in seen_lengths)


def test_sanitize_clean_html_large_document_streaming(monkeypatch):
    """sanitize_clean_html uses streaming HTMLParser above 512 KB without BS4."""
    from keepfor.consumer import extractor

    def fail_load_parsers():
        raise AssertionError("BeautifulSoup/trafilatura must not run on large HTML")

    monkeypatch.setattr(extractor, "_load_parsers", fail_load_parsers)

    large_html = _synthetic_rfc_html(1_100_000)
    assert len(large_html) > 1_000_000
    clean = sanitize_clean_html(large_html)
    assert "<script>" not in clean
    assert "body-xss" not in clean
    assert "<nav>" not in clean
    assert "Table of Contents" not in clean
    assert "onclick" not in clean
    assert "onerror" not in clean
    assert "javascript:" not in clean
    assert "<pre><pre>" not in clean
    assert "<pre><code>" not in clean
    assert "<pre>GET /index.html HTTP/1.1" in clean
    assert 'href="https://example.com/spec"' in clean
    assert 'src="https://example.com/diagram.png"' in clean


def test_extract_article_large_rfc_html_streaming_and_fast(monkeypatch):
    """extract_article handles >=1.5 MB HTML via streaming HTMLParser within budget."""
    import time

    from keepfor.consumer import extractor

    real_load = extractor._load_parsers

    def guarded_load_parsers():
        soup_cls, _ = real_load()

        def bounded_soup(markup, *args, **kwargs):
            assert len(markup) <= 65_536, (
                f"BeautifulSoup called on {len(markup)} chars (>65_536)"
            )
            return soup_cls(markup, *args, **kwargs)

        class NoTrafilatura:
            def __getattr__(self, name):
                raise AssertionError(f"trafilatura.{name} must not run on large HTML")

        return bounded_soup, NoTrafilatura()

    monkeypatch.setattr(extractor, "_load_parsers", guarded_load_parsers)

    large_rfc = _synthetic_rfc_html(1_600_000)
    assert len(large_rfc) >= 1_500_000

    t0 = time.monotonic()
    out = extract_article(large_rfc, "https://www.rfc-editor.org/rfc/rfc9110.html")
    elapsed = time.monotonic() - t0

    assert elapsed < 3.0, f"Large HTML extraction took {elapsed:.2f}s"
    assert out["is_fallback"] == 0
    assert out["title"] == "RFC 9110: HTTP Semantics"
    assert out["excerpt"] == "HTTP Semantics specification."
    assert out["image_url"] == "https://example.com/rfc-cover.png"
    assert out["word_count"] > 10_000
    assert "stateless application-level protocol" in out["content_text"]
    assert "<script>" not in out["clean_html"]
    assert "body-xss" not in out["clean_html"]
    assert "Table of Contents" not in out["clean_html"]
    assert "<pre><pre>" not in out["clean_html"]
    assert "<h1" not in out["clean_html"]
    assert out["blocked_reason"] is None


def test_sanitize_clean_html_large_pre_code_no_stray_end_tag():
    """Closing </code> inside <pre> must not emit a stray </code> end tag."""
    padding = "<p>padding</p>" * 40_000  # > 512 KB to trigger streaming path
    html = (
        f"<div>{padding}<pre><code>foo</code></pre>"
        "<p>inline <code>bar</code> text</p></div>"
    )
    clean = sanitize_clean_html(html)
    assert "<pre>foo</pre>" in clean
    assert "foo</code>" not in clean
    assert "<p>inline <code>bar</code> text</p>" in clean


def test_load_parsers_and_extract_article_without_trafilatura(monkeypatch):
    """_load_parsers() and extract_article() fall back without trafilatura."""
    import sys

    from keepfor.consumer import extractor

    monkeypatch.setitem(sys.modules, "trafilatura", None)
    monkeypatch.setattr(extractor, "_parsers_loaded", False)
    monkeypatch.setattr(extractor, "_soup_cls", None)
    monkeypatch.setattr(extractor, "_trafilatura", None)

    soup_cls, traf = extractor._load_parsers()
    assert soup_cls is not None
    assert traf is None

    html = (
        "<html><head>"
        "<title>Fallback Title</title>"
        '<meta property="og:description" content="Short fallback description.">'
        "</head><body><p>Brief body.</p></body></html>"
    )
    out = extract_article(html, "https://example.com/fallback")
    assert out["is_fallback"] == 1
    assert out["title"] == "Fallback Title"
    assert "Short fallback description." in out["content_text"]
    assert "Short fallback description." in out["clean_html"]
    assert "Visit original link" in out["clean_html"]
