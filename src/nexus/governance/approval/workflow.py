"""Generic ApprovalWorkflow[T] base class.

Issue #1359 Phase 0: Provides submit/approve/reject/expire lifecycle
that any domain-specific approval can inherit from.

Domain-specific logic (ReBAC checks, escrow, amount validation) belongs
in the subclass, NOT here.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Generic, TypeVar

from nexus.governance.approval.state_machine import StateMachine
from nexus.governance.approval.types import ApprovalStatus

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Standard approval state machine
APPROVAL_TRANSITIONS: dict[str, frozenset[str]] = {
    ApprovalStatus.PENDING: frozenset(
        {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED}
    ),
    ApprovalStatus.APPROVED: frozenset(),
    ApprovalStatus.REJECTED: frozenset(),
    ApprovalStatus.EXPIRED: frozenset(),
}

_APPROVAL_STATE_MACHINE = StateMachine(APPROVAL_TRANSITIONS)


@dataclass(frozen=True)
class ApprovalRecord:
    """Generic approval record returned by the workflow."""

    record_id: str
    status: ApprovalStatus
    submitted_by: str
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    expires_at: datetime | None = None
    metadata: dict[str, object] | None = None


class ApprovalWorkflow(Generic[T]):
    """Generic approval workflow with state machine enforcement.

    Subclasses provide domain-specific storage and validation.
    This base class handles:
        - State transitions (via StateMachine)
        - ID generation
        - Timestamp management
        - Expiry handling

    Usage:
        class MyApprovalWorkflow(ApprovalWorkflow[MyRecord]):
            def _persist_submit(self, record: ApprovalRecord) -> None: ...
            def _persist_decision(self, record_id: str, ...) -> None: ...
    """

    def __init__(
        self,
        state_machine: StateMachine | None = None,
        default_expiry_hours: float = 24.0,
    ) -> None:
        self._sm = state_machine or _APPROVAL_STATE_MACHINE
        self._default_expiry_hours = default_expiry_hours
        # In-memory store for testing / simple usage
        self._records: dict[str, ApprovalRecord] = {}

    @property
    def state_machine(self) -> StateMachine:
        """Access the underlying state machine."""
        return self._sm

    def submit(
        self,
        submitted_by: str,
        *,
        record_id: str | None = None,
        expires_at: datetime | None = None,
        metadata: dict[str, object] | None = None,
    ) -> ApprovalRecord:
        """Submit a new approval request.

        Returns:
            ApprovalRecord in PENDING status.
        """
        now = datetime.now(UTC)
        rid = record_id or str(uuid.uuid4())

        if expires_at is None and self._default_expiry_hours > 0:
            expires_at = now + timedelta(hours=self._default_expiry_hours)

        record = ApprovalRecord(
            record_id=rid,
            status=ApprovalStatus.PENDING,
            submitted_by=submitted_by,
            created_at=now,
            expires_at=expires_at,
            metadata=metadata,
        )
        self._records[rid] = record

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Approval submitted: id=%s by=%s", rid, submitted_by)

        return record

    def approve(self, record_id: str, decided_by: str) -> ApprovalRecord:
        """Approve a pending request.

        Raises:
            KeyError: Record not found.
            InvalidTransitionError: Not in PENDING state.
        """
        return self._decide(record_id, ApprovalStatus.APPROVED, decided_by)

    def reject(self, record_id: str, decided_by: str) -> ApprovalRecord:
        """Reject a pending request.

        Raises:
            KeyError: Record not found.
            InvalidTransitionError: Not in PENDING state.
        """
        return self._decide(record_id, ApprovalStatus.REJECTED, decided_by)

    def expire(self, record_id: str) -> ApprovalRecord:
        """Expire a pending request.

        Raises:
            KeyError: Record not found.
            InvalidTransitionError: Not in PENDING state.
        """
        return self._decide(record_id, ApprovalStatus.EXPIRED, decided_by="system")

    def get(self, record_id: str) -> ApprovalRecord | None:
        """Get an approval record by ID."""
        return self._records.get(record_id)

    def list_pending(self) -> list[ApprovalRecord]:
        """List all pending approval records."""
        return [r for r in self._records.values() if r.status == ApprovalStatus.PENDING]

    def list_all(self) -> list[ApprovalRecord]:
        """List all approval records."""
        return list(self._records.values())

    def _decide(
        self,
        record_id: str,
        new_status: ApprovalStatus,
        decided_by: str,
    ) -> ApprovalRecord:
        """Apply a decision transition."""
        record = self._records.get(record_id)
        if record is None:
            msg = f"Approval record {record_id!r} not found"
            raise KeyError(msg)

        # Validate transition
        self._sm.transition(record.status, new_status)

        now = datetime.now(UTC)
        updated = ApprovalRecord(
            record_id=record.record_id,
            status=new_status,
            submitted_by=record.submitted_by,
            created_at=record.created_at,
            decided_at=now,
            decided_by=decided_by,
            expires_at=record.expires_at,
            metadata=record.metadata,
        )
        self._records[record_id] = updated

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Approval %s: id=%s by=%s",
                new_status,
                record_id,
                decided_by,
            )

        return updated
