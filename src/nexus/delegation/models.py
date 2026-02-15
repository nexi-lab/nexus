"""Delegation domain models (Issue #1271).

Defines the core domain objects for agent delegation:
- DelegationMode: Enum for delegation namespace modes
- DelegationRecord: Frozen dataclass for immutable delegation snapshots
- DelegationResult: Return type from delegation operations

Follows AgentRecord pattern: frozen dataclass + SQLAlchemy model separation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
        scope_prefix: Optional path prefix filter applied to grants.
        lease_expires_at: When the delegation expires (None = no expiry).
        removed_grants: Paths explicitly removed (COPY mode).
        added_grants: Paths explicitly added (CLEAN mode).
        readonly_paths: Paths downgraded to viewer (COPY mode).
        zone_id: Zone isolation scope.
        created_at: When the delegation was created.
    """

    delegation_id: str
    agent_id: str
    parent_agent_id: str
    delegation_mode: DelegationMode
    scope_prefix: str | None
    lease_expires_at: datetime | None
    removed_grants: tuple[str, ...]
    added_grants: tuple[str, ...]
    readonly_paths: tuple[str, ...]
    zone_id: str | None
    created_at: datetime


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
