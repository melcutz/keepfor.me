# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Persisted import status: counting, persistence and the final summary.

GitHub issue #18: the upload result (notes imported, duplicates skipped,
links queued, attachments not imported, parse errors, plan-limit skips)
must be visible after a refresh.
"""

import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from keepfor.auth.service import login_user, register_user
from keepfor.models.imports import (
    ATTACHMENT_REASON,
    LINK_SKIP_REASONS,
    MAX_IMPORT_ERRORS,
    SKIP_REASON_LABELS,
    LinkSkipReason,
    apply_chunk_counts,
    create_import,
    fail_import,
    get_import,
    get_import_detail,
    list_import_items,
    list_imports,
    record_import_items,
    retry_all_failed_import_items,
    set_import_counts,
)
from tests.fakes import FakeQueue


class StatusEnv:
    """Minimal worker env: a queue binding plus the sqlite connection."""

    def __init__(self, conn):
        self.QUEUE = FakeQueue()
        self.sqlite_conn = conn


def _keep_zip(notes: list[dict]) -> bytes:
    """A Google Keep Takeout-style zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, note in enumerate(notes):
            zf.writestr(f"Takeout/Keep/note-{i}.json", json.dumps(note))
    return buf.getvalue()


def _keep_note(title: str, body: str = "body", **extra) -> dict:
    note = {
        "title": title,
        "textContent": body,
        "isTrashed": False,
        "createdTimestampUsec": 1000000,
    }
    note.update(extra)
    return note


@pytest.mark.asyncio
async def test_create_import_persists_a_running_record(db):
    user = await register_user(db, "create@test.local", "password123")

    import_id = await create_import(
        db, user["id"], source="keep", filename="takeout.zip"
    )

    record = await get_import(db, user["id"], import_id)
    assert record is not None
    assert record["status"] == "running"
    assert record["source"] == "keep"
    assert record["filename"] == "takeout.zip"
    assert record["notes_imported"] == 0
    assert record["errors"] == []
    assert record["progress"] == 0


@pytest.mark.asyncio
async def test_set_import_counts_stores_the_parse_stage_counts(db):
    user = await register_user(db, "counts@test.local", "password123")
    import_id = await create_import(
        db, user["id"], source="zip", filename="takeout.zip"
    )

    await set_import_counts(
        db,
        user["id"],
        import_id,
        source="keep",
        notes_total=12,
        trashed_skipped=3,
        urls_found=5,
        attachments_not_imported=2,
        parse_errors=1,
        errors=["note-4.json: invalid note file"],
    )

    record = await get_import(db, user["id"], import_id)
    assert record["source"] == "keep"
    assert record["notes_total"] == 12
    assert record["trashed_skipped"] == 3
    assert record["urls_found"] == 5
    assert record["attachments_not_imported"] == 2
    assert record["parse_errors"] == 1
    assert record["errors"] == ["note-4.json: invalid note file"]


@pytest.mark.asyncio
async def test_apply_chunk_counts_accumulates_and_completes(db):
    user = await register_user(db, "chunks@test.local", "password123")
    import_id = await create_import(db, user["id"], source="keep")
    await set_import_counts(
        db,
        user["id"],
        import_id,
        source="keep",
        notes_total=4,
        trashed_skipped=0,
        urls_found=2,
        attachments_not_imported=0,
        parse_errors=0,
    )

    await apply_chunk_counts(
        db,
        user["id"],
        import_id,
        {
            "notes_imported": 2,
            "notes_processed": 2,
            "duplicates_skipped": 1,
            "urls_queued": 1,
            "urls_skipped": 0,
            "parse_errors": 0,
            "plan_skipped": 0,
            "errors": [],
        },
    )
    record = await get_import(db, user["id"], import_id)
    assert record["notes_imported"] == 2
    assert record["notes_processed"] == 2
    assert record["duplicates_skipped"] == 1
    assert record["status"] == "running"
    assert record["progress"] == 50

    await apply_chunk_counts(
        db,
        user["id"],
        import_id,
        {
            "notes_imported": 1,
            "notes_processed": 2,
            "duplicates_skipped": 0,
            "urls_queued": 1,
            "urls_skipped": 0,
            "parse_errors": 1,
            "plan_skipped": 0,
            "errors": ["note: could not be imported"],
        },
    )
    record = await get_import(db, user["id"], import_id)
    assert record["notes_imported"] == 3
    assert record["notes_processed"] == 4
    assert record["urls_queued"] == 2
    assert record["parse_errors"] == 1
    assert record["status"] == "completed"
    assert record["progress"] == 100
    assert record["errors"] == ["note: could not be imported"]


