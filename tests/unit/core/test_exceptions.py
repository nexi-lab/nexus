"""Unit tests for Nexus exceptions."""

from nexus.core.exceptions import (
    AuditLogError,
    AuthenticationError,
    BackendError,
    ConflictError,
    InvalidPathError,
    MetadataError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ParserError,
    PermissionDeniedError,
    ValidationError,
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


def test_parser_error() -> None:
    """Test ParserError."""
    # Without parser or path
    error = ParserError("Parsing failed")
    assert str(error) == "Parsing failed"
    assert error.parser is None
    assert error.path is None

    # With parser
    error = ParserError("Parsing failed", parser="MarkItDown")
    assert "[MarkItDown]" in str(error)
    assert error.parser == "MarkItDown"

    # With parser and path
    error = ParserError("Parsing failed", path="/test/file.pdf", parser="MarkItDown")
    assert "[MarkItDown]" in str(error)
    assert "/test/file.pdf" in str(error)
    assert error.parser == "MarkItDown"
    assert error.path == "/test/file.pdf"


def test_exception_inheritance() -> None:
    """Test that all custom exceptions inherit from NexusError."""
    assert issubclass(NexusFileNotFoundError, NexusError)
    assert issubclass(NexusPermissionError, NexusError)
    assert issubclass(BackendError, NexusError)
    assert issubclass(InvalidPathError, NexusError)
    assert issubclass(MetadataError, NexusError)
    assert issubclass(ParserError, NexusError)

    # All should also be standard Exceptions
    assert issubclass(NexusError, Exception)


# ============================================================================
# Error Classification Tests (Issue #706)
# ============================================================================


def test_is_expected_default_values() -> None:
    """Test that each exception class has the correct is_expected default.

    Expected errors (is_expected=True):
    - User input validation failures
    - Resource not found (user requested non-existent item)
    - Permission denied (user lacks access)
    - Conflicts (optimistic concurrency)

    Unexpected errors (is_expected=False):
    - Backend/infrastructure failures
    - Internal state corruption
    - Bugs and unhandled conditions
    """
    # Expected errors (user errors) - should have is_expected=True
    assert NexusFileNotFoundError("/test").is_expected is True
    assert NexusPermissionError("/test").is_expected is True
    assert PermissionDeniedError("No access").is_expected is True
    assert InvalidPathError("/bad/path").is_expected is True
    assert ValidationError("Invalid input").is_expected is True
    assert ParserError("Cannot parse").is_expected is True
    assert ConflictError("/test", "etag1", "etag2").is_expected is True
    assert AuthenticationError("Token expired").is_expected is True

    # Unexpected errors (system errors) - should have is_expected=False
    assert BackendError("Connection failed").is_expected is False
    assert MetadataError("Database error").is_expected is False
    assert AuditLogError("Audit failed").is_expected is False

    # Base class defaults to False (unexpected)
    assert NexusError("Generic error").is_expected is False


def test_is_expected_instance_override() -> None:
    """Test that is_expected can be overridden at instance creation.

    Note: Only some exception classes support is_expected override in __init__.
    Classes with custom __init__ signatures (ConflictError, ParserError, etc.)
    use their class-level default.
    """
    # Base class supports override
    error = NexusError("Generic", is_expected=True)
    assert error.is_expected is True

    error = NexusError("Generic", is_expected=False)
    assert error.is_expected is False

    # ValidationError supports override
    error = ValidationError("Invalid", is_expected=False)
    assert error.is_expected is False  # Overridden from class default of True

    # MetadataError supports override
    error = MetadataError("DB error", is_expected=True)
    assert error.is_expected is True  # Overridden from class default of False


def test_is_expected_class_attribute() -> None:
    """Test that is_expected is a class attribute that can be checked."""
    # Class-level check (without instantiation)
    assert NexusFileNotFoundError.is_expected is True
    assert ValidationError.is_expected is True
    assert BackendError.is_expected is False
    assert MetadataError.is_expected is False


def test_base_error_is_expected_default() -> None:
    """Test that NexusError base class defaults to is_expected=False."""
    error = NexusError("Something went wrong")
    assert error.is_expected is False

    # Can be overridden
    error = NexusError("User mistake", is_expected=True)
    assert error.is_expected is True


def test_conflict_error_is_expected() -> None:
    """Test ConflictError is classified as expected (normal in concurrent systems)."""
    error = ConflictError("/path/file.txt", "etag-old", "etag-new")
    assert error.is_expected is True
    assert error.path == "/path/file.txt"
    assert error.expected_etag == "etag-old"
    assert error.current_etag == "etag-new"


def test_audit_log_error_is_unexpected() -> None:
    """Test AuditLogError is classified as unexpected (critical infrastructure)."""
    error = AuditLogError("Database write failed", path="/audit/log")
    assert error.is_expected is False
    assert error.path == "/audit/log"


def test_authentication_error_is_expected() -> None:
    """Test AuthenticationError is classified as expected (user auth issue)."""
    error = AuthenticationError("Token expired")
    assert error.is_expected is True
