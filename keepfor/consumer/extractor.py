# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

import html as html_module
import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from keepfor.models.cleanup import (
    BLOCKED_FETCH_COPY,
    BLOCKED_HTTP_STATUSES,
    DEAD_HTTP_STATUSES,
    classify_failure,
)
from keepfor.utils.dates import normalize_published_date

__all__ = [
    "BLOCKED_FETCH_COPY",
    "BLOCKED_HTTP_STATUSES",
    "DEAD_HTTP_STATUSES",
    "article_from_reader_markdown",
    "classify_failure",
    "detect_blocked_page",
    "detect_html_language",
    "detect_redirect_url",
    "extract_article",
    "extract_github_article",
    "extract_image_url",
    "extract_wikipedia_article",
    "extract_youtube_article",
    "sanitize_clean_html",
]

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


_LARGE_HTML_THRESHOLD = 512_000
_HEAD_SCAN_LIMIT = 65_536
_HEAD_CLOSE_RE = re.compile(r"</head\s*>", re.IGNORECASE)

_ALLOWED_TAGS = frozenset(
    {
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
)
# Tags whose *text* is not prose. Unwrapping these keeps their contents
# as visible text: a <script> unwrap leaks "alert('xss')" and a <style>
# unwrap leaks CSS into the article body (both observed in prod). Drop the
# whole subtree instead.
_DROPPED_TAGS = frozenset(
    {
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
)
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_BLOCK_TEXT_TAGS = frozenset(
    {
        "p",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "tr",
        "blockquote",
        "pre",
        "dd",
        "dt",
        "section",
        "figcaption",
        "caption",
        "br",
        "hr",
    }
)


def _head_slice(html: str, limit: int = _HEAD_SCAN_LIMIT) -> str:
    """Return at most `limit` leading chars, stopping at </head> when present."""
    if not html:
        return ""
    window = html[:limit]
    match = _HEAD_CLOSE_RE.search(window)
    return window[: match.end()] if match else window


class _StreamingHTMLSanitizer(HTMLParser):
    """Single-pass streaming HTML sanitizer and metadata/text extractor."""

    def __init__(
        self, *, skip_head: bool = False, extract_metadata: bool = False
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_head = skip_head
        self.extract_metadata = extract_metadata
        self.in_head = False
        self.in_title = False
        self.title_parts: list[str] = []
        self.dropped_stack: list[str] = []
        self.pre_depth = 0
        self.code_depth = 0
        self.html_chunks: list[str] = []
        self.text_chunks: list[str] = []

        self.og_title: str | None = None
        self.doc_title: str | None = None
        self.excerpt: str | None = None
        self.site_name: str | None = None
        self.byline: str | None = None
        self.published_date: str | None = None
        self.image_url: str | None = None

    @staticmethod
    def _attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {k: v for k, v in attrs if v is not None}

    def _record_meta(self, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = self._attrs_dict(attrs)
        content = (attr_map.get("content") or "").strip()
        if not content:
            return
        prop = (attr_map.get("property") or "").lower()
        name = (attr_map.get("name") or "").lower()
        if prop == "og:title" and not self.og_title:
            self.og_title = content
        elif prop == "og:description" and not self.excerpt:
            self.excerpt = content
        elif name == "description" and not self.excerpt:
            self.excerpt = content
        elif prop == "og:site_name" and not self.site_name:
            self.site_name = content
        elif name == "author" or prop == "article:author":
            if not self.byline:
                self.byline = content
        elif prop == "article:published_time" or name == "date":
            if not self.published_date:
                self.published_date = normalize_published_date(content)
        elif prop == "og:image" or name == "twitter:image":
            if not self.image_url:
                self.image_url = content

    def _format_a_start(self, attrs: list[tuple[str, str | None]]) -> str:
        attr_map = self._attrs_dict(attrs)
        href = attr_map.get("href", "")
        if href.startswith(("http://", "https://", "mailto:", "#")):
            safe_href = html_module.escape(href, quote=True)
            return f'<a href="{safe_href}" target="_blank" rel="noopener noreferrer">'
        return "<a>"

    def _format_img(self, attrs: list[tuple[str, str | None]]) -> str:
        attr_map = self._attrs_dict(attrs)
        src = attr_map.get("src", "")
        if src.startswith(("http://", "https://", "data:image/")):
            safe_src = html_module.escape(src, quote=True)
            alt = attr_map.get("alt")
            if alt is not None:
                safe_alt = html_module.escape(alt, quote=True)
                return f'<img src="{safe_src}" alt="{safe_alt}">'
            return f'<img src="{safe_src}">'
        return "<img>"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.extract_metadata:
            if tag == "title" and not self.doc_title:
                self.in_title = True
            elif tag == "meta":
                self._record_meta(attrs)

        if self.skip_head:
            if tag == "head":
                self.in_head = True
                return
            if tag == "body":
                self.in_head = False
            if self.in_head:
                return

        if self.dropped_stack:
            if tag in _DROPPED_TAGS and tag not in _VOID_TAGS:
                self.dropped_stack.append(tag)
            return

        if tag in _DROPPED_TAGS:
            if tag not in _VOID_TAGS:
                self.dropped_stack.append(tag)
            return

        if self.extract_metadata:
            if tag in _BLOCK_TEXT_TAGS:
                self.text_chunks.append("\n")
            elif tag in ("td", "th"):
                self.text_chunks.append(" ")

        if tag not in _ALLOWED_TAGS:
            return

        if tag == "pre":
            if self.pre_depth == 0:
                self.html_chunks.append("<pre>")
            self.pre_depth += 1
            return

        if tag == "code":
            if self.pre_depth == 0 and self.code_depth == 0:
                self.html_chunks.append("<code>")
            self.code_depth += 1
            return

        if tag == "a":
            self.html_chunks.append(self._format_a_start(attrs))
        elif tag == "img":
            self.html_chunks.append(self._format_img(attrs))
        else:
            self.html_chunks.append(f"<{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.extract_metadata and tag == "title" and self.in_title:
            self.in_title = False
            if not self.doc_title:
                self.doc_title = "".join(self.title_parts).strip() or None

        if self.skip_head:
            if tag == "head":
                self.in_head = False
                return
            if self.in_head:
                return

        if tag in _VOID_TAGS:
            return

        if self.dropped_stack:
            if tag in self.dropped_stack:
                while self.dropped_stack:
                    popped = self.dropped_stack.pop()
                    if popped == tag:
                        break
            return

        if self.extract_metadata:
            if tag in _BLOCK_TEXT_TAGS:
                self.text_chunks.append("\n")
            elif tag in ("td", "th"):
                self.text_chunks.append(" ")

        if tag not in _ALLOWED_TAGS:
            return

        if tag == "pre":
            if self.pre_depth > 0:
                self.pre_depth -= 1
                if self.pre_depth == 0:
                    self.html_chunks.append("</pre>")
            return

        if tag == "code":
            if self.code_depth > 0:
                self.code_depth -= 1
                if self.code_depth == 0:
                    self.html_chunks.append("</code>")
            return

        self.html_chunks.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.skip_head and self.in_head:
            return
        if self.dropped_stack:
            return
        self.html_chunks.append(html_module.escape(data, quote=False))
        if self.extract_metadata:
            self.text_chunks.append(data)


def sanitize_clean_html(html_str: str) -> str:
    """Ensures extracted HTML contains only safe tags and attributes."""
    if not html_str:
        return ""
    if len(html_str) > _LARGE_HTML_THRESHOLD:
        parser = _StreamingHTMLSanitizer(skip_head=False, extract_metadata=False)
        parser.feed(html_str)
        parser.close()
        return _normalize_text_nodes("".join(parser.html_chunks))

    soup_cls, _ = _load_parsers()
    soup = soup_cls(html_str, "html.parser")
    for tag in soup.find_all(True):
        # A parent may have been decomposed above, detaching this child.
        if getattr(tag, "decomposed", False) or tag.parent is None:
            continue
        if tag.name in _DROPPED_TAGS:
            tag.decompose()
        elif tag.name not in _ALLOWED_TAGS:
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


# The reader renders the item title as the page heading. An article that
# repeats the headline in its own leading <h1> would show it twice
# (issue #57), so the leading H1 is dropped when it matches the title.
_TITLE_QUOTES_RE = re.compile(r"[\u00ab\u00bb\u2018\u2019\u201c\u201d\"']")
_TITLE_SPACE_RE = re.compile(r"\s+")


def _fold_title(text: str | None) -> str:
    """Fold a title or heading for comparison: entities, case, quotes, space."""
    folded = html_module.unescape(text or "").lower()
    folded = _TITLE_QUOTES_RE.sub("", folded)
    return _TITLE_SPACE_RE.sub(" ", folded).strip()


_LEADING_H1_OPEN_RE = re.compile(r"^\s*<h1(?:\s|>)", re.IGNORECASE)
_LEADING_H1_BLOCK_RE = re.compile(
    r"^\s*<h1(?:\s[^>]*)?>(.*?)</h1>\s*", re.IGNORECASE | re.DOTALL
)


def _drop_duplicate_leading_h1(html_str: str, title: str | None) -> str:
    """Remove a leading <h1> that repeats the article title.

    A later <h1> is a section heading, not a duplicate: it stays.
    """
    if not html_str or not title:
        return html_str or ""
    prefix = html_str[:256].lstrip()
    if not prefix.startswith("<!--"):
        if not _LEADING_H1_OPEN_RE.match(prefix):
            return html_str
        match = _LEADING_H1_BLOCK_RE.match(html_str[:8192])
        if match and "<h1" not in match.group(1).lower():
            h1_text = _HTML_TAG_RE.sub("", match.group(1))
            if _fold_title(h1_text) == _fold_title(title):
                return html_str[match.end() :].strip()
            return html_str
    if len(html_str) > _LARGE_HTML_THRESHOLD:
        return html_str

    soup_cls, _ = _load_parsers()
    soup = soup_cls(html_str, "html.parser")
    first = next(
        (n for n in soup.children if getattr(n, "name", None) or str(n).strip()),
        None,
    )
    if first is None or getattr(first, "name", None) != "h1":
        return html_str
    if _fold_title(first.get_text(" ", strip=True)) != _fold_title(title):
        return html_str
    first.decompose()
    return str(soup).strip()


def extract_image_url(html: str, metadata: Any = None) -> str | None:
    """Extract cover image URL from trafilatura metadata or OpenGraph tags.

    Lazy-parses with BeautifulSoup (already cached via _load_parsers) so the
    heavy parsing stack is never imported at module scope.
    """
    if metadata is not None:
        img = getattr(metadata, "image", None)
        if img and isinstance(img, str) and img.strip():
            return img.strip()
    head_html = _head_slice(html)
    if not head_html:
        return None
    try:
        soup_cls, _ = _load_parsers()
        soup = soup_cls(head_html, "html.parser")
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


# The page's own language, when it declares one (<html lang="de">). Cheap
# regex over the head of the document: the tagger needs it to cut German
# pages at German stopwords (issue #58), and a full parse is not worth it.
_HTML_LANG_RE = re.compile(
    r"<html[^>]*\blang\s*=\s*[\"']?([a-zA-Z]{2,3})", re.IGNORECASE
)


def detect_html_language(html: str) -> str | None:
    """The page language code ("de") from <html lang="...">, else None."""
    match = _HTML_LANG_RE.search(html or "", 0, 20000)
    if not match:
        return None
    return match.group(1).lower()


# Pages that return HTTP 200 but carry no article. Seen across the library:
# SSO redirects ("Redirecting", "SSO_login"), soft 404s, and consent walls.
# They extract to a 1-word stub and get saved as if they were articles, so
# they are detected here and reported to the caller instead.
_BLOCK_TITLE_RE = re.compile(
    r"^(?:redirecting|sign\s?in|log\s?in|log\s?out|login|logout|sso|"
    r"authentication\s+required|unauthorized|access\s+denied|forbidden|"
    r"page\s+not\s+found|401|404|403|410|429|not\s+found|error|"
    r"too\s+many\s+requests|"
    r"we\s+are\s+sorry|sorry|verifying|"
    r"just\s+a\s+moment|attention\s+required|checking\s+your\s+browser|"
    r"one\s+moment|security\s+check|ddos|bot\s+detection|"
    r"content\s+not\s+available|this\s+page\s+doesn'?t\s+exist|gone|removed|"
    r"deleted|not\s+available)",
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
    "410 gone",
    "we are sorry",
    "this content isn't available",
    "verify you are human",
    "session expired",
    "authentication required",
    "this page doesn't exist",
    "content not available",
    "page has been moved",
    "page has been removed",
    "this content is no longer available",
    "the page you are looking for",
    "error 404",
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
    soup = soup_cls(html[:_HEAD_SCAN_LIMIT], "html.parser")

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


def _is_youtube_url(url: str) -> bool:
    """Check if URL is a YouTube video page."""
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    return (
        host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtu.be"
        or host.endswith(".youtu.be")
    )


def _is_github_url(url: str) -> bool:
    """Check if URL is a GitHub page (not github.io)."""
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    return host == "github.com" or host.endswith(".github.com")


def _is_wikipedia_url(url: str) -> bool:
    """Check if URL is a Wikipedia article page."""
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    return host == "wikipedia.org" or host.endswith(".wikipedia.org")


# YouTube embeds the full video description in `ytInitialPlayerResponse`.
# og:description is truncated to ~160 chars; this is the whole thing.
_YT_SHORT_DESC_RE = re.compile(r'"shortDescription":"((?:[^"\\]|\\.)*)"')


def _youtube_short_description(html: str) -> str | None:
    """Pull the full description from YouTube's embedded player response."""
    match = _YT_SHORT_DESC_RE.search(html or "")
    if not match:
        return None
    try:
        text = json.loads(f'"{match.group(1)}"')
    except (ValueError, TypeError):
        return None
    return text.strip() or None


def extract_youtube_article(html: str, url: str) -> dict[str, Any]:
    """Extract YouTube video metadata from initial HTML (JS-rendered pages)."""
    soup_cls, _ = _load_parsers()
    soup = soup_cls(html or "", "html.parser")

    title = None
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
        if title.endswith(" - YouTube"):
            title = title[: -len(" - YouTube")].strip()
    if not title:
        title = url

    description = _youtube_short_description(html)
    if not description:
        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content"):
            description = og_desc["content"].strip()
    if not description:
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            description = meta_desc["content"].strip()

    byline = None
    og_author = soup.find("meta", attrs={"name": "author"})
    if og_author and og_author.get("content"):
        byline = og_author["content"].strip()
    if not byline:
        for link in soup.find_all("link", attrs={"itemprop": "name"}):
            if link.get("content"):
                byline = link["content"].strip()
                break

    published_date = None
    og_date = soup.find("meta", attrs={"itemprop": "datePublished"})
    if og_date and og_date.get("content"):
        published_date = normalize_published_date(og_date["content"])

    image_url = None
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        image_url = og_img["content"].strip()

    site_name = "YouTube"

    parts = [p for p in [title, description] if p]
    plain_text = "\n\n".join(parts) if parts else title
    safe_title = html_module.escape(title or "", quote=True)
    safe_desc = html_module.escape(description, quote=True) if description else ""
    clean_html = sanitize_clean_html(
        f"<h1>{safe_title}</h1>" + (f"<p>{safe_desc}</p>" if safe_desc else "")
    )
    plain_text = _clean_plain_text(plain_text)
    word_count = len(plain_text.split())

    return {
        "title": title,
        "byline": byline,
        "site_name": site_name,
        "published_date": published_date,
        "excerpt": description[:300] if description else None,
        "content_text": plain_text,
        "clean_html": clean_html,
        "is_fallback": 0,
        "word_count": word_count,
        "image_url": image_url,
        "language": detect_html_language(html),
        "blocked_reason": detect_blocked_page(title, plain_text, None),
        "redirect_url": None,
    }


def extract_github_article(html: str, url: str) -> dict[str, Any]:
    """Extract GitHub README content, skipping session banners and nav chrome."""
    soup_cls, _ = _load_parsers()
    soup = soup_cls(html or "", "html.parser")

    for banner in soup.find_all("div", class_="js-session-flash"):
        banner.decompose()
    for banner in soup.find_all("div", class_="flash-banner"):
        banner.decompose()

    readme = (
        soup.find("article")
        or soup.find("div", id="readme")
        or soup.find("div", class_="markdown-body")
    )

    title = None
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
        for suffix in (" · GitHub", " · GitHub Pages"):
            if title.endswith(suffix):
                title = title[: -len(suffix)].strip()
                break
    if not title:
        title = url

    description = None
    og_desc = soup.find("meta", property="og:description")
    if og_desc and og_desc.get("content"):
        description = og_desc["content"].strip()

    plain_text = ""
    if readme:
        for p in readme.find_all("p"):
            text = p.get_text(strip=True)
            if text:
                plain_text += text + "\n\n"
        plain_text = plain_text.strip()

    if not plain_text or len(plain_text) < 50:
        plain_text = description or title

    safe_title = html_module.escape(title or "", quote=True)
    safe_desc = html_module.escape(description, quote=True) if description else ""
    clean_html = sanitize_clean_html(
        f"<h1>{safe_title}</h1>" + (f"<p>{safe_desc}</p>" if safe_desc else "")
    )
    plain_text = _clean_plain_text(plain_text)
    word_count = len(plain_text.split())

    return {
        "title": title,
        "byline": None,
        "site_name": "GitHub",
        "published_date": None,
        "excerpt": description[:300] if description else None,
        "content_text": plain_text,
        "clean_html": clean_html,
        "is_fallback": 0 if len(plain_text) >= 50 else 1,
        "word_count": word_count,
        "image_url": None,
        "language": detect_html_language(html),
        "blocked_reason": detect_blocked_page(title, plain_text, None),
        "redirect_url": None,
    }


# Wikipedia is a fixed template: the article body is always inside
# #mw-content-text, every heading carries an "[edit]" fragment, the title is
# repeated in the body and the schema.org block carries the real metadata
# (author = "Contributors to Wikimedia projects", dateModified = last edit).
_WIKI_CHROME_SELECTORS = (
    ".mw-editsection",  # the "[edit]" fragment next to every heading
    ".mw-editsection-like",  # same fragment inside transcluded excerpts
    ".mw-empty-elt",  # spacing-only spans
    ".navbox",  # "Navigation menu" boxes
    ".vertical-navbox",
    ".sistersitebox",
    ".metadata",  # authority control box
    ".catlinks",
    ".printfooter",
)
_WIKI_TITLE_SUFFIX_RE = re.compile(r"\s*[-–—]\s*wikipedia\s*$", re.IGNORECASE)
_WIKI_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_HTML_BLOCK_END_RE = re.compile(
    r"(?i)</(?:p|div|h[1-6]|li|tr|blockquote|pre|dd|dt|section|figcaption|caption)>"
    r"|<br\s*/?>"
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _tag_text(node: Any) -> str | None:
    """Text content of a node, or None when there is nothing to read."""
    if node is None:
        return None
    text = node.get_text(" ", strip=True) if hasattr(node, "get_text") else str(node)
    return text.strip() or None


def _as_text(value: Any) -> str | None:
    """String value of a metadata field, or None."""
    return value.strip() or None if isinstance(value, str) else None


def _meta_content(soup: Any, prop: str) -> str | None:
    """Content of an OpenGraph/name meta tag, or None."""
    tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
    if tag and tag.get("content"):
        return str(tag["content"]).strip() or None
    return None


def _wikipedia_jsonld(soup: Any) -> dict[str, Any]:
    """Return the page's schema.org metadata (Wikipedia ships an Article)."""
    best: dict[str, Any] = {}
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(data, list):
            data = next((d for d in data if isinstance(d, dict)), None)
        if not isinstance(data, dict):
            continue
        if data.get("dateModified") or "Article" in str(data.get("@type", "")):
            return data
        best = best or data
    return best


def _wikipedia_last_modified(soup: Any) -> str | None:
    """Date of the footer's "This page was last edited on 13 August 2026"."""
    node = soup.find(id="footer-info-lastmod")
    if not node:
        return None
    text = node.get_text(" ", strip=True)
    match = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    if match:
        month = _WIKI_MONTHS.get(match.group(2).lower())
        if month:
            return f"{match.group(3)}-{month:02d}-{int(match.group(1)):02d}"
    return normalize_published_date(text)


def _html_to_text(html_str: str) -> str:
    """Flatten reader HTML to plain text, keeping block boundaries as lines."""
    text = re.sub(r"(?i)</t[dh]>", " ", html_str)
    text = _HTML_BLOCK_END_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub("", text)
    return _clean_plain_text(html_module.unescape(text))


def extract_wikipedia_article(html: str, url: str) -> dict[str, Any]:
    """Extract a Wikipedia article: one title, no [edit] links, one date."""
    soup_cls, _ = _load_parsers()
    soup = soup_cls(html or "", "html.parser")

    ld = _wikipedia_jsonld(soup)

    title = None
    for candidate in (
        _as_text(ld.get("name")),
        _tag_text(soup.select_one(".mw-page-title-main")),
        _tag_text(soup.find(id="firstHeading")),
        _meta_content(soup, "og:title"),
        _tag_text(soup.title),
    ):
        cleaned = candidate and _WIKI_TITLE_SUFFIX_RE.sub("", candidate).strip()
        if cleaned:
            title = cleaned
            break
    if not title:
        title = url

    # The byline Wikipedia exposes is "Contributors to Wikimedia projects":
    # chrome, not an author. The site name carries the source instead.
    published_date = normalize_published_date(ld.get("dateModified")) or (
        _wikipedia_last_modified(soup)
    )

    root = (
        soup.select_one("#mw-content-text .mw-parser-output")
        or soup.select_one(".mw-parser-output")
        or soup.find(id="mw-content-text")
        or soup.find("main")
        or soup.body
        or soup
    )

    # The short description is invisible metadata (display:none): it is the
    # card excerpt, not a line of the article.
    excerpt = _tag_text(root.select_one(".shortdescription")) or _as_text(
        ld.get("headline")
    )

    for selector in _WIKI_CHROME_SELECTORS:
        for node in root.select(selector):
            node.decompose()
    # Wikipedia ships parser debug reports as HTML comments: not content.
    from bs4 import Comment  # bs4 is loaded by _load_parsers() above

    for node in root.find_all(string=lambda s: isinstance(s, Comment)):
        node.extract()
    # Hidden elements (short description, citation metadata): the sanitizer
    # drops the style attribute, so they would surface as junk.
    for node in root.find_all(style=re.compile(r"display\s*:\s*none", re.IGNORECASE)):
        node.decompose()
    for node in root.find_all(
        ["nav", "header", "footer", "aside", "noscript", "script", "style"]
    ):
        node.decompose()
    # The reader renders the title itself: an H1 in the body duplicates it.
    for node in root.find_all("h1"):
        node.decompose()
    # Any edit link left over is Wikipedia chrome, never article content.
    for node in root.find_all("a", href=True):
        if "action=edit" in node["href"]:
            node.decompose()

    # Wikipedia links are site-relative; the reader needs absolute ones.
    for node in root.find_all(["a", "img"]):
        attr = "href" if node.name == "a" else "src"
        value = node.get(attr)
        if not value or value.startswith(("#", "data:")):
            continue
        node[attr] = urljoin(url, value)

    clean_html = sanitize_clean_html(str(root))
    plain_text = _html_to_text(clean_html)
    word_count = len(plain_text.split())

    image = ld.get("image")
    image_url = _meta_content(soup, "og:image") or (
        image if isinstance(image, str) else None
    )

    return {
        "title": title,
        "byline": None,
        "site_name": "Wikipedia",
        "published_date": published_date,
        "excerpt": excerpt,
        "content_text": plain_text,
        "clean_html": clean_html,
        "is_fallback": 0 if word_count >= 50 else 1,
        "word_count": word_count,
        "image_url": image_url,
        "language": detect_html_language(html),
        "blocked_reason": detect_blocked_page(title, plain_text, None),
        "redirect_url": detect_redirect_url(html, url),
    }


def _extract_with_readability(html: str, url: str) -> str | None:
    """Simple readability-like extraction using BeautifulSoup.

    Scores content containers and extracts paragraph text from the best one.
    Returns None if no substantial content found.
    """
    soup_cls, _ = _load_parsers()
    soup = soup_cls(html or "", "html.parser")

    for tag in soup.find_all(
        ["script", "style", "nav", "aside", "footer", "header", "form", "button"]
    ):
        tag.decompose()

    candidates: list[tuple[int, Any]] = []

    for article in soup.find_all("article"):
        candidates.append((100, article))
    for main in soup.find_all("main"):
        candidates.append((80, main))
    for role_main in soup.find_all(attrs={"role": "main"}):
        candidates.append((80, role_main))
    for cls in ["content", "post", "entry", "article-body", "post-content"]:
        for div in soup.find_all("div", class_=cls):
            candidates.append((60, div))
    for div_id in ["content", "main-content", "post-content"]:
        for div in soup.find_all("div", id=div_id):
            candidates.append((60, div))

    if not candidates:
        for div in soup.find_all("div"):
            p_count = len(div.find_all("p"))
            if p_count >= 2:
                candidates.append((p_count * 10, div))

    if not candidates:
        # Last resort: paragraphs that sit directly under <body> with no
        # wrapping container (common on simple blog/CMS pages).
        body = soup.body or soup
        if len(body.find_all("p")) >= 2:
            candidates.append((10, body))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0][1]

    paragraphs = []
    for p in best.find_all("p"):
        text = p.get_text(strip=True)
        if text and len(text) > 1:
            paragraphs.append(text)

    if not paragraphs:
        return None

    result = "\n\n".join(paragraphs)
    return result if len(result.strip()) >= 50 else None


_NAV_WORDS = frozenset(
    """
    about advertise all rights reserved cart contact copyright developers follow us
    home log in login logout menu newsletter policy privacy register search
    sign in sign up subscribe terms
    """.split()
)
# Nav/UI chrome is always short. Validated against 454 prod items: without
# this cap, a repeated-trigram heuristic flagged 82/252 long articles as
# junk (e.g. a 15k-word Wikiversity page). Chrome never exceeds this size.
_NAV_JUNK_MAX_WORDS = 200
# Readability fallback word count above which the result is treated as a
# genuine article (clears is_fallback) rather than a link-only stub.
_READABILITY_REAL_ARTICLE_WORDS = 200


def _is_nav_junk(text: str) -> bool:
    """Detect if extracted text is mostly navigation/UI chrome.

    Only fires on short text (>30% nav words, <=200 words). Long content is
    never chrome, so it is never reclassified.
    """
    words = (text or "").lower().split()
    n = len(words)
    if n < 10 or n > _NAV_JUNK_MAX_WORDS:
        return False

    nav_count = sum(1 for w in words if w in _NAV_WORDS)
    return nav_count / n > 0.30


_BINARY_MEDIA_PREFIXES = (
    "image/",
    "audio/",
    "video/",
    "font/",
    "application/vnd.openxmlformats-officedocument.",
)
_BINARY_MEDIA_TYPES = frozenset(
    {
        "application/pdf",
        "application/octet-stream",
        "application/zip",
        "application/gzip",
        "application/x-gzip",
        "application/x-tar",
        "application/x-bzip2",
        "application/x-xz",
        "application/x-7z-compressed",
        "application/x-rar-compressed",
        "application/wasm",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
    }
)
_PLAIN_TEXT_MEDIA_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/x-markdown",
        "text/x-rst",
        "text/restructuredtext",
        "text/asciidoc",
        "text/csv",
        "text/tab-separated-values",
    }
)
_HTML_STRUCTURE_RE = re.compile(
    r"<!doctype\s+html\b|<(?:"
    r"html|head|body|article|main|section|div|p|h[1-6]|"
    r"ul|ol|li|table|meta|title|script|style|form|nav|header|footer|"
    r"blockquote|pre"
    r")(?:\s|>|/>)",
    re.IGNORECASE,
)
_ATX_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)(?:\s+#+)?$")
_RST_UNDERLINE_RE = re.compile(r'^([=\-~^*#"])\1{2,}\s*$')


def _parse_media_type(content_type: str | None) -> str:
    """Return lowercased media type without parameters, or empty string."""
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().lower()


def _detect_binary_reason(text: str, media_type: str) -> str | None:
    """Return a rejection reason if Content-Type or bytes indicate binary data."""
    if media_type and (
        media_type in _BINARY_MEDIA_TYPES
        or media_type.startswith(_BINARY_MEDIA_PREFIXES)
    ):
        return f"binary content ({media_type})"
    sample = (text or "")[:8192]
    if "\x00" in sample:
        return "binary payload (nul bytes)"
    if sample:
        bad = sum(
            1
            for ch in sample
            if ch == "\ufffd" or (not ch.isprintable() and ch not in "\n\r\t")
        )
        if bad / len(sample) > 0.10:
            return "binary payload (non-printable bytes)"
    return None


def _binary_rejected_article(url: str, reason: str) -> dict[str, Any]:
    """Build a rejected extraction result for binary payloads."""
    return {
        "title": url,
        "byline": None,
        "site_name": urlparse(url).netloc or None,
        "published_date": None,
        "excerpt": None,
        "content_text": "",
        "clean_html": "",
        "is_fallback": 1,
        "word_count": 0,
        "image_url": None,
        "language": None,
        "blocked_reason": reason,
        "redirect_url": None,
    }


def _derive_plain_text_title(text: str, url: str) -> str:
    """Derive a title from Markdown/RST headings, first line, or URL filename."""
    lines = (text or "").splitlines()
    first_line: str | None = None
    checked = 0
    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        checked += 1
        if checked > 20:
            break
        atx = _ATX_HEADING_RE.match(line)
        if atx:
            return atx.group(1).strip()
        if not _RST_UNDERLINE_RE.match(line):
            if idx + 1 < len(lines) and _RST_UNDERLINE_RE.match(lines[idx + 1].strip()):
                return line
            if first_line is None:
                first_line = line

    if first_line and len(first_line) <= 140:
        return first_line
    filename = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    if filename:
        return filename
    if first_line:
        return first_line[:140]
    return url


def _extract_plain_text_article(text: str, url: str) -> dict[str, Any]:
    """Extract a plain-text, Markdown, or RST document as readable prose."""
    title = _derive_plain_text_title(text, url)
    plain_text = _clean_plain_text(text)
    paragraphs = [
        f"<p>{html_module.escape(para.strip(), quote=True)}</p>"
        for para in re.split(r"\r?\n\s*\r?\n", text or "")
        if para.strip()
    ]
    clean_html = _normalize_text_nodes("".join(paragraphs))
    word_count = len(plain_text.split())
    collapsed = re.sub(r"\s+", " ", plain_text)
    excerpt = collapsed[:300] if collapsed else None

    return {
        "title": title,
        "byline": None,
        "site_name": urlparse(url).netloc or None,
        "published_date": None,
        "excerpt": excerpt,
        "content_text": plain_text,
        "clean_html": clean_html,
        "is_fallback": 0,
        "word_count": word_count,
        "image_url": None,
        "language": None,
        "blocked_reason": detect_blocked_page(title, plain_text, None),
        "redirect_url": None,
    }


def _extract_large_html(html: str, url: str) -> dict[str, Any]:
    """Single-pass streaming extraction for large HTML documents (>512 KB)."""
    parser = _StreamingHTMLSanitizer(skip_head=True, extract_metadata=True)
    parser.feed(html)
    parser.close()

    title = parser.og_title or parser.doc_title or url
    clean_html = _normalize_text_nodes("".join(parser.html_chunks))
    clean_html = _drop_duplicate_leading_h1(clean_html, title)
    plain_text = _clean_plain_text("".join(parser.text_chunks))
    word_count = len(plain_text.split())
    is_fallback = word_count < 50 or _is_nav_junk(plain_text)

    collapsed = re.sub(r"\s+", " ", plain_text)
    excerpt = parser.excerpt or (collapsed[:300] if collapsed else None)
    image_url = parser.image_url or extract_image_url(html)
    blocked_reason = detect_blocked_page(title, plain_text, html[:_HEAD_SCAN_LIMIT])
    redirect_url = detect_redirect_url(html, url)

    return {
        "title": title,
        "byline": parser.byline,
        "site_name": parser.site_name,
        "published_date": parser.published_date,
        "excerpt": excerpt,
        "content_text": plain_text,
        "clean_html": clean_html,
        "is_fallback": 1 if is_fallback else 0,
        "word_count": word_count,
        "image_url": image_url,
        "language": detect_html_language(html),
        "blocked_reason": blocked_reason,
        "redirect_url": redirect_url,
    }


def extract_article(
    html: str, url: str, *, content_type: str | None = None
) -> dict[str, Any]:
    """Extract article content and reader HTML with Trafilatura and OG fallback."""
    media_type = _parse_media_type(content_type)
    binary_reason = _detect_binary_reason(html, media_type)
    if binary_reason:
        return _binary_rejected_article(url, binary_reason)
    if media_type in _PLAIN_TEXT_MEDIA_TYPES:
        return _extract_plain_text_article(html, url)
    if (
        not media_type
        and (html or "").strip()
        and not _HTML_STRUCTURE_RE.search((html or "")[:16384])
    ):
        return _extract_plain_text_article(html, url)

    if _is_youtube_url(url):
        return extract_youtube_article(html, url)
    if _is_github_url(url):
        return extract_github_article(html, url)
    if _is_wikipedia_url(url):
        return extract_wikipedia_article(html, url)
    if len(html) > _LARGE_HTML_THRESHOLD:
        return _extract_large_html(html, url)

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
            published_date = normalize_published_date(metadata.date)
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

        safe_title = html_module.escape(title or "", quote=True)
        readable = _extract_with_readability(html, url)
        if readable and len(readable.strip()) >= 50:
            plain_text = readable
            safe_readable = html_module.escape(readable, quote=True)
            clean_html_raw = f"<h1>{safe_title}</h1><p>{safe_readable}</p>"
            # A substantial paragraph recovery is a real article, not a
            # link-only stub: don't show the "Link Only" badge for it.
            if len(readable.split()) >= _READABILITY_REAL_ARTICLE_WORDS:
                is_fallback = False
        else:
            plain_text = excerpt or title or "No readable text extracted."
            safe_excerpt = html_module.escape(excerpt or "", quote=True)
            safe_url = html_module.escape(url or "", quote=True)
            clean_html_raw = (
                f"<h1>{safe_title}</h1><p>{safe_excerpt}</p>"
                f"<p><a href='{safe_url}' target='_blank'>Visit original link</a></p>"
            )

    clean_html = sanitize_clean_html(clean_html_raw or "")
    # The reader shows the title as the page heading: an article that repeats
    # it in a leading <h1> would render the headline twice (issue #57).
    clean_html = _drop_duplicate_leading_h1(clean_html, title)
    plain_text = _clean_plain_text(plain_text or "")
    word_count = len(plain_text.split())

    if not is_fallback and _is_nav_junk(plain_text):
        is_fallback = True

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
        "language": detect_html_language(html),
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
        # Reader-proxy markdown carries no <html lang>: the tagger guesses.
        "language": None,
        # Reader-proxy text is genuine article content: never a wall.
        "blocked_reason": detect_blocked_page(title, plain_text, None),
        "redirect_url": None,
    }
