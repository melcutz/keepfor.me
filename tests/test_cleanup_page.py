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
