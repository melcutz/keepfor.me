import html as html_module
import re
from typing import Any
from urllib.parse import urljoin, urlparse

# NOTE: trafilatura and bs4 are imported lazily (see _load_parsers) rather
# than at module scope. This module is reachable from the Worker entrypoint
# via the consumer chain, so eager imports made *every* request -- including
# static pages like /auth/login -- pay to load the article-parsing stack
# (~0.1-0.2s, measured) and grew the cold-start import graph.
_soup_cls: Any = None
_trafilatura: Any = None
_parsers_loaded = False


def _load_parsers() -> tuple[Any, Any]:
    """Import and cache BeautifulSoup + trafilatura on first extraction."""
    global _soup_cls, _trafilatura, _parsers_loaded
    if not _parsers_loaded:
        import warnings

        from bs4 import BeautifulSoup

        # Cloudflare Workers have no system timezone database, so tzlocal (via
        # trafilatura's date parsing) warns once per article and defaults to
        # UTC -- which is what we want. Silence it so error logs stay
        # meaningful.
        warnings.filterwarnings(
            "ignore",
            message="Can not find any timezone configuration.*",
            category=UserWarning,
        )

        _soup_cls = BeautifulSoup
        try:
            import trafilatura as _tf

            _trafilatura = _tf
        except ImportError:
            _trafilatura = None
        _parsers_loaded = True
    return _soup_cls, _trafilatura


def sanitize_clean_html(html_str: str) -> str:
    """Ensures extracted HTML contains only safe tags and attributes."""
    if not html_str:
        return ""
    soup_cls, _ = _load_parsers()
    soup = soup_cls(html_str, "html.parser")
    allowed_tags = {
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "ul",
        "ol",
        "li",
        "pre",
        "code",
        "em",
        "strong",
        "b",
        "i",
        "a",
        "img",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "hr",
        "br",
    }
    # Tags whose *text* is not prose. Unwrapping these keeps their contents
    # as visible text: a <script> unwrap leaks "alert('xss')" and a <style>
    # unwrap leaks CSS into the article body (both observed in prod). Drop the
    # whole subtree instead.
    dropped_tags = {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "math",
        "canvas",
        "iframe",
        "object",
        "embed",
        "video",
        "audio",
        "form",
        "button",
        "select",
        "option",
        "nav",
        "aside",
        "footer",
        "figcaption",
    }
    for tag in soup.find_all(True):
        # A parent may have been decomposed above, detaching this child.
        if getattr(tag, "decomposed", False) or tag.parent is None:
            continue
        if tag.name in dropped_tags:
            tag.decompose()
        elif tag.name not in allowed_tags:
            tag.unwrap()
        else:
            # Filter attributes
            allowed_attrs = {}
            if tag.name == "a" and tag.has_attr("href"):
                href = tag["href"]
                if href.startswith(("http://", "https://", "mailto:", "#")):
                    allowed_attrs["href"] = href
                    allowed_attrs["target"] = "_blank"
                    allowed_attrs["rel"] = "noopener noreferrer"
            elif tag.name == "img" and tag.has_attr("src"):
                src = tag["src"]
                if src.startswith(("http://", "https://", "data:image/")):
                    allowed_attrs["src"] = src
                    if tag.has_attr("alt"):
                        allowed_attrs["alt"] = tag["alt"]
            tag.attrs = allowed_attrs

    # Flatten redundant nesting: <pre><pre>code</pre></pre> (seen in prod)
    # breaks the monospace block and adds a stray scroll container.
    for tag in soup.find_all("pre"):
        for child in tag.find_all(["pre", "code"]):
            child.unwrap()
    for tag in soup.find_all("code"):
        for child in tag.find_all("code"):
            child.unwrap()

    return _normalize_text_nodes(str(soup))


# Zero-width and soft-hyphen noise: invisible, but they break exact-phrase
# FTS matching and leak into "copy for AI" markdown.
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff\u00ad]")
_SPACE_RUN_RE = re.compile(r"[ \t\u00a0]{2,}")


def _normalize_text_nodes(html_str: str) -> str:
    """Strip invisible chars and collapse runs of whitespace in text nodes."""
    cleaned = _ZERO_WIDTH_RE.sub("", html_str)
    # Normalize inside text nodes only, so attribute values stay untouched.
    parts = re.split(r"(<[^>]+>)", cleaned)
    for i, part in enumerate(parts):
        if part.startswith("<"):
            continue
        part = part.replace("\u00a0", " ")
        part = _SPACE_RUN_RE.sub(" ", part)
        part = re.sub(r" *\n *", "\n", part)
        parts[i] = part
    out = "".join(parts)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def extract_image_url(html: str, metadata: Any = None) -> str | None:
    """Extract cover image URL from trafilatura metadata or OpenGraph tags.

    Lazy-parses with BeautifulSoup (already cached via _load_parsers) so the
    heavy parsing stack is never imported at module scope.
    """
    if metadata is not None:
        img = getattr(metadata, "image", None)
        if img and isinstance(img, str) and img.strip():
            return img.strip()
    try:
        soup_cls, _ = _load_parsers()
        soup = soup_cls(html or "", "html.parser")
        og_img = soup.find("meta", property="og:image") or soup.find(
            "meta", attrs={"name": "twitter:image"}
        )
        if og_img and og_img.get("content"):
            content = str(og_img["content"]).strip()
            return content or None
    except Exception:
        return None
    return None


