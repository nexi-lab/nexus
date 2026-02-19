"""Governance service protocol (ops-scenario-matrix S25: Governance).

Defines the unified contract for the governance domain — anomaly detection,
collusion/fraud analysis, constraint graph management, throttling,
suspension/appeal workflow.

Combines public APIs from:
    - ``bricks/governance/anomaly_service.AnomalyService``
    - ``bricks/governance/collusion_service.CollusionService``
    - ``bricks/governance/governance_graph_service.GovernanceGraphService``
    - ``bricks/governance/response_service.ResponseService``

Storage Affinity: **RecordStore** (alerts, edges, suspensions, throttles,
                  fraud scores) + **CacheStore** (constraint TTL cache).

References:
    - docs/architecture/ops-scenario-matrix.md  (S25)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #1287: Extract NexusFS domain services from god object
    - Issue #2129: Governance brick extraction — replaced Any with concrete types
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.bricks.governance.models import (
        AnomalyAlert,
        AnomalySeverity,
        ConstraintCheckResult,
        ConstraintType,
        FraudRing,
        FraudScore,
        GovernanceEdge,
        SuspensionRecord,
        ThrottleConfig,
    )


@runtime_checkable
class GovernanceProtocol(Protocol):
    """Service contract for governance: anomaly, collusion, constraints, response.

    Unifies the four governance sub-services into a single protocol so that
    consumers need only one dependency.
    """

    # ── Anomaly Detection (AnomalyService) ────────────────────────────

    async def analyze_transaction(
        self,
        agent_id: str,
        zone_id: str,
        amount: float,
        to: str,
        timestamp: datetime | None = None,
    ) -> list[AnomalyAlert]:
        """Analyze a transaction for anomalies (hot path).

        Returns:
            List of AnomalyAlert records.
        """
        ...

    async def get_alerts(
        self,
        zone_id: str,
        severity: AnomalySeverity | None = None,
        resolved: bool | None = None,
    ) -> list[AnomalyAlert]:
        """Query anomaly alerts with optional filters.

        Returns:
            List of AnomalyAlert records.
        """
        ...

    async def resolve_alert(
        self,
        alert_id: str,
        resolved_by: str,
    ) -> AnomalyAlert | None:
        """Mark an anomaly alert as resolved.

        Returns:
            Updated AnomalyAlert or None if not found.
        """
        ...

    # ── Collusion / Fraud (CollusionService) ──────────────────────────

    async def detect_rings(self, zone_id: str) -> list[FraudRing]:
        """Detect transaction rings (cycles) in the interaction graph.

        Returns:
            List of FraudRing records.
        """
        ...

    async def detect_sybils(self, zone_id: str) -> list[set[str]]:
        """Detect Sybil clusters using EigenTrust scores.

        Returns:
            List of agent-ID sets forming suspected Sybil clusters.
        """
        ...

    async def compute_fraud_scores(self, zone_id: str) -> dict[str, FraudScore]:
        """Compute composite fraud scores for all agents in a zone.

        Returns:
            Mapping of agent_id -> FraudScore.
        """
        ...

    async def get_fraud_score(
        self,
        agent_id: str,
        zone_id: str,
    ) -> FraudScore | None:
        """Get cached fraud score for an agent.

        Returns:
            FraudScore or None.
        """
        ...

    # ── Constraint Graph (GovernanceGraphService) ─────────────────────

    async def add_constraint(
        self,
        from_agent: str,
        to_agent: str,
        zone_id: str,
        constraint_type: ConstraintType,
        reason: str = "",
    ) -> GovernanceEdge:
        """Add a governance constraint between two agents.

        Returns:
            GovernanceEdge record.
        """
        ...

    async def remove_constraint(self, edge_id: str) -> bool:
        """Remove a constraint by edge ID.

        Returns:
            True if removed, False if not found.
        """
        ...

    async def check_constraint(
        self,
        from_agent: str,
        to_agent: str,
        zone_id: str,
    ) -> ConstraintCheckResult:
        """Check if there is a constraint between two agents (hot path).

        Returns:
            ConstraintCheckResult.
        """
        ...

    async def list_constraints(
        self,
        zone_id: str,
        agent_id: str | None = None,
    ) -> list[GovernanceEdge]:
        """List constraint edges, optionally filtered by agent.

        Returns:
            List of GovernanceEdge records.
        """
        ...

    # ── Response Actions (ResponseService) ────────────────────────────

    async def auto_throttle(
        self,
        agent_id: str,
        zone_id: str,
        fraud_score: FraudScore,
    ) -> ThrottleConfig | None:
        """Apply automatic throttling based on fraud score.

        Returns:
            ThrottleConfig or None if score below threshold.
        """
        ...

    async def suspend_agent(
        self,
        agent_id: str,
        zone_id: str,
        reason: str,
        duration_hours: float = 24.0,
        severity: AnomalySeverity = ...,
    ) -> SuspensionRecord:
        """Suspend an agent for a specified duration.

        Returns:
            SuspensionRecord.
        """
        ...

    async def appeal_suspension(
        self,
        suspension_id: str,
        reason: str,
    ) -> SuspensionRecord:
        """File an appeal for a suspension.

        Returns:
            Updated SuspensionRecord.
        """
        ...

    async def decide_appeal(
        self,
        suspension_id: str,
        approved: bool,
        decided_by: str,
    ) -> SuspensionRecord:
        """Decide on a suspension appeal.

        Returns:
            Updated SuspensionRecord.
        """
        ...

    async def list_suspensions(
        self,
        zone_id: str,
        agent_id: str | None = None,
    ) -> list[SuspensionRecord]:
        """List suspensions, optionally filtered by agent.

        Returns:
            List of SuspensionRecord.
        """
        ...
