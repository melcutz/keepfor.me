# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Mem export importer unit and integration tests.

Tests export detection, collection index parsing into tags, attachment counting,
source and tag invariants, route dispatching, and duplicate handling.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from keepfor.app import app
from keepfor.auth.service import login_user, register_user
from keepfor.models.imports import list_imports
from keepfor.utils.importer import (
    ZipBombError,
    import_notes,
    is_mem_zip,
    parse_markdown_zip,
    parse_mem_zip,
)
from tests.fakes import FakeEnv, FakeQueue
from tests.mem_export_fixture import make_mem_export_zip


class MockScope:
    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id


class MockProviders:
    def __init__(self):
        self.entitlements = MockEntitlements()


class MockEntitlements:
    async def check(self, tenant_id: str, feature: str, qty: int = 1):
        class Decision:
            allowed = True
            reason = ""
            upgrade_url = None

        return Decision()

    async def record(self, tenant_id: str, feature: str, qty: int = 1):
        pass


def test_is_mem_zip_detection_root_and_nested():
    """Test is_mem_zip correctly detects root and nested Mem export archives."""
    # Root layout
    root_zip = make_mem_export_zip()
    with zipfile.ZipFile(io.BytesIO(root_zip)) as zf:
        is_mem, root = is_mem_zip(zf)
        assert is_mem is True
        assert root == ""

    # Nested layout
    nested_zip = make_mem_export_zip(outer_folder="Mem Export 2026-05-10")
    with zipfile.ZipFile(io.BytesIO(nested_zip)) as zf:
        is_mem, root = is_mem_zip(zf)
        assert is_mem is True
        assert root == "Mem Export 2026-05-10/"

    # Ignored __MACOSX and dotfiles
    extra_files = {
        "__MACOSX/._notes": b"garbage",
        "__MACOSX/notes/._note.md": b"garbage",
        ".DS_Store": b"garbage",
    }
    macos_zip = make_mem_export_zip(extra_files=extra_files)
    with zipfile.ZipFile(io.BytesIO(macos_zip)) as zf:
        is_mem, root = is_mem_zip(zf)
        assert is_mem is True
        assert root == ""


def test_is_mem_zip_rejects_generic_markdown_zip():
    """Generic markdown zip and zips with only notes/ are rejected."""
    # Generic markdown archive
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("docs/readme.md", "# Readme")
        zf.writestr("index.md", "# Index")
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        is_mem, root = is_mem_zip(zf)
        assert is_mem is False
        assert root == ""

    # Zip with only notes/ (missing collections/, attachments/, and export-report.md)
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w") as zf:
        zf.writestr("notes/note1.md", "# Note 1")
    with zipfile.ZipFile(io.BytesIO(buf2.getvalue())) as zf:
        is_mem, root = is_mem_zip(zf)
        assert is_mem is False
        assert root == ""

    # Zip with multiple top-level folders
    buf3 = io.BytesIO()
    with zipfile.ZipFile(buf3, "w") as zf:
        zf.writestr("folderA/notes/n1.md", "# N1")
        zf.writestr("folderB/notes/n2.md", "# N2")
        zf.writestr("folderA/export-report.md", "# Report")
    with zipfile.ZipFile(io.BytesIO(buf3.getvalue())) as zf:
        is_mem, root = is_mem_zip(zf)
        assert is_mem is False
        assert root == ""


def test_parse_mem_zip_notes_only_skips_indexes_and_report():
    """Only notes/**/*.md are parsed; collections/ and export-report.md are skipped."""
    zip_bytes = make_mem_export_zip()
    stats: dict = {}
    notes = parse_mem_zip(zip_bytes, stats=stats)

    assert len(notes) == 4
    source_ids = {n["source_id"] for n in notes}
    assert source_ids == {
        "notes/project-alpha.md",
        "notes/weekly-sync.md",
        "notes/architecture.md",
        "notes/quarterly review.md",
    }

    assert "export-report.md" not in source_ids
    assert not any("collections/" in sid for sid in source_ids)
    assert stats.get("export_report_skipped") == 1
    assert stats.get("collections_skipped") == 2


def test_parse_mem_zip_collections_become_tags():
    """Collection membership is converted to tags (including multi-collection notes)."""
    zip_bytes = make_mem_export_zip()
    notes = parse_mem_zip(zip_bytes)
    by_source = {n["source_id"]: n for n in notes}

    # "quarterly review.md" sits in both "Projects.md" and "Company Reviews.md"
    qr_note = by_source["notes/quarterly review.md"]
    assert "projects" in qr_note["tags"]
    assert "company reviews" in qr_note["tags"]
    assert "from-mem" in qr_note["tags"]
    assert "finance" in qr_note["tags"]

    # "project-alpha.md" is in "Projects.md"
    pa_note = by_source["notes/project-alpha.md"]
    assert "projects" in pa_note["tags"]

    # "architecture.md" is in "Company Reviews.md"
    arch_note = by_source["notes/architecture.md"]
    assert "company reviews" in arch_note["tags"]


