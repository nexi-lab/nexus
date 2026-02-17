"""Reputation service protocol (ops-scenario-matrix S26: Reputation / Agent Economy).

Defines the contract for agent reputation management — feedback submission,
score querying, and leaderboards.

Storage Affinity: **RecordStore** (reputation events, materialized scores)
                  + **CacheStore** (score TTLCache).

References:
    - docs/architecture/ops-scenario-matrix.md  (S26)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #1287: Extract NexusFS domain services from god object

Note:
    Dispute resolution is defined separately in ``DisputeProtocol``
    (protocols/dispute.py) per Interface Segregation Principle — the
    reputation scoring and dispute mediation subsystems have distinct
    consumers and different storage affinities.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ReputationProtocol(Protocol):
    """Service contract for agent reputation scoring.

    Mirrors ``services/reputation/reputation_service.ReputationService``.
    """

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
    ) -> Any | None:
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
