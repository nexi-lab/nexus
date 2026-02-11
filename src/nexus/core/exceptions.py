"""Custom exceptions for Nexus filesystem operations.

Exception Classification:
    All Nexus exceptions have an `is_expected` attribute that distinguishes
    user errors (expected) from system errors (unexpected):

    Expected errors (is_expected=True):
        - User input validation failures
        - Resource not found (user requested non-existent item)
        - Permission denied (user lacks access)
        - Conflicts (optimistic concurrency)
        These are logged at INFO level without stack traces.

    Unexpected errors (is_expected=False):
        - Backend/infrastructure failures
        - Internal state corruption
        - Bugs and unhandled conditions
        These are logged at ERROR level with full stack traces.

Usage:
    try:
        result = operation()
    except NexusError as e:
        if e.is_expected:
            logger.info(f"Expected error: {e}")
        else:
            logger.error(f"System error: {e}", exc_info=True)
"""


class NexusError(Exception):
    """Base exception for all Nexus errors.

    Attributes:
        message: Human-readable error description
        path: Optional file/resource path for context
        is_expected: Whether this is an expected user error (True) or
                     unexpected system error (False). Subclasses set
                     appropriate defaults.
    """

    is_expected: bool = False  # Default: unexpected (system error)

    def __init__(self, message: str, path: str | None = None, is_expected: bool | None = None):
        self.message = message
        self.path = path
        # Allow instance override of class default
        if is_expected is not None:
            self.is_expected = is_expected
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        """Format error message with optional path."""
        if self.path:
            return f"{self.message}: {self.path}"
        return self.message


class NexusFileNotFoundError(NexusError, FileNotFoundError):
    """Raised when a file or directory does not exist.

    This is an expected error - the user requested a resource that doesn't exist.
    """

    is_expected = True  # User asked for non-existent resource

    def __init__(self, path: str, message: str | None = None):
        msg = message or "File not found"
        super().__init__(msg, path)


class NexusPermissionError(NexusError):
    """Raised when access to a file or directory is denied.

    This is an expected error - the user attempted an operation they lack
    permissions for.
    """

    is_expected = True  # User lacks required permissions

    def __init__(self, path: str, message: str | None = None):
        msg = message or "Permission denied"
        super().__init__(msg, path)


class PermissionDeniedError(NexusError):
    """Raised when ReBAC permission check fails.

    This is an expected error - the user attempted an operation they lack
    ReBAC permissions for.

    Examples:
        >>> raise PermissionDeniedError("No permission to read skill 'my-skill'")
        >>> raise PermissionDeniedError("User lacks 'approve' permission", path="/skills/my-skill")
    """

    is_expected = True  # User lacks ReBAC permissions

    def __init__(self, message: str, path: str | None = None):
        super().__init__(message, path)


class StaleSessionError(NexusError):
    """Raised when agent's session generation is stale (Issue #1240).

    A newer session has been established for this agent, invalidating the
    current session. The client should re-authenticate or obtain a new session.

    This is an expected error — maps to HTTP 409 Conflict.
    """

    is_expected = True  # Agent session was superseded by a newer one

    def __init__(self, agent_id: str, message: str | None = None):
        self.agent_id = agent_id
        msg = message or f"Agent session expired for '{agent_id}'"
        super().__init__(msg)


class BackendError(NexusError):
    """Raised when a backend operation fails.

    This is an unexpected error - indicates infrastructure/system failure
    that requires investigation.
    """

    is_expected = False  # System/infrastructure failure

    def __init__(self, message: str, backend: str | None = None, path: str | None = None):
        self.backend = backend
        if backend:
            message = f"[{backend}] {message}"
        super().__init__(message, path)


class InvalidPathError(NexusError):
    """Raised when a path is invalid or contains illegal characters.

    This is an expected error - the user provided an invalid path.
    """

    is_expected = True  # User provided invalid input

    def __init__(self, path: str, message: str | None = None):
        msg = message or "Invalid path"
        super().__init__(msg, path)


class MetadataError(NexusError):
    """Raised when metadata operations fail.

    This is an unexpected error - indicates internal state corruption
    or system failure.
    """

    is_expected = False  # Internal state/system failure

    def __init__(self, message: str, path: str | None = None, is_expected: bool | None = None):
        super().__init__(message, path, is_expected)


