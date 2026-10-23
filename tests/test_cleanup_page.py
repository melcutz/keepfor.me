from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from keepfor.app import app
from keepfor.auth.service import login_user, register_user


@pytest.fixture
def client(db, fake_env, monkeypatch):
    env = fake_env
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    return TestClient(app)


@pytest.fixture
async def user(db):
    return await register_user(db, "cleanup-page@example.com", "password123")


@pytest.fixture
async def session(db, user):
    _, sid = await login_user(db, "cleanup-page@example.com", "password123")
    return sid


async def _insert_item(
    db, user_id, item_id, url, title, *, status="ok", fail_reason=None
):
    await db.execute(
        "INSERT INTO items (id, user_id, url, canonical_url, title, status,"
        " fail_reason) VALUES (?,?,?,?,?,?,?);",
        (item_id, user_id, url, url, title, status, fail_reason),
    )


async def _tag_item(db, user_id, item_id, name):
    await db.execute(
        "INSERT INTO tags (id, user_id, name) VALUES (?,?,?);",
        (f"tag-{item_id}", user_id, name),
    )
    await db.execute(
        "INSERT INTO item_tags (item_id, tag_id) VALUES (?,?);",
        (item_id, f"tag-{item_id}"),
    )


def _flat(html: str) -> str:
    """Page text without tags, so assertions do not depend on markup."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


@pytest.mark.asyncio
async def test_cleanup_requires_login(client, db, fake_env):
    r = client.get("/cleanup", follow_redirects=False)
    assert r.status_code == 303
    assert "/auth/login" in r.headers["location"]


@pytest.mark.asyncio
async def test_cleanup_duplicates_renders(client, db, user, session):
    # Tracking params differ per save, so real duplicates share a dedupe
    # key while canonical urls stay unique (the shape save_item produces).
    await _insert_item(db, user["id"], "dup-1", "https://example.com/same", "Dup One")
    await _insert_item(
        db, user["id"], "dup-2", "https://example.com/same?utm_source=x", "Dup Two"
    )
    await _insert_item(
        db, user["id"], "other", "https://example.com/other", "Other Item"
    )
    client.cookies["kfm_session"] = session
    r = client.get("/cleanup?kind=duplicates")
    assert r.status_code == 200
    assert "Dup One" in r.text
    assert "Dup Two" in r.text
    assert "Other Item" not in r.text
    assert "We found 1 duplicate group." in _flat(r.text)
    # Tabs per kind with counts.
    assert "Duplicates (1)" in _flat(r.text)


@pytest.mark.asyncio
async def test_cleanup_failed_extraction_renders(client, db, user, session):
    # A non-HTTP failure failed the extraction; it does not prove a dead link.
    await _insert_item(
        db,
        user["id"],
        "wall",
        "https://example.com/wall",
        "Wall Item",
        status="failed",
        fail_reason="login wall",
    )
    await _insert_item(db, user["id"], "fine", "https://example.com/fine", "Fine Item")
    client.cookies["kfm_session"] = session
    r = client.get("/cleanup?kind=failed_extraction")
    assert r.status_code == 200
    assert "Wall Item" in r.text
    assert "Fine Item" not in r.text
    assert "We found 1 failed extraction." in _flat(r.text)
    r_dead = client.get("/cleanup?kind=dead_links")
    assert "Wall Item" not in r_dead.text


@pytest.mark.asyncio
async def test_cleanup_dead_links_renders(client, db, user, session):
    await _insert_item(
        db,
        user["id"],
        "gone",
        "https://example.com/gone",
        "Gone Item",
        status="failed",
        fail_reason="HTTP 404 returned by origin server (https://example.com/gone)",
    )
    client.cookies["kfm_session"] = session
    r = client.get("/cleanup?kind=dead_links")
    assert r.status_code == 200
    assert "Gone Item" in r.text
    assert "We found 1 dead link." in _flat(r.text)
    # Rows are the ordinary card: the title opens our reader copy (not the
    # dead origin) and Move to Trash reuses the existing DELETE route.
    assert 'href="/items/gone"' in r.text
    assert 'hx-delete="/api/items/gone"' in r.text
    # An origin HTTP error is not a mere failed extraction.
    r_fe = client.get("/cleanup?kind=failed_extraction")
    assert "Gone Item" not in r_fe.text


@pytest.mark.asyncio
async def test_cleanup_untagged_renders(client, db, user, session):
    await _insert_item(db, user["id"], "bare", "https://example.com/bare", "Bare Item")
    await _insert_item(
        db, user["id"], "tagged", "https://example.com/tagged", "Tagged Item"
    )
    await _tag_item(db, user["id"], "tagged", "news")
    client.cookies["kfm_session"] = session
    r = client.get("/cleanup?kind=untagged")
    assert r.status_code == 200
    assert "Bare Item" in r.text
    assert "Tagged Item" not in r.text
    assert "We found 1 untagged item." in _flat(r.text)


@pytest.mark.asyncio
async def test_cleanup_pager_pages_the_list(client, db, user, session):
    # 11 duplicate pairs: 22 rows, so page 1 holds 20 and page 2 the rest.
    for i in range(11):
        await _insert_item(
            db, user["id"], f"pair-a-{i}", f"https://example.com/p{i}", f"Pair A {i}"
        )
        await _insert_item(
            db,
            user["id"],
            f"pair-b-{i}",
            f"https://example.com/p{i}?utm_source=x",
            f"Pair B {i}",
        )
    client.cookies["kfm_session"] = session
    r1 = client.get("/cleanup?kind=duplicates")
    assert r1.status_code == 200
    assert r1.text.count('id="item-card-') == 20
    assert "Older →" in r1.text
    assert "← Newer" not in r1.text
    assert "We found 11 duplicate groups." in _flat(r1.text)
    r2 = client.get("/cleanup?kind=duplicates&page=2")
    assert r2.status_code == 200
    assert r2.text.count('id="item-card-') == 2
    assert "← Newer" in r2.text
    assert "Older →" not in r2.text


@pytest.mark.asyncio
async def test_cleanup_empty_copy_per_kind(client, db, user, session):
    client.cookies["kfm_session"] = session
    for kind, copy in [
        ("duplicates", "We found no duplicate items."),
        ("failed_extraction", "We found no failed extractions."),
        ("dead_links", "We found no dead links."),
        ("untagged", "We found no untagged items."),
    ]:
        r = client.get(f"/cleanup?kind={kind}")
        assert r.status_code == 200
        assert copy in r.text
    # An unknown kind falls back to duplicates instead of erroring.
    r = client.get("/cleanup?kind=nonsense")
    assert r.status_code == 200
    assert "We found no duplicate items." in r.text


@pytest.mark.asyncio
async def test_cleanup_is_scoped_to_the_user(client, db, fake_env, user, session):
    other = await register_user(
        db, "other-cleanup@example.com", "password123", allow_public_signups=True
    )
    await _insert_item(
        db,
        other["id"],
        "other-dead",
        "https://example.com/other-dead",
        "Other Dead",
        status="failed",
        fail_reason="HTTP 410 returned by origin server",
    )
    await _insert_item(
        db, other["id"], "other-bare", "https://example.com/other-bare", "Other Bare"
    )
    client.cookies["kfm_session"] = session
    for kind in ("duplicates", "failed_extraction", "dead_links", "untagged"):
        r = client.get(f"/cleanup?kind={kind}")
        assert r.status_code == 200
        assert "Other Dead" not in r.text
        assert "Other Bare" not in r.text
    # The tab counts are the caller's own, not the other user's.
    assert "Dead links (0)" in _flat(client.get("/cleanup?kind=dead_links").text)


@pytest.mark.asyncio
async def test_cleanup_links_from_stats_and_settings(client, db, user, session):
    client.cookies["kfm_session"] = session
    assert "Clean up library" in client.get("/stats").text
    assert "Clean up library" in client.get("/settings").text


@pytest.mark.asyncio
async def test_cleanup_trash_form_no_js_and_same_origin(client, db, user, session):
    await _insert_item(
        db,
        user["id"],
        "trash-me",
        "https://example.com/trash-me",
        "Trash Me",
        status="failed",
        fail_reason="HTTP 404 returned by origin server",
    )
    client.cookies["kfm_session"] = session
    page = client.get("/cleanup?kind=dead_links")
    assert 'action="/items/trash-me/trash"' in page.text

    # Cross-origin Origin header is rejected (403) and leaves the item untouched.
    bad_origin = client.post(
        "/items/trash-me/trash",
        headers={"Origin": "https://evil.example.com"},
        follow_redirects=False,
    )
    assert bad_origin.status_code == 403
    row = await db.query_first("SELECT deleted_at FROM items WHERE id = 'trash-me';")
    assert row["deleted_at"] is None

    # Same-origin form POST moves the item to Trash and 303-redirects back.
    ok = client.post(
        "/items/trash-me/trash",
        headers={
            "Origin": "http://testserver",
            "Referer": "http://testserver/cleanup?kind=dead_links",
        },
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert ok.headers["location"] == "/cleanup?kind=dead_links"
    row = await db.query_first("SELECT deleted_at FROM items WHERE id = 'trash-me';")
    assert row["deleted_at"] is not None


def test_classify_failure_statuses_and_reasons():
    from keepfor.consumer.extractor import classify_failure as extractor_classify
    from keepfor.models.cleanup import (
        BLOCKED_FETCH_COPY,
        BLOCKED_HTTP_STATUSES,
        DEAD_HTTP_STATUSES,
        classify_failure,
    )

    assert extractor_classify is classify_failure
    assert BLOCKED_HTTP_STATUSES == frozenset({401, 403, 429})
    assert DEAD_HTTP_STATUSES == frozenset({404, 410})
    assert (
        BLOCKED_FETCH_COPY == "The site blocked our fetch. Open the original or retry."
    )

    for blocked in (
        401,
        403,
        429,
        "HTTP 401 returned by origin server",
        "HTTP 403 returned by origin server (https://example.com/a)",
        "HTTP 429 returned by origin server",
        f"HTTP 403 returned by origin server: {BLOCKED_FETCH_COPY}",
        f"Blocked: {BLOCKED_FETCH_COPY} (403 Forbidden)",
        "Access denied by Cloudflare challenge",
        "Too Many Requests",
        "login wall",
    ):
        assert classify_failure(blocked) == "blocked", blocked

    for dead in (
        404,
        410,
        "HTTP 404 returned by origin server",
        "HTTP 410 returned by origin server",
        "Dead link, no article content: 404 Not Found (https://example.com/missing)",
        "Network error: <urlopen error [Errno -2] Name or service not known>",
        "DNS lookup failed: NXDOMAIN",
        "Connection refused by origin host",
        "Connection reset by peer",
    ):
        assert classify_failure(dead) == "dead", dead

    for other in (
        500,
        502,
        503,
        "HTTP 500 returned by origin server",
        "HTTP 503 returned by origin server",
        "No extractable content found on page",
        "Extraction timed out",
        "",
        None,
        True,
        False,
    ):
        assert classify_failure(other) == "failed_extraction", other


@pytest.mark.asyncio
async def test_fetch_page_html_retries_403_with_browser_headers(monkeypatch):
    from keepfor.consumer.processor import (
        BLOCKED_FETCH_COPY,
        BROWSER_HEADERS,
        BROWSER_RETRY_HEADERS,
        OriginHttpError,
        fetch_page_html,
    )
    from keepfor.utils.egress import FetchResult

    calls: list[dict] = []

    async def fake_fetch_succeeds_on_retry(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if len(calls) == 1:
            return FetchResult(
                text="Forbidden",
                status=403,
                url=url,
            )
        return FetchResult(
            text="<html><body><article>Recovered</article></body></html>",
            status=200,
            url=url,
        )

    monkeypatch.setattr(
        "keepfor.consumer.processor.fetch_limited", fake_fetch_succeeds_on_retry
    )
    html = await fetch_page_html("https://example.com/protected")
    assert "Recovered" in html
    assert len(calls) == 2
    assert calls[0].get("headers") == BROWSER_HEADERS
    retry_headers = calls[1].get("headers") or {}
    assert retry_headers == BROWSER_RETRY_HEADERS
    assert "Mozilla/5.0" in retry_headers["User-Agent"]
    assert "text/html" in retry_headers["Accept"]
    assert "en-US" in retry_headers["Accept-Language"]

    # When the browser-UA retry also returns 403, raise OriginHttpError once with
    # classification="blocked" and the blocked copy.
    calls.clear()

    async def fake_fetch_always_403(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FetchResult(
            text="Forbidden",
            status=403,
            url=url,
        )

    monkeypatch.setattr(
        "keepfor.consumer.processor.fetch_limited", fake_fetch_always_403
    )
    with pytest.raises(OriginHttpError) as exc_info:
        await fetch_page_html("https://example.com/still-blocked")
    assert len(calls) == 2
    assert exc_info.value.status_code == 403
    assert exc_info.value.classification == "blocked"
    assert BLOCKED_FETCH_COPY in str(exc_info.value)

    # 404 is dead, not blocked, so it does not trigger a browser-UA retry.
    calls.clear()

    async def fake_fetch_404(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FetchResult(
            text="Not Found",
            status=404,
            url=url,
        )

    monkeypatch.setattr("keepfor.consumer.processor.fetch_limited", fake_fetch_404)
    with pytest.raises(OriginHttpError) as exc_404:
        await fetch_page_html("https://example.com/missing")
    assert len(calls) == 1
    assert exc_404.value.status_code == 404
    assert exc_404.value.classification == "dead"


@pytest.mark.asyncio
async def test_blocked_copy_and_failed_count_matches_cleanup_totals(
    client, db, user, session
):
    from keepfor.models.cleanup import BLOCKED_FETCH_COPY, find_cleanup
    from keepfor.search.engine import get_status_counts

    uid = user["id"]
    # Blocked HTTP statuses: 401, 403, 429 -> folded into failed_extraction.
    await _insert_item(
        db,
        uid,
        "b-401",
        "https://example.com/b401",
        "Blocked 401",
        status="failed",
        fail_reason="HTTP 401 returned by origin server",
    )
    await _insert_item(
        db,
        uid,
        "b-403",
        "https://example.com/b403",
        "Blocked 403",
        status="failed",
        fail_reason=f"HTTP 403 returned by origin server: {BLOCKED_FETCH_COPY}",
    )
    await _insert_item(
        db,
        uid,
        "b-429",
        "https://example.com/b429",
        "Blocked 429",
        status="failed",
        fail_reason="HTTP 429 returned by origin server",
    )
    # Dead links: only 404, 410, and DNS/connection failures.
    await _insert_item(
        db,
        uid,
        "d-404",
        "https://example.com/d404",
        "Dead 404",
        status="failed",
        fail_reason="HTTP 404 returned by origin server",
    )
    await _insert_item(
        db,
        uid,
        "d-410",
        "https://example.com/d410",
        "Dead 410",
        status="failed",
        fail_reason="HTTP 410 returned by origin server",
    )
    await _insert_item(
        db,
        uid,
        "d-dns",
        "https://no-such-host.invalid/article",
        "Dead DNS",
        status="failed",
        fail_reason=(
            "Network error: <urlopen error [Errno -2] Name or service not known>"
        ),
    )
    # Other extraction failures: 5xx and parser/paywall failures.
    await _insert_item(
        db,
        uid,
        "f-502",
        "https://example.com/f502",
        "Failed 502",
        status="failed",
        fail_reason="HTTP 502 returned by origin server",
    )
    await _insert_item(
        db,
        uid,
        "f-wall",
        "https://example.com/fwall",
        "Failed Wall",
        status="failed",
        fail_reason="login wall",
    )
    await _insert_item(db, uid, "ok-1", "https://example.com/ok1", "Healthy Item")

    report = await find_cleanup(db, uid)
    dead_ids = {item["id"] for item in report["dead_links"]["items"]}
    failed_ext_ids = {item["id"] for item in report["failed_extraction"]["items"]}

    assert dead_ids == {"d-404", "d-410", "d-dns"}
    assert failed_ext_ids == {"b-401", "b-403", "b-429", "f-502", "f-wall"}

    status_counts = await get_status_counts(db, uid)
    cleanup_failed_total = (
        report["dead_links"]["count"] + report["failed_extraction"]["count"]
    )
    assert status_counts["failed"] == 8
    assert cleanup_failed_total == status_counts["failed"]

    client.cookies["kfm_session"] = session

    # Library Failed filter shows all 8 failed items and renders the blocked copy.
    lib_res = client.get("/?status=failed")
    assert lib_res.status_code == 200
    assert "Failed" in lib_res.text
    assert BLOCKED_FETCH_COPY in lib_res.text

    # Clean up tabs sum to the same Failed count (5 + 3 = 8).
    cleanup_res = client.get("/cleanup?kind=failed_extraction")
    assert cleanup_res.status_code == 200
    flat_cleanup = _flat(cleanup_res.text)
    assert "Failed extractions (5)" in flat_cleanup
    assert "Dead links (3)" in flat_cleanup
    assert BLOCKED_FETCH_COPY in cleanup_res.text

    # Reader page for a blocked item shows the blocked copy even if only the raw
    # status code was stored in fail_reason.
    reader_res = client.get("/items/b-401")
    assert reader_res.status_code == 200
    assert BLOCKED_FETCH_COPY in reader_res.text
