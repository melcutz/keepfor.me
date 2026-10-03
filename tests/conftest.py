"""Pytest configuration and shared fixtures."""

import os
import sqlite3
import tempfile
from typing import AsyncGenerator

import pytest

from src.models.db import Database
from src.utils.logging import clear_context


@pytest.fixture
def db_path() -> str:
    """Create a temporary SQLite database for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    # Cleanup
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def sqlite_conn(db_path: str) -> sqlite3.Connection:
    """Create and initialize SQLite connection with schema."""
    # Use check_same_thread=False to allow async code to use the connection
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Apply schema
    schema_path = os.path.join(
        os.path.dirname(__file__), "..", "migrations", "0001_initial_schema.sql"
    )

    if os.path.exists(schema_path):
        with open(schema_path, "r") as f:
            schema = f.read()
            # Split by semicolon and execute each statement
            for statement in schema.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.commit()

    yield conn
    conn.close()


@pytest.fixture
def db(sqlite_conn: sqlite3.Connection) -> Database:
    """Create Database instance wrapping SQLite connection."""
    return Database(sqlite_conn=sqlite_conn)


@pytest.fixture
def test_user_data() -> dict:
    """Standard test user data."""
    return {"email": "test@example.com", "password": "test_password_123"}


@pytest.fixture
def test_item_data() -> dict:
    """Standard test item data."""
    return {
        "url": "https://example.com/article",
        "title": "Test Article",
        "byline": "Test Author",
        "excerpt": "Test excerpt",
        "site_name": "Example.com",
        "published_date": "2024-01-01",
        "content_text": "This is test content.",
        "word_count": 4,
    }


@pytest.fixture(autouse=True)
def clear_logging_context():
    """Clear logging context before each test."""
    clear_context()
    yield
    clear_context()


@pytest.fixture
async def async_db(sqlite_conn: sqlite3.Connection) -> AsyncGenerator[Database, None]:
    """Async database fixture."""
    db = Database(sqlite_conn=sqlite_conn)
    yield db
