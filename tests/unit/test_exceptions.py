"""Tests for Nexus exceptions."""

import pytest

from nexus.core.exceptions import (
    BackendError,
    FileNotFoundError,
    NexusError,
    PermissionError,
)


def test_base_exception():
    """Test base exception."""
    with pytest.raises(NexusError):
        raise NexusError("test error")


def test_file_not_found():
    """Test FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        raise FileNotFoundError("file not found")


def test_permission_error():
    """Test PermissionError."""
    with pytest.raises(PermissionError):
        raise PermissionError("permission denied")


def test_backend_error():
    """Test BackendError."""
    with pytest.raises(BackendError):
        raise BackendError("backend error")
