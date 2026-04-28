"""Tier-neutral exception types for the Nexus VFS (Issue #1501).

Canonical home for all shared exception classes.  This module has **zero**
runtime imports from ``nexus.*`` — only stdlib — so bricks, services, and
backends can depend on it without pulling in kernel internals.

Exception Classification:
    All Nexus exceptions have an ``is_expected`` attribute that distinguishes
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

from collections.abc import Mapping
from typing import Any


class NexusError(Exception):
    """Base exception for all Nexus errors.

    Attributes:
        message: Human-readable error description
        path: Optional file/resource path for context
        is_expected: Whether this is an expected user error (True) or
                     unexpected system error (False). Subclasses set
                     appropriate defaults.
        status_code: HTTP status code for this error type. Subclasses
                     override with the appropriate code (e.g. 404, 403).
                     Used by error_handlers.py to avoid cascading isinstance.
        error_type: Short HTTP error label (e.g. "Not Found", "Forbidden").
    """

    is_expected: bool = False  # Default: unexpected (system error)
    status_code: int = 500
    error_type: str = "Internal Server Error"

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


class BootError(NexusError):
    """Fatal boot-time error — a kernel-tier service failed to initialize.

    Raised when a service required for NexusFS boot cannot be constructed.
    This is an unexpected error — indicates a misconfiguration or missing
    dependency that prevents the system from starting.
    """

    is_expected = False  # Fatal boot failure

    def __init__(self, message: str, *, tier: str = "kernel", service_name: str = "") -> None:
        super().__init__(message)
        self.tier = tier
        self.service_name = service_name


class NexusFileNotFoundError(NexusError, FileNotFoundError):
    """Raised when a file or directory does not exist.

    This is an expected error - the user requested a resource that doesn't exist.
    """

    is_expected = True  # User asked for non-existent resource
    status_code = 404
    error_type = "Not Found"

    def __init__(self, path: str, message: str | None = None):
        msg = message or "File not found"
        super().__init__(msg, path)


class NexusPermissionError(NexusError):
    """Raised when access to a file or directory is denied.

    This is an expected error - the user attempted an operation they lack
    permissions for.
    """

    is_expected = True  # User lacks required permissions
    status_code = 403
    error_type = "Forbidden"

    def __init__(self, path: str, message: str | None = None):
        msg = message or "Permission denied"
        super().__init__(msg, path)


class PermissionDeniedError(NexusPermissionError):
    """Raised when ReBAC permission check fails.

    Subclass of NexusPermissionError — can be caught by
    ``except NexusPermissionError`` for unified permission handling.

    This is an expected error - the user attempted an operation they lack
    ReBAC permissions for.

    Examples:
        >>> raise PermissionDeniedError("No permission to read skill 'my-skill'")
        >>> raise PermissionDeniedError("User lacks 'approve' permission", path="/skills/my-skill")
    """

    is_expected = True  # User lacks ReBAC permissions
    status_code = 403
    error_type = "Forbidden"

    def __init__(self, message: str, path: str | None = None):
        super().__init__(path=path or "", message=message)


class StaleSessionError(NexusError):
    """Raised when agent's session generation is stale (Issue #1240).

    A newer session has been established for this agent, invalidating the
    current session. The client should re-authenticate or obtain a new session.

    This is an expected error — maps to HTTP 409 Conflict.
    """

    is_expected = True  # Agent session was superseded by a newer one
    status_code = 409
    error_type = "Conflict"

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
    status_code = 502
    error_type = "Bad Gateway"

    def __init__(self, message: str, backend: str | None = None, path: str | None = None):
        self.backend = backend
        if backend:
            message = f"[{backend}] {message}"
        super().__init__(message, path)


class MissingDependencyError(BackendError):
    """One or more runtime dependencies for a connector are missing.

    Raised by ``BackendFactory.create()`` when a connector's
    ``RUNTIME_DEPS`` cannot be satisfied in the current environment.  Each
    entry in ``missing`` is a ``(dep, human_reason)`` pair — the reason
    string already contains the install hint.

    This is an **expected** error: the user has an actionable path
    forward (install the hint, switch profiles, etc.), so it is logged at
    INFO level without stack traces.

    Attributes:
        backend: connector name that failed to mount
        missing: list of (RuntimeDep, reason) pairs for every unmet dep

    Note:
        The ``missing`` list is typed as ``list[tuple[Any, str]]`` rather
        than ``list[tuple[RuntimeDep, str]]`` so this module can stay
        stdlib-only (see module docstring). Callers in
        ``nexus.backends.base.runtime_deps`` pass ``RuntimeDep`` instances.
    """

    is_expected = True  # User-correctable — install the dep
    status_code = 424
    error_type = "Failed Dependency"

    def __init__(
        self,
        backend: str,
        missing: list[tuple[Any, str]],
    ) -> None:
        self.missing = missing
        count = len(missing)
        lines = [f"missing {count} runtime dep(s)"]
        for _, reason in missing:
            lines.append(f"  - {reason}")
        super().__init__("\n".join(lines), backend=backend)


class DatabaseError(BackendError):
    """Database operation failed. Wraps SQLAlchemy errors at storage boundary.

    This is an unexpected error — indicates database infrastructure failure.
    """

    is_expected = False

    def __init__(self, message: str, path: str | None = None):
        super().__init__(message, path=path)


class DatabaseConnectionError(DatabaseError):
    """Database connection failed (transient, should retry)."""

    pass


class DatabaseTimeoutError(DatabaseError):
    """Database query timed out."""

    pass


class DatabaseIntegrityError(DatabaseError):
    """Database integrity constraint violated (permanent, should not retry).

    This is an expected error — caused by user actions (e.g., duplicate key).
    """

    is_expected = True


class ConnectorError(BackendError):
    """External connector/API operation failed."""

    is_expected = False

    def __init__(self, message: str, path: str | None = None):
        super().__init__(message, path=path)


class ConnectorAuthError(ConnectorError):
    """Connector authentication/token refresh failed.

    This is an expected error — user needs to re-authenticate.
    """

    status_code = 401
    error_type = "Unauthorized"
    is_expected = True


class ConnectorRateLimitError(ConnectorError):
    """Connector hit rate limit (transient, should retry with backoff).

    This is an expected error — external API rate limiting.
    """

    status_code = 429
    error_type = "Too Many Requests"
    is_expected = True


class ConnectorQuotaError(ConnectorError):
    """Connector quota exceeded.

    This is an expected error — user/org quota limit reached.
    """

    is_expected = True


class RemoteFilesystemError(NexusError):
    """Enhanced remote filesystem error with detailed information.

    Raised when RPC/HTTP communication with a remote Nexus server fails.
    This is an unexpected error — indicates network or remote infrastructure failure.

    Defined in contracts/exceptions so both nexus.remote and nexus.fuse can
    import without cross-layer coupling.
    """

    is_expected = False  # Network / remote infrastructure failure

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
        method: str | None = None,
    ):
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        self.details = details or {}
        self.method = method

        error_parts = [message]
        if method:
            error_parts.append(f"(method: {method})")
        if status_code:
            error_parts.append(f"[HTTP {status_code}]")

        super().__init__(" ".join(error_parts))


class RemoteConnectionError(RemoteFilesystemError):
    """Error connecting to remote Nexus server."""

    pass


class RemoteTimeoutError(RemoteFilesystemError):
    """Timeout while communicating with remote server."""

    pass


class ConfigurationError(NexusError):
    """Raised when a required service or provider is not configured.

    This is an unexpected error — indicates a missing dependency or
    misconfiguration that should be fixed by an operator, not an end user.
    Maps to HTTP 500 Internal Server Error.
    """

    is_expected = False  # Misconfiguration, operator must fix
    status_code = 500
    error_type = "Internal Server Error"

    def __init__(self, message: str, path: str | None = None):
        super().__init__(message, path)


class ServiceUnavailableError(NexusError):
    """Service temporarily unavailable (e.g., circuit breaker open).

    This is an unexpected error — indicates infrastructure degradation
    that may self-heal. Maps to HTTP 503 Service Unavailable.
    """

    is_expected = False  # Infrastructure failure
    status_code = 503
    error_type = "Service Unavailable"

    def __init__(self, message: str, path: str | None = None):
        super().__init__(message, path)


class CircuitOpenError(ServiceUnavailableError):
    """Circuit breaker is open — database unreachable.

    Raised when the circuit breaker detects repeated infrastructure failures
    and short-circuits requests to fail fast, preventing cascade failures.

    This is an unexpected error — maps to HTTP 503 Service Unavailable.
    """

    is_expected = False  # Infrastructure failure (circuit open)
    status_code = 503
    error_type = "Service Unavailable"

    def __init__(self, service_name: str, message: str | None = None):
        self.service_name = service_name
        msg = message or f"Circuit breaker open for '{service_name}'"
        super().__init__(msg)


class InvalidPathError(NexusError):
    """Raised when a path is invalid or contains illegal characters.

    This is an expected error - the user provided an invalid path.
    """

    is_expected = True  # User provided invalid input
    status_code = 400
    error_type = "Bad Request"

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
    status_code = 400
    error_type = "Bad Request"

    def __init__(self, message: str, path: str | None = None, is_expected: bool | None = None):
        super().__init__(message, path, is_expected)


class ParserError(NexusError):
    """Raised when document parsing fails.

    This is an expected error - the user provided a document that
    couldn't be parsed (unsupported format, corrupted, etc.).
    """

    is_expected = True  # User provided unparseable document
    status_code = 422
    error_type = "Unprocessable Entity"

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
        ...     nx.write(path, content)
        ... except ConflictError as e:
        ...     print(f"Conflict: expected {e.expected_content_id}, got {e.current_content_id}")
        ...     # Retry with fresh read
        ...     result = nx.read(path, return_metadata=True)
        ...     nx.write(path, result['content'])
    """

    is_expected = True  # Normal condition in concurrent systems
    status_code = 409
    error_type = "Conflict"

    def __init__(self, path: str, expected_content_id: str, current_content_id: str):
        """Initialize conflict error.

        Args:
            path: Virtual file path that had the conflict
            expected_content_id: The content_id (if_match) value that was expected
            current_content_id: The actual current content_id value in the database
        """
        self.expected_content_id = expected_content_id
        self.current_content_id = current_content_id
        message = (
            f"Conflict detected - file was modified by another agent. "
            f"Expected content_id '{expected_content_id[:16]}...', "
            f"but current content_id is '{current_content_id[:16]}...'"
        )
        super().__init__(message, path)


