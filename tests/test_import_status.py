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
    MAX_IMPORT_ERRORS,
    apply_chunk_counts,
    create_import,
    fail_import,
    get_import,
    list_imports,
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
            "errors": ["note: OperationalError: database is locked"],
        },
    )
    record = await get_import(db, user["id"], import_id)
    assert record["notes_imported"] == 3
    assert record["notes_processed"] == 4
    assert record["urls_queued"] == 2
    assert record["parse_errors"] == 1
    assert record["status"] == "completed"
    assert record["progress"] == 100
    assert record["errors"] == ["note: OperationalError: database is locked"]


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
