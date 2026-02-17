"""Governance protocols — dependency inversion for pluggable detection.

Issue #1359: Protocol interfaces for governance services.

AnomalyDetectorProtocol: swappable detection strategy.
GovernanceGraphProtocol: constraint CRUD + cache.
AnomalyServiceProtocol: anomaly detection lifecycle.
CollusionServiceProtocol: collusion/fraud ring detection.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from nexus.services.governance.models import AnomalyAlert, TransactionSummary

if TYPE_CHECKING:
    from nexus.services.governance.models import (
        ConstraintCheckResult,
        ConstraintType,
        FraudRing,
        FraudScore,
        GovernanceEdge,
    )


@runtime_checkable
class AnomalyDetectorProtocol(Protocol):
    """Protocol for anomaly detection implementations.

    Default: StatisticalAnomalyDetector (Z-score, IQR).
    Future: ML-based detector can swap in via this interface.
    """

    def detect(self, transaction: TransactionSummary) -> list[AnomalyAlert]:
        """Analyze a transaction and return any anomaly alerts.

        Args:
            transaction: The transaction to analyze.

        Returns:
            List of anomaly alerts (empty if no anomalies detected).
        """
        ...


@runtime_checkable
class GovernanceGraphProtocol(Protocol):
    """Protocol for governance constraint graph operations.

    Implementations manage constraint edges between agents,
    provide fast cached lookups, and handle cache invalidation.
    """

    async def add_constraint(
        self,
        from_agent: str,
        to_agent: str,
        zone_id: str,
        constraint_type: ConstraintType,
        reason: str = "",
    ) -> GovernanceEdge:
        """Add a governance constraint between two agents."""
        ...

    async def remove_constraint(self, edge_id: str) -> bool:
        """Remove a constraint by edge ID."""
        ...

    async def check_constraint(
        self,
        from_agent: str,
        to_agent: str,
        zone_id: str,
    ) -> ConstraintCheckResult:
        """Check if there's a constraint between two agents."""
        ...

    async def list_constraints(
        self,
        zone_id: str,
        agent_id: str | None = None,
    ) -> list[GovernanceEdge]:
        """List constraint edges, optionally filtered by agent."""
        ...


@runtime_checkable
class AnomalyServiceProtocol(Protocol):
    """Protocol for anomaly detection service lifecycle.

    Implementations analyze transactions, persist alerts,
    and manage alert resolution.
    """

    async def analyze_transaction(
        self,
        agent_id: str,
        zone_id: str,
        amount: float,
        to: str,
        timestamp: datetime | None = None,
    ) -> list[AnomalyAlert]:
        """Analyze a transaction for anomalies."""
        ...


@runtime_checkable
class CollusionServiceProtocol(Protocol):
    """Protocol for collusion/fraud ring detection.

    Implementations build interaction graphs and detect
    suspicious patterns (rings, Sybil clusters, fraud scores).
    """

    async def detect_rings(
        self,
        zone_id: str,
    ) -> list[FraudRing]:
        """Detect transaction rings (cycles) in the interaction graph."""
        ...

    async def compute_fraud_scores(self, zone_id: str) -> dict[str, FraudScore]:
        """Compute composite fraud scores for all agents in a zone."""
        ...
