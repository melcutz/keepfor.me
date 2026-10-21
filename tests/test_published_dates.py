# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Published dates are normalised to YYYY-MM-DD and labelled on the card.

The generic extractor handed the raw `article:published_time`
("2024-03-01T12:00:00+00:00") to the database and the library card rendered
it verbatim. Every extractor now stores a YYYY-MM-DD date, and the card shows
it labelled, next to the date the item was saved.
"""

import pytest
from fastapi.testclient import TestClient

from keepfor.app import app
from keepfor.auth.service import login_user, register_user
from keepfor.consumer import extractor, processor
from keepfor.consumer.extractor import extract_article
from keepfor.utils.dates import normalize_published_date

ARTICLE_URL = "https://example.com/shipping-slower"
ARTICLE_HTML = """<html><head>
<title>Shipping slower</title>
<meta property="og:title" content="Shipping slower">
<meta property="article:published_time" content="2024-03-01T12:00:00+00:00">
</head><body><article>
<p>Fulfilment times doubled this month and the carriers say the backlog will
clear only in April. The warehouses in the north are full.</p>
</article></body></html>"""


class _StubMetadata:
    """Metadata as the older trafilatura releases hand it over: raw."""

    title = "Shipping slower"
    author = None
    sitename = "Example"
    description = "Fulfilment times doubled"
    date = "2024-03-01T12:00:00+00:00"


class _StubTrafilatura:
    """Stand-in for trafilatura: the date is the raw meta value."""

    def extract_metadata(self, html, default_url=None):
        return _StubMetadata()

    def extract(self, html, url=None, output_format=None, **kwargs):
        if output_format == "txt":
            return (
                "Fulfilment times doubled this month and the carriers say the "
                "backlog will clear only in April."
            )
        return "<p>Fulfilment times doubled this month.</p>"


class MockEnv:
    """Minimal env: no R2, no AI, no Vectorize (extraction must still work)."""

    DB = None
    BUCKET = None
    AI = None
    VECTORIZE = None
    QUEUE = None


@pytest.fixture
def client(db, monkeypatch):
    def mock_get_db(request):
        return db

    monkeypatch.setattr("keepfor.deps.get_db", mock_get_db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: MockEnv())
    return TestClient(app)


@pytest.fixture
async def auth_headers(db):
    await register_user(db, "dates@keepfor.me", "password123")
    user, session_id = await login_user(db, "dates@keepfor.me", "password123")
    return {"admin_session": session_id, "admin_user": user}


@pytest.fixture
def raw_trafilatura(monkeypatch):
    """Trafilatura hands over the raw meta value, like the 1.x releases."""
    extractor._load_parsers()  # load the real parsers before replacing one
    monkeypatch.setattr(extractor, "_trafilatura", _StubTrafilatura())
    return extractor


def test_normalize_published_date_parses_iso_dates():
    assert normalize_published_date("2024-03-01") == "2024-03-01"
    assert normalize_published_date("2024-03-01T12:00:00+00:00") == "2024-03-01"
    assert normalize_published_date("2024-03-01T12:00:00Z") == "2024-03-01"
    assert normalize_published_date("2024-03-01 12:00:00") == "2024-03-01"
    assert normalize_published_date("2024-3-1") == "2024-03-01"


def test_normalize_published_date_rejects_unparseable_values():
    assert normalize_published_date(None) is None
    assert normalize_published_date("") is None
    assert normalize_published_date("not a date") is None
    assert normalize_published_date("March 2024") is None
    # An impossible date is not a date.
    assert normalize_published_date("2024-13-45") is None
    assert normalize_published_date("2024-02-30") is None


def test_generic_extractor_normalises_the_published_date(raw_trafilatura):
    """The raw meta timestamp never leaves the extractor."""
    out = extract_article(ARTICLE_HTML, ARTICLE_URL)
    assert out["published_date"] == "2024-03-01"


@pytest.mark.asyncio
async def test_card_labels_the_published_date(
    client, db, auth_headers, raw_trafilatura, monkeypatch
):
    """The card keeps the save date and adds the published one, labelled."""
    user = auth_headers["admin_user"]
    item_id = "66666666-6666-6666-6666-666666666666"
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status, created_at) "
        "VALUES (?,?,?,?, 'queued', '2026-01-01 00:00:00');",
        (item_id, user["id"], ARTICLE_URL, ARTICLE_URL),
    )

    async def fake_fetch(url, headers=None):
        return ARTICLE_HTML

    monkeypatch.setattr(processor, "fetch_page_html", fake_fetch)
    await processor.extract_and_store(db, MockEnv(), item_id, ARTICLE_URL)

    row = await db.query_first(
        "SELECT published_date FROM items WHERE id = ?;", (item_id,)
    )
    assert row["published_date"] == "2024-03-01"

    client.cookies["kfm_session"] = auth_headers["admin_session"]
    card = client.get(f"/items/{item_id}/card")
    assert card.status_code == 200
    assert "2026-01-01" in card.text  # the save date
    assert "Published 2024-03-01" in card.text
    # The raw timestamp is never rendered.
    assert "2024-03-01T12:00:00" not in card.text
    assert "12:00:00" not in card.text


@pytest.mark.asyncio
async def test_card_ignores_an_unparseable_published_date(client, db, auth_headers):
    """A stored value that is not a date renders no label at all."""
    user = auth_headers["admin_user"]
    item_id = "77777777-7777-7777-7777-777777777777"
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, status,"
        " published_date, created_at) "
        "VALUES (?,?,?,?, 'ok', 'sometime in March', '2026-01-01 00:00:00');",
        (item_id, user["id"], ARTICLE_URL, ARTICLE_URL),
    )

    client.cookies["kfm_session"] = auth_headers["admin_session"]
    card = client.get(f"/items/{item_id}/card")
    assert card.status_code == 200
    assert "Published" not in card.text
    assert "sometime in March" not in card.text
    assert "2026-01-01" in card.text  # the save date is still there
