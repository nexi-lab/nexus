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
def windows_cleanup_delay():
    """Add cleanup delay on Windows to let OS release file handles.

    This fixture runs after every test on Windows to give the OS time
    to release database file locks before pytest tries to cleanup temp directories.
    """
    import gc

    yield

    # Only add delay on Windows after test completes
    if platform.system() == "Windows":
        # Force multiple GC passes to release any lingering references
        for _ in range(3):
            gc.collect()
            gc.collect(1)
            gc.collect(2)
        # Increased delay for Windows CI - needs more time than local Windows
        time.sleep(0.5)  # 500ms delay for Windows file handle release
