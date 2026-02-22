"""Shared approval workflow infrastructure.

Issue #1359 Phase 0: Generic state machine and approval workflow base
extracted from SpendingApproval, SkillApproval, and DisputeRecord.
"""

from nexus.services.governance.approval.state_machine import InvalidTransitionError, StateMachine
from nexus.services.governance.approval.types import (
    ApprovalStatus,
    ApprovalTimestamps,
    ExpiryPolicy,
)
from nexus.services.governance.approval.workflow import ApprovalWorkflow

__all__ = [
    "ApprovalStatus",
    "ApprovalTimestamps",
    "ApprovalWorkflow",
    "ExpiryPolicy",
    "InvalidTransitionError",
    "StateMachine",
]
