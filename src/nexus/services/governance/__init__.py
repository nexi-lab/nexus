"""Anti-Fraud & Anti-Collusion Governance Graphs.

Issue #1359: Governance graphs, anomaly detection, collusion detection,
and response actions for the Nexus exchange.

Public API:
    - Approval workflow: StateMachine, ApprovalWorkflow, ApprovalStatus
    - Anomaly detection: AnomalyService, AnomalyDetectionConfig
    - Collusion detection: CollusionService, FraudRing, FraudScore
    - Governance graphs: GovernanceGraphService, GovernanceEnforcedPayment
    - Response actions: ResponseService, SuspensionRecord, ThrottleConfig
"""

from __future__ import annotations

# Services (lazy imports to avoid heavy deps at package level)
from nexus.services.governance.anomaly_service import AnomalyService

# Phase 0: Shared approval workflow
from nexus.services.governance.approval.state_machine import InvalidTransitionError, StateMachine
from nexus.services.governance.approval.types import (
    ApprovalStatus,
    ApprovalTimestamps,
    ExpiryPolicy,
)
from nexus.services.governance.collusion_service import CollusionService
from nexus.services.governance.governance_graph_service import GovernanceGraphService
from nexus.services.governance.governance_wrapper import (
    GovernanceApprovalRequired,
    GovernanceBlockedError,
    GovernanceEnforcedPayment,
)

# Phase 1: Anomaly detection
from nexus.services.governance.models import (
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
from nexus.services.governance.protocols import AnomalyDetectorProtocol
from nexus.services.governance.response_service import ResponseService

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
