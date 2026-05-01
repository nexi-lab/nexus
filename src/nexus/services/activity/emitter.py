"""Service-side emitter implementations.

The brick-facing API (Emitter Protocol, NoopEmitter, set_emitter, get_emitter,
emit, EventKind, Result) lives in ``nexus.contracts.protocols.activity`` so
bricks can call ``emit(...)`` without crossing the brick / services boundary.
This module owns the production ``QueueEmitter`` that lifespan installs, plus
small helpers for ID and timestamp generation. ``Emitter``, ``NoopEmitter``,
``set_emitter``, ``get_emitter``, ``emit`` are re-exported here so existing
service-side imports keep working.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from nexus.contracts.protocols.activity import (
    Emitter,
    EventKind,
    NoopEmitter,
    Result,
    emit,
    get_emitter,
    set_emitter,
)
from nexus.services.activity.events import ActivityEvent, Actor, Subject

__all__ = [
    "Emitter",
    "NoopEmitter",
    "QueueEmitter",
    "emit",
    "get_emitter",
    "set_emitter",
]


def _new_id() -> str:
    """Sortable id: ms-timestamp prefix + random suffix (no third-party dep).

    IDs from different milliseconds sort in time order. IDs within the
    same millisecond have no time-order guarantee.
    """
    ms = int(time.time() * 1000)
    rand = uuid.uuid4().hex[:16]
    return f"{ms:013d}{rand}"


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="microseconds")


class QueueEmitter:
    """Production emitter — non-blocking enqueue with drop counter.

    Thread-safe when constructed with a ``loop``: off-loop callers are
    bridged through ``loop.call_soon_threadsafe`` so the ``asyncio.Queue``
    is only mutated by the loop thread. Lifespan registration always
    supplies the running loop. ``loop=None`` is supported for unit tests
    that run synchronously with no concurrent consumer; in that mode the
    emitter performs a direct ``put_nowait`` and the caller is responsible
    for staying on a single thread.

    Lifecycle: a single ``_inflight`` counter guarded by ``_lock`` covers
    both active ``emit()`` calls (before they have scheduled or enqueued)
    and scheduled-but-not-run callbacks. ``quiesce_pending()`` flips the
    closing flag (rejecting new emits) and waits for that counter to
    reach zero so shutdown cannot orphan an in-flight emission.
    """

    def __init__(
        self,
        *,
        queue: asyncio.Queue[ActivityEvent],
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._queue = queue
        self._loop = loop
        self._drop_count = 0
        self._lock = threading.Lock()
        self._inflight = 0
        self._closing = False
        # Bound in-flight emissions to the queue's nominal capacity so a
        # threadpool burst cannot pile up unbounded ActivityEvent objects
        # in scheduled callbacks before drops are recorded.
        self._max_inflight = max(1, queue.maxsize) if queue.maxsize > 0 else 0

    @property
    def drop_count(self) -> int:
        return self._drop_count

    @property
    def pending_off_loop(self) -> int:
        # Backward-compatible name: counts both active emits and scheduled
        # callbacks. Tests that asserted bound on the burst pattern still
        # see the same upper bound (queue capacity).
        return self._inflight

    async def quiesce_pending(self, *, timeout: float = 2.0) -> None:
        """Mark closing and wait for all in-flight emissions to land.

        After installing NoopEmitter at shutdown, lifespan calls this so
        existing emitters (which threads may still be inside) finish
        scheduling and the loop runs the resulting callbacks before the
        worker drains the queue. The closing flag rejects late submissions
        once quiesce starts — they count as drops instead of orphaning.
        """
        with self._lock:
            self._closing = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            with self._lock:
                if self._inflight == 0:
                    return
            await asyncio.sleep(0.005)

    def emit(
        self,
        *,
        kind: EventKind,
        result: Result,
        actor_token_hash: str | None = None,
        actor_agent: str | None = None,
        actor_user: str | None = None,
        subject_zone: str | None = None,
        subject_extra: dict[str, Any] | None = None,
        latency_ms: int | None = None,
        trace_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        # Lifecycle + capacity gate. Closing emitters or saturated queues
        # admit nothing new — the event is counted as a drop and we exit
        # before constructing the ActivityEvent or recording metrics.
        # Capacity check is `inflight + already-queued >= max` so a stalled
        # worker (queue full, no scheduled callbacks pending) still drops
        # before paying construction + metrics cost on the hot path.
        with self._lock:
            if self._closing:
                self._record_drop_locked()
                return
            if self._max_inflight > 0:
                queued = self._queue.qsize()
                if self._inflight + queued >= self._max_inflight:
                    self._record_drop_locked()
                    return
            self._inflight += 1

        scheduled = False
        try:
            event = ActivityEvent(
                id=_new_id(),
                ts=_now_iso(),
                kind=kind,
                result=result,
                latency_ms=latency_ms,
                trace_id=trace_id,
                actor=Actor(token_hash=actor_token_hash, agent=actor_agent, user=actor_user),
                subject=Subject(zone=subject_zone, extra=subject_extra),
                meta=meta,
            )
            try:
                from nexus.services.activity.metrics import record_metrics

                record_metrics(
                    kind=kind,
                    result=result,
                    actor_token_hash=actor_token_hash,
                    subject_zone=subject_zone,
                    subject_extra=subject_extra,
                    latency_ms=latency_ms,
                )
            except Exception:  # metrics must never break the hot path
                pass
            if self._loop is None:
                self._enqueue(event)
                return
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is self._loop:
                self._enqueue(event)
            else:
                # Off-loop: transfer the inflight slot to the scheduled
                # callback. The callback decrements after enqueuing.
                try:
                    self._loop.call_soon_threadsafe(self._enqueue_from_thread, event)
                    scheduled = True
                except RuntimeError:
                    # Loop closed — drop and let the finally clause
                    # decrement the slot.
                    self._record_drop()
        finally:
            if not scheduled:
                with self._lock:
                    self._inflight -= 1

    def _enqueue_from_thread(self, event: ActivityEvent) -> None:
        try:
            self._enqueue(event)
        finally:
            with self._lock:
                self._inflight -= 1

    def _enqueue(self, event: ActivityEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._record_drop()

    def _record_drop(self) -> None:
        self._drop_count += 1
        try:
            from nexus.services.activity.metrics import ACTIVITY_DROPS

            ACTIVITY_DROPS.inc()
        except Exception:
            pass

    def _record_drop_locked(self) -> None:
        # Called while self._lock is held — same logic as _record_drop.
        self._drop_count += 1
        try:
            from nexus.services.activity.metrics import ACTIVITY_DROPS

            ACTIVITY_DROPS.inc()
        except Exception:
            pass