def test_parse_mem_zip_front_matter_and_fallback():
    """Tolerant front matter parsing extracts title and date; falls back cleanly."""
    zip_bytes = make_mem_export_zip()
    notes = parse_mem_zip(zip_bytes)
    by_source = {n["source_id"]: n for n in notes}

    pa_note = by_source["notes/project-alpha.md"]
    assert pa_note["title"] == "Project Alpha Overview"
    assert pa_note["created_at"] == "2026-05-10T09:30:00Z"
    assert "project" in pa_note["tags"]
    assert "roadmap" in pa_note["tags"]
    assert "planning" in pa_note["tags"]

    ws_note = by_source["notes/weekly-sync.md"]
    assert ws_note["title"] == "Weekly Team Sync"
    assert "Transcript" in ws_note["body"]
    assert "Speaker 1" in ws_note["body"]


def test_parse_mem_zip_source_and_tag_invariants():
    """Every note gets source='mem', 'from-mem' tag, and stable source_id."""
    for outer in (None, "Mem Export 2026-05-10"):
        zip_bytes = make_mem_export_zip(outer_folder=outer)
        notes = parse_mem_zip(zip_bytes)
        assert len(notes) == 4
        for note in notes:
            assert note["source"] == "mem"
            assert note["source_id"].startswith("notes/")
            assert not note["source_id"].startswith("Mem Export")
            assert "from-mem" in note["tags"]
            assert "markdown" not in note["tags"]
            assert "notes" not in note["tags"]


def test_parse_mem_zip_attachment_reference_counting():
    """Attachment references are counted and categorized without reading payloads."""
    zip_bytes = make_mem_export_zip()
    stats: dict = {}
    notes = parse_mem_zip(zip_bytes, stats=stats)
    by_source = {n["source_id"]: n for n in notes}

    arch_note = by_source["notes/architecture.md"]
    assert arch_note["attachments_count"] == 2
    assert "![diagram](../attachments/diagram.png)" in arch_note["body"]
    assert "[Q3 deck](../attachments/q3-deck.pdf)" in arch_note["body"]

    assert stats.get("attachments_not_imported") == 2
    types = stats.get("attachments_by_type", {})
    assert types.get("png") == 1
    assert types.get("pdf") == 1


def test_parse_mem_zip_empty_notes_folder():
    """Archive with empty notes folder returns empty list."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("notes/", "")
        zf.writestr("collections/col1.md", "# Col1\n")
        zf.writestr("export-report.md", "# Report\n")
    notes = parse_mem_zip(buf.getvalue())
    assert notes == []


def test_parse_mem_zip_corrupt_zip_raises_or_empty(monkeypatch):
    """Corrupt zip raises BadZipFile; zip bomb raises ZipBombError."""
    from keepfor.utils import importer

    corrupt_bytes = b"not a valid zip file format"
    with pytest.raises(zipfile.BadZipFile):
        parse_mem_zip(corrupt_bytes)

    monkeypatch.setattr(importer, "ZIP_MAX_TOTAL_BYTES", 100)
    zip_bytes = make_mem_export_zip()
    with pytest.raises(ZipBombError, match="uncompressed"):
        parse_mem_zip(zip_bytes)


@pytest.mark.asyncio
async def test_import_route_mem_zip_dispatch(db, monkeypatch):
    """POST /import dispatches Mem zip to parse_mem_zip and queues notes."""
    env = FakeEnv(db=db, queue=FakeQueue())
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    client = TestClient(app)

    user = await register_user(db, "mem_route@test.local", "password123")
    _, session_id = await login_user(db, "mem_route@test.local", "password123")
    client.cookies.set("kfm_session", session_id)

    zip_bytes = make_mem_export_zip()
    resp = client.post(
        "/import",
        files={"file": ("mem-export.zip", zip_bytes, "application/zip")},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    records = await list_imports(db, user["id"])
    assert len(records) == 1
    record = records[0]
    assert record["status"] == "running"
    assert record["source"] == "mem"
    assert record["filename"] == "mem-export.zip"
    assert record["notes_total"] == 4
    assert record["attachments_not_imported"] == 2

    note_messages = [m for m in env.QUEUE.sent if "import_notes_batch" in m]
    assert len(note_messages) == 1
    batch = note_messages[0]["import_notes_batch"]
    assert len(batch) == 4
    assert note_messages[0]["source"] == "mem"
    for note in batch:
        assert note["source"] == "mem"
        assert "from-mem" in note["tags"]


@pytest.mark.asyncio
async def test_import_notes_mem_deduplication(db):
    """Re-importing Mem notes deduplicates based on source and source_id."""
    user = await register_user(db, "mem_dedupe@test.local", "password123")
    user_id = user["id"]

    providers = MockProviders()
    scope = MockScope(user_id)
    env = FakeEnv(db=db, queue=FakeQueue())

    zip_bytes = make_mem_export_zip()
    parsed = parse_mem_zip(zip_bytes)

    summary1 = await import_notes(db, env, user_id, parsed, providers, scope, "mem")
    assert summary1["notes_imported"] == 4
    assert summary1["duplicates_skipped"] == 0

    summary2 = await import_notes(db, env, user_id, parsed, providers, scope, "mem")
    assert summary2["notes_imported"] == 0
    assert summary2["duplicates_skipped"] == 4


def test_generic_markdown_zip_unchanged():
    """Generic markdown zip is not recognized as Mem and maintains existing behavior."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("docs/guide.md", "# Guide\n\nSome documentation.")
        zf.writestr("readme.md", "# Readme\n\nProject overview.")
    data = buf.getvalue()

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        is_mem, root = is_mem_zip(zf)
        assert is_mem is False
        assert root == ""

    parsed = parse_markdown_zip(data)
    assert len(parsed) == 2
    for note in parsed:
        assert note["source"] == "markdown"
        assert "markdown" in note["tags"]
        assert "from-mem" not in note["tags"]