class LockTimeout(NexusError):
    """Raised when a distributed lock cannot be acquired within timeout.

    This is an expected error - indicates the resource is currently locked
    by another agent/process. The caller should retry or abort.

    Examples:
        >>> try:
        ...     with nx.locked("/shared/config.json", timeout=5.0):
        ...         # do work
        ... except LockTimeout:
        ...     print("Resource is busy, try again later")
    """

    is_expected = True  # Normal condition in concurrent systems
    status_code = 423
    error_type = "Locked"

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
    """Raised when audit logging fails and AuditConfig.strict_mode is enabled.

    This is an unexpected error - indicates critical infrastructure failure
    that requires immediate investigation. P0 COMPLIANCE issue.

    P0 COMPLIANCE: This exception prevents operations from succeeding without
    proper audit trail, ensuring compliance with SOX, HIPAA, GDPR, PCI DSS.

    When AuditConfig(strict_mode=True) (default):
    - Write operations FAIL if audit logging fails
    - Ensures complete audit trail for compliance
    - Prevents silent audit gaps

    When AuditConfig(strict_mode=False):
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
        >>> raise AuthenticationError(
        ...     "Token expired",
        ...     provider="google",
        ...     user_email="user@example.com",
        ...     auth_url="https://accounts.google.com/o/oauth2/auth?...",
        ... )
    """

    is_expected = True  # User auth issue (invalid/expired credentials)
    status_code = 401
    error_type = "Unauthorized"

    def __init__(
        self,
        message: str,
        path: str | None = None,
        *,
        provider: str | None = None,
        user_email: str | None = None,
        auth_url: str | None = None,
        recovery_hint: Mapping[str, str | list[str]] | None = None,
    ):
        self.provider = provider
        self.user_email = user_email
        self.auth_url = auth_url
        # ``recovery_hint`` lets raisers ship a machine-actionable re-auth
        # target — e.g., ``{"endpoint": "/v2/connectors/auth/init",
        # "method": "POST", "connector": "gdrive", "provider":
        # "google-drive"}`` — so clients can drive the next step without
        # guessing.  ``auth_url`` remains for callers that want a ready
        # clickable URL; a raiser is free to set one or the other (or
        # both).
        self.recovery_hint = recovery_hint
        super().__init__(message, path)


