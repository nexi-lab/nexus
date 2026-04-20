"""Service lifecycle contract — PersistentService protocol (Issue #1577).

One-dimension model: the only user-facing lifecycle dimension is
**daemon vs on-demand** (PersistentService).  Hook management uses
duck-typed ``hook_spec()`` — the kernel auto-captures hooks via
``hasattr(instance, 'hook_spec')`` at enlist() time.

Usage::

    from nexus.contracts.protocols.service_lifecycle import PersistentService

    if isinstance(svc, PersistentService):
        await svc.start()              # begin background work
        await svc.stop()               # graceful shutdown

    # Hook management — duck-typed, no protocol needed:
    if hasattr(svc, 'hook_spec'):
        spec = svc.hook_spec()         # declare VFS hooks

Design decisions:
    - Protocol (structural) over ABC (nominal) — services satisfy the contract
      by implementing the methods, no explicit inheritance required.
    - @runtime_checkable — coordinator and CLI can use isinstance() checks.
    - hook_spec() is duck-typed convention, not a protocol requirement.
      The kernel auto-captures via hasattr() at enlist() time.
    - HotSwappable protocol deleted (YAGNI) — all 22 implementations had
      trivial drain()/activate().  Swap uses unified refcount drain path.
    - ServiceQuadrant enum deleted — the "HotSwappable" axis was a kernel
      implementation detail, not a user-facing constraint.

Linux analogy:
    PersistentService ~ kthread (kernel thread that runs in background)

References:
    - docs/architecture/KERNEL-ARCHITECTURE.md
    - Issue #1452: Service lifecycle / hot-swap
    - Issue #1577: HotSwappable + PersistentService protocols
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# BackgroundService — requires long-running process
# ---------------------------------------------------------------------------


@runtime_checkable
class BackgroundService(Protocol):
    """Service that requires a long-running process (background workers).

    BackgroundServices have background tasks that run continuously
    (e.g., event delivery polling, deferred permission flushing).
    They need an ``asyncio`` event loop and cannot operate in
    on-demand mode (Lambda, Cloud Run single-request).

    The kernel and CLI use this protocol to determine distro type::

        background = [
            name for name, svc in registry.all()
            if isinstance(svc, BackgroundService)
        ]
        if background:
            log.info("Background distro: %s", background)
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
        and integrates with the BLM FSM.  ``BackgroundService`` is lighter —
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
