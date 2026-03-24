"""Service lifecycle contracts — HotSwappable + PersistentService (Issue #1577).

Two runtime-checkable Protocols that let the kernel, coordinator, and CLI
distinguish service lifecycle tiers without coupling to concrete classes:

    HotSwappable        — service declares VFS hooks and supports drain/activate
    PersistentService   — service requires a long-running process (background workers)

Four-quadrant classification:

    +--------------------+-----------------+---------------------+
    |                    | On-demand       | Persistent-required |
    +--------------------+-----------------+---------------------+
    | Restart-required   | SearchService   | EventDeliveryWorker |
    |                    | LLMService      |                     |
    +--------------------+-----------------+---------------------+
    | HotSwappable       | ReBACService    | (future)            |
    |                    | MountService    |                     |
    +--------------------+-----------------+---------------------+

Usage::

    from nexus.contracts.protocols.service_lifecycle import (
        HotSwappable,
        PersistentService,
    )

    if isinstance(svc, HotSwappable):
        spec = svc.hook_spec()         # declare VFS hooks
        await svc.drain()              # stop accepting new work
        await svc.activate()           # start serving after swap

    if isinstance(svc, PersistentService):
        await svc.start()              # begin background work
        await svc.stop()               # graceful shutdown

Design decisions:
    - Protocol (structural) over ABC (nominal) — services satisfy the contract
      by implementing the methods, no explicit inheritance required.
    - @runtime_checkable — coordinator and CLI can use isinstance() checks.
    - Separate from BrickLifecycleProtocol — that protocol covers the full
      FSM (REGISTERED -> ACTIVE -> UNMOUNTED) with health_check().  These
      protocols are lighter-weight and composable.

Linux analogy:
    HotSwappable    ~ module with pre_remove()/post_install() callbacks
    PersistentService ~ kthread (kernel thread that runs in background)

References:
    - docs/architecture/KERNEL-ARCHITECTURE.md
    - Issue #1452: Service lifecycle / hot-swap
    - Issue #1577: HotSwappable + PersistentService protocols
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec


# ---------------------------------------------------------------------------
# ServiceQuadrant — first-class quadrant classification (Issue #1673)
# ---------------------------------------------------------------------------


class ServiceQuadrant(enum.Enum):
    """Four-quadrant classification based on protocol conformance.

    Constraint lattice (Q1 = fewest constraints, Q4 = most)::

              Q4 (both) ← most constrained
             /        \\
           Q2          Q3
             \\        /
              Q1 (restart-required) ← least constrained

    Q2 and Q3 are independent constraint dimensions:
    - Q2 adds hot-swap capability (service must implement drain/activate/hook_spec)
    - Q3 adds persistent requirement (environment must support long-running process)
    - Q4 is their union — both constraints apply

    Naming rationale:
    - **Restart-required** (Q1): replacing this service requires a full restart,
      because it does not implement drain/activate for safe hot-swap.
    - **HotSwappable** (Q2): service declares dispatch hooks and supports
      drain → swap → activate at runtime — a capability declaration to the kernel.
    - **On-demand** (column): service has no background tasks, only handles calls.
    - **Persistent-required** (column): service has background work that must
      be started/stopped (event loops, polling, workers).
    """

    Q1_RESTART_REQUIRED = "Q1"
    Q2_HOT_SWAPPABLE = "Q2"
    Q3_PERSISTENT = "Q3"
    Q4_BOTH = "Q4"

    @staticmethod
    def of(instance: Any) -> ServiceQuadrant:
        """Classify a service instance into its quadrant."""
        is_hot = isinstance(instance, HotSwappable)
        is_persistent = isinstance(instance, PersistentService)
        if is_hot and is_persistent:
            return ServiceQuadrant.Q4_BOTH
        if is_hot:
            return ServiceQuadrant.Q2_HOT_SWAPPABLE
        if is_persistent:
            return ServiceQuadrant.Q3_PERSISTENT
        return ServiceQuadrant.Q1_RESTART_REQUIRED

    @property
    def is_hot_swappable(self) -> bool:
        """True if this quadrant includes HotSwappable capability (Q2/Q4)."""
        return self in (ServiceQuadrant.Q2_HOT_SWAPPABLE, ServiceQuadrant.Q4_BOTH)

    @property
    def is_persistent(self) -> bool:
        """True if this quadrant requires persistent process (Q3/Q4)."""
        return self in (ServiceQuadrant.Q3_PERSISTENT, ServiceQuadrant.Q4_BOTH)

    @property
    def label(self) -> str:
        """Human-readable label for error messages and CLI output."""
        labels = {
            ServiceQuadrant.Q1_RESTART_REQUIRED: "Q1 (restart-required)",
            ServiceQuadrant.Q2_HOT_SWAPPABLE: "Q2 (HotSwappable)",
            ServiceQuadrant.Q3_PERSISTENT: "Q3 (PersistentService)",
            ServiceQuadrant.Q4_BOTH: "Q4 (HotSwappable + PersistentService)",
        }
        return labels[self]


# ---------------------------------------------------------------------------
# HotSwappable — runtime hot-swap support
# ---------------------------------------------------------------------------


@runtime_checkable
class HotSwappable(Protocol):
    """Service that supports runtime hot-swap via ServiceRegistry.

    A HotSwappable service declares its VFS hooks and supports graceful
    drain (stop accepting new work) and activate (start serving) transitions.

    The coordinator uses this protocol to decide swap behaviour::

        if isinstance(old_svc, HotSwappable):
            spec = old_svc.hook_spec()
            await old_svc.drain()
            # ... unregister old hooks, register new hooks ...
            await new_svc.activate()
        else:
            # Simple registry replace — no hook management
            registry.replace_service(name, new_svc)

    Implementors provide:
        hook_spec()  — return a HookSpec describing VFS hooks this service owns
        drain()      — stop accepting new work; called before hook unregistration
        activate()   — start serving; called after hook registration

    Example::

        class MyPermissionService:
            def hook_spec(self) -> HookSpec:
                return HookSpec(
                    read_hooks=(self._read_hook,),
                    write_hooks=(self._write_hook,),
                )

            async def drain(self) -> None:
                self._accepting = False

            async def activate(self) -> None:
                self._accepting = True
    """

    def hook_spec(self) -> HookSpec:
        """Declare VFS hooks this service owns.

        Called by the coordinator during swap to unregister old hooks
        and register new hooks.  Must return a stable snapshot —
        the coordinator may call this once and cache the result.
        """
        ...

    async def drain(self) -> None:
        """Stop accepting new work.

        Called before hook unregistration during hot-swap.
        In-flight calls tracked by ServiceRef will complete normally;
        drain() is an additional signal for service-internal cleanup
        (e.g., stop background polling, flush buffers).

        Must be idempotent — may be called multiple times.
        """
        ...

    async def activate(self) -> None:
        """Start serving after swap.

        Called after the new instance's hooks are registered and
        BLM state transitions to ACTIVE.  The service should begin
        accepting work and initialize any runtime state.

        Must be idempotent — may be called multiple times.
        """
        ...


# ---------------------------------------------------------------------------
# PersistentService — requires long-running process
# ---------------------------------------------------------------------------


@runtime_checkable
class PersistentService(Protocol):
    """Service that requires a long-running process (background workers).

    PersistentServices have background tasks that run continuously
    (e.g., event delivery polling, deferred permission flushing).
    They need an ``asyncio`` event loop and cannot operate in
    on-demand mode (Lambda, Cloud Run single-request).

    The kernel and CLI use this protocol to determine distro type::

        persistent = [
            name for name, svc in registry.all()
            if isinstance(svc, PersistentService)
        ]
        if persistent:
            log.info("Persistent distro: %s", persistent)
        else:
            log.info("On-demand compatible distro")

    Implementors provide:
        start()  — begin background work (spawn tasks, open connections)
        stop()   — graceful shutdown (drain queues, close connections)

    Example::

        class EventDeliveryWorker:
            async def start(self) -> None:
                self._task = asyncio.create_task(self._poll_loop())

            async def stop(self) -> None:
                self._running = False
                await self._task

    Design note:
        Separate from ``BrickLifecycleProtocol`` which adds ``health_check()``
        and integrates with the BLM FSM.  ``PersistentService`` is lighter —
        just the start/stop contract for distro classification.
    """

    async def start(self) -> None:
        """Begin background work.

        Called during ``nx.bootstrap()`` to start background tasks.
        Must be safe to call at any point after ``nx.initialize()``.
        Must be idempotent — calling start() on an already-started
        service should be a no-op.
        """
        ...

    async def stop(self) -> None:
        """Gracefully shut down background work.

        Called during ``nx.close()`` or process shutdown.
        Must drain in-flight work and release resources.
        Must be idempotent — calling stop() on an already-stopped
        service should be a no-op.
        """
        ...