# --- Router / Path Exceptions ---


class PathNotMountedError(NexusError):
    """Raised when no mount exists for a given path.

    This is an expected error — the user referenced a path that has no
    backend mount configured. Maps to HTTP 404 Not Found.
    """

    is_expected = True  # User referenced unmounted path
    status_code = 404
    error_type = "Not Found"

    def __init__(self, path: str, message: str | None = None):
        msg = message or "No mount found for path"
        super().__init__(msg, path)


class CredentialError(NexusError):
    """Credential operation error with structured code (Issue #1753).

    Used for credential issuance, verification, and revocation failures.
    This is an expected error — maps to HTTP 400 Bad Request.

    Codes: "expired", "revoked", "invalid_jws", "unknown_issuer",
           "invalid_signature", "malformed".
    """

    is_expected = True
    status_code = 400
    error_type = "Bad Request"

    def __init__(self, code: str, message: str, credential_id: str | None = None):
        self.code = code
        self.credential_id = credential_id
        super().__init__(message)


class NexusURIError(InvalidPathError):
    """Invalid URI format for nexus.mount().

    This is an expected error — the user provided a malformed or unsupported
    mount URI (e.g., missing scheme, unknown scheme, empty authority).

    Examples:
        >>> raise NexusURIError("bucket-name", "Missing scheme. Did you mean 's3://bucket-name'?")
        >>> raise NexusURIError("xyz://foo", "Unsupported scheme: xyz://")
    """

    is_expected = True
    status_code = 400
    error_type = "Bad Request"

    def __init__(self, uri: str, message: str | None = None):
        self.uri = uri
        msg = message or f"Invalid URI: {uri}"
        super().__init__(path=uri, message=msg)