def _clean_plain_text(text: str) -> str:
    """Normalize extracted plain text: drop invisible chars, collapse runs."""
    cleaned = _ZERO_WIDTH_RE.sub("", text or "")
    cleaned = cleaned.replace("\u00a0", " ")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# Pages that return HTTP 200 but carry no article. Seen across the library:
# SSO redirects ("Redirecting", "SSO_login"), soft 404s, and consent walls.
# They extract to a 1-word stub and get saved as if they were articles, so
# they are detected here and reported to the caller instead.
_BLOCK_TITLE_RE = re.compile(
    r"^(?:redirecting|sign\s?in|log\s?in|log\s?out|login|logout|sso|"
    r"authentication\s+required|access\s+denied|forbidden|page\s+not\s+found|"
    r"404|403|429|not\s+found|error|too\s+many\s+requests|"
    r"we\s+are\s+sorry|sorry|verifying|"
    r"just\s+a\s+moment|attention\s+required|checking\s+your\s+browser|"
    r"one\s+moment|security\s+check|ddos|bot\s+detection)",
    re.IGNORECASE,
)
_BLOCK_BODY_MARKERS = (
    "sign in to continue",
    "sign in to view",
    "log in to continue",
    "you do not have permission",
    "enable javascript to continue",
    "please enable javascript",
    "checking your browser",
    "unusual traffic",
    "request blocked",
    "access denied",
    "page not found",
    "404 not found",
    "we are sorry",
    "this content isn't available",
    "verify you are human",
    "session expired",
    "authentication required",
)


def detect_blocked_page(
    title: str | None, text: str | None, raw_html: str | None = None
) -> str | None:
    """Return a short reason when the fetch yielded a wall, not an article.

    Conservative by design: only fires on short output plus a known marker,
    so genuinely brief pages are kept.
    """
    body = (text or "").strip()
    words = len(body.split())
    if words > 120:
        return None

    t = (title or "").strip()
    if t and _BLOCK_TITLE_RE.match(t):
        return f"blocked page (title: {t[:60]!r})"

    low = body.lower()
    for marker in _BLOCK_BODY_MARKERS:
        if marker in low:
            return f"blocked page (body marker: {marker!r})"

    html = (raw_html or "").lower()
    if words < 20:
        if 'http-equiv="refresh"' in html or "http-equiv='refresh'" in html:
            return "redirect stub (meta refresh)"
        if "window.location" in html and "href=" not in low:
            return "redirect stub (js navigation)"

    # JavaScript app shell: the HTML ships an empty mount point and the text
    # arrives client-side. Prod had 1-word "saved" items from these
    # (ServiceNow portals, SharePoint redirects).
    if words < 5 and (
        re.search(r'id=[\'"]?(app|root|__next)[\'"\s>]', html)
        or "__next_data__" in html
        or "enable javascript" in html
    ):
        return "javascript app shell (no server-rendered text)"
    return None


def detect_redirect_url(html: str, url: str) -> str | None:
    """Find the real destination of a redirect stub (meta refresh / JS / link).

    Lets the caller follow one hop instead of storing the stub. Bounded to
    http(s) targets that differ from the URL we already fetched.
    """
    if not html:
        return None
    soup_cls, _ = _load_parsers()
    soup = soup_cls(html, "html.parser")

    candidates: list[str] = []
    meta = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
    if meta and meta.get("content"):
        m = re.search(r"url\s*=\s*['\"]?([^'\";\s]+)", str(meta["content"]), re.I)
        if m:
            candidates.append(m.group(1))
    for script in soup.find_all("script"):
        body = script.string or script.get_text() or ""
        for m in re.finditer(
            r"(?:window\.location(?:\.href)?\s*=|location\.replace\()\s*['\"]([^'\"]+)",
            body,
        ):
            candidates.append(m.group(1))
    link = soup.find("link", attrs={"rel": "canonical"})
    if link and link.get("href"):
        candidates.append(str(link["href"]))

    for cand in candidates:
        cand = cand.strip()
        if not cand or cand.startswith(("javascript:", "#", "mailto:")):
            continue
        absolute = urljoin(url, cand)
        if absolute.rstrip("/") == url.rstrip("/"):
            continue
        return absolute
    return None


