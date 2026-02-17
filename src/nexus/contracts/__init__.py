"""Tier-neutral shared contracts for the Nexus VFS (Issue #1501).

This package is the canonical home for types and exceptions that are shared
across kernel, bricks, services, and backends.  It has **zero** runtime
imports from ``nexus.core`` or any other kernel module.

Usage:
    from nexus.contracts import OperationContext, Permission, NexusError
    from nexus.contracts.types import ContextIdentity, extract_context_identity
    from nexus.contracts.exceptions import BackendError, ValidationError
"""

from nexus.contracts.exceptions import (
    AccessDeniedError,
    AuditLogError,
    AuthenticationError,
    BackendError,
    BootError,
    BranchConflictError,
    BranchError,
    BranchExistsError,
    BranchNotFoundError,
    BranchProtectedError,
    BranchStateError,
    CircuitOpenError,
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
    LockTimeout,
    MetadataError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ParserError,
    PathNotMountedError,
    PermissionDeniedError,
    RemoteConnectionError,
    RemoteFilesystemError,
    RemoteTimeoutError,
    ServiceUnavailableError,
    StalePointerError,
    StaleSessionError,
    UploadChecksumMismatchError,
    UploadExpiredError,
    UploadNotFoundError,
    UploadOffsetMismatchError,
    ValidationError,
)
from nexus.contracts.registry import BaseRegistry, BrickInfo, BrickRegistry
from nexus.contracts.types import (
    ContextIdentity,
    OperationContext,
    Permission,
    extract_context_identity,
)

__all__ = [
    # Registry
    "BaseRegistry",
    "BrickInfo",
    "BrickRegistry",
    # Types
    "ContextIdentity",
    "OperationContext",
    "Permission",
    "extract_context_identity",
    # Exceptions
    "AccessDeniedError",
    "AuditLogError",
    "AuthenticationError",
    "BackendError",
    "BootError",
    "BranchConflictError",
    "BranchError",
    "BranchExistsError",
    "BranchNotFoundError",
    "BranchProtectedError",
    "BranchStateError",
    "CircuitOpenError",
    "ConflictError",
    "ConnectorAuthError",
    "ConnectorError",
    "ConnectorQuotaError",
    "ConnectorRateLimitError",
    "DatabaseConnectionError",
    "DatabaseError",
    "DatabaseIntegrityError",
    "DatabaseTimeoutError",
    "InvalidPathError",
    "LockTimeout",
    "MetadataError",
    "NexusError",
    "NexusFileNotFoundError",
    "NexusPermissionError",
    "ParserError",
    "PathNotMountedError",
    "PermissionDeniedError",
    "RemoteConnectionError",
    "RemoteFilesystemError",
    "RemoteTimeoutError",
    "ServiceUnavailableError",
    "StalePointerError",
    "StaleSessionError",
    "UploadChecksumMismatchError",
    "UploadExpiredError",
    "UploadNotFoundError",
    "UploadOffsetMismatchError",
    "ValidationError",
]
