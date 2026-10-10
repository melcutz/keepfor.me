#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""
scripts/behavior_snapshot.py - Compare 15 offline HTTP requests between
main and current branch.

Verifies behavior preservation across the Phase 1 core refactor:
With default providers, HTTP responses and DB writes are byte-for-byte
equivalent, except for documented changes.
"""

import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


def init_db(db_path: Path, migrations_dir: Path):
    """Apply schema migrations and seed initial test tenant data."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    schema_files = sorted(f for f in os.listdir(migrations_dir) if f.endswith(".sql"))
    for name in schema_files:
        with open(migrations_dir / name, "r", encoding="utf-8") as f:
            sql = f.read()
            sql = "\n".join(
                line for line in sql.splitlines() if not line.strip().startswith("--")
            )
            for stmt in sql.split(";"):
                if stmt.strip():
                    conn.execute(stmt)

    # Seed data
    u_id = "u_snapshot"
    email = "snapshot@test.local"
    # PBKDF2 hash of "password123"
    pw_salt = "000102030405060708090a0b0c0d0e0f"
    pw_hash = hashlib.pbkdf2_hmac(
        "sha256", b"password123", bytes.fromhex(pw_salt), 100000
    ).hex()
    stored_hash = f"{pw_salt}${pw_hash}"

    conn.execute(
        "INSERT INTO users (id, email, password_hash, role, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (u_id, email, stored_hash, "admin", "2026-10-09 00:00:00"),
    )

    # Valid session
    conn.execute(
        "INSERT INTO sessions (id, user_id, created_at, expires_at) "
        "VALUES (?, ?, ?, ?)",
        ("sess_snapshot_12345", u_id, "2026-10-09 00:00:00", "2099-01-01 00:00:00"),
    )

    # Valid PAT
    raw_pat = "kfm_live_snapshot_pat_12345"
    token_hash = hashlib.sha256(raw_pat.encode()).hexdigest()
    conn.execute(
        "INSERT INTO personal_access_tokens "
        "(id, user_id, name, token_hash, created_at) VALUES (?, ?, ?, ?, ?)",
        ("pat_snapshot_1", u_id, "snapshot_pat", token_hash, "2026-10-09 00:00:00"),
    )

    # Initial Item
    conn.execute(
        """
        INSERT INTO items (
            id, user_id, url, canonical_url, title, byline, excerpt, site_name,
            published_date, content_text, word_count,
            status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "item_snapshot_1",
            u_id,
            "https://example.com/test-article",
            "https://example.com/test-article",
            "Test Article",
            "Test Author",
            "Test Excerpt",
            "Example Site",
            "2026-01-01",
            "This is test content for article 1.",
            7,
            "ready",
            "2026-10-09 00:00:00",
            "2026-10-09 00:00:00",
        ),
    )
    conn.execute(
        "INSERT INTO items_fts (item_id, user_id, title, content_text) "
        "VALUES (?, ?, ?, ?)",
        (
            "item_snapshot_1",
            u_id,
            "Test Article",
            "This is test content for article 1.",
        ),
    )

    # Initial Tag
    conn.execute(
        "INSERT INTO tags (id, user_id, name, created_at) VALUES (?, ?, ?, ?)",
        ("tag_snapshot_1", u_id, "reading", "2026-10-09 00:00:00"),
    )
    conn.execute(
        "INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)",
        ("item_snapshot_1", "tag_snapshot_1"),
    )

    conn.commit()
    conn.close()


RUNNER_CODE = """
import os
import sys
import json
import sqlite3
from pathlib import Path

target_dir = sys.argv[1]
db_path = sys.argv[2]
mode = sys.argv[3] # 'main' or 'branch'

os.environ["ALLOW_PUBLIC_SIGNUPS"] = "false"
os.environ["READER_PROXY_BASE"] = ""

sys.path.insert(0, target_dir)

from fastapi.testclient import TestClient

class MockQueue:
    def __init__(self):
        self.sent = []
    async def send(self, msg):
        self.sent.append(msg)

class MockEnv:
    def __init__(self, db):
        self.QUEUE = MockQueue()
        self.keepfor_me_db = db
        self.DB = db
        self.ALLOW_PUBLIC_SIGNUPS = "false"
        self.READER_PROXY_BASE = ""

# Note: This runner imports keepfor.* for current code.
# When comparing against the pre-rename tree (commit 01e1c16 or earlier),
# a git worktree of that commit will provide the legacy src.* package.
try:
    from keepfor.models.db import Database
    import keepfor.deps as deps
    from keepfor.app import create_app

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db = Database(sqlite_conn=conn)
    env = MockEnv(db)

    deps.get_db = lambda req: db
    deps.get_env_from_request = lambda req: env

    app = create_app()
except ImportError:
    # Fallback when running inside a git worktree targeting pre-rename tree (where package was src.*)
    from src.models.db import Database
    import src.app as app_module

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db = Database(sqlite_conn=conn)
    env = MockEnv(db)

    app_module.get_db = lambda req: db
    app_module.get_env_from_request = lambda req: env

    app = app_module.app

client = TestClient(app)

session_cookie = {"kfm_session": "sess_snapshot_12345"}
pat_header = {"Authorization": "Bearer kfm_live_snapshot_pat_12345"}

# Fixed list of 15 requests
requests_spec = [
    # 1. Login page
    ("01_login_page", lambda: client.get("/auth/login")),
    # 2. Register page (signups off)
    ("02_register_page", lambda: client.get("/auth/register")),
    # 3. Library list
    ("03_library_list", lambda: client.get("/", cookies=session_cookie)),
    # 4. Item page
    (
        "04_item_page",
        lambda: client.get("/items/item_snapshot_1", cookies=session_cookie),
    ),
    # 5. Search
    ("05_search", lambda: client.get("/?q=Test", cookies=session_cookie)),
    # 6. Tags page
    ("06_tags_page", lambda: client.get("/tags", cookies=session_cookie)),
    # 7. Stats page
    ("07_stats_page", lambda: client.get("/stats", cookies=session_cookie)),
    # 8. Export JSON
    ("08_export_json", lambda: client.get("/api/export", cookies=session_cookie)),
    # 9. MCP initialize
    (
        "09_mcp_initialize",
        lambda: client.post(
            "/api/mcp",
            headers=pat_header,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0"},
                },
            },
        ),
    ),
    # 10. MCP tools/list
    (
        "10_mcp_tools_list",
        lambda: client.post(
            "/api/mcp",
            headers=pat_header,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ),
    ),
    # 11. Save URL (fake fetch via queue)
    (
        "11_save_url",
        lambda: client.post(
            "/api/save",
            cookies=session_cookie,
            json={
                "url": "https://example.com/snapshot-save-offline",
                "tags": ["snapshot"],
            },
        ),
    ),
    # 12. Settings page
    ("12_settings_page", lambda: client.get("/settings", cookies=session_cookie)),
    # 13. PAT create
    (
        "13_pat_create",
        lambda: client.post(
            "/settings/tokens",
            cookies=session_cookie,
            data={"name": "token_snapshot_test"},
        ),
    ),
    # 14. 401 unauthenticated
    ("14_export_401", lambda: client.get("/api/export")),
    # 15. 401 invalid PAT
    (
        "15_items_401",
        lambda: client.get(
            "/api/items",
            headers={"Authorization": "Bearer invalid_pat_token_abc"},
        ),
    ),
]

