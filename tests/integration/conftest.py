"""Shared fixtures for integration tests."""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Create an isolated database path for integration tests.

    This fixture ensures each test gets a completely unique database path
    to prevent any cross-test pollution. It also clears environment variables
    that could override the database path.

    Usage:
        def test_something(isolated_db):
            nx = NexusFS(backend=..., db_path=isolated_db)
            # Test code here
            nx.close()

    Returns:
        Path: Unique database file path in temporary directory
    """
    # Clear environment variables that would override db_path
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    unique_id = str(uuid.uuid4())[:8]
    db_path = tmp_path / f"integration_test_db_{unique_id}.db"

    yield db_path

    # Clean up database file after test
    if db_path.exists():
        from contextlib import suppress

        with suppress(Exception):  # Best effort cleanup
            db_path.unlink()
