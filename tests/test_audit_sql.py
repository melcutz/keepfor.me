# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Tests for strict SQL AST audit parser, including deliberate violations."""

import ast

from scripts.audit_sql import SqlAuditVisitor, is_scoped_sql


def test_audit_sql_valid_queries():
    """Verify standard correctly scoped SQL queries pass."""
    valid_queries = [
        "SELECT * FROM items WHERE user_id = ?;",
        "SELECT id, title FROM items WHERE id = ? AND user_id = ?;",
        "SELECT i.id FROM items i WHERE i.user_id = ?;",
        "SELECT * FROM items WHERE ? = user_id;",
        (
            "SELECT t.name FROM item_tags it JOIN items i "
            "ON i.id = it.item_id AND i.user_id = ?;"
        ),
        "INSERT INTO items (id, user_id, title) VALUES (?, ?, ?);",
        (
            "INSERT OR IGNORE INTO personal_access_tokens "
            "(id, user_id, name, token_hash) VALUES (?, ?, ?, ?);"
        ),
        "UPDATE items SET title = ? WHERE id = ? AND user_id = ?;",
        "DELETE FROM items WHERE id = ? AND user_id = ?;",
        (
            "DELETE FROM item_tags WHERE item_id IN "
            "(SELECT id FROM items WHERE user_id = ?);"
        ),
    ]
    for q in valid_queries:
        assert is_scoped_sql(q, {"user_id"}), f"Expected valid: {q}"


def test_audit_sql_deliberate_violation_projection_only():
    """Projection-only mention (SELECT user_id, ...) must not count as scoped."""
    query = "SELECT user_id, title FROM items WHERE id = ?;"
    assert not is_scoped_sql(query, {"user_id"}), "Projection-only mention must fail"

    query_no_where = "SELECT user_id, title FROM items;"
    assert not is_scoped_sql(query_no_where, {"user_id"}), (
        "Projection-only mention without WHERE must fail"
    )


def test_audit_sql_deliberate_violation_comment_only():
    """Comment-only mention (-- user_id = ?) must be stripped and not count."""
    query_line_comment = "SELECT * FROM items WHERE id = ?; -- user_id = ?"
    assert not is_scoped_sql(query_line_comment, {"user_id"}), (
        "Line comment mention must fail"
    )

    query_block_comment = "SELECT * FROM items WHERE id = ? /* AND user_id = ? */;"
    assert not is_scoped_sql(query_block_comment, {"user_id"}), (
        "Block comment mention must fail"
    )


def test_audit_sql_deliberate_violation_string_literal_only():
    """String literal mention ('user_id = ?') must not count as scoped."""
    query = "SELECT * FROM items WHERE title = 'user_id = ?';"
    assert not is_scoped_sql(query, {"user_id"}), "String literal mention must fail"


def test_audit_sql_deliberate_violation_fstring():
    """F-string interpolation without bound ? parameter must fail."""
    # Test AST visitor on f-string code
    code_fstring_quoted = """
def test_fn():
    user_id = "u1"
    q = f"SELECT * FROM items WHERE user_id = '{user_id}';"
"""
    tree = ast.parse(code_fstring_quoted)
    visitor = SqlAuditVisitor("dummy.py", allowlist=[])
    visitor.visit(tree)
    assert len(visitor.findings) == 1
    assert visitor.findings[0].is_violation, (
        "Quoted f-string interpolation must be a violation"
    )

    code_fstring_raw = """
def test_fn():
    user_id = "u1"
    q = f"SELECT * FROM items WHERE user_id = {user_id};"
"""
    tree2 = ast.parse(code_fstring_raw)
    visitor2 = SqlAuditVisitor("dummy.py", allowlist=[])
    visitor2.visit(tree2)
    assert len(visitor2.findings) == 1
    assert visitor2.findings[0].is_violation, (
        "Raw f-string interpolation must be a violation"
    )