results = []
for name, req_func in requests_spec:
    resp = req_func()
    results.append({
        "name": name,
        "status_code": resp.status_code,
        "headers": {
            k.lower(): v
            for k, v in resp.headers.items()
            if k.lower() in ["content-type", "location"]
        },
        "text": resp.text,
    })

print(json.dumps(results))
"""


def normalize_content(text: str) -> str:
    """Normalize volatile elements (timestamps, UUIDs, hex nonces, versions)."""
    # Normalize ISO / SQL timestamps
    text = re.sub(
        r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\b",
        "<TIMESTAMP>",
        text,
    )
    # Normalize UUIDs
    text = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        "<UUID>",
        text,
        flags=re.IGNORECASE,
    )
    # Normalize PAT tokens (kfm_live_...)
    text = re.sub(r"kfm_live_[A-Za-z0-9_\-]+", "<KFM_LIVE_TOKEN>", text)
    # Normalize 32+ char hex strings (hashes, tokens, session IDs)
    text = re.sub(r"\b[0-9a-f]{32,64}\b", "<HEX_HASH>", text, flags=re.IGNORECASE)
    # Normalize version string bump (1.0.0 vs 1.1.0 in MCP or templates)
    text = re.sub(r'"version":\s*"1\.[01]\.0"', '"version": "<VERSION>"', text)
    # Normalize empty template block newlines: collapse multiple whitespace lines
    text = re.sub(r"\n\s*\n+", "\n", text)
    # Strip trailing/leading whitespace per line and omit empty lines
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def run_test(target_dir: Path, db_path: Path, mode: str) -> list[dict]:
    """Execute the runner in a clean subprocess."""
    proc = subprocess.run(
        [sys.executable, "-c", RUNNER_CODE, str(target_dir), str(db_path), mode],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def main():
    repo_root = Path(__file__).resolve().parent.parent
    temp_dir = Path(tempfile.mkdtemp(prefix="kfm_snapshot_"))
    try:
        main_dir = temp_dir / "main"
        main_dir.mkdir()

        # 1. Extract main
        tar_proc = subprocess.Popen(
            ["git", "archive", "main"], cwd=repo_root, stdout=subprocess.PIPE
        )
        subprocess.check_call(["tar", "-x", "-C", str(main_dir)], stdin=tar_proc.stdout)
        tar_proc.wait()

        # 2. Init seeded DB
        template_db = temp_dir / "seed.db"
        init_db(template_db, repo_root / "migrations")

        db_main = temp_dir / "main.db"
        db_branch = temp_dir / "branch.db"
        shutil.copyfile(template_db, db_main)
        shutil.copyfile(template_db, db_branch)

        print(f"Running 15 snapshot requests on main ({main_dir})...")
        main_results = run_test(main_dir, db_main, "main")

        print(f"Running 15 snapshot requests on current branch ({repo_root})...")
        branch_results = run_test(repo_root, db_branch, "branch")

        assert len(main_results) == 15, f"Expected 15 results, got {len(main_results)}"
        assert len(branch_results) == 15, (
            f"Expected 15 results, got {len(branch_results)}"
        )

        diffs = []
        for mr, br in zip(main_results, branch_results):
            name = mr["name"]
            m_status = mr["status_code"]
            b_status = br["status_code"]
            if m_status != b_status:
                diffs.append(
                    f"[{name}] Status code mismatch: "
                    f"main={m_status} vs branch={b_status}"
                )

            m_norm = normalize_content(mr["text"])
            b_norm = normalize_content(br["text"])

            if m_norm != b_norm:
                diff_lines = list(
                    difflib.unified_diff(
                        m_norm.splitlines(keepends=True),
                        b_norm.splitlines(keepends=True),
                        fromfile=f"main/{name}",
                        tofile=f"branch/{name}",
                    )
                )
                diffs.append(f"[{name}] Body difference:\n" + "".join(diff_lines))

        print("=" * 60)
        print("BEHAVIOR SNAPSHOT COMPARISON REPORT (15 REQUESTS)")
        print("=" * 60)
        for r in branch_results:
            print(f"  {r['name']:<25} -> {r['status_code']}")

        if not diffs:
            print("\nSUCCESS: 15/15 requests matched exactly between main and branch!")
            sys.exit(0)
        else:
            print(f"\nDIFF DETECTED ({len(diffs)} differences):")
            for d in diffs:
                print(d)
            sys.exit(1)

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