class ValidationError(NexusError):
    """Raised when validation fails.

    This is an expected error - the user provided invalid input that
    failed validation. Should be converted to HTTP 400 Bad Request.

    Examples:
        >>> raise ValidationError("name is required")
        >>> raise ValidationError("size cannot be negative", path="/data/file.txt")
    """

    is_expected = True  # User input validation failure

    def __init__(self, message: str, path: str | None = None, is_expected: bool | None = None):
        super().__init__(message, path, is_expected)


class ParserError(NexusError):
    """Raised when document parsing fails.

    This is an expected error - the user provided a document that
    couldn't be parsed (unsupported format, corrupted, etc.).
    """

    is_expected = True  # User provided unparseable document

    def __init__(self, message: str, path: str | None = None, parser: str | None = None):
        self.parser = parser
        if parser:
            message = f"[{parser}] {message}"
        super().__init__(message, path)


class ConflictError(NexusError):
    """Raised when optimistic concurrency check fails.

    This is an expected error - indicates concurrent modification which
    is a normal condition in multi-agent systems.

    Agents must handle this error explicitly by:
    1. Retrying with a fresh read
    2. Merging changes
    3. Aborting the operation
    4. Force overwriting (dangerous)

    Examples:
        >>> try:
        ...     nx.write(path, content, if_match=old_etag)
        ... except ConflictError as e:
        ...     print(f"Conflict: expected {e.expected_etag}, got {e.current_etag}")
        ...     # Retry with fresh read
        ...     result = nx.read(path, return_metadata=True)
        ...     nx.write(path, content, if_match=result['etag'])
    """

    is_expected = True  # Normal condition in concurrent systems

    def __init__(self, path: str, expected_etag: str, current_etag: str):
        """Initialize conflict error.

        Args:
            path: Virtual file path that had the conflict
            expected_etag: The etag value that was expected (from if_match)
            current_etag: The actual current etag value in the database
        """
        self.expected_etag = expected_etag
        self.current_etag = current_etag
        message = (
            f"Conflict detected - file was modified by another agent. "
            f"Expected etag '{expected_etag[:16]}...', but current etag is '{current_etag[:16]}...'"
        )
        super().__init__(message, path)


class LockTimeout(NexusError):
    """Raised when a distributed lock cannot be acquired within timeout.

    This is an expected error - indicates the resource is currently locked
    by another agent/process. The caller should retry or abort.

    Examples:
        >>> try:
        ...     async with nx.locked("/shared/config.json", timeout=5.0):
        ...         # do work
        ... except LockTimeout:
        ...     print("Resource is busy, try again later")
    """

    is_expected = True  # Normal condition in concurrent systems

    def __init__(self, path: str, timeout: float, message: str | None = None):
        """Initialize lock timeout error.

        Args:
            path: Virtual file path that could not be locked
            timeout: The timeout value that was exceeded
            message: Optional custom message
        """
        self.timeout = timeout
        msg = message or f"Could not acquire lock within {timeout}s"
        super().__init__(msg, path)


class AuditLogError(NexusError):
    """Raised when audit logging fails and audit_strict_mode is enabled.

    This is an unexpected error - indicates critical infrastructure failure
    that requires immediate investigation. P0 COMPLIANCE issue.

    P0 COMPLIANCE: This exception prevents operations from succeeding without
    proper audit trail, ensuring compliance with SOX, HIPAA, GDPR, PCI DSS.

    When audit_strict_mode=True (default):
    - Write operations FAIL if audit logging fails
    - Ensures complete audit trail for compliance
    - Prevents silent audit gaps

    When audit_strict_mode=False:
    - Write operations SUCCEED even if audit logging fails
    - Failure is logged at CRITICAL level
    - Use only in high-availability scenarios where availability > auditability
    """

    is_expected = False  # Critical infrastructure failure

    def __init__(
        self, message: str, path: str | None = None, original_error: Exception | None = None
    ):
        self.original_error = original_error
        super().__init__(message, path)


class AuthenticationError(NexusError):
    """Raised when authentication fails.

    This is an expected error - the user's credentials are invalid or expired.
    Common in OAuth flows when tokens need refresh.

    Examples:
        >>> raise AuthenticationError("No OAuth credential found for google:user@example.com")
        >>> raise AuthenticationError("Failed to refresh token: refresh_token revoked")
    """

    is_expected = True  # User auth issue (invalid/expired credentials)

    def __init__(self, message: str, path: str | None = None):
        super().__init__(message, path)


# Alias for convenience (used in time-travel debugging)
NotFoundError = NexusFileNotFoundError
