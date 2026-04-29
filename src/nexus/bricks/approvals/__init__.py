"""Approval decision queue brick (Issue #3790)."""

from nexus.bricks.approvals.config import ApprovalConfig
from nexus.bricks.approvals.errors import (
    ApprovalDenied,
    ApprovalError,
    ApprovalTimeout,
    GatewayClosed,
)
from nexus.bricks.approvals.models import (
    ApprovalKind,
    ApprovalRequest,
    ApprovalRequestStatus,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.policy_gate import PolicyGate

__all__ = [
    "ApprovalConfig",
    "ApprovalDenied",
    "ApprovalError",
    "ApprovalTimeout",
    "GatewayClosed",
    "ApprovalKind",
    "ApprovalRequest",
    "ApprovalRequestStatus",
    "Decision",
    "DecisionScope",
    "DecisionSource",
    "PolicyGate",
]
