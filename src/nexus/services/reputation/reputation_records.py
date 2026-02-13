"""Reputation domain records (Issue #1356).

Frozen dataclasses for immutable snapshots of reputation data.
Three records: ReputationEvent, ReputationScore, DisputeRecord.
Follows the same pattern as agent_key_record.py (Decision #6A).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ReputationEvent:
    """Immutable snapshot of a reputation event (feedback, dispute outcome, etc.).

    Attributes:
        id: Unique event identifier.
        rater_agent_id: Agent providing the feedback.
        rated_agent_id: Agent being rated.
        exchange_id: Exchange this feedback pertains to.
        zone_id: Zone/organization scope.
        event_type: One of "feedback", "dispute_filed", "dispute_resolved",
                    "auto_complete", "penalty".
        outcome: One of "positive", "negative", "neutral", "mixed".
        reliability_score: Optional 0.0-1.0 rating for reliability dimension.
        quality_score: Optional 0.0-1.0 rating for quality dimension.
        timeliness_score: Optional 0.0-1.0 rating for timeliness dimension.
        fairness_score: Optional 0.0-1.0 rating for fairness dimension.
        evidence_hash: SHA-256 hash of attached evidence (optional).
        context: Reputation context category (default "general").
        weight: Event weight for scoring (default 1.0).
        record_hash: SHA-256 self-hash for tamper detection.
        created_at: When the event was recorded.
    """

    id: str
    rater_agent_id: str
    rated_agent_id: str
    exchange_id: str
    zone_id: str
    event_type: str
    outcome: str
    reliability_score: float | None
    quality_score: float | None
    timeliness_score: float | None
    fairness_score: float | None
    evidence_hash: str | None
    context: str
    weight: float
    record_hash: str
    created_at: datetime


@dataclass(frozen=True)
class ReputationScore:
    """Materialized aggregate of an agent's reputation in a given context/window.

    Attributes:
        agent_id: The rated agent.
        context: Reputation context category (e.g. "general", "data_exchange").
        window: Time window: "all_time", "30d", "90d", "365d".
        reliability_alpha/beta: Beta distribution parameters for reliability.
        quality_alpha/beta: Beta distribution parameters for quality.
        timeliness_alpha/beta: Beta distribution parameters for timeliness.
        fairness_alpha/beta: Beta distribution parameters for fairness.
        composite_score: Weighted average of dimension scores (0.0-1.0).
        composite_confidence: Overall confidence level (0.0-1.0).
        total_interactions: Total number of interactions.
        positive_interactions: Count of positive outcomes.
        negative_interactions: Count of negative outcomes.
        disputed_interactions: Count of disputed interactions.
        global_trust_score: Optional cross-zone trust score.
        updated_at: Last materialization time.
        zone_id: Zone scope.
    """

    agent_id: str
    context: str
    window: str
    reliability_alpha: float
    reliability_beta: float
    quality_alpha: float
    quality_beta: float
    timeliness_alpha: float
    timeliness_beta: float
    fairness_alpha: float
    fairness_beta: float
    composite_score: float
    composite_confidence: float
    total_interactions: int
    positive_interactions: int
    negative_interactions: int
    disputed_interactions: int
    global_trust_score: float | None
    updated_at: datetime
    zone_id: str


@dataclass(frozen=True)
class DisputeRecord:
    """Immutable snapshot of a dispute lifecycle.

    State machine: filed -> auto_mediating -> resolved | dismissed.

    Attributes:
        id: Unique dispute identifier.
        exchange_id: The disputed exchange.
        zone_id: Zone scope.
        complainant_agent_id: Agent who filed the dispute.
        respondent_agent_id: Agent being complained about.
        status: One of "filed", "auto_mediating", "resolved", "dismissed".
        tier: Dispute resolution tier (1 = auto-mediation).
        reason: Reason for filing the dispute.
        resolution: Resolution description (set when resolved/dismissed).
        resolution_evidence_hash: SHA-256 hash of resolution evidence.
        escrow_amount: Escrowed amount as string (decimal precision).
        escrow_released: Whether escrow has been released.
        filed_at: When the dispute was filed.
        resolved_at: When the dispute was resolved/dismissed.
        appeal_deadline: Deadline for filing an appeal.
    """

    id: str
    exchange_id: str
    zone_id: str
    complainant_agent_id: str
    respondent_agent_id: str
    status: str
    tier: int
    reason: str
    resolution: str | None
    resolution_evidence_hash: str | None
    escrow_amount: str | None
    escrow_released: bool
    filed_at: datetime
    resolved_at: datetime | None
    appeal_deadline: datetime | None
