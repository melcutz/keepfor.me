import html as html_module
import re
from typing import Any
from urllib.parse import urlparse

try:
    import trafilatura
except ImportError:
    trafilatura = None

import warnings

from bs4 import BeautifulSoup

# Cloudflare Workers have no system timezone database, so tzlocal (via
# trafilatura's date parsing) warns once per article and defaults to UTC --
# which is what we want. Silence it so error logs stay meaningful.
warnings.filterwarnings(
    "ignore",
    message="Can not find any timezone configuration.*",
    category=UserWarning,
)


def sanitize_clean_html(html_str: str) -> str:
    """Ensures extracted HTML contains only safe tags and attributes."""
    if not html_str:
        return ""
    soup = BeautifulSoup(html_str, "html.parser")
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
    for tag in soup.find_all(True):
        if tag.name not in allowed_tags:
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

    return str(soup)


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
    if trafilatura is not None:
        metadata = trafilatura.extract_metadata(html, default_url=url)
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
        soup = BeautifulSoup(html, "html.parser")
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
    word_count = len(plain_text.split())

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
    word_count = len(body.split())

    return {
        "title": title,
        "byline": None,
        "site_name": site_name,
        "published_date": None,
        "excerpt": excerpt,
        "content_text": body,
        "clean_html": clean_html,
        "is_fallback": 0,
        "word_count": word_count,
    }
