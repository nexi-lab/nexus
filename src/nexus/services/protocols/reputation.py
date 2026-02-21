"""Reputation service protocol (ops-scenario-matrix S26: Reputation / Agent Economy).

Defines the contract for agent reputation management — feedback submission,
score querying, leaderboards, and dispute resolution.

Storage Affinity: **RecordStore** (reputation events, materialized scores,
                  dispute records) + **CacheStore** (score TTLCache).

References:
    - docs/architecture/ops-scenario-matrix.md  (S26)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #1287: Extract NexusFS domain services from god object
    - Issue #2131: Extract reputation into nexus/bricks/reputation
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ReputationProtocol(Protocol):
    """Service contract for agent reputation and dispute resolution.

    Combines the public APIs of ``bricks/reputation/reputation_service.ReputationService``
    and ``bricks/reputation/dispute_service.DisputeService``.
    """

    # -- Feedback & Scores --------------------------------------------------

    def submit_feedback(
        self,
        rater_agent_id: str,
        rated_agent_id: str,
        exchange_id: str,
        zone_id: str,
        outcome: str,
        reliability_score: float | None = None,
        quality_score: float | None = None,
        timeliness_score: float | None = None,
        fairness_score: float | None = None,
        evidence_hash: str | None = None,
        context: str = "general",
    ) -> Any:
        """Submit feedback for an exchange, creating an event and updating scores.

        Returns:
            ReputationEvent record.
        """
        ...

    def get_reputation(
        self,
        agent_id: str,
        context: str = "general",
        window: str = "all_time",
    ) -> Any:
        """Get materialized reputation score for an agent.

        Returns:
            ReputationScore record or None if not found.
        """
        ...

    def get_leaderboard(
        self,
        zone_id: str,
        context: str = "general",
        limit: int = 50,
    ) -> list[Any]:
        """Get reputation leaderboard for a zone.

        Returns:
            List of ReputationScore records ordered by composite_score descending.
        """
        ...

    def get_feedback_for_exchange(
        self,
        exchange_id: str,
    ) -> list[Any]:
        """Get all feedback events for an exchange.

        Returns:
            List of ReputationEvent records.
        """
        ...

    # -- Dispute Resolution -------------------------------------------------

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

    def get_dispute(self, dispute_id: str) -> Any:
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
