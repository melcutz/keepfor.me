#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Audit SQL queries across the codebase to enforce tenant isolation discipline.

Walks Python AST in the target directory (default: keepfor/), finds SQL statements
touching tenant tables, and verifies that every statement contains `user_id` or is
explicitly recorded in an allowlist.
"""

import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from keepfor.models.tenant_tables import TENANT_TABLES
except ImportError:
    # Fallback if keepfor is not installed in the current environment
    TENANT_TABLES = (
        "items",
        "items_fts",
        "tags",
        "item_tags",
        "chunks",
        "suggested_tags",
        "tag_rules",
        "rule_suggestion_dismissals",
        "item_opens",
        "sessions",
        "personal_access_tokens",
    )

SQL_KEYWORDS = re.compile(
    r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH|REPLACE)\b", re.IGNORECASE
)
TABLE_REGEX = re.compile(
    r"\b(?:DELETE\s+FROM|FROM|JOIN|INTO|UPDATE)\s+([a-zA-Z0-9_]+)", re.IGNORECASE
)
USER_ID_REGEX = re.compile(r"\buser_id\b", re.IGNORECASE)


@dataclass
class AuditFinding:
    file: str
    function: str
    line: int
    sql_text: str
    tables_touched: list[str]
    tenant_tables: list[str]
    has_user_id: bool
    is_allowlisted: bool = False
    allowlist_reason: str | None = None

    @property
    def is_violation(self) -> bool:
        return bool(
            self.tenant_tables and not self.has_user_id and not self.is_allowlisted
        )


class SqlAuditVisitor(ast.NodeVisitor):
    def __init__(self, filename: str, allowlist: list[dict] | None = None):
        self.filename = str(filename)
        self.allowlist = allowlist or []
        self.scope_stack: list[str] = []
        self.findings: list[AuditFinding] = []

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef):
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_Constant(self, node: ast.Constant):
        if isinstance(node.value, str):
            self._check_sql(node.value, node.lineno)

    def visit_JoinedStr(self, node: ast.JoinedStr):
        parts: list[str] = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("{...}")
        full_text = "".join(parts)
        self._check_sql(full_text, node.lineno)
        # Visit child expressions in FormattedValue (e.g. nested calls)
        for v in node.values:
            if isinstance(v, ast.FormattedValue):
                self.visit(v.value)

    def _check_sql(self, text: str, lineno: int):
        if not SQL_KEYWORDS.search(text):
            return

        tables = [t.lower() for t in TABLE_REGEX.findall(text)]
        tenant_tables = [t for t in tables if t in TENANT_TABLES]
        has_user_id = bool(USER_ID_REGEX.search(text))
        current_func = self.scope_stack[-1] if self.scope_stack else "<module>"

        is_allowlisted = False
        allowlist_reason = None

        if tenant_tables and not has_user_id:
            norm_file = self.filename.replace("\\", "/")
            for entry in self.allowlist:
                entry_file = entry.get("file", "").replace("\\", "/")
                entry_func = entry.get("function")
                entry_table = entry.get("table", "").lower()

                if (
                    norm_file.endswith(entry_file)
                    and entry_func == current_func
                    and entry_table in tenant_tables
                ):
                    is_allowlisted = True
                    allowlist_reason = entry.get("reason", "")
                    break

        self.findings.append(
            AuditFinding(
                file=self.filename,
                function=current_func,
                line=lineno,
                sql_text=text.strip(),
                tables_touched=tables,
                tenant_tables=tenant_tables,
                has_user_id=has_user_id,
                is_allowlisted=is_allowlisted,
                allowlist_reason=allowlist_reason,
            )
        )


def load_allowlist(allowlist_path: Path | str | None) -> list[dict]:
    if not allowlist_path:
        return []
    p = Path(allowlist_path)
    if not p.is_file():
        return []
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def audit_file(
    file_path: Path | str, allowlist: list[dict] | None = None
) -> list[AuditFinding]:
    p = Path(file_path)
    with open(p, "r", encoding="utf-8") as f:
        content = f.read()
    tree = ast.parse(content, filename=str(p))
    visitor = SqlAuditVisitor(filename=str(p), allowlist=allowlist)
    visitor.visit(tree)
    return visitor.findings


def audit_codebase(
    root_dir: Path | str = "keepfor",
    allowlist_path: Path | str | None = "tests/sql_scope_allowlist.json",
) -> list[AuditFinding]:
    allowlist = load_allowlist(allowlist_path)
    all_findings: list[AuditFinding] = []
    root = Path(root_dir)

    if root.is_file():
        return audit_file(root, allowlist)

    for py_file in sorted(root.rglob("*.py")):
        all_findings.extend(audit_file(py_file, allowlist))

    return all_findings


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Audit SQL queries for tenant isolation."
    )
    parser.add_argument(
        "--path",
        default="keepfor",
        help="Path to Python file or directory to audit (default: keepfor)",
    )
    parser.add_argument(
        "--allowlist",
        default="tests/sql_scope_allowlist.json",
        help="Path to allowlist JSON (default: tests/sql_scope_allowlist.json)",
    )
    args = parser.parse_args()

    findings = audit_codebase(args.path, args.allowlist)
    tenant_findings = [f for f in findings if f.tenant_tables]
    violations = [f for f in tenant_findings if f.is_violation]

    # Print table of findings
    col_file = 35
    col_func = 24
    col_line = 6
    col_tables = 28
    col_status = 14

    header = (
        f"{'File':<{col_file}} {'Function':<{col_func}} {'Line':<{col_line}} "
        f"{'Tenant Tables':<{col_tables}} {'Status':<{col_status}}"
    )
    print("=" * len(header))
    print(header)
    print("=" * len(header))

    for f in tenant_findings:
        short_file = f.file
        if len(short_file) > col_file:
            short_file = "..." + short_file[-(col_file - 3) :]

        short_func = f.function
        if len(short_func) > col_func:
            short_func = short_func[: col_func - 3] + "..."

        tables_str = ",".join(f.tenant_tables)
        if len(tables_str) > col_tables:
            tables_str = tables_str[: col_tables - 3] + "..."

        if f.is_violation:
            status = "VIOLATION"
        elif f.is_allowlisted:
            status = "ALLOWLISTED"
        else:
            status = "OK"

        print(
            f"{short_file:<{col_file}} {short_func:<{col_func}} {f.line:<{col_line}} "
            f"{tables_str:<{col_tables}} {status:<{col_status}}"
        )

    t_cnt = len(tenant_findings)
    allowlisted_count = sum(1 for f in tenant_findings if f.is_allowlisted)
    print(
        f"Scanned {len(findings)} statements ({t_cnt} on tenant tables). "
        f"Allowlisted: {allowlisted_count}. "
        f"Violations: {len(violations)}."
    )

    if violations:
        print("\nVIOLATIONS FOUND:")
        for v in violations:
            print(f"  {v.file}:{v.line} in {v.function}()")
            print(f"    Tables: {v.tenant_tables}")
            print(f"    SQL: {v.sql_text!r}")
        return 1

    print("\nAll tenant table queries are properly scoped with user_id!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
