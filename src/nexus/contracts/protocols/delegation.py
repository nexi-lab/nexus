"""Delegation service protocol (ops-scenario-matrix S23: Agent Delegation).

Defines the contract for agent identity delegation — coordinator agents
provisioning worker agents with narrowed permissions via namespace
derivation and ReBAC tuple injection.

Storage Affinity: **RecordStore** (delegation records, API keys) +
                  ReBAC tuples (permission materialization).

References:
    - docs/architecture/ops-scenario-matrix.md  (S23)
    - Issue #1271: Agent delegation service
    - Issue #1618: Delegation lifecycle enhancements
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.bricks.delegation.models import (
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

    Coordinator agents can provision worker agents with narrower
    permissions derived from the coordinator's own grants.
    """

    def delegate(
        self,
        coordinator_agent_id: str,
        coordinator_owner_id: str,
        worker_id: str,
        worker_name: str,
        delegation_mode: DelegationMode,
        zone_id: str | None = None,
        scope_prefix: str | None = None,
        remove_grants: list[str] | None = None,
        add_grants: list[str] | None = None,
        readonly_paths: list[str] | None = None,
        ttl_seconds: int | None = None,
        intent: str = "",
        can_sub_delegate: bool = False,
        scope: DelegationScope | None = None,
    ) -> DelegationResult: ...

    def revoke_delegation(self, delegation_id: str) -> bool: ...

    def list_delegations(
        self,
        parent_agent_id: str | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
        status_filter: DelegationStatus | None = None,
    ) -> tuple[list[DelegationRecord], int]: ...

    def get_delegation_by_id(
        self,
        delegation_id: str,
    ) -> DelegationRecord | None: ...

    def get_delegation(
        self,
        agent_id: str,
    ) -> DelegationRecord | None: ...

    def get_delegation_chain(
        self,
        delegation_id: str,
    ) -> list[DelegationRecord]: ...

    def complete_delegation(
        self,
        delegation_id: str,
        outcome: DelegationOutcome,
        quality_score: float | None = None,
    ) -> DelegationRecord: ...

    def update_namespace_config(
        self,
        delegation_id: str,
        *,
        scope_prefix: str | None = None,
        clear_scope_prefix: bool = False,
        remove_grants: list[str] | None = None,
        add_grants: list[str] | None = None,
        readonly_paths: list[str] | None = None,
    ) -> DelegationRecord: ...
