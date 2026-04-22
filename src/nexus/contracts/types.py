"""Tier-neutral domain types for the Nexus VFS (Issue #1501).

Canonical home for shared types imported by 72+ files across the codebase.
This module has zero runtime ``nexus.*`` imports --- no kernel internals ---
so bricks, services, and backends can depend on it safely.

Types:
    - ``Permission``: IntFlag for file operation permissions (read/write/execute/traverse).
    - ``OperationContext``: Dataclass carrying auth context through filesystem operations.
    - ``ContextIdentity``: Frozen dataclass for extracted zone/user/admin identity.
    - ``extract_context_identity()``: DRY helper to extract identity from OperationContext.
"""

import logging
import uuid
from dataclasses import dataclass, field
from enum import IntFlag, StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from nexus.storage.read_set import ReadSet

logger = logging.getLogger(__name__)


@runtime_checkable
class VFSOperations(Protocol):
    """Minimal async VFS interface that extracted services depend on.

    NexusFS satisfies this naturally.  Services receive it via DI so they
    never import from ``nexus.core`` at runtime.
    """

    def mkdir(
        self, path: str, parents: bool = True, exist_ok: bool = True, context: Any = None
    ) -> None: ...

    def sys_write(self, path: str, buf: bytes | str, *, context: Any = None) -> int: ...

    def write(self, path: str, buf: bytes | str, *, context: Any = None) -> dict: ...

    def sys_read(self, path: str, *, context: Any = None) -> bytes: ...

    def access(self, path: str, context: Any = None) -> bool: ...

    def sys_readdir(self, path: str = "/", **kw: Any) -> list: ...

    def sys_unlink(self, path: str, **kw: Any) -> None: ...


class Permission(IntFlag):
    """Permission flags for file operations.

    Note: These are still IntFlag for backward compatibility with
    bit operations, but they map to ReBAC permissions:
    - READ -> "read" permission
    - WRITE -> "write" permission
    - EXECUTE -> "execute" permission
    - TRAVERSE -> "traverse" permission (can stat/access by name, but not list contents)

    TRAVERSE is similar to Unix execute permission on directories - it allows
    accessing a path by name without the ability to list its contents.
    This enables O(1) permission checks for path traversal in FUSE operations.
    """

    NONE = 0
    EXECUTE = 1  # x
    WRITE = 2  # w
    READ = 4  # r
    TRAVERSE = 8  # t - can traverse/stat but not list (like Unix x on directories)
    ALL = 7  # rwx (does not include TRAVERSE by default)
    ALL_WITH_TRAVERSE = 15  # rwxt