@pytest.mark.asyncio
async def test_import_errors_are_capped(db):
    user = await register_user(db, "capped@test.local", "password123")
    import_id = await create_import(db, user["id"], source="keep")
    await set_import_counts(
        db,
        user["id"],
        import_id,
        source="keep",
        notes_total=1,
        trashed_skipped=0,
        urls_found=0,
        attachments_not_imported=0,
        parse_errors=0,
    )

    await apply_chunk_counts(
        db,
        user["id"],
        import_id,
        {
            "notes_processed": 1,
            "notes_imported": 0,
            "parse_errors": 30,
            "errors": [f"error {i}" for i in range(30)],
        },
    )

    record = await get_import(db, user["id"], import_id)
    assert len(record["errors"]) == MAX_IMPORT_ERRORS
    assert record["errors"][0] == "error 0"
    assert len(record["errors"][-1]) <= 200


@pytest.mark.asyncio
async def test_fail_import_marks_the_record_failed(db):
    user = await register_user(db, "failed@test.local", "password123")
    import_id = await create_import(
        db, user["id"], source="enex", filename="notes.enex"
    )

    await fail_import(db, user["id"], import_id, "The ENEX file could not be parsed.")

    record = await get_import(db, user["id"], import_id)
    assert record["status"] == "failed"
    assert record["error"] == "The ENEX file could not be parsed."


@pytest.mark.asyncio
async def test_import_records_are_scoped_to_their_owner(db):
    owner = await register_user(db, "owner@test.local", "password123")
    other = await register_user(
        db, "other@test.local", "password123", allow_public_signups=True
    )

    import_id = await create_import(
        db, owner["id"], source="keep", filename="secret.zip"
    )

    assert [r["id"] for r in await list_imports(db, owner["id"])] == [import_id]
    assert await list_imports(db, other["id"]) == []
    assert await get_import(db, other["id"], import_id) is None


