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
def windows_db_cleanup(request):
    """Cleanup fixture for Windows database tests.

    Automatically runs after each test on Windows to release database connections.
    Only applies delay to tests that use fixtures indicating database/filesystem usage.
    """
    import gc

    yield

    # Only add delay on Windows for tests that might use databases
    if platform.system() == "Windows":
        # Check if test uses any fixture that suggests database usage
        fixture_names = request.fixturenames
        needs_cleanup = any(
            name in ("tmp_path", "temp_dir", "embedded", "tmp_dir") for name in fixture_names
        )

        if needs_cleanup:
            gc.collect()
            time.sleep(0.05)  # 50ms delay for Windows file handle release
