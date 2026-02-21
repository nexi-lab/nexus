"""Delegation service protocol (ops-scenario-matrix S23: Agent Delegation).

Defines the contract for agent identity delegation — coordinator agents
provisioning worker agents with narrower permissions via namespace
derivation (COPY / CLEAN / SHARED modes).

Storage Affinity: **RecordStore** (delegation records, agent registration,
                  API keys) + **ReBAC** (permission tuples).

References:
    - docs/architecture/ops-scenario-matrix.md  (S23)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #1287: Extract NexusFS domain services from god object
    - Issue #2131: Extract delegation into nexus/bricks/delegation
"""

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.services.delegation.models import (
        DelegationMode,
        DelegationOutcome,
        DelegationRecord,
        DelegationResult,
        DelegationScope,
        DelegationStatus,
    )


@runtime_checkable
class DelegationProtocol(Protocol):
    """Service contract for agent identity delegation.

    Mirrors ``bricks/delegation/service.DelegationService``.

    Concrete implementations coordinate ReBAC, entity registry,
    namespace manager, and API key auth to provision delegated agents.
    """

    def delegate(
        self,
        coordinator_agent_id: str,
        coordinator_owner_id: str,
        worker_id: str,
        worker_name: str,
        delegation_mode: "DelegationMode | Any",
        zone_id: str | None = None,
        scope_prefix: str | None = None,
        remove_grants: list[str] | None = None,
        add_grants: list[str] | None = None,
        readonly_paths: list[str] | None = None,
        ttl_seconds: int | None = None,
        intent: str = "",
        can_sub_delegate: bool = False,
        scope: "DelegationScope | None" = None,
        min_trust_score: float = 0.0,
    ) -> "DelegationResult | Any":
        """Create a delegated worker agent with narrowed permissions.

        Args:
            coordinator_agent_id: The coordinator agent creating the delegation.
            coordinator_owner_id: The user who owns the coordinator agent.
            worker_id: Desired ID for the worker agent.
            worker_name: Human-readable name for the worker.
            delegation_mode: How to derive grants (COPY/CLEAN/SHARED).
            zone_id: Zone isolation scope.
            scope_prefix: Optional path prefix filter.
            remove_grants: Paths to exclude (COPY mode).
            add_grants: Paths to include (CLEAN mode).
            readonly_paths: Paths to downgrade to viewer (COPY mode).
            ttl_seconds: Delegation TTL in seconds (max 86400).
            intent: Immutable purpose description for audit trail.
            can_sub_delegate: Whether worker can create further delegations.
            scope: Fine-grained operation/resource/budget constraints.
            min_trust_score: Minimum trust score for coordinator (0.0 = disabled).

        Returns:
            DelegationResult with worker agent ID, API key, and mount table.
        """
        ...

    def revoke_delegation(self, delegation_id: str) -> bool:
        """Revoke a delegation: delete grants, revoke API key, remove record.

        Args:
            delegation_id: The delegation to revoke.

        Returns:
            True if revoked successfully.
        """
        ...

    def list_delegations(
        self,
        parent_agent_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        status_filter: "DelegationStatus | None" = None,
    ) -> "tuple[list[DelegationRecord | Any], int]":
        """List all delegations created by a coordinator agent.

        Args:
            parent_agent_id: The coordinator agent ID.
            limit: Maximum records to return (default 50).
            offset: Number of records to skip (default 0).
            status_filter: Optional filter by status.

        Returns:
            Tuple of (records, total_count).
        """
        ...

    def get_delegation_by_id(self, delegation_id: str) -> "DelegationRecord | Any | None":
        """Get delegation record by delegation_id.

        Args:
            delegation_id: The delegation record ID.

        Returns:
            DelegationRecord or None if not found.
        """
        ...

    def get_delegation(self, agent_id: str) -> "DelegationRecord | Any | None":
        """Get delegation record for a worker agent.

        Args:
            agent_id: The worker agent ID.

        Returns:
            DelegationRecord or None if not a delegated agent.
        """
        ...

    def get_delegation_chain(self, delegation_id: str) -> "list[DelegationRecord | Any]":
        """Trace delegation chain from child to root.

        Args:
            delegation_id: Starting delegation ID.

        Returns:
            List of DelegationRecord from child to root.
        """
        ...

    def complete_delegation(
        self,
        delegation_id: str,
        outcome: "DelegationOutcome | Any",
        quality_score: float | None = None,
    ) -> "DelegationRecord | Any":
        """Complete a delegation and submit feedback to the reputation system.

        Args:
            delegation_id: The delegation to complete.
            outcome: How the delegation ended (COMPLETED/FAILED/TIMEOUT).
            quality_score: Optional quality rating (0.0-1.0) for COMPLETED outcome.

        Returns:
            Updated DelegationRecord.
        """
        ...