@pytest.mark.asyncio
async def test_import_route_persists_status_and_the_consumer_fills_it_in(
    db, sqlite_conn, monkeypatch
):
    """The upload writes a record; the queue consumer adds the real counts."""
    from fastapi.testclient import TestClient

    from keepfor.app import app
    from keepfor.consumer.processor import process_queue_batch

    env = StatusEnv(sqlite_conn)
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    client = TestClient(app)

    user = await register_user(db, "status@test.local", "password123")
    _, session_id = await login_user(db, "status@test.local", "password123")
    client.cookies.set("kfm_session", session_id)

    zip_bytes = _keep_zip(
        [
            _keep_note("First"),
            _keep_note("Second", "see https://example.com/a"),
            _keep_note("Third"),
        ]
    )
    resp = client.post(
        "/import",
        files={"file": ("takeout.zip", zip_bytes, "application/zip")},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # The record exists before the consumer runs: progress, not just a toast.
    records = await list_imports(db, user["id"])
    assert len(records) == 1
    record = records[0]
    assert record["status"] == "running"
    assert record["source"] == "keep"
    assert record["filename"] == "takeout.zip"
    assert record["notes_total"] == 3
    assert record["notes_processed"] == 0

    # The queued chunks carry the import id, so the consumer can report back.
    note_messages = [m for m in env.QUEUE.sent if "import_notes_batch" in m]
    assert note_messages
    assert all(m["import_id"] == record["id"] for m in note_messages)

    for msg in note_messages:
        await process_queue_batch(
            SimpleNamespace(
                messages=[
                    SimpleNamespace(body=msg, ack=lambda: None, retry=lambda: None)
                ]
            ),
            env,
        )

    record = (await list_imports(db, user["id"]))[0]
    assert record["notes_imported"] == 3
    assert record["notes_processed"] == 3
    assert record["urls_queued"] == 1
    assert record["status"] == "completed"
    assert record["progress"] == 100

    # The result is still there after a refresh.
    page = client.get("/settings")
    assert "Import complete" in page.text
    assert "3 notes imported" in page.text
    assert "1 link queued" in page.text


@pytest.mark.asyncio
async def test_failed_upload_is_persisted_as_a_failed_import(
    db, sqlite_conn, monkeypatch
):
    """A rejected upload leaves a failed record behind."""
    from fastapi.testclient import TestClient

    from keepfor.app import app

    env = StatusEnv(sqlite_conn)
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    client = TestClient(app)

    user = await register_user(db, "zipbomb@test.local", "password123")
    _, session_id = await login_user(db, "zipbomb@test.local", "password123")
    client.cookies.set("kfm_session", session_id)

    resp = client.post(
        "/import",
        files={"file": ("bomb.zip", b"PK\x03\x04not-a-zip", "application/zip")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings"

    records = await list_imports(db, user["id"])
    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert records[0]["error"]

    page = client.get("/settings")
    assert "Import failed" in page.text


@pytest.mark.asyncio
async def test_apply_chunk_counts_concurrent_chunks_reach_completed(db, monkeypatch):
    """Concurrent chunks that together reach notes_total must mark status completed."""
    import asyncio

    user = await register_user(db, "race@test.local", "password123")
    import_id = await create_import(db, user["id"], source="keep")
    await set_import_counts(
        db,
        user["id"],
        import_id,
        source="keep",
        notes_total=4,
        trashed_skipped=0,
        urls_found=0,
        attachments_not_imported=0,
        parse_errors=0,
    )

    orig_query_first = db.query_first
    read_count = 0
    both_read = asyncio.Event()

    async def synchronized_query_first(sql: str, params=()):
        nonlocal read_count
        res = await orig_query_first(sql, params)
        if "FROM import_status" in sql:
            read_count += 1
            if read_count >= 2:
                both_read.set()
            await asyncio.wait_for(both_read.wait(), timeout=2.0)
        return res

    monkeypatch.setattr(db, "query_first", synchronized_query_first)

    await asyncio.gather(
        apply_chunk_counts(
            db,
            user["id"],
            import_id,
            {"notes_imported": 2, "notes_processed": 2, "errors": []},
        ),
        apply_chunk_counts(
            db,
            user["id"],
            import_id,
            {"notes_imported": 2, "notes_processed": 2, "errors": []},
        ),
    )

    monkeypatch.setattr(db, "query_first", orig_query_first)
    record = await get_import(db, user["id"], import_id)
    assert record["notes_processed"] == 4
    assert record["notes_imported"] == 4
    assert record["status"] == "completed"


@pytest.mark.asyncio
async def test_live_status_polling_attributes_and_transition(
    db, sqlite_conn, monkeypatch
):
    """Running imports poll /imports/{id}/status; finished imports omit hx-trigger."""
    from fastapi.testclient import TestClient

    from keepfor.app import app

    env = StatusEnv(sqlite_conn)
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    client = TestClient(app)

    user = await register_user(db, "poll@test.local", "password123")
    _, session_id = await login_user(db, "poll@test.local", "password123")
    client.cookies.set("kfm_session", session_id)

    import_id = await create_import(
        db, user["id"], source="keep", filename="takeout.zip"
    )
    await set_import_counts(
        db,
        user["id"],
        import_id,
        source="keep",
        notes_total=10,
        trashed_skipped=0,
        urls_found=2,
        attachments_not_imported=1,
        parse_errors=0,
    )
    await apply_chunk_counts(
        db,
        user["id"],
        import_id,
        {"notes_imported": 4, "notes_processed": 4, "urls_queued": 1, "errors": []},
    )

    # Both Library and Settings show live progress with htmx polling and detail link.
    for path in ("/", "/settings"):
        page = client.get(path)
        assert page.status_code == 200
        assert f'hx-get="/imports/{import_id}/status"' in page.text
        assert 'hx-trigger="every 3s"' in page.text
        assert "4 of 10 notes processed" in page.text
        assert f'href="/imports/{import_id}"' in page.text

    # Partial endpoint while running includes the poll trigger.
    partial_running = client.get(f"/imports/{import_id}/status")
    assert partial_running.status_code == 200
    assert f'hx-get="/imports/{import_id}/status"' in partial_running.text
    assert 'hx-trigger="every 3s"' in partial_running.text
    assert "4 of 10 notes processed" in partial_running.text
    assert 'style="width: 40%"' in partial_running.text

    # Complete the import: partial flips to final counts and omits hx-trigger.
    await apply_chunk_counts(
        db,
        user["id"],
        import_id,
        {
            "notes_imported": 6,
            "notes_processed": 6,
            "urls_queued": 1,
            "urls_skipped": 2,
            "errors": [],
        },
    )
    partial_done = client.get(f"/imports/{import_id}/status")
    assert partial_done.status_code == 200
    assert "hx-trigger" not in partial_done.text
    assert "Import complete" in partial_done.text
    assert "10 notes imported" in partial_done.text
    assert "2 links queued" in partial_done.text
    assert "2 links skipped" in partial_done.text
    assert f'href="/imports/{import_id}"' in partial_done.text


@pytest.mark.asyncio
async def test_import_detail_queued_links_live_states_and_deep_links(
    db, sqlite_conn, monkeypatch
):
    """Detail view derives queued/extracting/ready/failed from linked items."""
    from fastapi.testclient import TestClient

    from keepfor.app import app
    from keepfor.models.items import save_item

    env = StatusEnv(sqlite_conn)
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    client = TestClient(app)

    user = await register_user(db, "states@test.local", "password123")
    user_id = user["id"]
    _, session_id = await login_user(db, "states@test.local", "password123")
    client.cookies.set("kfm_session", session_id)

    it_queued, _ = await save_item(db, env, user_id, "https://example.com/q")
    it_extracting, _ = await save_item(db, env, user_id, "https://example.com/e")
    it_ready, _ = await save_item(db, env, user_id, "https://example.com/r")
    it_failed, _ = await save_item(db, env, user_id, "https://example.com/f")

    await db.execute(
        "UPDATE items SET status = 'extracting' WHERE id = ? AND user_id = ?;",
        (it_extracting["id"], user_id),
    )
    await db.execute(
        "UPDATE items SET status = 'ok' WHERE id = ? AND user_id = ?;",
        (it_ready["id"], user_id),
    )
    await db.execute(
        "UPDATE items SET status = 'failed' WHERE id = ? AND user_id = ?;",
        (it_failed["id"], user_id),
    )

    import_id = await create_import(db, user_id, source="keep", filename="keep.zip")
    await set_import_counts(
        db,
        user_id,
        import_id,
        source="keep",
        notes_total=1,
        trashed_skipped=0,
        urls_found=4,
        attachments_not_imported=0,
        parse_errors=0,
    )
    await apply_chunk_counts(
        db,
        user_id,
        import_id,
        {"notes_imported": 1, "notes_processed": 1, "urls_queued": 4, "errors": []},
    )
    await record_import_items(
        db,
        user_id,
        import_id,
        [
            {
                "kind": "link",
                "state": "queued",
                "url": "https://example.com/q",
                "item_id": it_queued["id"],
            },
            {
                "kind": "link",
                "state": "queued",
                "url": "https://example.com/e",
                "item_id": it_extracting["id"],
            },
            {
                "kind": "link",
                "state": "queued",
                "url": "https://example.com/r",
                "item_id": it_ready["id"],
            },
            {
                "kind": "link",
                "state": "queued",
                "url": "https://example.com/f",
                "item_id": it_failed["id"],
            },
        ],
    )

    detail = await get_import_detail(db, user_id, import_id)
    assert detail is not None
    assert len(detail["queued_links"]) == detail["import"]["urls_queued"] == 4
    by_url = {r["url"]: r for r in detail["queued_links"]}
    assert by_url["https://example.com/q"]["live_state"] == "queued"
    assert by_url["https://example.com/e"]["live_state"] == "extracting"
    assert by_url["https://example.com/r"]["live_state"] == "ready"
    assert by_url["https://example.com/f"]["live_state"] == "failed"
    assert by_url["https://example.com/f"]["can_retry"] is True

    resp = client.get(f"/imports/{import_id}")
    assert resp.status_code == 200
    for item in (it_queued, it_extracting, it_ready, it_failed):
        assert f'href="/items/{item["id"]}"' in resp.text
    for state in ("queued", "extracting", "ready", "failed"):
        assert f'data-live-state="{state}"' in resp.text


@pytest.mark.asyncio
async def test_import_detail_all_skip_reasons_and_human_labels(
    db, sqlite_conn, monkeypatch
):
    """Every LinkSkipReason enum value has a label and is surfaced in detail view."""
    from fastapi.testclient import TestClient

    from keepfor.app import app

    assert set(LINK_SKIP_REASONS) == {
        "duplicate_in_file",
        "already_in_library",
        "plan_limit",
        "unsupported_scheme",
        "queue_failed",
        "import_limit",
    }
    for reason in LINK_SKIP_REASONS:
        assert SKIP_REASON_LABELS.get(reason)

    env = StatusEnv(sqlite_conn)
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    client = TestClient(app)

    user = await register_user(db, "reasons@test.local", "password123")
    user_id = user["id"]
    _, session_id = await login_user(db, "reasons@test.local", "password123")
    client.cookies.set("kfm_session", session_id)

    import_id = await create_import(db, user_id, source="keep", filename="keep.zip")
    await set_import_counts(
        db,
        user_id,
        import_id,
        source="keep",
        notes_total=1,
        trashed_skipped=0,
        urls_found=6,
        attachments_not_imported=1,
        parse_errors=0,
    )
    await apply_chunk_counts(
        db,
        user_id,
        import_id,
        {
            "notes_imported": 1,
            "notes_processed": 1,
            "urls_queued": 0,
            "urls_skipped": 5,
            "plan_skipped": 1,
            "errors": [],
        },
    )
    rows_to_insert = [
        {
            "kind": "link",
            "state": "skipped",
            "url": f"https://example.com/{r.value}",
            "reason": r.value,
        }
        for r in LinkSkipReason
    ]
    rows_to_insert.append(
        {
            "kind": "attachment",
            "state": "skipped",
            "reason": ATTACHMENT_REASON,
            "mime_or_ext": "image/png",
        }
    )
    await record_import_items(db, user_id, import_id, rows_to_insert)

    detail = await get_import_detail(db, user_id, import_id)
    assert detail is not None
    assert len(detail["skipped_links"]) == detail["import"]["urls_skipped"] == 5
    assert len(detail["plan_skipped_links"]) == detail["import"]["plan_skipped"] == 1
    assert (
        len(detail["attachments"]) == detail["import"]["attachments_not_imported"] == 1
    )

    resp = client.get(f"/imports/{import_id}")
    assert resp.status_code == 200
    for r in LinkSkipReason:
        assert r.value in resp.text
        assert SKIP_REASON_LABELS[r.value] in resp.text
    assert ATTACHMENT_REASON in resp.text
    assert "image/png" in resp.text


@pytest.mark.asyncio
async def test_import_retry_csrf_requeue_no_double_entitlement_and_partial(
    db, sqlite_conn, monkeypatch
):
    """Retry enforces CSRF, avoids double entitlement counting, and reports partial."""
    from fastapi.testclient import TestClient

    from keepfor.app import create_app
    from keepfor.models.items import save_item
    from keepfor.runtime import set_providers
    from keepfor.spi import Decision, Providers

    class CountingEntitlements:
        def __init__(self):
            self.import_recorded = 0
            self.save_recorded = 0

        async def check(self, tenant_id, action, qty=1):
            return Decision(allowed=True)

        async def record(self, tenant_id, action, qty=1):
            if action == "import":
                self.import_recorded += qty
            elif action == "save_item":
                self.save_recorded += qty

    counting = CountingEntitlements()
    providers = Providers(entitlements=counting).resolved()
    set_providers(providers)

    env = StatusEnv(sqlite_conn)
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    app = create_app(providers=providers)
    client = TestClient(app)

    user = await register_user(db, "retry@test.local", "password123")
    user_id = user["id"]
    _, session_id = await login_user(db, "retry@test.local", "password123")
    client.cookies.set("kfm_session", session_id)

    existing_failed, _ = await save_item(
        db, env, user_id, "https://example.com/existing-failed", check_entitlement=False
    )
    await db.execute(
        "UPDATE items SET status = 'failed', fail_reason = 'timeout' "
        "WHERE id = ? AND user_id = ?;",
        (existing_failed["id"], user_id),
    )

    import_id = await create_import(db, user_id, source="keep", filename="keep.zip")
    await set_import_counts(
        db,
        user_id,
        import_id,
        source="keep",
        notes_total=1,
        trashed_skipped=0,
        urls_found=3,
        attachments_not_imported=0,
        parse_errors=0,
    )
    await apply_chunk_counts(
        db,
        user_id,
        import_id,
        {
            "notes_imported": 1,
            "notes_processed": 1,
            "urls_queued": 1,
            "urls_skipped": 2,
            "errors": [],
        },
    )
    row_ids = await record_import_items(
        db,
        user_id,
        import_id,
        [
            {
                "kind": "link",
                "state": "queued",
                "url": "https://example.com/existing-failed",
                "item_id": existing_failed["id"],
            },
            {
                "kind": "link",
                "state": "skipped",
                "url": "https://example.com/queue-failed-no-item",
                "reason": LinkSkipReason.QUEUE_FAILED.value,
            },
            {
                "kind": "link",
                "state": "skipped",
                "url": "https://example.com/dup",
                "reason": LinkSkipReason.DUPLICATE_IN_FILE.value,
            },
        ],
    )
    row_existing_failed, row_queue_failed, row_dup = row_ids

    # 1. CSRF check: cross-origin POST is rejected with 403.
    csrf_resp = client.post(
        f"/imports/{import_id}/items/{row_existing_failed}/retry",
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert csrf_resp.status_code == 403
    csrf_all = client.post(
        f"/imports/{import_id}/retry",
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert csrf_all.status_code == 403

    # 2. Non-retryable row (duplicate_in_file) returns 409.
    not_retryable = client.post(
        f"/imports/{import_id}/items/{row_dup}/retry",
        follow_redirects=False,
    )
    assert not_retryable.status_code == 409

    # 3. Retrying existing failed item re-queues extraction without new entitlement.
    before_imports = counting.import_recorded
    before_saves = counting.save_recorded
    r1 = client.post(
        f"/imports/{import_id}/items/{row_existing_failed}/retry",
        follow_redirects=False,
    )
    assert r1.status_code == 303
    assert r1.headers["location"].endswith("?retry=ok")
    assert counting.import_recorded == before_imports
    assert counting.save_recorded == before_saves
    requeued_item = await db.query_first(
        "SELECT status FROM items WHERE id = ? AND user_id = ?;",
        (existing_failed["id"], user_id),
    )
    assert requeued_item["status"] == "queued"

    # 4. Retrying queue_failed row with no item saves a fresh item (1 import unit)
    # and updates import_status counts (urls_queued: 1->2, urls_skipped: 2->1).
    r2 = client.post(
        f"/imports/{import_id}/items/{row_queue_failed}/retry",
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert r2.headers["location"].endswith("?retry=ok")
    assert counting.import_recorded == before_imports + 1
    assert counting.save_recorded == 0
    updated_record = await get_import(db, user_id, import_id)
    assert updated_record["urls_queued"] == 2
    assert updated_record["urls_skipped"] == 1

    # 5. Partial bulk retry: 1 succeeds, 1 fails -> status == 'partial', never 'ok'.
    await db.execute(
        "UPDATE items SET status = 'failed' WHERE id = ? AND user_id = ?;",
        (existing_failed["id"], user_id),
    )
    extra_ids = await record_import_items(
        db,
        user_id,
        import_id,
        [
            {
                "kind": "link",
                "state": "skipped",
                "url": "https://example.com/will-fail-queue",
                "reason": LinkSkipReason.QUEUE_FAILED.value,
            }
        ],
    )
    send_calls = 0
    orig_send_batch = env.QUEUE.send_batch

    async def flaky_send_batch(batch):
        nonlocal send_calls
        send_calls += 1
        if send_calls >= 2:
            raise RuntimeError("queue down on second item")
        return await orig_send_batch(batch)

    monkeypatch.setattr(env.QUEUE, "send_batch", flaky_send_batch)
    bulk_res = await retry_all_failed_import_items(
        db, env, user_id, import_id, providers=providers
    )
    assert bulk_res["status"] == "partial"
    assert bulk_res["ok"] is False
    assert bulk_res["succeeded"] == 1
    assert bulk_res["failed"] == 1
    assert extra_ids[0]


@pytest.mark.asyncio
async def test_import_privacy_invariant_no_note_titles_bodies_or_paths(
    db, sqlite_conn, monkeypatch
):
    """Status/detail routes and import_items never store or expose note text/paths."""
    from fastapi.testclient import TestClient

    from keepfor.app import app
    from keepfor.consumer.processor import process_queue_batch

    env = StatusEnv(sqlite_conn)
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    client = TestClient(app)

    user = await register_user(db, "privacy@test.local", "password123")
    user_id = user["id"]
    _, session_id = await login_user(db, "privacy@test.local", "password123")
    client.cookies.set("kfm_session", session_id)

    sentinel_title = "SECRET_NOTE_TITLE_99182"
    sentinel_body = "SECRET_NOTE_BODY_77412"
    sentinel_att_name = "CONFIDENTIAL_SCAN_8821"
    sentinel_dir = "/home/alice/private-exports"

    zip_bytes = _keep_zip(
        [
            _keep_note(
                sentinel_title,
                f"{sentinel_body} https://example.com/private-check ftp://files.example.com/doc",
                attachments=[
                    {
                        "filePath": f"secret-folder/{sentinel_att_name}.png",
                        "mimetype": "image/png",
                    }
                ],
            )
        ]
    )

    resp = client.post(
        "/import",
        files={"file": (f"{sentinel_dir}/takeout.zip", zip_bytes, "application/zip")},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    note_messages = [m for m in env.QUEUE.sent if "import_notes_batch" in m]
    for msg in note_messages:
        await process_queue_batch(
            SimpleNamespace(
                messages=[
                    SimpleNamespace(body=msg, ack=lambda: None, retry=lambda: None)
                ]
            ),
            env,
        )

    records = await list_imports(db, user_id)
    assert len(records) == 1
    import_id = records[0]["id"]
    assert records[0]["filename"] == "takeout.zip"

    # Check raw DB rows in import_items: no note text or attachment filename stored.
    raw_rows = await db.query_all(
        "SELECT * FROM import_items WHERE user_id = ? AND import_id = ?;",
        (user_id, import_id),
    )
    serialized_db = json.dumps(raw_rows)
    for forbidden in (
        sentinel_title,
        sentinel_body,
        sentinel_att_name,
        sentinel_dir,
    ):
        assert forbidden not in serialized_db

    # Check HTTP responses for status partial, detail view, and settings.
    for url in (
        f"/imports/{import_id}/status",
        f"/imports/{import_id}",
        "/settings",
    ):
        page = client.get(url)
        assert page.status_code == 200
        for forbidden in (
            sentinel_title,
            sentinel_body,
            sentinel_att_name,
            sentinel_dir,
        ):
            assert forbidden not in page.text


@pytest.mark.asyncio
async def test_keep_regression_fixture_reimport_stability_and_500_cap(db, sqlite_conn):
    """Keep fixture with duplicates, already-in-library URL, PNG attachment,
    stable re-import, and 500 MAX_URLS_PER_IMPORT cap surfaced as import_limit."""
    from keepfor.defaults import default_scope
    from keepfor.models.items import save_item
    from keepfor.runtime import get_providers
    from keepfor.utils.importer import (
        MAX_URLS_PER_IMPORT,
        import_notes,
        parse_keep_takeout,
    )

    env = StatusEnv(sqlite_conn)
    sqlite_conn.execute("PRAGMA synchronous = OFF;")
    user = await register_user(db, "regression@test.local", "password123")
    user_id = user["id"]
    scope = default_scope(db, env, user_id)
    providers = get_providers()

    # Pre-save one URL so it is already in the user's library.
    await save_item(db, env, user_id, "https://example.com/in-library")

    fixture_notes = [
        _keep_note(
            "Fixture Note 1",
            "Links: https://example.com/new-one https://example.com/dup-url "
            "https://example.com/in-library mailto:test@example.com",
            attachments=[{"filePath": "photo.png", "mimetype": "image/png"}],
        ),
        _keep_note(
            "Fixture Note 2",
            "Duplicate across notes: https://example.com/dup-url",
        ),
    ]
    zip_bytes = _keep_zip(fixture_notes)
    parsed = parse_keep_takeout(zip_bytes)

    # First import
    imp1 = await create_import(db, user_id, source="keep", filename="fixture.zip")
    await set_import_counts(
        db,
        user_id,
        imp1,
        source="keep",
        notes_total=len(parsed),
        trashed_skipped=0,
        urls_found=sum(len(n["urls"]) for n in parsed),
        attachments_not_imported=sum(n["attachments_count"] for n in parsed),
        parse_errors=0,
    )
    s1 = await import_notes(
        db, env, user_id, parsed, providers, scope, "keep", import_id=imp1
    )
    assert s1["notes_imported"] == 2
    assert s1["duplicates_skipped"] == 0
    assert s1["urls_queued"] == 2
    assert s1["urls_skipped"] == 3
    assert s1["attachments_not_imported"] == 1

    d1 = await get_import_detail(db, user_id, imp1)
    assert d1 is not None
    assert len(d1["queued_links"]) == d1["import"]["urls_queued"] == 2
    assert [r["url"] for r in d1["queued_links"]] == [
        "https://example.com/new-one",
        "https://example.com/dup-url",
    ]
    assert len(d1["skipped_links"]) == d1["import"]["urls_skipped"] == 3
    assert d1["skip_reason_counts"][LinkSkipReason.DUPLICATE_IN_FILE.value] == 1
    assert d1["skip_reason_counts"][LinkSkipReason.ALREADY_IN_LIBRARY.value] == 1
    assert d1["skip_reason_counts"][LinkSkipReason.UNSUPPORTED_SCHEME.value] == 1
    assert len(d1["attachments"]) == d1["import"]["attachments_not_imported"] == 1
    assert d1["attachments"][0]["reason"] == ATTACHMENT_REASON
    assert d1["attachments"][0]["mime_or_ext"] == "image/png"

    # Re-import of the same Keep fixture is stable
    parsed2 = parse_keep_takeout(zip_bytes)
    imp2 = await create_import(db, user_id, source="keep", filename="fixture.zip")
    await set_import_counts(
        db,
        user_id,
        imp2,
        source="keep",
        notes_total=len(parsed2),
        trashed_skipped=0,
        urls_found=sum(len(n["urls"]) for n in parsed2),
        attachments_not_imported=sum(n["attachments_count"] for n in parsed2),
        parse_errors=0,
    )
    s2 = await import_notes(
        db, env, user_id, parsed2, providers, scope, "keep", import_id=imp2
    )
    assert s2["notes_imported"] == 0
    assert s2["duplicates_skipped"] == 2
    assert s2["urls_queued"] == 0
    assert s2["urls_skipped"] == 0
    assert s2["attachments_not_imported"] == 1
    d2 = await get_import_detail(db, user_id, imp2)
    assert len(d2["queued_links"]) == d2["import"]["urls_queued"] == 0
    assert len(d2["skipped_links"]) == d2["import"]["urls_skipped"] == 0
    assert len(d2["attachments"]) == d2["import"]["attachments_not_imported"] == 1

    # 500 MAX_URLS_PER_IMPORT cap is surfaced as import_limit skips
    cap_user = await register_user(
        db, "cap500@test.local", "password123", allow_public_signups=True
    )
    cap_user_id = cap_user["id"]
    cap_scope = default_scope(db, env, cap_user_id)
    many_urls = " ".join(
        f"https://example.com/cap-{i}" for i in range(MAX_URLS_PER_IMPORT + 5)
    )
    cap_parsed = parse_keep_takeout(_keep_zip([_keep_note("Cap Note", many_urls)]))
    imp_cap = await create_import(db, cap_user_id, source="keep", filename="cap.zip")
    await set_import_counts(
        db,
        cap_user_id,
        imp_cap,
        source="keep",
        notes_total=1,
        trashed_skipped=0,
        urls_found=MAX_URLS_PER_IMPORT,
        attachments_not_imported=0,
        parse_errors=0,
    )
    s_cap = await import_notes(
        db,
        env,
        cap_user_id,
        cap_parsed,
        providers,
        cap_scope,
        "keep",
        import_id=imp_cap,
    )
    assert s_cap["urls_queued"] == MAX_URLS_PER_IMPORT
    assert s_cap["urls_skipped"] == 5
    d_cap = await get_import_detail(db, cap_user_id, imp_cap)
    assert (
        len(d_cap["queued_links"])
        == d_cap["import"]["urls_queued"]
        == MAX_URLS_PER_IMPORT
    )
    assert len(d_cap["skipped_links"]) == d_cap["import"]["urls_skipped"] == 5
    assert d_cap["skip_reason_counts"][LinkSkipReason.IMPORT_LIMIT.value] == 5
    assert all(
        r["reason"] == LinkSkipReason.IMPORT_LIMIT.value for r in d_cap["skipped_links"]
    )
    assert await list_import_items(db, cap_user_id, imp_cap)
