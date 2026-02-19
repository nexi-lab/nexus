"""Anti-Fraud & Anti-Collusion Governance Brick.

Issue #1359: Governance graphs, anomaly detection, collusion detection,
and response actions for the Nexus exchange.
Issue #2129: Extracted from ``services/governance/`` to a self-contained brick.

Public API:
    - Approval workflow: StateMachine, ApprovalWorkflow, ApprovalStatus
    - Anomaly detection: AnomalyService, AnomalyDetectionConfig
    - Collusion detection: CollusionService, FraudRing, FraudScore
    - Governance graphs: GovernanceGraphService, GovernanceEnforcedPayment
    - Response actions: ResponseService, SuspensionRecord, ThrottleConfig
"""

from __future__ import annotations

# Services (lazy imports to avoid heavy deps at package level)
from nexus.bricks.governance.anomaly_service import AnomalyService

# Phase 0: Shared approval workflow
from nexus.bricks.governance.approval.state_machine import InvalidTransitionError, StateMachine
from nexus.bricks.governance.approval.types import (
    ApprovalStatus,
    ApprovalTimestamps,
    ExpiryPolicy,
)
from nexus.bricks.governance.collusion_service import CollusionService
from nexus.bricks.governance.governance_graph_service import GovernanceGraphService
from nexus.bricks.governance.governance_wrapper import (
    GovernanceApprovalRequired,
    GovernanceBlockedError,
    GovernanceEnforcedPayment,
)

# Phase 1: Anomaly detection
from nexus.bricks.governance.models import (
    AgentBaseline,
    AnomalyAlert,
    AnomalyDetectionConfig,
    AnomalySeverity,
    ConstraintCheckResult,
    ConstraintType,
    FraudRing,
    FraudScore,
    GovernanceEdge,
    GovernanceNode,
    NodeType,
    SuspensionRecord,
    ThrottleConfig,
    TransactionSummary,
)
from nexus.bricks.governance.protocols import AnomalyDetectorProtocol
from nexus.bricks.governance.response_service import ResponseService
from nexus.bricks.governance.snapshot import GovernanceSnapshot

__all__ = [
    # Approval
    "ApprovalStatus",
    "ApprovalTimestamps",
    "ExpiryPolicy",
    "InvalidTransitionError",
    "StateMachine",
    # Models
    "AgentBaseline",
    "AnomalyAlert",
    "AnomalyDetectionConfig",
    "AnomalySeverity",
    "ConstraintCheckResult",
    "ConstraintType",
    "FraudRing",
    "FraudScore",
    "GovernanceEdge",
    "GovernanceNode",
    "GovernanceSnapshot",
    "NodeType",
    "SuspensionRecord",
    "ThrottleConfig",
    "TransactionSummary",
    # Protocols
    "AnomalyDetectorProtocol",
    # Services
    "AnomalyService",
    "CollusionService",
    "GovernanceApprovalRequired",
    "GovernanceBlockedError",
    "GovernanceEnforcedPayment",
    "GovernanceGraphService",
    "ResponseService",
]
