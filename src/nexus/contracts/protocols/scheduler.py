"""Scheduler service protocol (Issue #1383, #1274, #2360).

Defines the contract for agent work-request scheduling.

Also defines ``CreditsReservationProtocol`` -- the narrow interface
the scheduler uses for credit-based priority boosting, decoupling
the system service tier from the pay brick (LEGO 3.3).

Storage Affinity: **CacheStore** -- ephemeral work queue (Dragonfly sorted set).

References:
    - docs/architecture/KERNEL-ARCHITECTURE.md 3
    - docs/architecture/data-storage-matrix.md (Four Pillars)
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md 2.4, 4.2
    - Issue #1383: Define 6 kernel protocol interfaces
    - Issue #1274: Astraea-style state-aware scheduler
    - Issue #2360: Promote scheduler to always-started system service
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Maximum completed tasks retained by InMemoryScheduler to prevent
# unbounded memory growth in long-running edge deployments.
_MAX_COMPLETED = 10_000


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """A work request submitted to the scheduler.

    Attributes:
        agent_id: Target agent identifier.
        zone_id: Zone/organization ID for multi-zone isolation.
        priority: Scheduling priority (lower = more urgent, matches PriorityTier).  Default 0.
        submitted_at: ISO-8601 timestamp of submission.
        payload: Arbitrary request-specific data.
        executor_id: Target executor agent (Astraea).
        task_type: Task type identifier for classification (Astraea).
        request_state: Current execution state for classification (Astraea).
        priority_class: Scheduling class: interactive/batch/background (Astraea).
        deadline: Optional ISO-8601 deadline (Astraea).
        boost_amount: Credits for priority boost as decimal string (Astraea).
        estimated_service_time: Estimated service time in seconds (Astraea/HRRN).
    """

    agent_id: str
    zone_id: str | None
    priority: int = 0
    submitted_at: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    # Astraea extensions (all optional for backward compat)
    executor_id: str | None = None
    task_type: str = ""
    request_state: str = "pending"
    priority_class: str = "batch"
    deadline: str | None = None
    boost_amount: str = "0"
    estimated_service_time: float = 30.0
    idempotency_key: str | None = None
    overlap_policy: str = "skip"  # allow | skip | cancel (Issue #2749)


@runtime_checkable
class SchedulerProtocol(Protocol):
    """Service contract for agent work-request scheduling.

    10-method interface: 8 operations + initialize/shutdown lifecycle.
    Both SchedulerService and InMemoryScheduler implement all methods,
    making duck-typing checks (``hasattr``) unnecessary.
    """

    async def submit(self, request: AgentRequest) -> str: ...

    async def next(self, *, executor_id: str | None = None) -> AgentRequest | None: ...

    async def pending_count(self, *, zone_id: str | None = None) -> int: ...

    async def cancel(self, agent_id: str) -> int: ...

    async def get_status(self, task_id: str) -> dict[str, Any] | None: ...

    async def complete(self, task_id: str, *, error: str | None = None) -> None: ...

    async def classify(self, request: AgentRequest) -> str: ...

    async def metrics(self, *, zone_id: str | None = None) -> dict[str, Any]: ...

    async def initialize(self, *args: Any, **kwargs: Any) -> None: ...

    async def shutdown(self) -> None: ...


# ---------------------------------------------------------------------------
# CreditsReservationProtocol -- narrow interface for pay brick decoupling
# ---------------------------------------------------------------------------


@runtime_checkable
class CreditsReservationProtocol(Protocol):
    """Narrow credit-reservation interface used by the scheduler.

    Decouples SchedulerService (system service) from CreditsService (pay brick)
    per LEGO 3.3: "A brick MUST NOT import from other bricks."

    Only the two methods the scheduler actually uses are exposed:
    ``reserve()`` for priority-boost escrow and ``release_reservation()``
    for cancellation refunds.
    """

    async def reserve(
        self,
        agent_id: str,
        amount: Decimal,
        timeout_seconds: int = 300,
        *,
        zone_id: str = "",
    ) -> str:
        """Reserve credits for a priority boost.

        Returns:
            Reservation ID string.
        """
        ...

    async def release_reservation(self, reservation_id: str) -> None:
        """Void a pending reservation (full refund)."""
        ...


class NullCreditsReservation:
    """No-op credits stub for deployments without the pay brick.

    Structurally satisfies ``CreditsReservationProtocol``.
    All operations succeed silently with placeholder values.
    """

    async def reserve(
        self,
        agent_id: str,  # noqa: ARG002
        amount: Decimal,  # noqa: ARG002
        timeout_seconds: int = 300,  # noqa: ARG002
        *,
        zone_id: str = "",  # noqa: ARG002
    ) -> str:
        return "null-reservation"

    async def release_reservation(self, reservation_id: str) -> None:  # noqa: ARG002
        pass
