"""Tests for the generic ApprovalWorkflow.

Issue #1359 Phase 0: Generic workflow TDD.
"""

from __future__ import annotations

import pytest

from nexus.governance.approval.state_machine import InvalidTransitionError
from nexus.governance.approval.types import ApprovalStatus
from nexus.governance.approval.workflow import ApprovalWorkflow


@pytest.fixture
def workflow() -> ApprovalWorkflow[str]:
    """Fresh approval workflow."""
    return ApprovalWorkflow(default_expiry_hours=24.0)


class TestSubmit:
    """Tests for submitting approval requests."""

    def test_submit_creates_pending_record(self, workflow: ApprovalWorkflow[str]) -> None:
        record = workflow.submit(submitted_by="alice")
        assert record.status == ApprovalStatus.PENDING
        assert record.submitted_by == "alice"
        assert record.record_id is not None
        assert record.created_at is not None
        assert record.expires_at is not None

    def test_submit_with_custom_id(self, workflow: ApprovalWorkflow[str]) -> None:
        record = workflow.submit(submitted_by="alice", record_id="custom-id")
        assert record.record_id == "custom-id"

    def test_submit_with_metadata(self, workflow: ApprovalWorkflow[str]) -> None:
        record = workflow.submit(submitted_by="alice", metadata={"key": "value"})
        assert record.metadata == {"key": "value"}

    def test_submit_multiple(self, workflow: ApprovalWorkflow[str]) -> None:
        r1 = workflow.submit(submitted_by="alice")
        r2 = workflow.submit(submitted_by="bob")
        assert r1.record_id != r2.record_id
        assert len(workflow.list_pending()) == 2


class TestApprove:
    """Tests for approving requests."""

    def test_approve_pending(self, workflow: ApprovalWorkflow[str]) -> None:
        record = workflow.submit(submitted_by="alice")
        approved = workflow.approve(record.record_id, decided_by="admin")
        assert approved.status == ApprovalStatus.APPROVED
        assert approved.decided_by == "admin"
        assert approved.decided_at is not None

    def test_approve_already_approved_raises(self, workflow: ApprovalWorkflow[str]) -> None:
        record = workflow.submit(submitted_by="alice")
        workflow.approve(record.record_id, decided_by="admin")
        with pytest.raises(InvalidTransitionError):
            workflow.approve(record.record_id, decided_by="admin2")

    def test_approve_nonexistent_raises(self, workflow: ApprovalWorkflow[str]) -> None:
        with pytest.raises(KeyError, match="not found"):
            workflow.approve("nonexistent", decided_by="admin")


class TestReject:
    """Tests for rejecting requests."""

    def test_reject_pending(self, workflow: ApprovalWorkflow[str]) -> None:
        record = workflow.submit(submitted_by="alice")
        rejected = workflow.reject(record.record_id, decided_by="admin")
        assert rejected.status == ApprovalStatus.REJECTED
        assert rejected.decided_by == "admin"

    def test_reject_already_rejected_raises(self, workflow: ApprovalWorkflow[str]) -> None:
        record = workflow.submit(submitted_by="alice")
        workflow.reject(record.record_id, decided_by="admin")
        with pytest.raises(InvalidTransitionError):
            workflow.reject(record.record_id, decided_by="admin2")

    def test_reject_approved_raises(self, workflow: ApprovalWorkflow[str]) -> None:
        record = workflow.submit(submitted_by="alice")
        workflow.approve(record.record_id, decided_by="admin")
        with pytest.raises(InvalidTransitionError):
            workflow.reject(record.record_id, decided_by="admin2")


class TestExpire:
    """Tests for expiring requests."""

    def test_expire_pending(self, workflow: ApprovalWorkflow[str]) -> None:
        record = workflow.submit(submitted_by="alice")
        expired = workflow.expire(record.record_id)
        assert expired.status == ApprovalStatus.EXPIRED
        assert expired.decided_by == "system"

    def test_expire_already_expired_raises(self, workflow: ApprovalWorkflow[str]) -> None:
        record = workflow.submit(submitted_by="alice")
        workflow.expire(record.record_id)
        with pytest.raises(InvalidTransitionError):
            workflow.expire(record.record_id)


class TestQuery:
    """Tests for querying records."""

    def test_get_existing(self, workflow: ApprovalWorkflow[str]) -> None:
        record = workflow.submit(submitted_by="alice")
        fetched = workflow.get(record.record_id)
        assert fetched is not None
        assert fetched.record_id == record.record_id

    def test_get_nonexistent(self, workflow: ApprovalWorkflow[str]) -> None:
        assert workflow.get("nonexistent") is None

    def test_list_pending(self, workflow: ApprovalWorkflow[str]) -> None:
        workflow.submit(submitted_by="alice")
        workflow.submit(submitted_by="bob")
        r3 = workflow.submit(submitted_by="charlie")
        workflow.approve(r3.record_id, decided_by="admin")

        pending = workflow.list_pending()
        assert len(pending) == 2

    def test_list_all(self, workflow: ApprovalWorkflow[str]) -> None:
        r1 = workflow.submit(submitted_by="alice")
        workflow.submit(submitted_by="bob")
        workflow.approve(r1.record_id, decided_by="admin")

        assert len(workflow.list_all()) == 2


class TestImmutability:
    """Tests that records are immutable."""

    def test_record_is_frozen(self, workflow: ApprovalWorkflow[str]) -> None:
        record = workflow.submit(submitted_by="alice")
        with pytest.raises(AttributeError):
            record.status = ApprovalStatus.APPROVED  # type: ignore[misc]

    def test_approve_returns_new_record(self, workflow: ApprovalWorkflow[str]) -> None:
        record = workflow.submit(submitted_by="alice")
        approved = workflow.approve(record.record_id, decided_by="admin")
        assert record is not approved
        assert record.status == ApprovalStatus.PENDING
        assert approved.status == ApprovalStatus.APPROVED
