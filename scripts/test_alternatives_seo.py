"""Focused SEO and structure tests for keepfor.me comparison pages (#378)."""

import os
import re
import sys
from bs4 import BeautifulSoup

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
import verify_site  # noqa: E402

COMPARISON_PAGES = [
    "alternatives/pocket/index.html",
    "alternatives/omnivore/index.html",
    "alternatives/karakeep/index.html",
    "alternatives/readwise/index.html",
    "alternatives/best-read-it-later-apps-2026/index.html",
]


def _read_page(rel_path: str) -> str:
    full_path = os.path.join(BASE_DIR, rel_path)
    assert os.path.isfile(full_path), f"Missing expected page: {rel_path}"
    with open(full_path, "r", encoding="utf-8") as f:
        return f.read()


def _norm_href(href: str) -> str:
    h = href.strip().split("#")[0].split("?")[0]
    if h.startswith("https://keepfor.me"):
        h = h[len("https://keepfor.me"):]
    if not h.startswith("/"):
        h = "/" + h
    if h.endswith("/index.html"):
        h = h[: -len("/index.html")]
    return h.rstrip("/")


def test_five_pages_exist_and_have_unique_seo_meta():
    titles = {}
    descriptions = {}
    canonicals = {}

    for rel_path in COMPARISON_PAGES:
        html = _read_page(rel_path)
        soup = BeautifulSoup(html, "html.parser")
        slug = rel_path[: -len("/index.html")]
        expected_url = f"https://keepfor.me/{slug}/"

        title_tag = soup.find("title")
        assert title_tag is not None, f"{rel_path}: missing <title>"
        title_text = title_tag.get_text(strip=True)
        assert title_text, f"{rel_path}: empty <title>"
        titles[rel_path] = title_text

        desc_tag = soup.find("meta", attrs={"name": "description"})
        assert desc_tag is not None, f"{rel_path}: missing meta description"
        desc_text = (desc_tag.get("content") or "").strip()
        assert desc_text, f"{rel_path}: empty meta description"
        descriptions[rel_path] = desc_text

        canon_tags = [
            link
            for link in soup.find_all("link")
            if "canonical"
            in [
                r.lower()
                for r in (
                    link.get("rel")
                    if isinstance(link.get("rel"), list)
                    else (link.get("rel") or "").split()
                )
            ]
        ]
        assert len(canon_tags) == 1, f"{rel_path}: expected 1 canonical link"
        canon_href = (canon_tags[0].get("href") or "").strip()
        assert canon_href == expected_url, f"{rel_path}: canonical {canon_href} != {expected_url}"
        canonicals[rel_path] = canon_href

        for prop in ("og:title", "og:description", "og:url", "og:image"):
            og_tag = soup.find("meta", attrs={"property": prop})
            assert og_tag is not None, f"{rel_path}: missing {prop}"
            og_val = (og_tag.get("content") or "").strip()
            assert og_val, f"{rel_path}: empty {prop}"
            if prop == "og:url":
                assert og_val == expected_url, f"{rel_path}: og:url {og_val} != {expected_url}"

    assert len(set(titles.values())) == len(COMPARISON_PAGES), "Duplicate <title> across comparison pages"
    assert len(set(descriptions.values())) == len(COMPARISON_PAGES), "Duplicate meta description across comparison pages"
    assert len(set(canonicals.values())) == len(COMPARISON_PAGES), "Duplicate canonical URL across comparison pages"


def test_pocket_alternative_legacy_page_canonical_and_sitemap():
    legacy_html = _read_page("pocket-alternative/index.html")
    legacy_soup = BeautifulSoup(legacy_html, "html.parser")
    canon = legacy_soup.find("link", attrs={"rel": "canonical"})
    assert canon is not None
    assert canon.get("href") == "https://keepfor.me/alternatives/pocket/"

    sitemap_xml = _read_page("sitemap.xml")
    locs = [loc.strip().rstrip("/") for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sitemap_xml)]

    for rel_path in COMPARISON_PAGES:
        slug = rel_path[: -len("/index.html")]
        assert f"https://keepfor.me/{slug}" in locs, f"sitemap.xml missing {slug}"

    assert "https://keepfor.me/pocket-alternative" not in locs, (
        "sitemap.xml must not list competing canonical /pocket-alternative/ alongside /alternatives/pocket/"
    )


def test_footer_links_on_homepage_and_marketing_pages():
    pages_with_footer = [
        "index.html",
        "pricing/index.html",
        "self-host/index.html",
        "pocket-alternative/index.html",
        "keepfor-vs-raindrop/index.html",
        "claude-mcp/index.html",
        "terms/index.html",
        "privacy/index.html",
        *COMPARISON_PAGES,
    ]
    for rel_path in pages_with_footer:
        soup = BeautifulSoup(_read_page(rel_path), "html.parser")
        footer = soup.find("footer")
        assert footer is not None, f"{rel_path}: missing <footer>"
        hrefs = {_norm_href(a["href"]) for a in footer.find_all("a", href=True) if a.get("href")}
        for comp_page in COMPARISON_PAGES:
            slug = comp_page[: -len("/index.html")]
            assert f"/{slug}" in hrefs, f"{rel_path}: footer missing /{slug}/"


