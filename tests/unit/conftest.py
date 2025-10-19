"""Shared fixtures for FUSE tests."""

from __future__ import annotations

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


@pytest.fixture(autouse=True)
def mock_fuse_module():
    """Mock the fuse module for all FUSE tests.

    This fixture automatically runs before each test to ensure
    a fresh fuse module mock, preventing test pollution.
    """
    # Create a fresh mock for the fuse module
    fuse_mock = MagicMock()
    fuse_mock.FUSE = MagicMock
    fuse_mock.Operations = object
    fuse_mock.FuseOSError = FuseOSError

    # Set it in sys.modules
    sys.modules["fuse"] = fuse_mock

    yield fuse_mock

    # Cleanup is automatic - pytest will create a new mock for the next test