class CloudCredentialError(NexusError):
    """Missing or invalid credentials for a cloud storage backend.

    This is an expected error — the user needs to configure credentials
    for the cloud backend (AWS, GCP, etc.).

    Distinct from CredentialError which is for JWS/internal credential operations.

    Examples:
        >>> raise CloudCredentialError("s3", "AWS credentials not found. Run `aws configure` or set AWS_ACCESS_KEY_ID")
        >>> raise CloudCredentialError("gcs", "GCP ADC not found. Run `gcloud auth application-default login`")
    """

    is_expected = True
    status_code = 401
    error_type = "Unauthorized"

    def __init__(self, backend: str, message: str | None = None):
        self.backend = backend
        msg = message or f"Credentials not found for {backend}. Run `nexus doctor` for details"
        super().__init__(msg)


class BackendNotFoundError(BackendError):
    """Cloud resource (bucket, container, project) not found.

    This is an expected error — the user referenced a cloud resource
    that doesn't exist or isn't accessible.

    Examples:
        >>> raise BackendNotFoundError("my-buket", "s3", "Bucket 'my-buket' not found")
    """

    is_expected = True
    status_code = 404
    error_type = "Not Found"

    def __init__(self, resource: str, backend: str | None = None, message: str | None = None):
        self.resource = resource
        msg = message or f"Backend resource not found: {resource}"
        super().__init__(msg, backend=backend)


class BackendPermissionError(NexusPermissionError):
    """IAM-level permission denied by cloud provider.

    This is an expected error — the cloud provider's IAM rejected the
    operation. Distinct from ReBAC permission errors (PermissionDeniedError).

    Examples:
        >>> raise BackendPermissionError("/s3/bucket/file.txt", "Access denied by AWS IAM")
    """

    is_expected = True
    status_code = 403
    error_type = "Forbidden"

    def __init__(self, path: str, message: str | None = None):
        msg = message or "Access denied by backend IAM"
        super().__init__(path=path, message=msg)


