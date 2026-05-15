"""Unit tests for Nexus exceptions."""

import inspect

from nexus.contracts.exceptions import (
    AccessDeniedError,
    AuditLogError,
    AuthenticationError,
    BackendError,
    ConflictError,
    ConnectorAuthError,
    ConnectorError,
    ConnectorQuotaError,
    ConnectorRateLimitError,
    DatabaseConnectionError,
    DatabaseError,
    DatabaseIntegrityError,
    DatabaseTimeoutError,
    InvalidPathError,
    MetadataError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ParserError,
    PathNotMountedError,
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
    error = ParserError("Parsing failed", parser="PdfInspector")
    assert "[PdfInspector]" in str(error)
    assert error.parser == "PdfInspector"

    # With parser and path
    error = ParserError("Parsing failed", path="/test/file.pdf", parser="PdfInspector")
    assert "[PdfInspector]" in str(error)
    assert "/test/file.pdf" in str(error)
    assert error.parser == "PdfInspector"
    assert error.path == "/test/file.pdf"


def test_exception_inheritance() -> None:
    """Test that all custom exceptions inherit from NexusError."""
    assert issubclass(NexusFileNotFoundError, NexusError)
    assert issubclass(NexusPermissionError, NexusError)
    assert issubclass(BackendError, NexusError)
    assert issubclass(InvalidPathError, NexusError)
    assert issubclass(MetadataError, NexusError)
    assert issubclass(ParserError, NexusError)
    assert issubclass(AccessDeniedError, NexusError)
    assert issubclass(PathNotMountedError, NexusError)

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
    assert error.expected_content_id == "etag-old"
    assert error.current_content_id == "etag-new"


def test_audit_log_error_is_unexpected() -> None:
    """Test AuditLogError is classified as unexpected (critical infrastructure)."""
    error = AuditLogError("Database write failed", path="/audit/log")
    assert error.is_expected is False
    assert error.path == "/audit/log"


def test_authentication_error_is_expected() -> None:
    """Test AuthenticationError is classified as expected (user auth issue)."""
    error = AuthenticationError("Token expired")
    assert error.is_expected is True


# ============================================================================
# Issue #1460: Unified Exception Hierarchy Tests
# ============================================================================


def test_all_nexus_error_subclasses_have_is_expected() -> None:
    """Every NexusError subclass must define is_expected as a class attribute.

    Uses introspection to auto-discover all subclasses so new exceptions
    added in the future are automatically covered.
    """
    import nexus.contracts.exceptions as exc_module

    for name, cls in inspect.getmembers(exc_module, inspect.isclass):
        if issubclass(cls, NexusError) and cls is not NexusError:
            assert hasattr(cls, "is_expected"), f"{name} missing is_expected class attribute"
            assert isinstance(cls.is_expected, bool), (
                f"{name}.is_expected must be bool, got {type(cls.is_expected)}"
            )


def test_permission_denied_is_subclass_of_nexus_permission_error() -> None:
    """PermissionDeniedError must be a subclass of NexusPermissionError (Issue #1460).

    This ensures a single 'except NexusPermissionError' catches both
    generic permission errors and ReBAC-specific permission errors.
    """
    assert issubclass(PermissionDeniedError, NexusPermissionError)
    assert issubclass(PermissionDeniedError, NexusError)

    # Verify catch works: except NexusPermissionError catches PermissionDeniedError
    caught = False
    try:
        raise PermissionDeniedError("ReBAC denied")
    except NexusPermissionError:
        caught = True
    assert caught, "except NexusPermissionError must catch PermissionDeniedError"


def test_permission_denied_error_attributes() -> None:
    """PermissionDeniedError preserves path and message after reparenting."""
    error = PermissionDeniedError("No read access", path="/workspace/secret.txt")
    assert error.is_expected is True
    assert error.path == "/workspace/secret.txt"
    assert "No read access" in str(error)


def test_access_denied_error() -> None:
    """AccessDeniedError (from router) now inherits NexusError."""
    error = AccessDeniedError("Namespace 'system' requires admin privileges")
    assert isinstance(error, NexusError)
    assert error.is_expected is True
    assert error.path is None
    assert "Namespace 'system'" in str(error)

    # With path
    error = AccessDeniedError("Zone isolation violation", path="/shared/zone-a/file.txt")
    assert error.path == "/shared/zone-a/file.txt"


def test_path_not_mounted_error() -> None:
    """PathNotMountedError (from router) now inherits NexusError."""
    error = PathNotMountedError("/unmounted/path")
    assert isinstance(error, NexusError)
    assert error.is_expected is True
    assert error.path == "/unmounted/path"
    assert "No mount found for path" in str(error)

    # Custom message
    error = PathNotMountedError("/custom", message="Backend offline")
    assert "Backend offline" in str(error)
    assert error.path == "/custom"


def test_not_found_error_alias_removed() -> None:
    """NotFoundError alias must be removed from exceptions module (Issue #1460)."""
    import nexus.contracts.exceptions as exc_module

    assert not hasattr(exc_module, "NotFoundError"), (
        "NotFoundError alias should be removed; use NexusFileNotFoundError directly"
    )


def test_router_exceptions_in_error_handler() -> None:
    """Error handler must map router exceptions to correct HTTP status codes."""
    from unittest.mock import MagicMock

    from nexus.server.error_handlers import nexus_error_handler

    request = MagicMock()

    # AccessDeniedError → 403
    resp = nexus_error_handler(request, AccessDeniedError("Admin required"))
    assert resp.status_code == 403

    # PathNotMountedError → 404
    resp = nexus_error_handler(request, PathNotMountedError("/no/mount"))
    assert resp.status_code == 404

    # PermissionDeniedError (subclass of NexusPermissionError) → 403
    resp = nexus_error_handler(request, PermissionDeniedError("ReBAC denied"))
    assert resp.status_code == 403

    # NexusPermissionError → 403
    resp = nexus_error_handler(request, NexusPermissionError("/test"))
    assert resp.status_code == 403

    # InvalidPathError → 400
    resp = nexus_error_handler(request, InvalidPathError("/bad\x00path"))
    assert resp.status_code == 400

    # NexusFileNotFoundError → 404
    resp = nexus_error_handler(request, NexusFileNotFoundError("/missing"))
    assert resp.status_code == 404

    # BackendError → 502
    resp = nexus_error_handler(request, BackendError("Connection failed"))
    assert resp.status_code == 502

    # Generic NexusError → 500
    resp = nexus_error_handler(request, NexusError("Unknown"))
    assert resp.status_code == 500


def test_error_handler_response_includes_is_expected() -> None:
    """Error handler response body must include is_expected flag."""
    import json
    from unittest.mock import MagicMock

    from nexus.server.error_handlers import nexus_error_handler

    request = MagicMock()

    # Expected error
    resp = nexus_error_handler(request, AccessDeniedError("Admin required"))
    body = json.loads(resp.body)
    assert body["is_expected"] is True

    # Unexpected error
    resp = nexus_error_handler(request, BackendError("DB down"))
    body = json.loads(resp.body)
    assert body["is_expected"] is False


def test_all_nexus_error_subclasses_have_status_code() -> None:
    """Every NexusError subclass must define status_code and error_type.

    Uses introspection to auto-discover all subclasses so new exceptions
    added in the future are automatically covered (Issue #1519, option 7A).
    """
    import nexus.contracts.exceptions as exc_module

    for name, cls in inspect.getmembers(exc_module, inspect.isclass):
        if issubclass(cls, NexusError) and cls is not NexusError:
            assert hasattr(cls, "status_code"), f"{name} missing status_code class attribute"
            assert isinstance(cls.status_code, int), (
                f"{name}.status_code must be int, got {type(cls.status_code)}"
            )
            assert 400 <= cls.status_code <= 599, (
                f"{name}.status_code must be 4xx or 5xx, got {cls.status_code}"
            )
            assert hasattr(cls, "error_type"), f"{name} missing error_type class attribute"
            assert isinstance(cls.error_type, str), (
                f"{name}.error_type must be str, got {type(cls.error_type)}"
            )


def test_status_code_class_values() -> None:
    """Verify specific status_code mappings match HTTP semantics."""
    from nexus.contracts.exceptions import (
        LockTimeout,
        ServiceUnavailableError,
        StaleSessionError,
        UploadChecksumMismatchError,
        UploadExpiredError,
        UploadNotFoundError,
        UploadOffsetMismatchError,
    )

    # 4xx expected errors
    assert NexusFileNotFoundError.status_code == 404
    assert NexusPermissionError.status_code == 403
    assert PermissionDeniedError.status_code == 403
    assert InvalidPathError.status_code == 400
    assert ValidationError.status_code == 400
    assert AuthenticationError.status_code == 401
    assert PathNotMountedError.status_code == 404
    assert AccessDeniedError.status_code == 403
    assert ConflictError.status_code == 409
    assert StaleSessionError.status_code == 409
    assert ParserError.status_code == 422
    assert LockTimeout.status_code == 423

    # 5xx unexpected errors
    assert NexusError.status_code == 500
    assert BackendError.status_code == 502
    assert ServiceUnavailableError.status_code == 503
    assert MetadataError.status_code == 500
    assert AuditLogError.status_code == 500

    # Upload-specific
    assert UploadNotFoundError.status_code == 404
    assert UploadExpiredError.status_code == 410
    assert UploadOffsetMismatchError.status_code == 409
    assert UploadChecksumMismatchError.status_code == 460


def test_error_handler_uses_class_status_code() -> None:
    """Error handler reads status_code from exception class, not isinstance chain."""
    from unittest.mock import MagicMock

    from nexus.contracts.exceptions import (
        LockTimeout,
        ServiceUnavailableError,
        StaleSessionError,
    )
    from nexus.server.error_handlers import nexus_error_handler

    request = MagicMock()

    # Verify each exception type gets its class-defined status_code
    cases = [
        (NexusFileNotFoundError("/test"), 404),
        (NexusPermissionError("/test"), 403),
        (PermissionDeniedError("denied"), 403),
        (InvalidPathError("/bad"), 400),
        (ValidationError("invalid"), 400),
        (AuthenticationError("expired"), 401),
        (PathNotMountedError("/none"), 404),
        (AccessDeniedError("no access"), 403),
        (ConflictError("/file", "e1", "e2"), 409),
        (StaleSessionError("agent-1"), 409),
        (ParserError("cannot parse"), 422),
        (BackendError("down"), 502),
        (ServiceUnavailableError("circuit open"), 503),
        (MetadataError("db error"), 500),
        (AuditLogError("audit failed"), 500),
        (LockTimeout("/locked", 5.0), 423),
        (NexusError("generic"), 500),
    ]
    for exc, expected_code in cases:
        resp = nexus_error_handler(request, exc)
        assert resp.status_code == expected_code, (
            f"{type(exc).__name__} expected {expected_code}, got {resp.status_code}"
        )


def test_kernel_uses_canonical_exceptions() -> None:
    """Kernel path_utils must use exceptions from contracts.exceptions, not local definitions."""
    from nexus.core import path_utils as path_utils_module

    # path_utils should use the canonical exception classes
    for name in ("InvalidPathError",):
        cls = getattr(path_utils_module, name, None)
        if cls is not None:
            assert issubclass(cls, NexusError), (
                f"path_utils.{name} must be a NexusError subclass, got bases: {cls.__bases__}"
            )


# ============================================================================
# Database & Connector Exception Hierarchy Tests (Issue #1254)
# ============================================================================


def test_database_error_hierarchy() -> None:
    """Test DatabaseError inherits BackendError → NexusError."""
    assert issubclass(DatabaseError, BackendError)
    assert issubclass(DatabaseError, NexusError)
    assert issubclass(DatabaseConnectionError, DatabaseError)
    assert issubclass(DatabaseTimeoutError, DatabaseError)
    assert issubclass(DatabaseIntegrityError, DatabaseError)


def test_database_error_creation() -> None:
    """Test DatabaseError can be created with message and optional path."""
    error = DatabaseError("Connection lost")
    assert "Connection lost" in str(error)
    assert error.is_expected is False

    error = DatabaseError("Query failed", path="/data/table")
    assert "Query failed" in str(error)
    assert error.path == "/data/table"


def test_database_children_is_expected() -> None:
    """Test is_expected classification for DatabaseError subtypes."""
    # Connection errors are unexpected (infrastructure failure)
    assert DatabaseConnectionError("Connection refused").is_expected is False
    # Timeout errors are unexpected (infrastructure failure)
    assert DatabaseTimeoutError("Query timed out").is_expected is False
    # Integrity errors are expected (user-caused, e.g., duplicate key)
    assert DatabaseIntegrityError("Duplicate key").is_expected is True


def test_database_error_caught_by_backend_error() -> None:
    """Test that except BackendError catches DatabaseError and children."""
    with_caught = []
    for exc_class in (
        DatabaseError,
        DatabaseConnectionError,
        DatabaseTimeoutError,
        DatabaseIntegrityError,
    ):
        try:
            raise exc_class("test")
        except BackendError:
            with_caught.append(exc_class.__name__)
    assert len(with_caught) == 4


def test_connector_error_hierarchy() -> None:
    """Test ConnectorError inherits BackendError → NexusError."""
    assert issubclass(ConnectorError, BackendError)
    assert issubclass(ConnectorError, NexusError)
    assert issubclass(ConnectorAuthError, ConnectorError)
    assert issubclass(ConnectorRateLimitError, ConnectorError)
    assert issubclass(ConnectorQuotaError, ConnectorError)


def test_connector_error_creation() -> None:
    """Test ConnectorError can be created with message and optional path."""
    error = ConnectorError("API call failed")
    assert "API call failed" in str(error)
    assert error.is_expected is False

    error = ConnectorError("Timeout", path="/mnt/gmail")
    assert error.path == "/mnt/gmail"


def test_connector_children_is_expected() -> None:
    """Test is_expected classification for ConnectorError subtypes."""
    # Auth errors are expected (user needs to re-authenticate)
    assert ConnectorAuthError("Token expired").is_expected is True
    # Rate limit errors are expected (transient)
    assert ConnectorRateLimitError("Rate limited").is_expected is True
    # Quota errors are expected (user-caused)
    assert ConnectorQuotaError("Quota exceeded").is_expected is True
    # Base connector error is unexpected (generic failure)
    assert ConnectorError("Unknown failure").is_expected is False


def test_connector_error_caught_by_backend_error() -> None:
    """Test that except BackendError catches ConnectorError and children."""
    with_caught = []
    for exc_class in (
        ConnectorError,
        ConnectorAuthError,
        ConnectorRateLimitError,
        ConnectorQuotaError,
    ):
        try:
            raise exc_class("test")
        except BackendError:
            with_caught.append(exc_class.__name__)
    assert len(with_caught) == 4


def test_connector_validation_error_caught_by_core() -> None:
    """Test that connectors.base.ValidationError is caught by core ValidationError."""
    from nexus.backends.connectors.base import ValidationError as ConnectorValidationError

    assert issubclass(ConnectorValidationError, ValidationError)

    # Verify catch works
    try:
        raise ConnectorValidationError(
            code="TEST",
            message="test error",
        )
    except ValidationError:
        pass  # Should be caught


def test_is_expected_defaults_new_types() -> None:
    """Test is_expected class attributes for new exception types."""
    assert DatabaseError.is_expected is False
    assert DatabaseConnectionError.is_expected is False
    assert DatabaseTimeoutError.is_expected is False
    assert DatabaseIntegrityError.is_expected is True
    assert ConnectorError.is_expected is False
    assert ConnectorAuthError.is_expected is True
    assert ConnectorRateLimitError.is_expected is True
    assert ConnectorQuotaError.is_expected is True
