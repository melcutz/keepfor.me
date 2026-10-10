#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""AST checker to enforce that auth precedes get_db in route handlers."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Routes exempt from auth requirements (e.g. public auth routes, assets)
EXEMPT_FUNCTION_NAMES = {
    "login_page",
    "login_submit",
    "register_page",
    "register_submit",
    "logout_route",
    "manifest",
    "favicon_ico",
    "favicon_svg",
    "pwa_icon",
    "health",
    "version",
}

AUTH_FUNC_NAMES = {
    "require_user",
    "get_current_user",
}

DB_FUNC_NAMES = {
    "get_db",
}


def get_call_name(node: ast.Call) -> str | None:
    """Extract simple function name from ast.Call node (e.g., get_db or deps.get_db)."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def check_file(path: Path) -> list[str]:
    violations = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError) as e:
        violations.append(f"Failed to parse {path}: {e}")
        return violations

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        if node.name in EXEMPT_FUNCTION_NAMES:
            continue

        # Collect calls at function level (skip nested function definitions)
        auth_calls: list[int] = []
        db_calls: list[int] = []

        for child in ast.walk(node):
            # Don't inspect child functions of this function
            if child is not node and isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue

            if isinstance(child, ast.Call):
                name = get_call_name(child)
                if name in AUTH_FUNC_NAMES:
                    auth_calls.append(child.lineno)
                elif name in DB_FUNC_NAMES:
                    db_calls.append(child.lineno)

        if auth_calls and db_calls:
            first_auth = min(auth_calls)
            first_db = min(db_calls)
            if first_db < first_auth:
                violations.append(
                    f"{path}:{node.lineno} in '{node.name}': "
                    f"get_db() at line {first_db} before auth at line {first_auth}"
                )

    return violations


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    keepfor_dir = repo_root / "keepfor"
    if not keepfor_dir.exists():
        print(f"Error: directory {keepfor_dir} does not exist.", file=sys.stderr)
        return 1

    files_to_check: list[Path] = []
    app_py = keepfor_dir / "app.py"
    if app_py.exists():
        files_to_check.append(app_py)

    routes_dir = keepfor_dir / "routes"
    if routes_dir.exists():
        files_to_check.extend(sorted(routes_dir.glob("*.py")))

    if not files_to_check:
        print("No route files found to check.", file=sys.stderr)
        return 1

    all_violations: list[str] = []
    for file_path in files_to_check:
        violations = check_file(file_path)
        all_violations.extend(violations)

    if all_violations:
        print("Auth order violations found:", file=sys.stderr)
        for v in all_violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    print(f"Auth order check passed across {len(files_to_check)} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