def test_pricing_and_mcp_facts_on_comparison_pages():
    for rel_path in COMPARISON_PAGES:
        soup = BeautifulSoup(_read_page(rel_path), "html.parser")
        text = soup.get_text(" ", strip=True)
        assert not re.search(r"\b(?:TODO|TBD|FIXME)\b", text), f"{rel_path}: contains placeholder marker"
        assert "$5/mo" in text or "$5/month" in text, f"{rel_path}: missing Personal $5/mo pricing"
        assert "$9/mo" in text or "$9/month" in text, f"{rel_path}: missing Pro $9/mo pricing"
        counts = re.findall(
            r"\b(\d+)\s+(?:MCP\s+tools|tools\s+over\s+(?:the\s+)?(?:Model\s+Context\s+Protocol|MCP))",
            text,
            re.IGNORECASE,
        )
        assert counts, f"{rel_path}: missing sourced MCP tool count"
        for c in counts:
            assert int(c) == 46, f"{rel_path}: expected 46 MCP tools, found {c}"


def test_zero_em_or_en_dashes_and_footer_donation():
    assert verify_site.check_em_dashes() is True
    assert verify_site.check_footer_donation_links() is True
    assert os.path.isfile(os.path.join(BASE_DIR, "CNAME"))


def test_internal_links_resolve_locally():
    all_pages = [
        "index.html",
        "pricing/index.html",
        "self-host/index.html",
        "pocket-alternative/index.html",
        "keepfor-vs-raindrop/index.html",
        "claude-mcp/index.html",
        "terms/index.html",
        "privacy/index.html",
        *COMPARISON_PAGES,
    ]
    for rel_path in all_pages:
        soup = BeautifulSoup(_read_page(rel_path), "html.parser")
        for tag in soup.find_all(["a", "link", "script"]):
            href = (tag.get("href") or tag.get("src") or "").strip()
            if not href or href.startswith(("#", "data:", "mailto:", "javascript:")):
                continue
            if href.startswith("https://keepfor.me"):
                href = href[len("https://keepfor.me"):] or "/"
            elif href.startswith(("http://", "https://")):
                continue
            clean = href.split("#")[0].split("?")[0]
            if not clean or clean == "/":
                target = os.path.join(BASE_DIR, "index.html")
            elif clean.endswith("/"):
                target = os.path.join(BASE_DIR, clean.lstrip("/"), "index.html")
            else:
                target = os.path.join(BASE_DIR, clean.lstrip("/"))
                if os.path.isdir(target):
                    target = os.path.join(target, "index.html")
            assert os.path.isfile(target), f"{rel_path}: broken internal link {href} -> {target}"


def test_raindrop_mcp_claims_accurate():
    false_raindrop_mcp_patterns = [
        r"\bnot\s+MCP\b",
        r"\bno\s+(?:native\s+|official\s+|built-in\s+)?MCP\b",
        r"\bonly\s+a\s+classic\s+(?:REST\s+)?API\b",
        r"\bclassic\s+(?:REST\s+)?API\s*,\s*not\s+MCP\b",
        r"\bclassic\s+(?:REST\s+)?API\s+rather\s+than\s+(?:a\s+|an\s+)?(?:native\s+|official\s+)?MCP\b",
        r"\brather\s+than\s+(?:a\s+|an\s+)?(?:native\s+|official\s+)?MCP\s+server\b",
        r"\binstead\s+of\s+(?:a\s+|an\s+)?(?:native\s+|official\s+)?MCP\b",
        r"\bclassic\s+(?:REST\s+)?API\b[^.<>\n]{0,120}\bnot\s+the\s+same\s+thing\s+as\s+an\s+AI-native\b",
        r"\bMCP\s+support\s+is\s+(?:currently\s+)?unique\b",
        r"\bRaindrop(?:\.io)?\b[^.<>\n]{0,100}\b(?:lacks|without|does\s+not\s+(?:have|offer|provide|support)|doesn't\s+(?:have|offer|provide|support))\b[^.<>\n]{0,60}\bMCP\b",
    ]
    raindrop_pages = [
        "alternatives/best-read-it-later-apps-2026/index.html",
        "keepfor-vs-raindrop/index.html",
    ]
    for rel_path in raindrop_pages:
        html = _read_page(rel_path)
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)

        for pat in false_raindrop_mcp_patterns:
            assert not re.search(pat, text, re.IGNORECASE), f"{rel_path}: matched false Raindrop MCP pattern {pat}"
            assert not re.search(pat, html, re.IGNORECASE), f"{rel_path}: matched false Raindrop MCP HTML pattern {pat}"

        mcp_row_found = False
        for tr in soup.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) >= 3 and re.search(r"\bMCP\b", cells[0].get_text(" ", strip=True), re.IGNORECASE):
                mcp_row_found = True
                raindrop_cell = cells[2].get_text(" ", strip=True)
                assert not re.match(r"^(?:no|none)\b", raindrop_cell, re.IGNORECASE), (
                    f"{rel_path}: Raindrop MCP cell still starts with no/none: {raindrop_cell}"
                )
                assert re.search(r"\b(?:yes|mcp|official)\b", raindrop_cell, re.IGNORECASE), (
                    f"{rel_path}: Raindrop MCP cell does not acknowledge official MCP: {raindrop_cell}"
                )
        assert mcp_row_found, f"{rel_path}: missing MCP comparison row"
        assert "Stella" in text and "ChatGPT" in text and "Claude" in text, (
            f"{rel_path}: expected mention of Raindrop Stella features in ChatGPT and Claude via MCP"
        )
        assert "47 tools" not in text, f"{rel_path}: outdated 47 tools count found"


