"""Zero-dependency leaf module for shared domain types (Issue #1291).

This module contains types that are imported by 72+ files across the codebase.
It has ZERO runtime imports from ``nexus.*``, breaking the circular import hub
that previously existed in ``core/permissions.py``.

Backward compatibility:
    ``from nexus.core.permissions import OperationContext, Permission`` still works
    via re-exports in ``permissions.py``.

Types:
    - ``Permission``: IntFlag for file operation permissions (read/write/execute/traverse).
    - ``OperationContext``: Dataclass carrying auth context through filesystem operations.
    - ``ContextIdentity``: Frozen dataclass for extracted zone/user/admin identity.
    - ``extract_context_identity()``: DRY helper to extract identity from OperationContext.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from enum import IntFlag
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.core.read_set import ReadSet

logger = logging.getLogger(__name__)


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
    """Context for file operations with subject identity (v0.5.0).

    This class carries authentication and authorization context through
    all filesystem operations to enable permission checking.

    v0.5.0 ACE: Unified agent identity system
    - user_id: Human owner (always tracked)
    - agent_id: Agent identity (optional)
    - subject_type: "user" or "agent" (for authentication)
    - subject_id: Actual identity (user_id or agent_id)

    Agent lifecycle managed via API key TTL (no agent_type field needed).

    Subject-based identity supports:
    - user: Human users (alice, bob)
    - agent: AI agents (claude_001, gpt4_agent)
    - service: Backend services (backup_service, indexer)
    - session: Temporary sessions (session_abc123)

    Attributes:
        user: Subject ID performing the operation (LEGACY: use user_id)
        user_id: Human owner ID (v0.5.0: NEW, for explicit tracking)
        agent_id: Agent ID if operation is from agent (optional)
        subject_type: Type of subject (user, agent, service, session)
        subject_id: Unique identifier for the subject
        groups: List of group IDs the subject belongs to
        zone_id: Zone/organization ID for multi-zone isolation (optional)
        is_admin: Whether the subject has admin privileges
        is_system: Whether this is a system operation (bypasses all checks)
        admin_capabilities: Set of granted admin capabilities (P0-4)
        request_id: Unique ID for audit trail correlation (P0-4)
        backend_path: Backend-relative path for connector backends (optional)

    Examples:
        >>> # Human user context
        >>> ctx = OperationContext(
        ...     user="alice",
        ...     groups=["developers"],
        ...     zone_id="org_acme"
        ... )
        >>> # User-authenticated agent (uses user's auth)
        >>> ctx = OperationContext(
        ...     user="alice",
        ...     agent_id="notebook_xyz",
        ...     subject_type="user",  # Authenticates as user
        ...     groups=[]
        ... )
        >>> # Agent-authenticated (has own API key)
        >>> ctx = OperationContext(
        ...     user="alice",
        ...     agent_id="agent_data_analyst",
        ...     subject_type="agent",  # Authenticates as agent
        ...     subject_id="agent_data_analyst",
        ...     groups=[]
        ... )
    """

    user: str  # LEGACY: Kept for backward compatibility (maps to user_id)
    groups: list[str]
    zone_id: str | None = None
    agent_id: str | None = None  # Agent identity (optional)
    agent_generation: int | None = None  # Session generation counter (Issue #1240)
    is_admin: bool = False
    is_system: bool = False

    # v0.5.0 ACE: Unified agent identity
    user_id: str | None = None  # NEW: Human owner (auto-populated from user if None)

    # P0-2: Subject-based identity
    subject_type: str = "user"  # Default to "user" for backward compatibility
    subject_id: str | None = None  # If None, uses self.user

    # P0-4: Admin capabilities and audit trail
    admin_capabilities: set[str] = field(default_factory=set)  # Scoped admin capabilities
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # Audit trail correlation ID

    # Backend path for path-based connectors (GCS, S3, etc.)
    backend_path: str | None = None  # Backend-relative path for connector backends
    virtual_path: str | None = None  # Full virtual path with mount prefix (for cache keys)

    # Issue #1166: Read Set Tracking for Query Dependencies
    # When track_reads=True, operations automatically record what they read
    # to enable precise cache invalidation and efficient subscription updates
    read_set: ReadSet | None = None  # Read set for this operation (lazy-initialized)
    track_reads: bool = False  # Enable read tracking for this operation

    def __post_init__(self) -> None:
        """Validate context and apply defaults."""
        # v0.5.0: Auto-populate user_id from user if not provided
        if self.user_id is None:
            self.user_id = self.user

        # P0-2: If subject_id not provided, use user field for backward compatibility
        if self.subject_id is None:
            self.subject_id = self.user

        if not self.user:
            raise ValueError("user is required")
        if not isinstance(self.groups, list):
            raise TypeError(f"groups must be list, got {type(self.groups)}")

    def get_subject(self) -> tuple[str, str]:
        """Get subject as (type, id) tuple for ReBAC.

        Returns properly typed subject for permission checking.

        Returns:
            Tuple of (subject_type, subject_id)

        Example:
            >>> ctx = OperationContext(user="alice", groups=[])
            >>> ctx.get_subject()
            ('user', 'alice')
            >>> ctx = OperationContext(
            ...     user="alice",
            ...     agent_id="agent_data_analyst",
            ...     subject_type="agent",
            ...     subject_id="agent_data_analyst",
            ...     groups=[]
            ... )
            >>> ctx.get_subject()
            ('agent', 'agent_data_analyst')
        """
        return (self.subject_type, self.subject_id or self.user)

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
            >>> ctx = OperationContext(user="alice", groups=[], track_reads=True)
            >>> ctx.enable_read_tracking("zone1")
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

    def enable_read_tracking(self, zone_id: str | None = None) -> None:
        """Enable read tracking and initialize read set (Issue #1166).

        Call this before operations to track what resources are accessed.
        After the operation completes, the read_set can be registered
        with the ReadSetRegistry for subscription updates.

        Args:
            zone_id: Zone ID for the read set (defaults to self.zone_id)

        Example:
            >>> ctx = OperationContext(user="alice", groups=[], zone_id="org1")
            >>> ctx.enable_read_tracking()
            >>> # ... perform operations ...
            >>> registry.register(ctx.read_set)
        """
        from nexus.core.read_set import ReadSet

        self.track_reads = True
        self.read_set = ReadSet.create(zone_id=zone_id or self.zone_id or "default")

    def disable_read_tracking(self) -> None:
        """Disable read tracking.

        The read_set is preserved so it can still be registered/inspected.
        """
        self.track_reads = False


@dataclass(frozen=True)
class ContextIdentity:
    """Extracted identity from OperationContext (DRY helper).

    Replaces the pattern::

        zone_id = getattr(context, "zone_id", None) or "default"
        user_id = getattr(context, "user", None) or "anonymous"
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
        return ContextIdentity(zone_id="default", user_id="anonymous", is_admin=False)
    return ContextIdentity(
        zone_id=getattr(context, "zone_id", None) or "default",
        user_id=(
            getattr(context, "user", None) or getattr(context, "subject_id", None) or "anonymous"
        ),
        is_admin=getattr(context, "is_admin", False),
    )
