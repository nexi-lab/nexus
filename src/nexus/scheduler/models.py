"""Data models for the Nexus Scheduler.

Uses frozen dataclasses for immutability (SDK-side models).
Pydantic models for API request/response are in the router module.

Related: Issue #1212
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from nexus.scheduler.constants import TASK_STATUS_QUEUED, PriorityTier


@dataclass(frozen=True)
class TaskSubmission:
    """Immutable task submission from an agent.

    Represents a request to schedule a task for execution.
    All priority signals are captured at submission time.
    """

    agent_id: str  # Submitting agent
    executor_id: str  # Target agent/service to execute
    task_type: str  # Task type identifier
    payload: dict[str, Any] = field(default_factory=dict)
    priority: PriorityTier = PriorityTier.NORMAL
    deadline: datetime | None = None
    boost_amount: Decimal = Decimal("0")
    idempotency_key: str | None = None


@dataclass(frozen=True)
class ScheduledTask:
    """Immutable scheduled task with computed priority.

    Represents a task that has been accepted into the queue
    with its effective priority computed.
    """

    id: str
    agent_id: str
    executor_id: str
    task_type: str
    payload: dict[str, Any]
    priority_tier: PriorityTier
    effective_tier: int  # Computed: tier - boost - aging (lower = higher priority)
    enqueued_at: datetime
    status: str = TASK_STATUS_QUEUED
    deadline: datetime | None = None
    boost_amount: Decimal = Decimal("0")
    boost_tiers: int = 0
    boost_reservation_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    zone_id: str = "default"
    idempotency_key: str | None = None
