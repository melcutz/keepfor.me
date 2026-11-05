# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for export download filenames including app name and UTC date (#380)."""

import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from keepfor import app as app_module
from keepfor.auth.service import login_user, register_user


class FakeQueue:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, message: dict) -> None:
        self.sent.append(message)


class MockEnv:
    def __init__(self):
        self.DB = None
        self.QUEUE = FakeQueue()
        self.AI = None
        self.VECTORIZE = None
        self.BUCKET = None


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr("keepfor.deps.get_db", lambda request: db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda request: MockEnv())
    return TestClient(app_module.app)


@pytest.fixture
async def auth_user(db):
    await register_user(db, "exportuser@test.local", "password123")
    _, session_id = await login_user(db, "exportuser@test.local", "password123")
    return {"session": session_id}


@pytest.mark.asyncio
async def test_export_filename_matches_pattern_with_app_name_and_utc_date(
    client, db, auth_user
):
    """Content-Disposition filename includes 'keepfor' and YYYY-MM-DD date."""
    client.cookies["kfm_session"] = auth_user["session"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # JSON export (default)
    resp_json = client.get("/api/export")
    assert resp_json.status_code == 200
    cd_json = resp_json.headers.get("content-disposition", "")
    assert re.search(
        r'filename="?keepfor.*?\d{4}-\d{2}-\d{2}\.json"?',
        cd_json,
        re.IGNORECASE,
    )
    assert today in cd_json

    # JSON export (explicit format)
    resp_json_explicit = client.get("/api/export?format=json")
    assert resp_json_explicit.status_code == 200
    cd_json_explicit = resp_json_explicit.headers.get("content-disposition", "")
    assert re.search(
        r'filename="?keepfor.*?\d{4}-\d{2}-\d{2}\.json"?',
        cd_json_explicit,
        re.IGNORECASE,
    )
    assert today in cd_json_explicit

    # HTML export
    resp_html = client.get("/api/export?format=html")
    assert resp_html.status_code == 200
    cd_html = resp_html.headers.get("content-disposition", "")
    assert re.search(
        r'filename="?keepfor.*?\d{4}-\d{2}-\d{2}\.html"?',
        cd_html,
        re.IGNORECASE,
    )
    assert today in cd_html
