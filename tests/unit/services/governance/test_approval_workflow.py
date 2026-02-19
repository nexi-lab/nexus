"""Unit tests for ApprovalWorkflow with in-memory store.

Tests submit/approve/reject/expire lifecycle, listing,
and state machine enforcement in the workflow.
"""

from __future__ import annotations

import pytest

from nexus.services.governance.approval.state_machine import InvalidTransitionError
from nexus.services.governance.approval.types import ApprovalStatus
from nexus.services.governance.approval.workflow import ApprovalWorkflow

# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


class TestSubmit:
    """Tests for ApprovalWorkflow.submit."""

    def test_submit_creates_pending_record(self) -> None:
        wf = ApprovalWorkflow()
        record = wf.submit("alice")
        assert record.status == ApprovalStatus.PENDING
        assert record.submitted_by == "alice"
        assert record.created_at is not None
        assert record.decided_at is None
        assert record.decided_by is None

    def test_submit_with_custom_id(self) -> None:
        wf = ApprovalWorkflow()
        record = wf.submit("alice", record_id="custom-123")
        assert record.record_id == "custom-123"

    def test_submit_auto_generates_id(self) -> None:
        wf = ApprovalWorkflow()
        record = wf.submit("alice")
        assert len(record.record_id) > 0

    def test_submit_sets_default_expiry(self) -> None:
        wf = ApprovalWorkflow(default_expiry_hours=48.0)
        record = wf.submit("alice")
        assert record.expires_at is not None

    def test_submit_no_expiry_when_zero(self) -> None:
        wf = ApprovalWorkflow(default_expiry_hours=0.0)
        record = wf.submit("alice")
        # With 0 hours, no automatic expiry is set
        assert record.expires_at is None

    def test_submit_with_metadata(self) -> None:
        wf = ApprovalWorkflow()
        record = wf.submit("alice", metadata={"amount": 100})
        assert record.metadata == {"amount": 100}


# ---------------------------------------------------------------------------
# Approve / Reject / Expire
# ---------------------------------------------------------------------------


class TestDecisions:
    """Tests for approve, reject, and expire."""

    @pytest.fixture()
    def wf_with_pending(self) -> tuple[ApprovalWorkflow, str]:
        wf = ApprovalWorkflow()
        record = wf.submit("alice", record_id="r1")
        return wf, record.record_id

    def test_approve(self, wf_with_pending: tuple[ApprovalWorkflow, str]) -> None:
        wf, rid = wf_with_pending
        record = wf.approve(rid, "bob")
        assert record.status == ApprovalStatus.APPROVED
        assert record.decided_by == "bob"
        assert record.decided_at is not None

    def test_reject(self, wf_with_pending: tuple[ApprovalWorkflow, str]) -> None:
        wf, rid = wf_with_pending
        record = wf.reject(rid, "bob")
        assert record.status == ApprovalStatus.REJECTED
        assert record.decided_by == "bob"

    def test_expire(self, wf_with_pending: tuple[ApprovalWorkflow, str]) -> None:
        wf, rid = wf_with_pending
        record = wf.expire(rid)
        assert record.status == ApprovalStatus.EXPIRED
        assert record.decided_by == "system"

    def test_approve_preserves_submitted_by(
        self, wf_with_pending: tuple[ApprovalWorkflow, str]
    ) -> None:
        wf, rid = wf_with_pending
        record = wf.approve(rid, "bob")
        assert record.submitted_by == "alice"

    def test_approve_preserves_metadata(self) -> None:
        wf = ApprovalWorkflow()
        wf.submit("alice", record_id="r1", metadata={"key": "val"})
        record = wf.approve("r1", "bob")
        assert record.metadata == {"key": "val"}


# ---------------------------------------------------------------------------
# Invalid transitions from terminal states
# ---------------------------------------------------------------------------


class TestInvalidTransitions:
    """Tests that decisions cannot be applied to terminal states."""

    def test_double_approve_raises(self) -> None:
        wf = ApprovalWorkflow()
        wf.submit("alice", record_id="r1")
        wf.approve("r1", "bob")
        with pytest.raises(InvalidTransitionError):
            wf.approve("r1", "charlie")

    def test_approve_then_reject_raises(self) -> None:
        wf = ApprovalWorkflow()
        wf.submit("alice", record_id="r1")
        wf.approve("r1", "bob")
        with pytest.raises(InvalidTransitionError):
            wf.reject("r1", "charlie")

    def test_reject_then_approve_raises(self) -> None:
        wf = ApprovalWorkflow()
        wf.submit("alice", record_id="r1")
        wf.reject("r1", "bob")
        with pytest.raises(InvalidTransitionError):
            wf.approve("r1", "charlie")

    def test_expired_then_approve_raises(self) -> None:
        wf = ApprovalWorkflow()
        wf.submit("alice", record_id="r1")
        wf.expire("r1")
        with pytest.raises(InvalidTransitionError):
            wf.approve("r1", "bob")


# ---------------------------------------------------------------------------
# Record not found
# ---------------------------------------------------------------------------


class TestRecordNotFound:
    """Tests for missing records."""

    def test_approve_missing_raises_key_error(self) -> None:
        wf = ApprovalWorkflow()
        with pytest.raises(KeyError, match="not found"):
            wf.approve("nonexistent", "bob")

    def test_reject_missing_raises_key_error(self) -> None:
        wf = ApprovalWorkflow()
        with pytest.raises(KeyError, match="not found"):
            wf.reject("nonexistent", "bob")

    def test_expire_missing_raises_key_error(self) -> None:
        wf = ApprovalWorkflow()
        with pytest.raises(KeyError, match="not found"):
            wf.expire("nonexistent")


# ---------------------------------------------------------------------------
# Listing and retrieval
# ---------------------------------------------------------------------------


class TestListingAndRetrieval:
    """Tests for get, list_pending, list_all."""

    def test_get_existing_record(self) -> None:
        wf = ApprovalWorkflow()
        wf.submit("alice", record_id="r1")
        record = wf.get("r1")
        assert record is not None
        assert record.record_id == "r1"

    def test_get_nonexistent_returns_none(self) -> None:
        wf = ApprovalWorkflow()
        assert wf.get("nonexistent") is None

    def test_list_pending(self) -> None:
        wf = ApprovalWorkflow()
        wf.submit("alice", record_id="r1")
        wf.submit("bob", record_id="r2")
        wf.submit("charlie", record_id="r3")
        wf.approve("r1", "admin")

        pending = wf.list_pending()
        assert len(pending) == 2
        ids = {r.record_id for r in pending}
        assert ids == {"r2", "r3"}

    def test_list_all(self) -> None:
        wf = ApprovalWorkflow()
        wf.submit("alice", record_id="r1")
        wf.submit("bob", record_id="r2")
        wf.approve("r1", "admin")

        all_records = wf.list_all()
        assert len(all_records) == 2

    def test_state_machine_accessible(self) -> None:
        wf = ApprovalWorkflow()
        sm = wf.state_machine
        assert sm is not None
        assert "pending" in sm.states


# ---------------------------------------------------------------------------
# Immutability check on ApprovalRecord
# ---------------------------------------------------------------------------


class TestApprovalRecordImmutability:
    """Tests that ApprovalRecord is frozen."""

    def test_record_is_frozen(self) -> None:
        wf = ApprovalWorkflow()
        record = wf.submit("alice")
        with pytest.raises(AttributeError):
            record.status = ApprovalStatus.APPROVED  # type: ignore[misc]
