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

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum, StrEnum
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Lifecycle phase constants — open ``str`` values
# so bricks/extensions can define custom phases without modifying this module.
# ---------------------------------------------------------------------------

PRE_MOUNT: str = "pre_mount"
POST_MOUNT: str = "post_mount"
PRE_UNMOUNT: str = "pre_unmount"
POST_UNMOUNT: str = "post_unmount"
PRE_UNREGISTER: str = "pre_unregister"
POST_UNREGISTER: str = "post_unregister"
BRICK_STARTED: str = "brick_started"
BRICK_STOPPED: str = "brick_stopped"
RECONCILE_STARTED: str = "reconcile_started"
RECONCILE_COMPLETED: str = "reconcile_completed"

# Zone lifecycle phases (Issue #2061)
PRE_ZONE_DRAIN: str = "pre_zone_drain"
POST_ZONE_DRAIN: str = "post_zone_drain"
PRE_ZONE_FINALIZE: str = "pre_zone_finalize"
POST_ZONE_FINALIZE: str = "post_zone_finalize"

# ---------------------------------------------------------------------------
# Event constants — string events used by the FSM transition table
# ---------------------------------------------------------------------------

EVENT_MOUNT: str = "mount"
EVENT_STARTED: str = "started"
EVENT_FAILED: str = "failed"
EVENT_UNMOUNT: str = "unmount"
EVENT_STOPPED: str = "stopped"
EVENT_UNMOUNTED: str = "unmounted"
EVENT_UNREGISTER: str = "unregister"
EVENT_RESET: str = "reset"

# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------


class ZoneState(Enum):
    """Lifecycle states for a zone (Kubernetes-style).

    Transition diagram::

        ACTIVE ──► TERMINATING ──► DESTROYED

    ``TERMINATING`` is a transient state during zone deprovision.
    ``DESTROYED`` is the terminal state after cleanup completes.
    """

    ACTIVE = "active"
    TERMINATING = "terminating"
    DESTROYED = "destroyed"


