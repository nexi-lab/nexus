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
from enum import Enum
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
