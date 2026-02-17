"""Governance service protocol (ops-scenario-matrix S25: Governance).

Defines the unified contract for the governance domain — anomaly detection,
collusion/fraud analysis, constraint graph management, throttling,
suspension/appeal workflow.

Combines public APIs from:
    - ``services/governance/anomaly_service.AnomalyService``
    - ``services/governance/collusion_service.CollusionService``
    - ``services/governance/governance_graph_service.GovernanceGraphService``
    - ``services/governance/response_service.ResponseService``

Storage Affinity: **RecordStore** (alerts, edges, suspensions, throttles,
                  fraud scores) + **CacheStore** (constraint TTL cache).

References:
    - docs/architecture/ops-scenario-matrix.md  (S25)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #1287: Extract NexusFS domain services from god object
"""

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

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
    ) -> list[Any]:
        """Analyze a transaction for anomalies (hot path).

        Returns:
            List of AnomalyAlert records.
        """
        ...

    async def get_alerts(
        self,
        zone_id: str,
        severity: Any | None = None,
        resolved: bool | None = None,
    ) -> list[Any]:
        """Query anomaly alerts with optional filters.

        Returns:
            List of AnomalyAlert records.
        """
        ...

    async def resolve_alert(
        self,
        alert_id: str,
        resolved_by: str,
    ) -> Any | None:
        """Mark an anomaly alert as resolved.

        Returns:
            Updated AnomalyAlert or None if not found.
        """
        ...

    # ── Collusion / Fraud (CollusionService) ──────────────────────────

    async def detect_rings(self, zone_id: str) -> list[Any]:
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

    async def compute_fraud_scores(self, zone_id: str) -> dict[str, Any]:
        """Compute composite fraud scores for all agents in a zone.

        Returns:
            Mapping of agent_id → FraudScore.
        """
        ...

    async def get_fraud_score(
        self,
        agent_id: str,
        zone_id: str,
    ) -> Any | None:
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
        constraint_type: Any,
        reason: str = "",
    ) -> Any:
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
    ) -> Any:
        """Check if there is a constraint between two agents (hot path).

        Returns:
            ConstraintCheckResult.
        """
        ...

    async def list_constraints(
        self,
        zone_id: str,
        agent_id: str | None = None,
    ) -> list[Any]:
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
        fraud_score: Any,
    ) -> Any | None:
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
        severity: Any = None,
    ) -> Any:
        """Suspend an agent for a specified duration.

        Returns:
            SuspensionRecord.
        """
        ...

    async def appeal_suspension(
        self,
        suspension_id: str,
        reason: str,
    ) -> Any:
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
    ) -> Any:
        """Decide on a suspension appeal.

        Returns:
            Updated SuspensionRecord.
        """
        ...

    async def list_suspensions(
        self,
        zone_id: str,
        agent_id: str | None = None,
    ) -> list[Any]:
        """List suspensions, optionally filtered by agent.

        Returns:
            List of SuspensionRecord.
        """
        ...
