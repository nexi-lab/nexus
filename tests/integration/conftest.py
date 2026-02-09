"""Shared fixtures for integration tests."""

from __future__ import annotations

import uuid

import pytest

# Conditionally ignore MCP tests if fastmcp is not installed
# This must be done at collection time, before any imports from test files
try:
    import fastmcp  # noqa: F401
except ImportError:
    collect_ignore_glob = ["mcp/*"]

from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Create an isolated database path for integration tests.

    This fixture ensures each test gets a completely unique database path
    to prevent any cross-test pollution. It also clears environment variables
    that could override the database path.

    Usage:
        def test_something(isolated_db):
            metadata_store = RaftMetadataStore.local(str(isolated_db).replace(".db", ""))
            nx = NexusFS(backend=..., metadata_store=metadata_store)
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


@pytest.fixture
def metadata_store(tmp_path):
    """Create Raft metadata store for integration tests (primary production path).

    Task #14: Breaking change - NexusFS now requires explicit metadata_store parameter.
    This fixture uses RaftMetadataStore (Strong Consistency, primary production default).

    Usage:
        def test_something(backend, metadata_store):
            nx = NexusFS(backend=backend, metadata_store=metadata_store)
            # Test code here
            nx.close()

    Returns:
        RaftMetadataStore: Raft-backed metadata store (SC mode)
    """
    store = RaftMetadataStore.local(str(tmp_path / "raft-metadata"))
    yield store
    # Cleanup handled by tmp_path


@pytest.fixture
def record_store():
    """Create in-memory RecordStore for integration tests.

    Task #14: Four Pillars — RecordStore provides SQL for Services layer
    (ReBAC, Auth, Audit, etc.). Uses in-memory SQLite for test isolation.
    Pass this to NexusFS when tests need Services (permissions, users, etc.).
    Tests exercising pure file operations can omit record_store.

    Returns:
        SQLAlchemyRecordStore: In-memory SQLite record store
    """
    store = SQLAlchemyRecordStore()  # defaults to sqlite:///:memory:
    yield store
    store.close()
