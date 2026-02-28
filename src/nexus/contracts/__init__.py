"""Tier-neutral shared contracts for the Nexus VFS (Issue #1501).

This package is the canonical home for types and exceptions that are shared
across kernel, bricks, services, and backends.  It has **zero** runtime
imports from ``nexus.core`` or any other kernel module.

Usage:
    from nexus.contracts import OperationContext, Permission, NexusError
    from nexus.contracts.types import ContextIdentity, extract_context_identity
    from nexus.contracts.exceptions import BackendError, ValidationError
"""

from nexus.contracts.access_manifest_types import (
    AccessManifest,
    ManifestEntry,
    ToolPermission,
)
from nexus.contracts.cache_store import CacheStoreABC, NullCacheStore
from nexus.contracts.constants import TIER_ALIASES, PriorityTier
from nexus.contracts.credential_types import (
    DEFAULT_CREDENTIAL_TTL,
    MAX_CAPABILITIES_PER_CREDENTIAL,
    MAX_CREDENTIAL_TTL,
    MAX_DELEGATION_DEPTH,
    MIN_CREDENTIAL_TTL,
    VC_CONTEXT,
    VC_TYPES,
    Ability,
    Capability,
    CredentialClaims,
    CredentialStatus,
)
from nexus.contracts.describable import Describable
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
    CredentialError,
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
from nexus.contracts.metadata import (
    DT_DIR,
    DT_MOUNT,
    DT_PIPE,
    DT_REG,
    FileMetadata,
)
from nexus.contracts.rebac_types import (
    CROSS_ZONE_ALLOWED_RELATIONS,
    WILDCARD_SUBJECT,
    CheckResult,
    ConsistencyLevel,
    ConsistencyMode,
    ConsistencyRequirement,
    Entity,
    GraphLimitExceeded,
    GraphLimits,
    TraversalStats,
    WriteResult,
)
from nexus.contracts.search_types import GlobStrategy, SearchStrategy
from nexus.contracts.types import (
    ContextIdentity,
    OperationContext,
    Permission,
    extract_context_identity,
)
from nexus.contracts.wirable_fs import WirableFS
from nexus.contracts.write_observer import WriteObserverProtocol
from nexus.lib.validators import (
    EmailAddress,
    EmailList,
    EmailListRequired,
    ISODateTimeStr,
)

__all__ = [
    # Fourth Pillar ABC (CacheStore — ephemeral KV + Pub/Sub)
    "CacheStoreABC",
    "NullCacheStore",
    # Constants (shared across bricks)
    "PriorityTier",
    "TIER_ALIASES",
    # Metadata types (Issue #891 — moved from core/ to contracts/)
    "DT_DIR",
    "DT_MOUNT",
    "DT_PIPE",
    "DT_REG",
    "FileMetadata",
    # Validators
    "EmailAddress",
    "EmailList",
    "EmailListRequired",
    "ISODateTimeStr",
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
    "CredentialError",
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
    # Protocols
    "Describable",
    "WirableFS",
    "WriteObserverProtocol",
    # ReBAC types (Issue #2190)
    "CheckResult",
    "ConsistencyLevel",
    "ConsistencyMode",
    "ConsistencyRequirement",
    "CROSS_ZONE_ALLOWED_RELATIONS",
    "Entity",
    "GraphLimitExceeded",
    "GraphLimits",
    "TraversalStats",
    "WILDCARD_SUBJECT",
    "WriteResult",
    # Search types (Issue #2190)
    "GlobStrategy",
    "SearchStrategy",
    # Credential types (Issue #1753)
    "Ability",
    "Capability",
    "CredentialClaims",
    "CredentialStatus",
    "DEFAULT_CREDENTIAL_TTL",
    "MAX_CAPABILITIES_PER_CREDENTIAL",
    "MAX_CREDENTIAL_TTL",
    "MAX_DELEGATION_DEPTH",
    "MIN_CREDENTIAL_TTL",
    "VC_CONTEXT",
    "VC_TYPES",
    # Access manifest types (Issue #1754)
    "AccessManifest",
    "ManifestEntry",
    "ToolPermission",
]
