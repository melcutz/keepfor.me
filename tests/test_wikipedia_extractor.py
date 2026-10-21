# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Wikipedia extraction rule (issue #43).

A saved Wikipedia page must reach the reader with one title, no "[edit]"
fragments, no junk byline and one date: the last modification of the article.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from keepfor.app import app
from keepfor.auth.service import login_user, register_user
from keepfor.consumer.extractor import extract_article, extract_wikipedia_article

FIXTURE = Path(__file__).parent / "fixtures" / "wikipedia_raft.html"
WIKI_URL = "https://en.wikipedia.org/wiki/Raft_(algorithm)"


class MockEnv:
    """Minimal env: no R2, no AI, no Vectorize (extraction must still work)."""

    DB = None
    BUCKET = None
    AI = None
    VECTORIZE = None
    QUEUE = None


# Endpoint fixtures mirror the pattern from test_tags_page/test_endpoints.
@pytest.fixture
def client(db, monkeypatch):
    def mock_get_db(request):
        return db

    monkeypatch.setattr("keepfor.deps.get_db", mock_get_db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: MockEnv())
    return TestClient(app)


@pytest.fixture
async def auth_headers(db):
    await register_user(db, "wiki@keepfor.me", "password123")
    user, session_id = await login_user(db, "wiki@keepfor.me", "password123")
    return {"admin_session": session_id, "admin_user": user}


@pytest.fixture(scope="module")
def wiki_html() -> str:
    """A saved Wikipedia page (head trimmed, body verbatim)."""
    return FIXTURE.read_text(encoding="utf-8")


def test_wikipedia_title_is_clean_and_not_duplicated(wiki_html):
    """The title loses the " - Wikipedia" suffix and is not repeated in the
    body: the reader already renders it as the page heading."""
    out = extract_article(wiki_html, WIKI_URL)
    assert out["title"] == "Raft (algorithm)"
    assert "<h1" not in out["clean_html"]


def test_wikipedia_drops_the_junk_byline(wiki_html):
    """Wikipedia's author is "Contributors to Wikimedia projects": chrome."""
    out = extract_article(wiki_html, WIKI_URL)
    assert out["byline"] is None
    assert out["site_name"] == "Wikipedia"
    assert "Contributors to Wikimedia projects" not in out["content_text"]


def test_wikipedia_removes_edit_fragments(wiki_html):
    """Every heading carries an "[edit]" link: not article content."""
    out = extract_article(wiki_html, WIKI_URL)
    assert "action=edit" not in out["clean_html"]
    assert "mw-editsection" not in out["clean_html"]
    # The headings themselves survive.
    assert "<h2>Basics</h2>" in out["clean_html"]


def test_wikipedia_normalizes_the_date_to_the_last_modification(wiki_html):
    """One date: the last edit, not the article's creation date."""
    out = extract_article(wiki_html, WIKI_URL)
    assert out["published_date"] == "2026-08-13"
    assert out["published_date"] != "2013-08-13"  # datePublished


def test_wikipedia_date_falls_back_to_the_footer_line(wiki_html):
    """Without schema.org metadata the footer's "last edited on" line is
    the remaining source of truth."""
    without_ld = re.sub(
        r'<script type="application/ld\+json">.*?</script>',
        "",
        wiki_html,
        flags=re.S,
    )
    out = extract_wikipedia_article(without_ld, WIKI_URL)
    assert out["published_date"] == "2026-08-13"


def test_wikipedia_keeps_the_article_body(wiki_html):
    out = extract_article(wiki_html, WIKI_URL)
    assert "consensus algorithm" in out["content_text"].lower()
    assert out["word_count"] > 500
    assert out["is_fallback"] == 0
    assert out["blocked_reason"] is None


def test_wikipedia_links_and_images_are_absolute(wiki_html):
    """Wikipedia links are site-relative; the reader needs absolute URLs."""
    out = extract_article(wiki_html, WIKI_URL)
    assert 'href="/wiki/' not in out["clean_html"]
    assert 'src="//' not in out["clean_html"]
    assert "https://en.wikipedia.org/wiki/" in out["clean_html"]


def test_is_wikipedia_url_domain_boundaries():
    from keepfor.consumer.extractor import _is_wikipedia_url

    assert _is_wikipedia_url("https://en.wikipedia.org/wiki/Raft_(algorithm)") is True
    assert _is_wikipedia_url("https://de.wikipedia.org/wiki/Algorithmus") is True
    assert _is_wikipedia_url("https://wikipedia.org/wiki/Raft") is True

    assert _is_wikipedia_url("https://notwikipedia.org/wiki/Raft") is False
    assert _is_wikipedia_url("https://wikipedia.com/wiki/Raft") is False
    assert _is_wikipedia_url("https://en.wikipedia.org.attacker.test/wiki/X") is False
    assert _is_wikipedia_url("https://wikimedia.org/wiki/Raft") is False


@pytest.mark.asyncio
async def test_card_and_reader_show_the_same_date(
    client, db, auth_headers, monkeypatch
):
    """The library card and the reader must not disagree about the date."""
    from keepfor.consumer import processor

    user = auth_headers["admin_user"]
    item_id = "55555555-5555-5555-5555-555555555555"
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status, created_at) "
        "VALUES (?,?,?,?, 'queued', '2026-01-01 00:00:00');",
        (item_id, user["id"], WIKI_URL, WIKI_URL),
    )

    async def fake_fetch(url, headers=None):
        return FIXTURE.read_text(encoding="utf-8")

    monkeypatch.setattr(processor, "fetch_page_html", fake_fetch)
    await processor.extract_and_store(db, MockEnv(), item_id, WIKI_URL)

    client.cookies["kfm_session"] = auth_headers["admin_session"]
    reader = client.get(f"/items/{item_id}")
    card = client.get(f"/items/{item_id}/card")
    assert reader.status_code == 200
    assert card.status_code == 200
    # The last modification, not the save date, and the same on both views.
    assert "2026-08-13" in reader.text
    assert "2026-08-13" in card.text
    assert "2026-01-01" not in reader.text
    # No site suffix, no contributor byline.
    assert "Raft (algorithm) - Wikipedia" not in reader.text
    assert "Contributors to Wikimedia projects" not in reader.text