class ZoneTerminatingError(NexusError):
    """Raised when a write is attempted on a zone in Terminating phase.

    During zone deprovisioning, writes are gated while reads remain
    allowed. Maps to HTTP 409 Conflict.

    Issue #2061: Zone finalizer protocol for ordered cleanup.
    """

    is_expected = True  # Normal during zone deprovisioning
    status_code = 409
    error_type = "Conflict"

    def __init__(self, zone_id: str):
        self.zone_id = zone_id
        msg = f"Zone '{zone_id}' is being deprovisioned — writes are blocked"
        super().__init__(msg)


class AccessDeniedError(NexusError):
    """Raised when access to a path is denied by namespace or zone rules.

    Distinct from NexusPermissionError (ReBAC / file-level permissions):
    AccessDeniedError covers zone isolation, read-only namespaces, and
    admin-only namespace enforcement in the VFS router layer.

    This is an expected error — maps to HTTP 403 Forbidden.
    """

    is_expected = True  # User lacks zone/namespace-level access
    status_code = 403
    error_type = "Forbidden"

    def __init__(self, message: str, path: str | None = None):
        super().__init__(message, path)


# --- Chunked Upload Exceptions (Issue #788) ---


class UploadNotFoundError(NexusError):
    """Raised when a chunked upload session is not found.

    This is an expected error — the upload ID does not exist or was already cleaned up.
    Maps to HTTP 404.
    """

    is_expected = True
    status_code = 404
    error_type = "Not Found"

    def __init__(self, upload_id: str, message: str | None = None):
        self.upload_id = upload_id
        msg = message or f"Upload session not found: {upload_id}"
        super().__init__(msg)


class UploadExpiredError(NexusError):
    """Raised when a chunked upload session has expired.

    This is an expected error — the upload's TTL has been exceeded.
    Maps to HTTP 410 Gone.
    """

    is_expected = True
    status_code = 410
    error_type = "Gone"

    def __init__(self, upload_id: str, message: str | None = None):
        self.upload_id = upload_id
        msg = message or f"Upload session expired: {upload_id}"
        super().__init__(msg)


class UploadOffsetMismatchError(NexusError):
    """Raised when a PATCH offset does not match the current session offset.

    This is an expected error — the client sent a chunk at the wrong offset.
    Maps to HTTP 409 Conflict (tus protocol requirement).
    """

    is_expected = True
    status_code = 409
    error_type = "Conflict"

    def __init__(self, upload_id: str, expected: int, received: int):
        self.upload_id = upload_id
        self.expected_offset = expected
        self.received_offset = received
        msg = f"Upload offset mismatch for {upload_id}: expected {expected}, received {received}"
        super().__init__(msg)


class UploadChecksumMismatchError(NexusError):
    """Raised when the chunk checksum does not match the Upload-Checksum header.

    This is an expected error — data corruption detected.
    Maps to HTTP 460 (tus-specific status code).
    """

    is_expected = True
    status_code = 460
    error_type = "Checksum Mismatch"

    def __init__(self, upload_id: str, algorithm: str, message: str | None = None):
        self.upload_id = upload_id
        self.algorithm = algorithm
        msg = message or f"Checksum mismatch ({algorithm}) for upload {upload_id}"
        super().__init__(msg)


# --- Context Branch Exceptions (Issue #1315) ---


class BranchError(NexusError):
    """Base exception for context branch operations.

    All branch exceptions are expected errors — they represent user-facing
    conditions that should be handled explicitly.
    """

    is_expected = True

    def __init__(self, message: str, branch_name: str | None = None, path: str | None = None):
        self.branch_name = branch_name
        super().__init__(message, path)


class BranchNotFoundError(BranchError):
    """Raised when a branch does not exist.

    Maps to HTTP 404 Not Found.
    """

    def __init__(self, branch_name: str, workspace_path: str | None = None):
        msg = f"Branch '{branch_name}' not found"
        if workspace_path:
            msg += f" in workspace '{workspace_path}'"
        super().__init__(msg, branch_name=branch_name, path=workspace_path)


