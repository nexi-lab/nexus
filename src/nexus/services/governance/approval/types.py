"""Shared approval types used across all approval workflows.

Issue #1359 Phase 0: Superset of statuses from SpendingApproval,
SkillApproval, and DisputeRecord.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ApprovalStatus(StrEnum):
    """Universal approval status (superset of all 3 existing workflows).

    Covers:
        - SpendingApproval: pending, approved, rejected, expired
        - SkillApproval: pending, approved, rejected
        - DisputeRecord: filed, auto_mediating, resolved, dismissed
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True)
class ApprovalTimestamps:
    """Immutable timestamps for approval lifecycle events."""

    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None


@dataclass(frozen=True)
class ExpiryPolicy:
    """Immutable expiry configuration for time-bounded approvals."""

    expires_at: datetime
    duration_hours: float = 24.0
