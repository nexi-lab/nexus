"""Anti-Fraud & Anti-Collusion Governance — backward-compatible re-exports.

Issue #2129: Canonical implementation moved to ``nexus.bricks.governance``.
This shim re-exports everything for backward compatibility.
"""

from nexus.bricks.governance import (
    AgentBaseline as AgentBaseline,
)
from nexus.bricks.governance import (
    AnomalyAlert as AnomalyAlert,
)
from nexus.bricks.governance import (
    AnomalyDetectionConfig as AnomalyDetectionConfig,
)
from nexus.bricks.governance import (
    AnomalyDetectorProtocol as AnomalyDetectorProtocol,
)
from nexus.bricks.governance import (
    AnomalyService as AnomalyService,
)
from nexus.bricks.governance import (
    AnomalySeverity as AnomalySeverity,
)
from nexus.bricks.governance import (
    ApprovalStatus as ApprovalStatus,
)
from nexus.bricks.governance import (
    ApprovalTimestamps as ApprovalTimestamps,
)
from nexus.bricks.governance import (
    CollusionService as CollusionService,
)
from nexus.bricks.governance import (
    ConstraintCheckResult as ConstraintCheckResult,
)
from nexus.bricks.governance import (
    ConstraintType as ConstraintType,
)
from nexus.bricks.governance import (
    ExpiryPolicy as ExpiryPolicy,
)
from nexus.bricks.governance import (
    FraudRing as FraudRing,
)
from nexus.bricks.governance import (
    FraudScore as FraudScore,
)
from nexus.bricks.governance import (
    GovernanceApprovalRequired as GovernanceApprovalRequired,
)
from nexus.bricks.governance import (
    GovernanceBlockedError as GovernanceBlockedError,
)
from nexus.bricks.governance import (
    GovernanceEdge as GovernanceEdge,
)
from nexus.bricks.governance import (
    GovernanceEnforcedPayment as GovernanceEnforcedPayment,
)
from nexus.bricks.governance import (
    GovernanceGraphService as GovernanceGraphService,
)
from nexus.bricks.governance import (
    GovernanceNode as GovernanceNode,
)
from nexus.bricks.governance import (
    InvalidTransitionError as InvalidTransitionError,
)
from nexus.bricks.governance import (
    NodeType as NodeType,
)
from nexus.bricks.governance import (
    ResponseService as ResponseService,
)
from nexus.bricks.governance import (
    StateMachine as StateMachine,
)
from nexus.bricks.governance import (
    SuspensionRecord as SuspensionRecord,
)
from nexus.bricks.governance import (
    ThrottleConfig as ThrottleConfig,
)
from nexus.bricks.governance import (
    TransactionSummary as TransactionSummary,
)

__all__ = [
    "AgentBaseline",
    "AnomalyAlert",
    "AnomalyDetectionConfig",
    "AnomalyDetectorProtocol",
    "AnomalyService",
    "AnomalySeverity",
    "ApprovalStatus",
    "ApprovalTimestamps",
    "CollusionService",
    "ConstraintCheckResult",
    "ConstraintType",
    "ExpiryPolicy",
    "FraudRing",
    "FraudScore",
    "GovernanceApprovalRequired",
    "GovernanceBlockedError",
    "GovernanceEdge",
    "GovernanceEnforcedPayment",
    "GovernanceGraphService",
    "GovernanceNode",
    "InvalidTransitionError",
    "NodeType",
    "ResponseService",
    "StateMachine",
    "SuspensionRecord",
    "ThrottleConfig",
    "TransactionSummary",
]