def extract_article(html: str, url: str) -> dict[str, Any]:
    """Extract article content and reader HTML with Trafilatura and OG fallback."""
    title = None
    byline = None
    site_name = None
    published_date = None
    excerpt = None
    clean_html_raw = None
    plain_text = None

    # 1. Attempt Trafilatura extraction if available
    soup_cls, trafilatura = _load_parsers()
    _metadata = None
    if trafilatura is not None:
        metadata = trafilatura.extract_metadata(html, default_url=url)
        _metadata = metadata
        clean_html_raw = trafilatura.extract(
            html,
            url=url,
            output_format="html",
            include_images=True,
            include_links=True,
            favor_recall=True,
        )
        plain_text = trafilatura.extract(
            html, url=url, output_format="txt", include_links=False
        )
        if metadata:
            title = metadata.title
            byline = metadata.author
            site_name = metadata.sitename
            published_date = metadata.date
            excerpt = metadata.description

    is_fallback = False

    # 2. Fallback using BeautifulSoup if body text is missing or very short
    if not plain_text or len(plain_text.strip()) < 50:
        soup = soup_cls(html, "html.parser")
        is_fallback = True

        if not title:
            og_title = soup.find("meta", property="og:title")
            title = (
                og_title["content"].strip()
                if og_title and og_title.get("content")
                else None
            )
        if not title and soup.title:
            title = soup.title.string.strip() if soup.title.string else None
        if not title:
            title = url

        if not excerpt:
            og_desc = soup.find("meta", property="og:description") or soup.find(
                "meta", attrs={"name": "description"}
            )
            excerpt = (
                og_desc["content"].strip()
                if og_desc and og_desc.get("content")
                else None
            )

        if not site_name:
            og_site = soup.find("meta", property="og:site_name")
            site_name = (
                og_site["content"].strip()
                if og_site and og_site.get("content")
                else None
            )

        plain_text = excerpt or title or "No readable text extracted."
        clean_html_raw = (
            f"<h1>{title}</h1><p>{excerpt or ''}</p>"
            f"<p><a href='{url}' target='_blank'>Visit original link</a></p>"
        )

    clean_html = sanitize_clean_html(clean_html_raw or "")
    plain_text = _clean_plain_text(plain_text or "")
    word_count = len(plain_text.split())
    image_url = extract_image_url(html, _metadata)
    blocked_reason = detect_blocked_page(title, plain_text, html)
    redirect_url = detect_redirect_url(html, url)

    return {
        "title": title or url,
        "byline": byline,
        "site_name": site_name,
        "published_date": published_date,
        "excerpt": excerpt,
        "content_text": plain_text,
        "clean_html": clean_html,
        "is_fallback": 1 if is_fallback else 0,
        "word_count": word_count,
        "image_url": image_url,
        "blocked_reason": blocked_reason,
        "redirect_url": redirect_url,
    }


def article_from_reader_markdown(url: str, reader_text: str) -> dict[str, Any]:
    """Build an article dict from reader-proxy markdown (e.g. Jina Reader).

    Used when the origin 403s direct fetches: the proxy already returns
    clean text, so trafilatura is bypassed. Content is genuine article
    text, hence is_fallback=0.
    """
    title = url
    body = (reader_text or "").strip()
    if "Markdown Content:" in body:
        head, _, md = body.partition("Markdown Content:")
        match = re.search(r"^Title:\s*(.+)$", head, re.MULTILINE)
        if match and match.group(1).strip():
            title = match.group(1).strip()
        body = md.strip() or body

    site_name = urlparse(url).netloc or None
    collapsed = re.sub(r"\s+", " ", body)
    excerpt = collapsed[:300] if collapsed else None

    # Minimal markdown -> HTML: escaped paragraphs plus [text](url) links.
    # sanitize_clean_html then strips anything outside the safe allowlist.
    def _to_html(text: str) -> str:
        parts = []
        for para in re.split(r"\n\s*\n", text):
            para = para.strip()
            if not para:
                continue
            para = html_module.escape(para)
            para = re.sub(
                r"\[([^\]]+)\]\((https?://[^)]+)\)",
                r"<a href='\2' target='_blank'>\1</a>",
                para,
            )
            parts.append(f"<p>{para}</p>")
        return "".join(parts)

    clean_html = sanitize_clean_html(_to_html(body))
    plain_text = _clean_plain_text(body)
    word_count = len(plain_text.split())

    return {
        "title": title,
        "byline": None,
        "site_name": site_name,
        "published_date": None,
        "excerpt": excerpt,
        "content_text": plain_text,
        "clean_html": clean_html,
        "is_fallback": 0,
        "word_count": word_count,
        "image_url": None,
        # Reader-proxy text is genuine article content: never a wall.
        "blocked_reason": detect_blocked_page(title, plain_text, None),
        "redirect_url": None,
    }
