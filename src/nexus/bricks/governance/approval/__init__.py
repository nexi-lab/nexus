"""Shared approval workflow infrastructure.

Issue #1359 Phase 0: Generic state machine and approval workflow base
extracted from SpendingApproval, SkillApproval, and DisputeRecord.
"""

from nexus.bricks.governance.approval.state_machine import InvalidTransitionError, StateMachine
from nexus.bricks.governance.approval.types import (
    ApprovalStatus,
    ApprovalTimestamps,
    ExpiryPolicy,
)
from nexus.bricks.governance.approval.workflow import ApprovalWorkflow

__all__ = [
    "ApprovalStatus",
    "ApprovalTimestamps",
    "ApprovalWorkflow",
    "ExpiryPolicy",
    "InvalidTransitionError",
    "StateMachine",
]