class BrickState(Enum):
    """Lifecycle states for a managed brick.

    Transition diagram::

        REGISTERED ──► STARTING ──► ACTIVE ──► STOPPING ──► UNMOUNTED ──► UNREGISTERED
              │              │                      │           │
              └──► FAILED ◄──┘──────────────────────┘───────────┘

        UNMOUNTED + "mount" → STARTING  (re-mount)
        FAILED   + "reset" → REGISTERED (retry)

    ``UNMOUNTED`` is the post-stop state: brick is still in the registry
    and can be re-mounted.  ``UNREGISTERED`` is the terminal state after
    explicit ``unregister()`` — the brick is removed from the registry.

    ``FAILED`` is reachable from ``STARTING``, ``ACTIVE``, ``STOPPING``,
    or ``UNMOUNTED`` when an unrecoverable error occurs.
    """

    REGISTERED = "registered"
    STARTING = "starting"
    ACTIVE = "active"
    STOPPING = "stopping"
    UNMOUNTED = "unmounted"
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
        stopped_at: Unix timestamp when brick entered UNMOUNTED/FAILED.
        unmounted_at: Unix timestamp when brick entered UNMOUNTED.
    """

    name: str
    state: BrickState
    protocol_name: str
    error: str | None = None
    started_at: float | None = None
    stopped_at: float | None = None
    unmounted_at: float | None = None


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


@dataclass(frozen=True, slots=True)
class ZoneDeprovisionReport:
    """Summary of a zone deprovision operation (Issue #2061).

    Attributes:
        zone_id: Identifier of the zone being deprovisioned.
        zone_state: Final zone state after deprovision.
        bricks_drained: Number of bricks successfully drained.
        bricks_finalized: Number of bricks successfully finalized.
        drain_errors: Number of errors during drain phase.
        finalize_errors: Number of errors during finalize phase.
        elapsed_seconds: Total wall-clock time for the operation.
        forced: True if grace period expired and teardown was forced.
    """

    zone_id: str
    zone_state: ZoneState
    bricks_drained: int
    bricks_finalized: int
    drain_errors: int
    finalize_errors: int
    elapsed_seconds: float
    forced: bool


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


class ZoneAwareBrickProtocol(Protocol):
    """Contract for bricks with zone-specific cleanup (Issue #2061).

    Opt-in, separate from ``BrickLifecycleProtocol``. Bricks that hold
    zone-specific resources (cached embeddings, open connections, event
    subscriptions) implement ``drain()`` and/or ``finalize()`` for ordered
    zone teardown.

    Both methods are independently optional — a brick may implement only
    ``drain()`` (e.g., stop accepting work) or only ``finalize()``
    (e.g., flush caches). Detection uses ``hasattr``/``callable`` duck
    typing rather than ``isinstance`` to support partial implementations.

    Not ``@runtime_checkable`` because partial implementation (drain-only
    or finalize-only) is the expected usage pattern.

    Example::

        class SearchBrick:
            async def drain(self, zone_id: str) -> None:
                self._stop_indexing(zone_id)

            async def finalize(self, zone_id: str) -> None:
                await self._flush_cache(zone_id)
    """

    async def drain(self, zone_id: str) -> None:
        """Stop accepting new work for this zone. Idempotent."""
        ...

    async def finalize(self, zone_id: str) -> None:
        """Clean up zone-specific resources. Called after drain."""
        ...


# ---------------------------------------------------------------------------
# Per-brick reconciliation protocol (Issue #2059)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReconcileContext:
    """Context passed to per-brick reconcile methods.

    Provides the brick with enough information to decide how to self-heal
    without requiring access to the lifecycle manager internals.

    Attributes:
        brick_name: Brick registry name.
        current_state: Current FSM state of the brick.
        desired_enabled: Whether the brick spec says it should be active.
        retry_count: Number of times this brick has been retried.
        last_error: Most recent error message (if any).
        last_healthy_at: Monotonic timestamp of last successful health check.
    """

    brick_name: str
    current_state: BrickState
    desired_enabled: bool
    retry_count: int
    last_error: str | None
    last_healthy_at: float | None


@dataclass(frozen=True, slots=True)
class BrickReconcileOutcome:
    """Per-brick reconcile result (Kubernetes-style).

    Returned by ``ReconcilerProtocol.reconcile()`` to signal the reconciler
    what to do next.

    Attributes:
        requeue: If True, the reconciler should retry this brick on the next pass.
        requeue_after: Optional explicit delay before next retry.
        error: If set, the brick transitions to FAILED with this message.
    """

    requeue: bool = False
    requeue_after: timedelta | None = None
    error: str | None = None


@runtime_checkable
class ReconcilerProtocol(Protocol):
    """Contract for bricks with custom self-healing logic (Issue #2059).

    Bricks that implement ``reconcile()`` gain per-brick recovery:
    instead of the global reset-and-remount strategy, the brick can
    inspect its own state and return a ``BrickReconcileOutcome`` to
    signal requeue, explicit backoff, or error.

    Example::

        class SearchBrick:
            async def reconcile(self, ctx: ReconcileContext) -> BrickReconcileOutcome:
                if not self._index.is_connected():
                    await self._index.reconnect()
                    return BrickReconcileOutcome(requeue=True)
                return BrickReconcileOutcome()  # healthy
    """

    async def reconcile(self, ctx: ReconcileContext) -> BrickReconcileOutcome:
        """Per-brick self-healing logic. Called by the reconciler each pass."""
        ...


class LifecycleManagerProtocol(Protocol):
    """Contract for the lifecycle manager as seen by the reconciler.

    Type-safe replacement for ``Any`` — documents the exact 8 methods
    the reconciler calls on the manager.
    """

    def iter_bricks(self) -> list[tuple[str, BrickSpec, BrickState, int, Any]]:
        """Snapshot of all bricks: (name, spec, state, retry_count, instance)."""
        ...

    def get_status(self, name: str) -> BrickStatus | None:
        """Get current status for a single brick."""
        ...

    def fail_brick(self, name: str, error: str) -> None:
        """Transition a brick to FAILED with an error message."""
        ...

    def reset_for_retry(self, name: str) -> int:
        """Reset a FAILED brick to REGISTERED; return new retry count."""
        ...

    async def mount(self, name: str) -> None:
        """Mount a registered/unmounted brick."""
        ...

    async def unmount(self, name: str) -> None:
        """Unmount an active brick."""
        ...

    def clear_retry_count(self, name: str) -> None:
        """Reset retry counter to 0 (on successful recovery)."""
        ...

    def get_retry_count(self, name: str) -> int:
        """Get current retry count for a brick."""
        ...