@dataclass
class OperationContext:
    """Context for file operations with subject identity.

    This class carries authentication and authorization context through
    all filesystem operations to enable permission checking.

    Attributes:
        user_id: Human owner / subject ID performing the operation.
        agent_id: Agent ID if operation is from agent (optional).
        subject_type: Type of subject (user, agent, service, session).
        subject_id: Unique identifier for the subject.
        groups: List of group IDs the subject belongs to.
        zone_id: Kernel namespace partition ID for multi-zone isolation (optional).
        is_admin: Whether the subject has admin privileges.
        is_system: Whether this is a system operation (bypasses all checks).
        admin_capabilities: Set of granted admin capabilities.
        request_id: Unique ID for audit trail correlation.
        backend_path: Backend-relative path for connector backends (optional).
        mount_path: Mount point for the backend, e.g. "/gws/gmail" (Issue #3728).
            Populated by the router when a request is dispatched to a mounted backend.
            Used by the virtual ``.readme/`` overlay to render absolute paths in
            auto-generated skill docs without per-connector instance state.

    Examples:
        >>> ctx = OperationContext(
        ...     user_id="alice",
        ...     groups=["developers"],
        ...     zone_id="org_acme"
        ... )
    """

    user_id: str
    groups: list[str]
    zone_id: str | None = None
    agent_id: str | None = None  # Agent identity (optional)
    agent_generation: int | None = None  # Session generation counter (Issue #1240)
    is_admin: bool = False
    is_system: bool = False

    subject_type: str = "user"
    subject_id: str | None = None  # If None, uses self.user_id

    admin_capabilities: set[str] = field(default_factory=set)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # Backend path for path-based connectors (GCS, S3, etc.)
    backend_path: str | None = None
    virtual_path: str | None = None  # Full virtual path with mount prefix (for cache keys)
    mount_path: str | None = None  # Mount point for the backend (Issue #3728)

    # Read Set Tracking for Query Dependencies (Issue #1166)
    read_set: "ReadSet | None" = None
    track_reads: bool = False

    # TTL for ephemeral content — routes to TTL-bucketed volumes (Issue #3405)
    ttl_seconds: float | None = None

    def __post_init__(self) -> None:
        """Validate context and apply defaults."""
        if self.subject_id is None:
            self.subject_id = self.user_id

        if not self.user_id:
            raise ValueError("user_id is required")
        if not isinstance(self.groups, list):
            raise TypeError(f"groups must be list, got {type(self.groups)}")

    def get_subject(self) -> tuple[str, str]:
        """Get subject as (type, id) tuple for ReBAC.

        Returns properly typed subject for permission checking.

        Returns:
            Tuple of (subject_type, subject_id)

        Example:
            >>> ctx = OperationContext(user_id="alice", groups=[])
            >>> ctx.get_subject()
            ('user', 'alice')
            >>> ctx = OperationContext(
            ...     user_id="alice",
            ...     agent_id="agent_data_analyst",
            ...     subject_type="agent",
            ...     subject_id="agent_data_analyst",
            ...     groups=[]
            ... )
            >>> ctx.get_subject()
            ('agent', 'agent_data_analyst')
        """
        return (self.subject_type, self.subject_id or self.user_id)

    def record_read(
        self,
        resource_type: str,
        resource_id: str,
        revision: int,
        access_type: str = "content",
    ) -> None:
        """Record a resource read for dependency tracking (Issue #1166).

        This method is called automatically by instrumented operations
        (read, list, stat) when track_reads=True.

        Args:
            resource_type: Type of resource (file, directory, metadata)
            resource_id: Path or identifier of the resource
            revision: Current revision of the resource
            access_type: Type of access (content, metadata, list, exists)

        Example:
            >>> from nexus.storage.read_set import enable_read_tracking
            >>> ctx = OperationContext(user_id="alice", groups=[], track_reads=True)
            >>> enable_read_tracking(ctx, "zone1")
            >>> ctx.record_read("file", "/inbox/a.txt", revision=10)
            >>> len(ctx.read_set)
            1
        """
        if not self.track_reads or self.read_set is None:
            return

        self.read_set.record_read(
            resource_type=resource_type,
            resource_id=resource_id,
            revision=revision,
            access_type=access_type,
        )

    def disable_read_tracking(self) -> None:
        """Disable read tracking.

        The read_set is preserved so it can still be registered/inspected.
        """
        self.track_reads = False


@dataclass(frozen=True)
class ContextIdentity:
    """Extracted identity from OperationContext (DRY helper).

    Replaces the pattern::

        zone_id = getattr(context, "zone_id", None) or ROOT_ZONE_ID
        user_id = getattr(context, "user_id", None) or "anonymous"
        is_admin = getattr(context, "is_admin", False)

    which appears 10+ times across mixins.
    """

    zone_id: str
    user_id: str
    is_admin: bool


def extract_context_identity(context: OperationContext | None) -> ContextIdentity:
    """Extract zone/user/admin from an OperationContext.

    Safe to call with ``None`` -- returns sensible defaults.

    Args:
        context: Optional OperationContext from a request.

    Returns:
        Frozen ContextIdentity with zone_id, user_id, is_admin.
    """
    if context is None:
        return ContextIdentity(zone_id=ROOT_ZONE_ID, user_id="anonymous", is_admin=False)
    return ContextIdentity(
        zone_id=getattr(context, "zone_id", None) or ROOT_ZONE_ID,
        user_id=(
            getattr(context, "user_id", None) or getattr(context, "subject_id", None) or "anonymous"
        ),
        is_admin=getattr(context, "is_admin", False),
    )


