# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

import json
from pathlib import Path

from scripts.audit_sql import audit_codebase, audit_file


def test_codebase_sql_tenant_scope_zero_violations():
    findings = audit_codebase("keepfor", "tests/sql_scope_allowlist.json")
    violations = [f for f in findings if f.is_violation]
    msg = f"Found {len(violations)} tenant scope SQL violations:\n" + "\n".join(
        f"{v.file}:{v.line} in {v.function}(): {v.sql_text}" for v in violations
    )
    assert not violations, msg


def test_audit_catches_deliberately_unscoped_query(tmp_path: Path):
    bad_file = tmp_path / "bad_code.py"
    bad_file.write_text(
        "async def delete_item_bad(db, i):\n"
        '    await db.execute("DELETE FROM items WHERE id = ?", (i,))\n',
        encoding="utf-8",
    )
    findings = audit_file(bad_file, allowlist=[])
    violations = [f for f in findings if f.is_violation]
    assert len(violations) == 1
    v = violations[0]
    assert "items" in v.tenant_tables
    assert not v.has_user_id
    assert v.function == "delete_item_bad"


def test_allowlist_entries_valid():
    allowlist_path = Path("tests/sql_scope_allowlist.json")
    assert allowlist_path.is_file(), "Allowlist file must exist"
    data = json.loads(allowlist_path.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    for entry in data:
        assert entry.get("file")
        assert entry.get("function")
        assert entry.get("table")
        assert "reason" in entry and len(entry["reason"].strip()) > 10