def test_omnivore_shutdown_and_official_zip_export_claims():
    omni_path = "alternatives/omnivore/index.html"
    omni_html = _read_page(omni_path)
    omni_soup = BeautifulSoup(omni_html, "html.parser")
    omni_text = omni_soup.get_text(" ", strip=True)

    assert re.search(r"\b(?:shut\s*down|shutdown)\b", omni_text, re.IGNORECASE)
    assert re.search(r"\bNovember\s+2024\b", omni_text, re.IGNORECASE)
    assert re.search(r"\bNovember\s+15,\s*2024\b", omni_text, re.IGNORECASE)
    assert "ElevenLabs" in omni_text

    assert re.search(r"\.zip\b|\bzip\b", omni_text, re.IGNORECASE)
    assert re.search(r"metadata_\*\.json", omni_html)
    assert re.search(r"content/\*\.html", omni_html)
    assert re.search(r"highlights/\*\.md", omni_html)
    assert "_prepare_zip_bookmarks" in omni_text
    assert re.search(r"links,\s*titles,\s*and\s*labels", omni_text, re.IGNORECASE)
    assert re.search(r"does\s+not\s+import\s+Omnivore's\s+highlights/\*\.md", omni_text, re.IGNORECASE)

    false_omnivore_export_patterns = [
        r"\bOmnivore\s+HTML,\s*CSV(?:,?\s*(?:or|and|&|&amp;)\s*Markdown)?\s+(?:exports?|import)\b",
        r"\bbookmark\s+HTML\s+files?\s+exported\s+by\s+Omnivore\b",
        r"\bfrom\s+Omnivore\s+and\s+Instapaper\s+CSV\b",
        r"\bOmnivore\s+CSV\s+(?:exports?|sheets?|files?)\b",
        r"\bOmnivore(?:'s)?\s+(?:official\s+|native\s+|standard\s+)?exports?\s+(?:was|were|is|are|in|as|produced)\s+(?:a\s+|an\s+)?(?:standard\s+)?(?:Netscape\s+)?(?:Bookmark\s+)?(?:HTML|CSV|Markdown)\b",
    ]
    for check_page in (
        omni_path,
        "alternatives/best-read-it-later-apps-2026/index.html",
        "alternatives/pocket/index.html",
        "pocket-alternative/index.html",
    ):
        page_html = _read_page(check_page)
        page_text = BeautifulSoup(page_html, "html.parser").get_text(" ", strip=True)
        for pat in false_omnivore_export_patterns:
            assert not re.search(pat, page_text, re.IGNORECASE), f"{check_page}: matched false Omnivore export pattern {pat}"
            assert not re.search(pat, page_html, re.IGNORECASE), f"{check_page}: matched false Omnivore export HTML pattern {pat}"


def test_non_blocking_pocket_karakeep_and_readwise_facts():
    for rel_path in (
        "alternatives/pocket/index.html",
        "alternatives/best-read-it-later-apps-2026/index.html",
        "keepfor-vs-raindrop/index.html",
        "pocket-alternative/index.html",
    ):
        text = BeautifulSoup(_read_page(rel_path), "html.parser").get_text(" ", strip=True)
        assert "10 million" not in text, f"{rel_path}: still contains unsourced '10 million' claim"
        assert "pulled back in May 2025" not in text, f"{rel_path}: still contains unsourced 'pulled back in May 2025' claim"

    karakeep_text = BeautifulSoup(_read_page("alternatives/karakeep/index.html"), "html.parser").get_text(" ", strip=True)
    assert "Docker Compose" in karakeep_text
    assert "Meilisearch" in karakeep_text

    readwise_text = BeautifulSoup(_read_page("alternatives/readwise/index.html"), "html.parser").get_text(" ", strip=True)
    assert "$9.99/month" in readwise_text
    assert "readwise.io/export" in readwise_text


