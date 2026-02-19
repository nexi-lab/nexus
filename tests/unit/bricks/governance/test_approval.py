"""Unit tests for ApprovalWorkflow expire() and lifecycle.

Issue #2129 §11A: Tests submit -> expire -> verify status == EXPIRED,
and expire non-pending -> InvalidTransitionError.
"""

from __future__ import annotations

import pytest

from nexus.bricks.governance.approval.state_machine import InvalidTransitionError
from nexus.bricks.governance.approval.types import ApprovalStatus
from nexus.bricks.governance.approval.workflow import ApprovalWorkflow


class TestApprovalExpire:
    def test_submit_then_expire(self) -> None:
        wf: ApprovalWorkflow[object] = ApprovalWorkflow(default_expiry_hours=24.0)
        record = wf.submit(submitted_by="agent1")
        assert record.status == ApprovalStatus.PENDING

        expired = wf.expire(record.record_id)
        assert expired.status == ApprovalStatus.EXPIRED
        assert expired.decided_by == "system"
        assert expired.decided_at is not None

    def test_expire_non_pending_raises(self) -> None:
        wf: ApprovalWorkflow[object] = ApprovalWorkflow(default_expiry_hours=24.0)
        record = wf.submit(submitted_by="agent1")

        # Approve first
        wf.approve(record.record_id, decided_by="admin")

        # Now expire should fail
        with pytest.raises(InvalidTransitionError):
            wf.expire(record.record_id)

    def test_submit_approve_reject_lifecycle(self) -> None:
        wf: ApprovalWorkflow[object] = ApprovalWorkflow()

        # Submit and approve
        r1 = wf.submit(submitted_by="a1")
        approved = wf.approve(r1.record_id, decided_by="admin")
        assert approved.status == ApprovalStatus.APPROVED

        # Submit and reject
        r2 = wf.submit(submitted_by="a2")
        rejected = wf.reject(r2.record_id, decided_by="admin")
        assert rejected.status == ApprovalStatus.REJECTED

    def test_double_approve_raises(self) -> None:
        wf: ApprovalWorkflow[object] = ApprovalWorkflow()
        r = wf.submit(submitted_by="a1")
        wf.approve(r.record_id, decided_by="admin")

        with pytest.raises(InvalidTransitionError):
            wf.approve(r.record_id, decided_by="admin2")