# ---------------------------------------------------------------------------
# Transaction Protocol (moved from nexus.bricks.pay.audit_types, Issue #2129)
# ---------------------------------------------------------------------------


def parse_operation_context(context: OperationContext | dict | None = None) -> OperationContext:
    """Parse a context dict or OperationContext into a canonical OperationContext.

    This is the tier-neutral equivalent of ``nexus.core.context_utils.parse_context``.
    Services in ``nexus.services.*`` should import from here to avoid depending on
    ``nexus.core``.

    Args:
        context: Optional dict or OperationContext.

    Returns:
        OperationContext instance with sensible defaults.
    """
    if isinstance(context, OperationContext):
        return context

    if context is None:
        context = {}

    return OperationContext(
        user_id=context.get("user_id", "system"),
        groups=context.get("groups", []),
        zone_id=context.get("zone_id"),
        agent_id=context.get("agent_id"),
        is_admin=context.get("is_admin", False),
        is_system=context.get("is_system", False),
        subject_type=context.get("subject_type", "user"),
        subject_id=context.get("subject_id"),
        admin_capabilities=set(context.get("admin_capabilities", ())),
        backend_path=context.get("backend_path"),
        virtual_path=context.get("virtual_path"),
        mount_path=context.get("mount_path"),
    )


class TransactionProtocol(StrEnum):
    """Payment protocol used for the transaction.

    Issue #1360 Phase 1: Transaction Audit Log types.
    Stored as String columns (not PG ENUM) for forward-compatible schema evolution.
    """

    X402 = "x402"
    ACP = "acp"
    AP2 = "ap2"
    INTERNAL = "internal"


class WriteMode(StrEnum):
    """Write consistency mode for file operations (Issue #2929).

    Maps to existing Metastore consistency parameter:
        SYNC  → consistency="sc" (strong, blocks until committed)
        ASYNC → consistency="ec" (eventual, returns write token)

    PRIMARY_SYNC is deferred — requires async side-effect orchestration.
    Stored as String for forward-compatible schema evolution.
    """

    SYNC = "sync"
    ASYNC = "async"

    def to_metastore_consistency(self) -> str:
        """Map WriteMode to Metastore consistency parameter."""
        if self == WriteMode.SYNC:
            return "sc"
        return "ec"


# ---------------------------------------------------------------------------
# Snapshot types (moved from nexus.contracts.protocols.transactional_snapshot, Issue #194)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SnapshotId:
    """Opaque identifier for a transactional snapshot."""

    id: str


# ---------------------------------------------------------------------------
# Audit configuration (moved from nexus.core.config, Issue #959)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditConfig:
    """Audit trail error-policy configuration (Issue #2152).

    Controls what happens when audit logging (RecordStore sync) fails
    during write operations. This is a P0 compliance concern — separate
    from permission enforcement (PermissionConfig).

    P0 COMPLIANCE: SOX, HIPAA, GDPR, PCI DSS require complete audit
    trails. ``strict_mode=True`` (default) ensures writes fail if audit
    logging fails, preventing silent audit gaps.

    Note on observers: ``strict_mode`` is enforced by the synchronous
    ``RecordStoreWriteObserver``.  The OBSERVE-phase observer receives
    events from the Rust kernel; error handling (retry + drop) is
    managed by the debounced flush, not by ``strict_mode``.
    """

    strict_mode: bool = True


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Result of a content write operation.

    Attributes:
        content_id: Opaque content identifier — the primary key for
            addressing this content in future read/delete/exists calls.
            CAS backends: SHA-256 hex digest.
            PAS backends: blob path (e.g., "prefix/data/file.txt").
        version: OCC (optimistic concurrency control) token.  Kernel uses
            this to detect concurrent modifications (not content_id).
            CAS backends: same as content_id (hash IS the version).
            PAS backends: cloud version_id or content hash.
        size: Content size in bytes (0 = unknown / not tracked).
    """

    content_id: str
    version: str = ""
    size: int = 0

    @property
    def content_hash(self) -> str:
        """Backward-compatible alias for legacy callers/tests."""
        return self.content_id
