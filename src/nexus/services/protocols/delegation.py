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
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DelegationProtocol(Protocol):
    """Service contract for agent identity delegation.

    Mirrors ``services/delegation/service.DelegationService``.

    Concrete implementations coordinate ReBAC, entity registry,
    namespace manager, and API key auth to provision delegated agents.
    """

    def delegate(
        self,
        coordinator_agent_id: str,
        coordinator_owner_id: str,
        worker_id: str,
        worker_name: str,
        delegation_mode: Any,
        zone_id: str | None = None,
        scope_prefix: str | None = None,
        remove_grants: list[str] | None = None,
        add_grants: list[str] | None = None,
        readonly_paths: list[str] | None = None,
        ttl_seconds: int | None = None,
    ) -> Any:
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

    def list_delegations(self, parent_agent_id: str) -> list[Any]:
        """List all delegations created by a coordinator agent.

        Args:
            parent_agent_id: The coordinator agent ID.

        Returns:
            List of DelegationRecord objects.
        """
        ...

    def get_delegation_by_id(self, delegation_id: str) -> Any | None:
        """Get delegation record by delegation_id.

        Args:
            delegation_id: The delegation record ID.

        Returns:
            DelegationRecord or None if not found.
        """
        ...

    def get_delegation(self, agent_id: str) -> Any | None:
        """Get delegation record for a worker agent.

        Args:
            agent_id: The worker agent ID.

        Returns:
            DelegationRecord or None if not a delegated agent.
        """
        ...
