"""Dispute resolution protocol (ops-scenario-matrix S26: Reputation / Agent Economy).

Defines the contract for dispute lifecycle — filing, mediation,
resolution, and querying of disputes between agents.

Split from ``ReputationProtocol`` per Interface Segregation Principle:
reputation scoring and dispute mediation have distinct consumers.

Storage Affinity: **RecordStore** (dispute records, evidence hashes).

References:
    - docs/architecture/ops-scenario-matrix.md  (S26)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #1287: Extract NexusFS domain services from god object
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DisputeProtocol(Protocol):
    """Service contract for dispute resolution.

    Mirrors ``services/reputation/dispute_service.DisputeService``.
    """

    def file_dispute(
        self,
        exchange_id: str,
        complainant_agent_id: str,
        respondent_agent_id: str,
        zone_id: str,
        reason: str,
        evidence_hash: str | None = None,
    ) -> Any:
        """File a new dispute for an exchange.

        Returns:
            DisputeRecord snapshot.
        """
        ...

    def auto_mediate(self, dispute_id: str) -> Any:
        """Transition dispute to auto_mediating state.

        Returns:
            Updated DisputeRecord.
        """
        ...

    def resolve(
        self,
        dispute_id: str,
        resolution: str,
        evidence_hash: str | None = None,
    ) -> Any:
        """Resolve a dispute.

        Returns:
            Updated DisputeRecord.
        """
        ...

    def dismiss(self, dispute_id: str, reason: str) -> Any:
        """Dismiss a dispute.

        Returns:
            Updated DisputeRecord.
        """
        ...

    def get_dispute(self, dispute_id: str) -> Any | None:
        """Get a dispute by ID.

        Returns:
            DisputeRecord or None if not found.
        """
        ...

    def list_disputes(
        self,
        exchange_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
        zone_id: str | None = None,
    ) -> list[Any]:
        """List disputes with optional filters.

        Returns:
            List of DisputeRecord.
        """
        ...
