# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Pytest configuration and shared fixtures."""

import os
import sqlite3
import tempfile
from collections.abc import AsyncGenerator

import pytest

from keepfor.models.db import Database
from keepfor.utils.logging import clear_context
from tests.fakes import FakeAI, FakeEnv, FakeQueue, FakeR2Bucket, FakeVectorize


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

    # Apply every migration in filename order (0001_..., 0002_...).
    # Previously this hardcoded 0001 only, so later migrations (e.g. the
    # auth rate-limit table) were invisible to the entire suite.
    migrations_dir = os.path.join(os.path.dirname(__file__), "..", "migrations")
    schema_files = sorted(f for f in os.listdir(migrations_dir) if f.endswith(".sql"))
    assert schema_files, f"no migrations found in {migrations_dir}"

    for name in schema_files:
        with open(os.path.join(migrations_dir, name), "r") as f:
            schema = f.read()
            # Strip `--` comment lines before splitting. A semicolon inside a
            # comment used to split mid-comment and leave unparseable SQL as
            # code (see migrations/0002_rate_limits.sql).
            schema = "\n".join(
                line
                for line in schema.splitlines()
                if not line.strip().startswith("--")
            )
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


@pytest.fixture
def fake_bucket() -> FakeR2Bucket:
    """In-memory FakeR2Bucket fixture."""
    return FakeR2Bucket()


@pytest.fixture
def fake_vectorize() -> FakeVectorize:
    """In-memory FakeVectorize fixture."""
    return FakeVectorize()


@pytest.fixture
def fake_ai() -> FakeAI:
    """In-memory FakeAI fixture that resets script between tests."""
    FakeAI.script(None)
    ai = FakeAI()
    yield ai
    FakeAI.script(None)


@pytest.fixture
def fake_queue() -> FakeQueue:
    """In-memory FakeQueue fixture."""
    return FakeQueue()


@pytest.fixture
def fake_env(
    db: Database,
    fake_bucket: FakeR2Bucket,
    fake_vectorize: FakeVectorize,
    fake_ai: FakeAI,
    fake_queue: FakeQueue,
) -> FakeEnv:
    """FakeEnv container composing fake bindings and test db."""
    return FakeEnv(
        db=db,
        bucket=fake_bucket,
        vectorize=fake_vectorize,
        ai=fake_ai,
        queue=fake_queue,
    )
