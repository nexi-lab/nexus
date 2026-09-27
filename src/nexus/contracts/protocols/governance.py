"""Governance protocols — tier-neutral contracts (ops-scenario-matrix S25).

Protocol interfaces for governance services: anomaly detection,
collusion/fraud ring detection, and constraint graph management.

Issue #1359: Protocol interfaces for governance services.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.bricks.governance.models import (
        AnomalyAlert,
        ConstraintCheckResult,
        ConstraintType,
        FraudRing,
        FraudScore,
        GovernanceEdge,
        TransactionSummary,
    )


@runtime_checkable
class AnomalyDetectorProtocol(Protocol):
    """Protocol for anomaly detection implementations.

    Default: StatisticalAnomalyDetector (Z-score, IQR).
    Future: ML-based detector can swap in via this interface.
    """

    def detect(self, transaction: "TransactionSummary") -> "list[AnomalyAlert]": ...


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
        constraint_type: "ConstraintType",
        reason: str = "",
    ) -> "GovernanceEdge": ...

    async def remove_constraint(self, edge_id: str, *, zone_id: str) -> bool: ...

    async def check_constraint(
        self,
        from_agent: str,
        to_agent: str,
        zone_id: str,
    ) -> "ConstraintCheckResult": ...

    async def list_constraints(
        self,
        zone_id: str,
        agent_id: str | None = None,
    ) -> "list[GovernanceEdge]": ...


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
    ) -> "list[AnomalyAlert]": ...


@runtime_checkable
class CollusionServiceProtocol(Protocol):
    """Protocol for collusion/fraud ring detection.

    Implementations build interaction graphs and detect
    suspicious patterns (rings, Sybil clusters, fraud scores).
    """

    async def detect_rings(
        self,
        zone_id: str,
    ) -> "list[FraudRing]": ...

    async def compute_fraud_scores(self, zone_id: str) -> "dict[str, FraudScore]": ...