class BranchExistsError(BranchError):
    """Raised when creating a branch that already exists.

    Maps to HTTP 409 Conflict.
    """

    def __init__(self, branch_name: str, workspace_path: str | None = None):
        msg = f"Branch '{branch_name}' already exists"
        if workspace_path:
            msg += f" in workspace '{workspace_path}'"
        super().__init__(msg, branch_name=branch_name, path=workspace_path)


class BranchConflictError(BranchError):
    """Raised when a merge has conflicting files.

    Contains the list of conflicting paths for the caller to handle.
    Maps to HTTP 409 Conflict.
    """

    def __init__(
        self,
        source_branch: str,
        target_branch: str,
        conflicting_paths: list[str],
    ):
        self.source_branch = source_branch
        self.target_branch = target_branch
        self.conflicting_paths = conflicting_paths
        paths_str = ", ".join(conflicting_paths[:5])
        if len(conflicting_paths) > 5:
            paths_str += f" (and {len(conflicting_paths) - 5} more)"
        msg = (
            f"Merge conflict: {len(conflicting_paths)} file(s) changed on both "
            f"'{source_branch}' and '{target_branch}': {paths_str}"
        )
        super().__init__(msg, branch_name=source_branch)


class BranchStateError(BranchError):
    """Raised when a branch operation is invalid for the current branch state.

    Examples: merging an already-merged branch, committing to a discarded branch.
    Maps to HTTP 409 Conflict.
    """

    def __init__(self, branch_name: str, message: str):
        super().__init__(message, branch_name=branch_name)


class BranchProtectedError(BranchError):
    """Raised when attempting to delete or discard a protected branch (e.g. 'main').

    Maps to HTTP 403 Forbidden.
    """

    def __init__(self, branch_name: str, operation: str = "delete"):
        msg = f"Cannot {operation} protected branch '{branch_name}'"
        super().__init__(msg, branch_name=branch_name)


class StalePointerError(BranchError):
    """Raised when optimistic concurrency check fails on branch pointer update.

    The branch's pointer_version has changed since the caller last read it,
    indicating a concurrent modification. Caller should retry with fresh state.
    Maps to HTTP 409 Conflict.
    """

    def __init__(self, branch_name: str, expected_version: int, current_version: int):
        self.expected_version = expected_version
        self.current_version = current_version
        msg = (
            f"Stale pointer for branch '{branch_name}': "
            f"expected version {expected_version}, current is {current_version}"
        )
        super().__init__(msg, branch_name=branch_name)


# =====================================================================
# Namespace Fork errors (Issue #1273)
# =====================================================================


class NamespaceForkError(NexusError):
    """Base error for namespace fork operations.

    All namespace fork errors are expected (user-facing) — they result
    from invalid fork IDs, merge conflicts, or stale state.
    Maps to HTTP 400 Bad Request by default.
    """

    is_expected = True
    status_code = 400
    error_type = "Bad Request"

    def __init__(self, message: str, *, fork_id: str | None = None):
        self.fork_id = fork_id
        super().__init__(message)


class NamespaceForkNotFoundError(NamespaceForkError):
    """Raised when a fork_id does not exist in the active forks.

    Maps to HTTP 404 Not Found.
    """

    status_code = 404
    error_type = "Not Found"

    def __init__(self, fork_id: str):
        msg = f"Namespace fork '{fork_id}' not found"
        super().__init__(msg, fork_id=fork_id)


class NamespaceMergeConflictError(NamespaceForkError):
    """Raised when a namespace merge has conflicting paths and strategy='fail'.

    Contains the list of conflicting paths for the caller to handle.
    Maps to HTTP 409 Conflict.
    """

    status_code = 409
    error_type = "Conflict"

    def __init__(self, fork_id: str, conflicting_paths: list[str]):
        self.conflicting_paths = conflicting_paths
        paths_str = ", ".join(conflicting_paths[:5])
        if len(conflicting_paths) > 5:
            paths_str += f" (and {len(conflicting_paths) - 5} more)"
        msg = (
            f"Namespace fork '{fork_id}' merge conflict: "
            f"{len(conflicting_paths)} path(s) changed in both fork and parent: {paths_str}"
        )
        super().__init__(msg, fork_id=fork_id)
