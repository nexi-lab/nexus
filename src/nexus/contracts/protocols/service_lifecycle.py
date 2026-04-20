"""Service lifecycle contract — ``BackgroundService`` protocol (Issue #1577).

``BackgroundService`` is the **opt-in** protocol for services that run
background work (event loops, queue pollers, replication scanners,
deferred flushers).  It is not a default — implement it **only** when
the service actually spawns long-running tasks that outlive a single
request.

**Most services don't implement this — they're plain classes.**  Plain
classes are on-demand compatible (AWS Lambda, Cloud Run single-request)
by default, no declaration needed.  A service becomes a
``BackgroundService`` by adding ``start()`` and ``stop()`` methods; the
``ServiceRegistry`` detects this structurally at ``enlist()`` time
(``@runtime_checkable``).

Two parallel shapes — pick whichever the service actually needs:

On-demand (default) — no lifecycle declaration::

    class SecretsService:
        def __init__(self, store: KVStore) -> None:
            self._store = store

        def get(self, key: str) -> str | None:
            return self._store.get(key)

        # No start() / stop() — plain class, registered on-demand.

Background (opt-in) — implements start/stop::

    class EventDeliveryWorker:
        async def start(self) -> None:
            self._task = asyncio.create_task(self._poll_loop())

        async def stop(self) -> None:
            self._running = False
            await self._task

Distribution classification:
    Services that do **not** implement ``BackgroundService`` → on-demand
    distro (Lambda / Cloud Run compatible). A distro with zero
    ``BackgroundService`` implementations can run without a long-lived
    process; any non-empty set requires a server/worker host.

Hook management is a separate, duck-typed concern: any service
(background or on-demand) may expose a ``hook_spec()`` method; the
kernel auto-captures it via ``hasattr(instance, 'hook_spec')`` at
``enlist()`` time.  ``hook_spec()`` is not part of this protocol.

Design decisions:
    - Protocol (structural) over ABC (nominal) — services satisfy the
      contract by implementing the methods; no explicit inheritance.
    - ``@runtime_checkable`` — registry and CLI use ``isinstance()``.
    - ``hook_spec()`` is a duck-typed convention, not a protocol
      requirement. Captured via ``hasattr()`` at enlist() time.
    - HotSwappable protocol deleted (YAGNI) — all 22 implementations had
      trivial drain()/activate().  Swap uses unified refcount drain path.
    - ServiceQuadrant enum deleted — the "HotSwappable" axis was a
      kernel implementation detail, not a user-facing constraint.

Linux analogy:
    ``BackgroundService`` ~ kthread (kernel thread that runs in the
    background).  On-demand services ~ regular functions invoked per
    request.

References:
    - docs/architecture/KERNEL-ARCHITECTURE.md
    - Issue #1452: Service lifecycle / hot-swap
    - Issue #1577: HotSwappable + PersistentService protocols
    - Issue #1580: Auto-lifecycle for background services
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# BackgroundService — opt-in: implement only for services with background work
# ---------------------------------------------------------------------------


@runtime_checkable
class BackgroundService(Protocol):
    """Opt-in protocol for services with long-running background work.

    Implementors provide:
        start()  — begin background work (spawn tasks, open connections)
        stop()   — graceful shutdown (drain queues, close connections)

    Most services should NOT implement this protocol (see module
    docstring for the on-demand default).
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
