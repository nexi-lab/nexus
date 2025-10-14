"""Unit tests for Nexus exceptions."""

from nexus.core.exceptions import (
    BackendError,
    InvalidPathError,
    MetadataError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
)


def test_nexus_error() -> None:
    """Test base NexusError."""
    # Without path
    error = NexusError("Something went wrong")
    assert str(error) == "Something went wrong"
    assert error.message == "Something went wrong"
    assert error.path is None

    # With path
    error = NexusError("Something went wrong", path="/test/file.txt")
    assert str(error) == "Something went wrong: /test/file.txt"
    assert error.message == "Something went wrong"
    assert error.path == "/test/file.txt"


def test_file_not_found_error() -> None:
    """Test NexusFileNotFoundError."""
    # Default message
    error = NexusFileNotFoundError("/missing/file.txt")
    assert "File not found" in str(error)
    assert "/missing/file.txt" in str(error)
    assert error.path == "/missing/file.txt"

    # Custom message
    error = NexusFileNotFoundError("/missing/file.txt", "Custom message")
    assert "Custom message" in str(error)
    assert error.path == "/missing/file.txt"


def test_permission_error() -> None:
    """Test NexusPermissionError."""
    # Default message
    error = NexusPermissionError("/forbidden/file.txt")
    assert "Permission denied" in str(error)
    assert "/forbidden/file.txt" in str(error)
    assert error.path == "/forbidden/file.txt"

    # Custom message
    error = NexusPermissionError("/forbidden/file.txt", "Access denied")
    assert "Access denied" in str(error)


def test_backend_error() -> None:
    """Test BackendError."""
    # Without backend or path
    error = BackendError("Operation failed")
    assert str(error) == "Operation failed"
    assert error.backend is None
    assert error.path is None

    # With backend
    error = BackendError("Operation failed", backend="s3")
    assert "[s3]" in str(error)
    assert error.backend == "s3"

    # With backend and path
    error = BackendError("Operation failed", backend="gcs", path="/test/file.txt")
    assert "[gcs]" in str(error)
    assert "/test/file.txt" in str(error)
    assert error.backend == "gcs"
    assert error.path == "/test/file.txt"


def test_invalid_path_error() -> None:
    """Test InvalidPathError."""
    # Default message
    error = InvalidPathError("../../etc/passwd")
    assert "Invalid path" in str(error)
    assert "../../etc/passwd" in str(error)

    # Custom message
    error = InvalidPathError("bad\x00path", "Contains null byte")
    assert "Contains null byte" in str(error)


def test_metadata_error() -> None:
    """Test MetadataError."""
    # Without path
    error = MetadataError("Database error")
    assert str(error) == "Database error"
    assert error.path is None

    # With path
    error = MetadataError("Database error", path="/test/file.txt")
    assert "Database error" in str(error)
    assert "/test/file.txt" in str(error)
    assert error.path == "/test/file.txt"


def test_exception_inheritance() -> None:
    """Test that all custom exceptions inherit from NexusError."""
    assert issubclass(NexusFileNotFoundError, NexusError)
    assert issubclass(NexusPermissionError, NexusError)
    assert issubclass(BackendError, NexusError)
    assert issubclass(InvalidPathError, NexusError)
    assert issubclass(MetadataError, NexusError)

    # All should also be standard Exceptions
    assert issubclass(NexusError, Exception)
