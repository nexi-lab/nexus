"""Tier-neutral domain types for the Nexus VFS (Issue #1501).

Canonical home for shared types imported by 72+ files across the codebase.
This module has **zero** runtime imports from ``nexus.*`` --- only stdlib --- so
bricks, services, and backends can depend on it without pulling in kernel
internals.

Backward compatibility:
    ``from nexus.core.types import OperationContext, Permission`` still works
    via re-exports in ``core/types.py``.

    ``from nexus.core.permissions import OperationContext, Permission`` still
    works via re-exports in ``core/permissions.py``.

Types:
    - ``Permission``: IntFlag for file operation permissions (read/write/execute/traverse).
    - ``OperationContext``: Dataclass carrying auth context through filesystem operations.
    - ``ContextIdentity``: Frozen dataclass for extracted zone/user/admin identity.
    - ``extract_context_identity()``: DRY helper to extract identity from OperationContext.
"""

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
    """Context for file operations with subject identity.

    This class carries authentication and authorization context through
    all filesystem operations to enable permission checking.

    Attributes:
        user_id: Human owner / subject ID performing the operation.
        agent_id: Agent ID if operation is from agent (optional).
        subject_type: Type of subject (user, agent, service, session).
        subject_id: Unique identifier for the subject.
        groups: List of group IDs the subject belongs to.
        zone_id: Zone/organization ID for multi-zone isolation (optional).
        is_admin: Whether the subject has admin privileges.
        is_system: Whether this is a system operation (bypasses all checks).
        admin_capabilities: Set of granted admin capabilities.
        request_id: Unique ID for audit trail correlation.
        backend_path: Backend-relative path for connector backends (optional).

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

    # Read Set Tracking for Query Dependencies (Issue #1166)
    read_set: "ReadSet | None" = None
    track_reads: bool = False

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
            >>> from nexus.core.read_set import enable_read_tracking
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

        zone_id = getattr(context, "zone_id", None) or "root"
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
        return ContextIdentity(zone_id="root", user_id="anonymous", is_admin=False)
    return ContextIdentity(
        zone_id=getattr(context, "zone_id", None) or "root",
        user_id=(
            getattr(context, "user_id", None) or getattr(context, "subject_id", None) or "anonymous"
        ),
        is_admin=getattr(context, "is_admin", False),
    )
