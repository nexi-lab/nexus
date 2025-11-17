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
    NotFoundError,
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
    assert issubclass(PermissionDeniedError, NexusError)
    assert issubclass(ValidationError, NexusError)
    assert issubclass(ConflictError, NexusError)
    assert issubclass(AuditLogError, NexusError)
    assert issubclass(AuthenticationError, NexusError)

    # All should also be standard Exceptions
    assert issubclass(NexusError, Exception)


def test_permission_denied_error() -> None:
    """Test PermissionDeniedError for ReBAC operations."""
    # Without path
    error = PermissionDeniedError("No permission to read skill 'my-skill'")
    assert "No permission to read skill 'my-skill'" in str(error)
    assert error.path is None

    # With path
    error = PermissionDeniedError("User lacks 'approve' permission", path="/skills/my-skill")
    assert "User lacks 'approve' permission" in str(error)
    assert "/skills/my-skill" in str(error)
    assert error.path == "/skills/my-skill"


def test_validation_error() -> None:
    """Test ValidationError."""
    # Without path
    error = ValidationError("name is required")
    assert str(error) == "name is required"
    assert error.path is None

    # With path
    error = ValidationError("size cannot be negative", path="/data/file.txt")
    assert "size cannot be negative" in str(error)
    assert "/data/file.txt" in str(error)
    assert error.path == "/data/file.txt"


def test_conflict_error() -> None:
    """Test ConflictError for optimistic concurrency."""
    expected_etag = "abc123def456ghi789jkl012mno345pqr678"
    current_etag = "xyz987wvu654tsr321qpo098nml765kji432"

    error = ConflictError("/workspace/file.txt", expected_etag, current_etag)

    # Check attributes
    assert error.path == "/workspace/file.txt"
    assert error.expected_etag == expected_etag
    assert error.current_etag == current_etag

    # Check message format (shows truncated etags)
    assert "Conflict detected" in str(error)
    assert "abc123def456ghi7" in str(error)  # First 16 chars of expected
    assert "xyz987wvu654tsr3" in str(error)  # First 16 chars of current
    assert "/workspace/file.txt" in str(error)


def test_audit_log_error() -> None:
    """Test AuditLogError for audit compliance."""
    # Without path or original error
    error = AuditLogError("Audit logging failed")
    assert str(error) == "Audit logging failed"
    assert error.path is None
    assert error.original_error is None

    # With path
    error = AuditLogError("Audit logging failed", path="/workspace/file.txt")
    assert "Audit logging failed" in str(error)
    assert "/workspace/file.txt" in str(error)
    assert error.path == "/workspace/file.txt"

    # With original error
    original = ValueError("Database connection failed")
    error = AuditLogError("Audit logging failed", original_error=original)
    assert error.original_error is original
    assert str(error) == "Audit logging failed"


def test_authentication_error() -> None:
    """Test AuthenticationError for OAuth and auth systems."""
    # Without path
    error = AuthenticationError("No OAuth credential found for google:user@example.com")
    assert "No OAuth credential found" in str(error)
    assert error.path is None

    # With path
    error = AuthenticationError("Failed to refresh token", path="/credentials/google")
    assert "Failed to refresh token" in str(error)
    assert "/credentials/google" in str(error)
    assert error.path == "/credentials/google"


def test_not_found_error_alias() -> None:
    """Test NotFoundError alias for NexusFileNotFoundError."""
    # Verify it's the same class
    assert NotFoundError is NexusFileNotFoundError

    # Can be used interchangeably
    error1 = NotFoundError("/missing.txt")
    error2 = NexusFileNotFoundError("/missing.txt")

    assert isinstance(error1, type(error2))  # noqa: E721
    assert isinstance(error1, NexusFileNotFoundError)
    assert isinstance(error2, NotFoundError)
