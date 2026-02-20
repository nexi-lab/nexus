"""Brick lifecycle protocol and data models (Issue #1704).

Defines the contract for brick lifecycle management — the orchestration
layer that handles mount/unmount/hook integration for feature bricks.

The ``BrickLifecycleProtocol`` is opt-in: bricks with ``start()``,
``stop()``, and ``health_check()`` methods naturally satisfy it via
structural subtyping.  Stateless bricks (e.g. ``pay/``) bypass lifecycle
management entirely.

Storage Affinity: **None** — lifecycle state is ephemeral (in-memory).

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §3.2 (Brick Lifecycle)
    - Issue #1704: Brick lifecycle manager
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Lifecycle phase constants — open ``str`` values (same pattern as hook_engine.py)
# so bricks/extensions can define custom phases without modifying this module.
# ---------------------------------------------------------------------------

PRE_MOUNT: str = "pre_mount"
POST_MOUNT: str = "post_mount"
PRE_UNMOUNT: str = "pre_unmount"
POST_UNMOUNT: str = "post_unmount"
BRICK_STARTED: str = "brick_started"
BRICK_STOPPED: str = "brick_stopped"
RECONCILE_STARTED: str = "reconcile_started"
RECONCILE_COMPLETED: str = "reconcile_completed"


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------


class BrickState(Enum):
    """Lifecycle states for a managed brick.

    Transition diagram::

        REGISTERED ──► STARTING ──► ACTIVE ──► STOPPING ──► UNREGISTERED
              │              │                      │
              └──► FAILED ◄──┘──────────────────────┘

    ``FAILED`` is reachable from ``STARTING``, ``ACTIVE``, or ``STOPPING``
    when an unrecoverable error occurs.
    """

    REGISTERED = "registered"
    STARTING = "starting"
    ACTIVE = "active"
    STOPPING = "stopping"
    UNREGISTERED = "unregistered"
    FAILED = "failed"


class DriftAction(StrEnum):
    """Actions the reconciler can take to resolve drift.

    Inherits from ``StrEnum`` so values serialize cleanly in REST responses
    and match statements work with string comparisons.
    """

    SKIP = "skip"
    RESET = "reset"
    MOUNT = "mount"
    UNMOUNT = "unmount"
    HEALTH_CHECK_FAILED = "health_check_failed"


# ---------------------------------------------------------------------------
# Data models — frozen, slots for immutability and performance
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrickStatus:
    """Snapshot of a single brick's lifecycle state.

    Attributes:
        name: Brick registry name.
        state: Current lifecycle state.
        protocol_name: Protocol type name this brick implements.
        error: Error message if state is FAILED.
        started_at: Unix timestamp when brick entered ACTIVE.
        stopped_at: Unix timestamp when brick entered UNREGISTERED/FAILED.
    """

    name: str
    state: BrickState
    protocol_name: str
    error: str | None = None
    started_at: float | None = None
    stopped_at: float | None = None


@dataclass(frozen=True, slots=True)
class BrickDependency:
    """Declares a brick's dependencies on other bricks.

    Used to build the DAG for topological startup/shutdown ordering.

    Attributes:
        brick_name: Name of the dependent brick.
        depends_on: Tuple of brick names this brick depends on.
    """

    brick_name: str
    depends_on: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BrickHealthReport:
    """Aggregated health report for all managed bricks.

    Attributes:
        total: Total number of registered bricks.
        active: Number of bricks in ACTIVE state.
        failed: Number of bricks in FAILED state.
        bricks: Per-brick status snapshots.
    """

    total: int
    active: int
    failed: int
    bricks: tuple[BrickStatus, ...]


# ---------------------------------------------------------------------------
# Spec / Drift models — desired-state declaration + drift detection (Issue #2060)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrickSpec:
    """Desired-state declaration for a brick (Kubernetes-inspired spec).

    Immutable — use ``dataclasses.replace()`` to create modified copies.
    The reconciler compares spec vs. status to detect drift.

    Attributes:
        name: Brick registry name.
        protocol_name: Protocol type name this brick implements.
        depends_on: Tuple of brick names this brick depends on.
        enabled: Whether the brick should be active (desired state).
    """

    name: str
    protocol_name: str
    depends_on: tuple[str, ...] = ()
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class DriftReport:
    """Single-brick drift report — what the reconciler observed.

    Attributes:
        brick_name: Name of the brick.
        spec_state: What spec says should be (e.g. "enabled" → should be ACTIVE).
        actual_state: What status says it is.
        action: What reconciler will do (e.g. "mount", "reset", "skip").
        detail: Human-readable explanation.
    """

    brick_name: str
    spec_state: str
    actual_state: BrickState
    action: DriftAction
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Summary of a single reconciliation pass.

    Attributes:
        total_bricks: Total number of bricks evaluated.
        drifted: Number of bricks with spec/status mismatch.
        actions_taken: Number of corrective actions performed.
        errors: Number of actions that failed.
        drifts: Per-brick drift reports.
    """

    total_bricks: int
    drifted: int
    actions_taken: int
    errors: int
    drifts: tuple[DriftReport, ...] = ()


# ---------------------------------------------------------------------------
# Protocol — opt-in via structural subtyping
# ---------------------------------------------------------------------------


@runtime_checkable
class BrickLifecycleProtocol(Protocol):
    """Contract for lifecycle-aware bricks.

    Bricks that implement ``start()``, ``stop()``, and ``health_check()``
    automatically satisfy this protocol via duck typing.  Stateless bricks
    that lack these methods are treated as always-active and skip lifecycle
    management.

    Example::

        class SearchBrick:
            async def start(self) -> None:
                self._index = await connect_index()

            async def stop(self) -> None:
                await self._index.close()

            async def health_check(self) -> bool:
                return self._index.is_connected()

        # isinstance(SearchBrick(), BrickLifecycleProtocol) == True
    """

    async def start(self) -> None:
        """Initialize the brick (connect to services, warm caches, etc.)."""
        ...

    async def stop(self) -> None:
        """Gracefully shut down the brick (drain queues, close connections)."""
        ...

    async def health_check(self) -> bool:
        """Return True if the brick is healthy and ready to serve."""
        ...
