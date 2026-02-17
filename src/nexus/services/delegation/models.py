"""Delegation domain models (Issue #1271).

Defines the core domain objects for agent delegation:
- DelegationMode: Enum for delegation namespace modes
- DelegationRecord: Frozen dataclass for immutable delegation snapshots
- DelegationResult: Return type from delegation operations

Follows AgentRecord pattern: frozen dataclass + SQLAlchemy model separation.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum


class DelegationMode(Enum):
    """Delegation namespace mode.

    COPY: Start with parent grants, narrowed by scope/removals/readonly.
    CLEAN: Empty namespace, only explicitly added grants (must be subset of parent).
    SHARED: Same view as parent (within optional scope_prefix).
    """

    COPY = "copy"
    CLEAN = "clean"
    SHARED = "shared"

class DelegationOutcome(Enum):
    """Outcome of a completed delegation (#1619).

    Used by ``complete_delegation()`` to determine feedback signal:
    - COMPLETED: Positive feedback on reliability + quality.
    - FAILED: Negative feedback on reliability.
    - TIMEOUT: Negative feedback on timeliness.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"

class DelegationStatus(Enum):
    """Delegation lifecycle state.

    ACTIVE: Delegation is live — worker can authenticate and operate.
    REVOKED: Coordinator explicitly revoked the delegation.
    EXPIRED: TTL elapsed (set by background cleanup or on-access check).
    COMPLETED: Worker finished its task and delegation was closed.
    """

    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    COMPLETED = "completed"

@dataclass(frozen=True)
class DelegationScope:
    """Fine-grained scope constraints for a delegation (#1618).

    Orthogonal to DelegationMode: mode determines WHICH grants are derived,
    scope constrains WHAT the worker can do with those grants.

    Attributes:
        allowed_operations: Set of permitted operations (read, write, execute).
            Empty means all operations allowed (backward compat).
        resource_patterns: Glob patterns for allowed resources.
            Empty means all resources within derived grants.
        budget_limit: Maximum spend in NexusPay credits (None = no limit).
        max_depth: Maximum sub-delegation depth (0 = cannot sub-delegate).
    """

    allowed_operations: frozenset[str] = field(default_factory=frozenset)
    resource_patterns: frozenset[str] = field(default_factory=frozenset)
    budget_limit: Decimal | None = None
    max_depth: int = 0

@dataclass(frozen=True)
class DelegationRecord:
    """Immutable snapshot of a delegation relationship.

    Records that a coordinator agent delegated a narrower identity
    to a worker agent. The record captures the delegation parameters
    at creation time for audit and revocation.

    Attributes:
        delegation_id: Unique delegation identifier.
        agent_id: Worker agent that received delegated identity.
        parent_agent_id: Coordinator agent that created the delegation.
        delegation_mode: How grants were derived (COPY/CLEAN/SHARED).
        status: Lifecycle state (ACTIVE/REVOKED/EXPIRED/COMPLETED).
        scope_prefix: Optional path prefix filter applied to grants.
        scope: Fine-grained operation/resource/budget constraints.
        lease_expires_at: When the delegation expires (None = no expiry).
        removed_grants: Paths explicitly removed (COPY mode).
        added_grants: Paths explicitly added (CLEAN mode).
        readonly_paths: Paths downgraded to viewer (COPY mode).
        zone_id: Zone isolation scope.
        intent: Immutable purpose description (audit trail).
        parent_delegation_id: Parent delegation for chain tracking (None = root).
        depth: Chain depth (0 = direct delegation from non-delegated agent).
        can_sub_delegate: Whether this worker can create further delegations.
        created_at: When the delegation was created.
    """

    delegation_id: str
    agent_id: str
    parent_agent_id: str
    delegation_mode: DelegationMode
    status: DelegationStatus = DelegationStatus.ACTIVE
    scope_prefix: str | None = None
    scope: DelegationScope | None = None
    lease_expires_at: datetime | None = None
    removed_grants: tuple[str, ...] = ()
    added_grants: tuple[str, ...] = ()
    readonly_paths: tuple[str, ...] = ()
    zone_id: str | None = None
    intent: str = ""
    parent_delegation_id: str | None = None
    depth: int = 0
    can_sub_delegate: bool = False
    created_at: datetime | None = None

@dataclass(frozen=True)
class DelegationResult:
    """Result of a successful delegation operation.

    Returned by DelegationService.delegate() with everything the
    coordinator needs to configure the worker agent.

    Attributes:
        delegation_id: Unique delegation identifier.
        worker_agent_id: The delegated worker agent ID.
        api_key: Raw API key for the worker (shown only once).
        mount_table: Worker's visible namespace paths.
        expires_at: When the delegation expires.
        delegation_mode: Mode used for grant derivation.
    """

    delegation_id: str
    worker_agent_id: str
    api_key: str
    mount_table: list[str]
    expires_at: datetime | None
    delegation_mode: DelegationMode
