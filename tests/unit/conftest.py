"""Shared fixtures for FUSE tests."""

from __future__ import annotations

import platform
import sys
import time
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
def windows_cleanup_delay(request):
    """Add cleanup delay on Windows to let OS release file handles.

    This fixture runs after every test on Windows to give the OS time
    to release database file locks before pytest tries to cleanup temp directories.

    Only applies to tests that use temp directories or databases.
    """
    import gc

    yield

    # Only add delay on Windows after test completes
    # Skip delay for tests that don't need it (no fixtures with 'tmp' or 'temp' in name)
    if platform.system() == "Windows":
        # Check if test uses temp directories or database fixtures
        fixture_names = [name.lower() for name in request.fixturenames]
        needs_cleanup = any(
            "tmp" in name or "temp" in name or "embedded" in name or "db" in name
            for name in fixture_names
        )

        if needs_cleanup:
            # Force garbage collection to release database connections
            for _ in range(2):
                gc.collect()
            # Reduced delay - 100ms should be sufficient with proper close() calls
            time.sleep(0.1)  # 100ms delay for Windows file handle release
