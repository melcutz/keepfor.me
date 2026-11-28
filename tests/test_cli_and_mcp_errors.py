# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for CLI exit codes and MCP structured errors (issue #156)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from keepfor.app import app
from keepfor.auth.service import create_pat, register_user
from keepfor.cli import EXIT_AUTH, EXIT_GENERAL, EXIT_OK, EXIT_USAGE, run_cli
from keepfor.mcp.server import process_mcp_request


class FakeQueue:
    """In-test queue."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, message: dict) -> None:
        self.sent.append(message)


class MockEnv:
    """Mock Cloudflare environment."""

    def __init__(self):
        self.DB = None
        self.QUEUE = FakeQueue()
        self.AI = None
        self.VECTORIZE = None
        self.BUCKET = None


# ==============================================================================
# CLI Tests
# ==============================================================================


def test_cli_help(capsys):
    """CLI --help outputs usage, documents exit codes 0-3, and exits 0."""
    with pytest.raises(SystemExit) as excinfo:
        run_cli(["--help"])
    assert excinfo.value.code == EXIT_OK
    captured = capsys.readouterr()
    assert "0: Success (OK)" in captured.out
    assert "1: General failure" in captured.out
    assert "2: Usage or validation error" in captured.out
    assert "3: Authentication error" in captured.out
    assert captured.err == ""


def test_cli_missing_command(capsys):
    """Running CLI without command exits 2 with usage error on stderr."""
    with pytest.raises(SystemExit) as excinfo:
        run_cli([])
    assert excinfo.value.code == EXIT_USAGE
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert captured.out == ""


def test_cli_missing_token(capsys, monkeypatch):
    """Running CLI without token exits 3 with auth error on stderr."""
    monkeypatch.delenv("KEEPFOR_TOKEN", raising=False)
    code = run_cli(["save", "https://example.com/test", "--token", ""])
    assert code == EXIT_AUTH
    captured = capsys.readouterr()
    assert "missing authentication token" in captured.err
    assert captured.out == ""


def test_cli_failing_save_invalid_url(capsys):
    """Failing save due to invalid URL exits 2 with error on stderr."""
    code = run_cli(
        ["save", "not-a-valid-url", "--token", "test-token", "--url", "http://test"]
    )
    assert code == EXIT_USAGE
    captured = capsys.readouterr()
    assert "invalid URL" in captured.err
    assert captured.out == ""


def test_cli_failing_save_server_400(capsys):
    """Failing save when server returns 400 exits 2 with validation error on stderr."""
    http_error = urllib.error.HTTPError(
        url="http://test/api/save",
        code=400,
        msg="Bad Request",
        hdrs=None,  # type: ignore
        fp=io.BytesIO(b'{"detail": "Invalid URL"}'),
    )
    with patch("urllib.request.urlopen", side_effect=http_error):
        code = run_cli(
            [
                "save",
                "https://example.com/valid",
                "--token",
                "test-token",
                "--url",
                "http://test",
            ]
        )
    assert code == EXIT_USAGE
    captured = capsys.readouterr()
    assert "validation failed (HTTP 400): Invalid URL" in captured.err
    assert captured.out == ""


def test_cli_bad_token_401(capsys):
    """Bad token resulting in HTTP 401 exits 3 with auth error on stderr."""
    http_error = urllib.error.HTTPError(
        url="http://test/api/save",
        code=401,
        msg="Unauthorized",
        hdrs=None,  # type: ignore
        fp=io.BytesIO(b'{"detail": "Invalid authentication token"}'),
    )
    with patch("urllib.request.urlopen", side_effect=http_error):
        code = run_cli(
            [
                "save",
                "https://example.com/valid",
                "--token",
                "bad-token",
                "--url",
                "http://test",
            ]
        )
    assert code == EXIT_AUTH
    captured = capsys.readouterr()
    assert (
        "authentication failed (HTTP 401): Invalid authentication token" in captured.err
    )
    assert captured.out == ""


def test_cli_bad_token_403(capsys):
    """Forbidden token resulting in HTTP 403 exits 3 with auth error on stderr."""
    http_error = urllib.error.HTTPError(
        url="http://test/api/save",
        code=403,
        msg="Forbidden",
        hdrs=None,  # type: ignore
        fp=io.BytesIO(b'{"detail": "Forbidden"}'),
    )
    with patch("urllib.request.urlopen", side_effect=http_error):
        code = run_cli(
            [
                "save",
                "https://example.com/valid",
                "--token",
                "bad-token",
                "--url",
                "http://test",
            ]
        )
    assert code == EXIT_AUTH
    captured = capsys.readouterr()
    assert "authentication failed (HTTP 403): Forbidden" in captured.err
    assert captured.out == ""


def test_cli_server_5xx(capsys):
    """Server 500 error exits 1 with server error on stderr."""
    http_error = urllib.error.HTTPError(
        url="http://test/api/save",
        code=500,
        msg="Internal Server Error",
        hdrs=None,  # type: ignore
        fp=io.BytesIO(b'{"detail": "Database connection lost"}'),
    )
    with patch("urllib.request.urlopen", side_effect=http_error):
        code = run_cli(
            [
                "save",
                "https://example.com/valid",
                "--token",
                "valid-token",
                "--url",
                "http://test",
            ]
        )
    assert code == EXIT_GENERAL
    captured = capsys.readouterr()
    assert "server error (HTTP 500): Database connection lost" in captured.err
    assert captured.out == ""


def test_cli_network_error(capsys):
    """Network connection failure exits 1 with network error on stderr."""
    url_error = urllib.error.URLError("Connection refused")
    with patch("urllib.request.urlopen", side_effect=url_error):
        code = run_cli(
            [
                "save",
                "https://example.com/valid",
                "--token",
                "valid-token",
                "--url",
                "http://test",
            ]
        )
    assert code == EXIT_GENERAL
    captured = capsys.readouterr()
    assert "network error: Connection refused" in captured.err
    assert captured.out == ""


def test_cli_save_success(capsys):
    """Successful save exits 0 with message on stdout and empty stderr."""
    mock_resp = MagicMock()
    mock_resp.status = 202
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.read.return_value = json.dumps(
        {
            "id": "item-123",
            "url": "https://example.com/article",
            "title": "Example Article",
            "status": "queued",
            "is_new": True,
        }
    ).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        code = run_cli(
            [
                "save",
                "https://example.com/article",
                "--token",
                "valid-token",
                "--tag",
                "reading,tech",
                "--title",
                "Example Article",
            ]
        )
    assert code == EXIT_OK
    captured = capsys.readouterr()
    assert "Saved: Example Article (id: item-123)" in captured.out
    assert captured.err == ""


def test_cli_save_json_success(capsys):
    """Successful save with --json outputs valid JSON to stdout."""
    payload = {
        "id": "item-123",
        "url": "https://example.com/article",
        "title": "Example Article",
        "status": "queued",
        "is_new": True,
    }
    mock_resp = MagicMock()
    mock_resp.status = 202
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        code = run_cli(
            [
                "save",
                "https://example.com/article",
                "--token",
                "valid-token",
                "--json",
            ]
        )
    assert code == EXIT_OK
    captured = capsys.readouterr()
    assert json.loads(captured.out) == payload
    assert captured.err == ""


def test_cli_search_success(capsys):
    """Successful search exits 0 with results on stdout."""
    payload = [
        {"id": "item-1", "title": "Article One", "url": "https://example.com/1"},
        {"id": "item-2", "title": "Article Two", "url": "https://example.com/2"},
    ]
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        code = run_cli(
            [
                "search",
                "python",
                "--token",
                "valid-token",
                "--limit",
                "5",
                "--mode",
                "hybrid",
            ]
        )
    assert code == EXIT_OK
    captured = capsys.readouterr()
    assert "- [item-1] Article One (https://example.com/1)" in captured.out
    assert "- [item-2] Article Two (https://example.com/2)" in captured.out
    assert captured.err == ""


def test_cli_get_success(capsys):
    """Successful get item exits 0 with details on stdout."""
    payload = {
        "id": "item-1",
        "title": "Article One",
        "url": "https://example.com/1",
        "status": "ok",
    }
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        code = run_cli(["get", "item-1", "--token", "valid-token"])
    assert code == EXIT_OK
    captured = capsys.readouterr()
    assert "Article One (id: item-1)" in captured.out
    assert "URL: https://example.com/1" in captured.out
    assert captured.err == ""


def test_cli_get_not_found(capsys):
    """Item not found (HTTP 404) exits 1 with error on stderr."""
    http_error = urllib.error.HTTPError(
        url="http://test/api/items/unknown",
        code=404,
        msg="Not Found",
        hdrs=None,  # type: ignore
        fp=io.BytesIO(b'{"detail": "Item not found"}'),
    )
    with patch("urllib.request.urlopen", side_effect=http_error):
        code = run_cli(["get", "unknown", "--token", "valid-token"])
    assert code == EXIT_GENERAL
    captured = capsys.readouterr()
    assert "not found (HTTP 404): Item not found" in captured.err
    assert captured.out == ""


def test_cli_list_success(capsys):
    """Successful list items exits 0 with items on stdout."""
    payload = [
        {"id": "item-1", "title": "Article One", "url": "https://example.com/1"},
    ]
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        code = run_cli(["list", "--token", "valid-token", "--limit", "10"])
    assert code == EXIT_OK
    captured = capsys.readouterr()
    assert "- [item-1] Article One (https://example.com/1)" in captured.out
    assert captured.err == ""


def test_cli_subprocess_invocation():
    """Verify CLI works via python -m keepfor subprocess."""
    res = subprocess.run(
        [sys.executable, "-m", "keepfor", "--help"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "0: Success (OK)" in res.stdout
    assert res.stderr == ""

    # Invalid URL validation failure in subprocess
    res_err = subprocess.run(
        [sys.executable, "-m", "keepfor", "save", "bad-url", "--token", "test"],
        capture_output=True,
        text=True,
    )
    assert res_err.returncode == EXIT_USAGE
    assert "error: invalid URL" in res_err.stderr
    assert res_err.stdout == ""


# ==============================================================================
# MCP Tests
# ==============================================================================


@pytest.fixture
async def mcp_test_user(db):
    """User and environment for MCP testing."""
    user = await register_user(db, "mcp_tester@example.com", "password123")
    pat = await create_pat(db, user["id"], "Test PAT")
    return user, MockEnv(), db, pat["token"]


@pytest.mark.asyncio
async def test_mcp_save_failing_invalid_url(mcp_test_user):
    """MCP save_url with invalid URL returns isError: true with stable code."""
    user, env, db, _ = mcp_test_user
    req = {
        "jsonrpc": "2.0",
        "id": "req-1",
        "method": "tools/call",
        "params": {
            "name": "save_url",
            "arguments": {"url": "not-a-valid-url"},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    assert res is not None
    assert res.get("isError") is True
    assert "error" in res
    assert res["error"]["isError"] is True
    assert res["error"]["data"]["type"] == "invalid_arguments"
    assert res["error"]["data"]["code"] == "invalid_arguments"
    assert "valid HTTP or HTTPS url" in res["error"]["message"]
    # Verify no stack trace or secrets leaked
    assert "Traceback" not in res["error"]["message"]


@pytest.mark.asyncio
async def test_mcp_save_failing_missing_url(mcp_test_user):
    """MCP save_url with missing URL returns isError: true with stable code."""
    user, env, db, _ = mcp_test_user
    req = {
        "jsonrpc": "2.0",
        "id": "req-2",
        "method": "tools/call",
        "params": {
            "name": "save_url",
            "arguments": {},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    assert res is not None
    assert res.get("isError") is True
    assert res["error"]["isError"] is True
    assert res["error"]["data"]["type"] == "invalid_arguments"
    assert res["error"]["data"]["code"] == "invalid_arguments"


def test_mcp_bad_token(db, monkeypatch):
    """Sending a request to /api/mcp with an invalid Bearer token returns 401."""

    def mock_get_db(request):
        return db

    monkeypatch.setattr("keepfor.deps.get_db", mock_get_db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda req: MockEnv())

    client = TestClient(app)
    req_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }
    res = client.post(
        "/api/mcp",
        json=req_body,
        headers={"Authorization": "Bearer kfm_live_invalidtoken123456789"},
    )
    assert res.status_code == 401
    assert "Authentication required" in res.json().get("detail", "")


@pytest.mark.asyncio
async def test_mcp_server_5xx_tool_exception(mcp_test_user):
    """Unhandled tool exception returns isError: true and internal_error."""
    user, env, db, _ = mcp_test_user
    req = {
        "jsonrpc": "2.0",
        "id": "req-3",
        "method": "tools/call",
        "params": {
            "name": "save_url",
            "arguments": {"url": "https://example.com/valid"},
        },
    }

    with patch(
        "keepfor.mcp.server.save_item",
        side_effect=RuntimeError("Storage pool failure: disk error at /dev/sda1"),
    ):
        res = await process_mcp_request(req, db, env, user)

    assert res is not None
    assert res.get("isError") is True
    assert "error" in res
    assert res["error"]["isError"] is True
    assert res["error"]["code"] == -32603
    assert res["error"]["data"]["type"] == "internal_error"
    assert res["error"]["data"]["code"] == "internal_error"
    # Ensure safe message with no internal stack trace or hardware leak
    assert res["error"]["message"] == "Internal server error"
    assert "disk error" not in res["error"]["message"]
    assert "Traceback" not in res["error"]["message"]


def test_mcp_endpoint_500_structured_error(db, monkeypatch):
    """When /api/mcp crashes, returns 500 with structured JSON error."""

    def mock_get_db(request):
        return db

    monkeypatch.setattr("keepfor.deps.get_db", mock_get_db)
    monkeypatch.setattr("keepfor.deps.get_env_from_request", lambda req: MockEnv())

    # Create valid user and PAT
    import asyncio

    user = asyncio.run(register_user(db, "endpoint500@example.com", "password123"))
    pat = asyncio.run(create_pat(db, user["id"], "pat500"))

    client = TestClient(app)
    req_body = {
        "jsonrpc": "2.0",
        "id": "req-crash",
        "method": "tools/call",
        "params": {"name": "save_url", "arguments": {"url": "https://example.com"}},
    }

    with patch(
        "keepfor.routes.api.process_mcp_request",
        side_effect=RuntimeError("Unexpected server crash"),
    ):
        res = client.post(
            "/api/mcp",
            json=req_body,
            headers={"Authorization": f"Bearer {pat['token']}"},
        )

    assert res.status_code == 500
    data = res.json()
    assert data.get("isError") is True
    assert "error" in data
    assert data["error"]["code"] == -32603
    assert data["error"]["message"] == "Internal server error"
    assert data["error"]["data"]["code"] == "internal_error"


@pytest.mark.asyncio
async def test_mcp_insufficient_scope_structured_error(db):
    """Read-only token calling a write tool returns structured isError: true."""
    user = await register_user(db, "readonly@example.com", "password123")
    pat = await create_pat(db, user["id"], "Read-only PAT", scopes=["read"])
    # Put scopes on user representation
    user_with_scopes = {**user, "scopes": pat["scopes"]}
    req = {
        "jsonrpc": "2.0",
        "id": "req-scope",
        "method": "tools/call",
        "params": {
            "name": "save_url",
            "arguments": {"url": "https://example.com"},
        },
    }
    res = await process_mcp_request(req, db, MockEnv(), user_with_scopes)
    assert res is not None
    assert res.get("isError") is True
    assert res["error"]["isError"] is True
    assert res["error"]["data"]["type"] == "insufficient_scope"
    assert res["error"]["data"]["code"] == "insufficient_scope"
    assert res["error"]["data"]["tool"] == "save_url"


@pytest.mark.asyncio
async def test_mcp_unknown_tool_structured_error(mcp_test_user):
    """Unknown tool call returns structured isError: true and not_found code."""
    user, env, db, _ = mcp_test_user
    req = {
        "jsonrpc": "2.0",
        "id": "req-unknown",
        "method": "tools/call",
        "params": {
            "name": "non_existent_tool",
            "arguments": {},
        },
    }
    res = await process_mcp_request(req, db, env, user)
    assert res is not None
    assert res.get("isError") is True
    assert res["error"]["isError"] is True
    assert res["error"]["data"]["type"] == "not_found"
    assert res["error"]["data"]["code"] == "not_found"
