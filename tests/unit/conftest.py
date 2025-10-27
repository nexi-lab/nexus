"""Shared fixtures for FUSE tests."""

from __future__ import annotations

import platform
import sys
from unittest.mock import MagicMock

import pytest


# Mock FuseOSError class
class FuseOSError(OSError):
    """Mock FuseOSError for testing."""

    def __init__(self, errno: int):
        """Initialize with errno."""
        self.errno = errno
        super().__init__(errno, f"FUSE error: {errno}")


# Mock the fuse module at import time (before any test imports happen)
# This ensures the fuse module is available when nexus.fuse modules are imported
_fuse_mock = MagicMock()
_fuse_mock.FUSE = MagicMock
_fuse_mock.Operations = object
_fuse_mock.FuseOSError = FuseOSError
sys.modules["fuse"] = _fuse_mock


@pytest.fixture(autouse=True)
def mock_fuse_module():
    """Reset the fuse module mock before each test.

    This fixture automatically runs before each test to ensure
    a fresh fuse module mock, preventing test pollution.
    """
    # Reset the existing mock to clear any side_effects
    _fuse_mock.reset_mock()
    _fuse_mock.FUSE = MagicMock
    _fuse_mock.Operations = object
    _fuse_mock.FuseOSError = FuseOSError

    yield _fuse_mock

    # Cleanup happens automatically before next test


@pytest.fixture(autouse=True)
def isolate_test_database(monkeypatch):
    """Automatically isolate test databases from production environment.

    This fixture runs before every test to clear environment variables that
    would cause tests to connect to production databases instead of using
    the test-specific db_path parameters.

    This prevents test pollution where version numbers, file counts, etc.
    accumulate across test runs.
    """
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    yield


@pytest.fixture(autouse=True)
def windows_db_cleanup():
    """Cleanup fixture for Windows database tests.

    Automatically runs after each test on Windows to release database connections.
    Minimal overhead approach - just GC, no delay since close() should handle everything.
    """
    import gc

    yield

    # Only do GC on Windows to ensure connections are released
    if platform.system() == "Windows":
        # Force garbage collection to release any lingering database connections
        # With proper close() calls in NexusFS, this should be enough
        gc.collect()


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Create an isolated database path for tests that need guaranteed fresh state.

    This fixture ensures each test gets a completely unique database path
    to prevent any cross-test pollution. It also clears environment variables
    that could override the database path (NEXUS_DATABASE_URL, POSTGRES_URL).
    The database is automatically cleaned up after the test completes.

    Usage:
        def test_something(isolated_db):
            nx = NexusFS(backend=..., db_path=isolated_db)
            # Test code here
            nx.close()

    Returns:
        Path: Unique database file path in temporary directory
    """
    import uuid

    # Clear environment variables that would override db_path
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    unique_id = str(uuid.uuid4())[:8]
    db_path = tmp_path / f"test_db_{unique_id}.db"

    yield db_path

    # Clean up database file after test
    if db_path.exists():
        from contextlib import suppress

        with suppress(Exception):  # Best effort cleanup
            db_path.unlink()