def _import_summary(resp) -> dict:
    """Parse the import summary cookie of an /import redirect."""
    set_cookie = resp.headers.get("set-cookie", "")
    raw = set_cookie.split("import_summary=", 1)[1].split(";", 1)[0]
    decoded = (
        raw.strip('"').replace("\\054", ",").replace("\\073", ";").replace('\\"', '"')
    )
    return json.loads(decoded)


@pytest.mark.asyncio
async def test_import_route_mem_parse_error_no_markdown_fallback(db, monkeypatch):
    """Mem parse error does not fall back to generic Markdown parser."""
    env = FakeEnv(db=db, queue=FakeQueue())
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    client = TestClient(app)

    user = await register_user(db, "mem_err@test.local", "password123")
    _, session_id = await login_user(db, "mem_err@test.local", "password123")
    client.cookies.set("kfm_session", session_id)

    markdown_called = False

    def fake_parse_markdown_zip(raw, stats=None):
        nonlocal markdown_called
        markdown_called = True
        return []

    def fake_parse_mem_zip(raw, stats=None):
        raise ValueError("Corrupt Mem export")

    monkeypatch.setattr(
        "keepfor.routes.settings.parse_markdown_zip", fake_parse_markdown_zip
    )
    monkeypatch.setattr("keepfor.routes.settings.parse_mem_zip", fake_parse_mem_zip)

    zip_bytes = make_mem_export_zip()
    resp = client.post(
        "/import",
        files={"file": ("mem-export.zip", zip_bytes, "application/zip")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/settings")

    assert markdown_called is False

    # No notes were queued or imported
    note_messages = [m for m in env.QUEUE.sent if "import_notes_batch" in m]
    assert len(note_messages) == 0

    items = await db.query_all("SELECT * FROM items WHERE user_id = ?;", (user["id"],))
    assert len(items) == 0

    records = await list_imports(db, user["id"])
    assert len(records) == 1
    record = records[0]
    assert record["status"] == "failed"
    expected_msg = "The Mem export could not be read; nothing was imported."
    assert record["error"] == expected_msg

    summary = _import_summary(resp)
    assert summary["error"] == expected_msg


@pytest.mark.asyncio
async def test_import_route_non_mem_zip_still_falls_back_to_markdown(db, monkeypatch):
    """Non-Mem zip upload still falls back to generic Markdown parser."""
    env = FakeEnv(db=db, queue=FakeQueue())
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: env)
    client = TestClient(app)

    user = await register_user(db, "markdown_fallback@test.local", "password123")
    _, session_id = await login_user(db, "markdown_fallback@test.local", "password123")
    client.cookies.set("kfm_session", session_id)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("docs/guide.md", "# Guide\n\nSome documentation.")
        zf.writestr("notes/simple.md", "# Simple\n\nJust a simple note.")
    data = buf.getvalue()

    resp = client.post(
        "/import",
        files={"file": ("markdown-export.zip", data, "application/zip")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/")

    note_messages = [m for m in env.QUEUE.sent if "import_notes_batch" in m]
    assert len(note_messages) == 1
    batch = note_messages[0]["import_notes_batch"]
    assert len(batch) == 2
    assert note_messages[0]["source"] == "markdown"
    for note in batch:
        assert note["source"] == "markdown"
        assert "markdown" in note["tags"]
        assert "from-mem" not in note["tags"]

    records = await list_imports(db, user["id"])
    assert len(records) == 1
    assert records[0]["status"] == "running"
    assert records[0]["source"] == "markdown"
