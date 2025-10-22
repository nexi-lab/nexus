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
