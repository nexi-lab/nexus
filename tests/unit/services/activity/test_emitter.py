"""Unit tests for the Emitter singleton and NoopEmitter."""

from __future__ import annotations

import asyncio
import threading

import pytest

from nexus.services.activity import EventKind, Result, emit, get_emitter, set_emitter
from nexus.services.activity.emitter import NoopEmitter, QueueEmitter
from nexus.services.activity.events import ActivityEvent


@pytest.fixture(autouse=True)
def _restore_emitter():
    saved = get_emitter()
    yield
    set_emitter(saved)


def test_default_emitter_is_noop() -> None:
    assert isinstance(get_emitter(), NoopEmitter)


def test_noop_emitter_drops_silently() -> None:
    emitter = NoopEmitter()
    emitter.emit(
        kind=EventKind.SEARCH,
        result=Result.OK,
        actor_token_hash=None,
        actor_agent=None,
        actor_user=None,
        subject_zone=None,
        subject_extra=None,
        latency_ms=None,
        trace_id=None,
        meta=None,
    )


def test_set_emitter_swaps_singleton() -> None:
    custom = NoopEmitter()
    set_emitter(custom)
    assert get_emitter() is custom


def test_emit_function_calls_current_emitter() -> None:
    class _Recording(NoopEmitter):
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def emit(self, **kw) -> None:
            self.calls.append(tuple(kw.items()))

    rec = _Recording()
    set_emitter(rec)
    emit(kind=EventKind.SEARCH, result=Result.OK)
    assert len(rec.calls) == 1


def _drain(q: asyncio.Queue) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def _event(zone: str | None = None):
    """Construct a bare ActivityEvent for queue pre-fill in tests."""
    from nexus.services.activity.events import ActivityEvent, Subject

    return ActivityEvent(
        id="test",
        ts="t",
        kind=EventKind.SEARCH,
        result=Result.OK,
        subject=Subject(zone=zone),
    )


def test_queue_emitter_enqueues_event() -> None:
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    emitter = QueueEmitter(queue=q)
    emitter.emit(kind=EventKind.SEARCH, result=Result.OK, subject_zone="eng", latency_ms=5)
    events = _drain(q)
    assert len(events) == 1
    ev = events[0]
    assert ev.kind is EventKind.SEARCH
    assert ev.subject.zone == "eng"
    assert ev.latency_ms == 5
    assert ev.id  # non-empty id
    assert ev.ts  # non-empty ISO ts


def test_queue_emitter_drops_on_overflow() -> None:
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    emitter = QueueEmitter(queue=q)
    emitter.emit(kind=EventKind.SEARCH, result=Result.OK)
    emitter.emit(kind=EventKind.SEARCH, result=Result.OK)
    emitter.emit(kind=EventKind.SEARCH, result=Result.OK)  # overflow → drop
    assert emitter.drop_count == 1
    assert q.qsize() == 2


def test_queue_emitter_never_raises() -> None:
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    emitter = QueueEmitter(queue=q)
    for _ in range(5):
        emitter.emit(kind=EventKind.SEARCH, result=Result.OK)
    assert emitter.drop_count == 4


def test_queue_emitter_works_without_running_loop() -> None:
    """Caller may emit from sync context — put_nowait does not require a loop."""
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    emitter = QueueEmitter(queue=q)
    emitter.emit(kind=EventKind.SEARCH, result=Result.OK)
    assert q.qsize() == 1


def test_queue_emitter_id_is_unique() -> None:
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    emitter = QueueEmitter(queue=q)
    for _ in range(5):
        emitter.emit(kind=EventKind.SEARCH, result=Result.OK)
    events = _drain(q)
    assert len({e.id for e in events}) == 5


@pytest.mark.asyncio
async def test_off_loop_emit_is_bounded_by_queue_capacity() -> None:
    """A burst of off-loop emits must not accumulate unbounded scheduled
    callbacks before any drop counter increments. The cap is the queue's
    nominal maxsize, so total in-flight memory stays O(maxsize)."""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=4)
    emitter = QueueEmitter(queue=q, loop=loop)

    burst_size = 50
    barrier = threading.Event()

    def _burst() -> None:
        barrier.wait()
        for _ in range(burst_size):
            emitter.emit(kind=EventKind.SEARCH, result=Result.OK)

    threads = [threading.Thread(target=_burst) for _ in range(2)]
    for t in threads:
        t.start()
    barrier.set()
    for t in threads:
        t.join()

    # No loop tick yet — callbacks haven't run. In-flight count (active
    # emits + scheduled callbacks) must be bounded by queue capacity (4),
    # not the burst size.
    assert emitter._inflight <= q.maxsize  # noqa: SLF001
    # Drops should reflect that the rest were rejected at submit time.
    assert emitter.drop_count >= (burst_size * 2) - q.maxsize


@pytest.mark.asyncio
async def test_full_queue_drops_before_constructing_event_or_recording_metrics() -> None:
    """A stalled worker with a full queue must drop hot-path emits at the
    capacity gate — before ActivityEvent construction or record_metrics —
    so back-pressured emit() does not record metrics for events that
    were never accepted."""
    from prometheus_client import REGISTRY

    from nexus.services.activity.metrics import SEARCH_REQUESTS

    def _sample_search(zone: str) -> float:
        for fam in REGISTRY.collect():
            for s in fam.samples:
                if s.name.startswith(SEARCH_REQUESTS._name) and s.labels.get("zone") == zone:
                    return s.value
        return 0.0

    loop = asyncio.get_running_loop()
    q: asyncio.Queue[ActivityEvent] = asyncio.Queue(maxsize=2)
    # Pre-fill the queue so the worker is "stalled" from emit's perspective.
    q.put_nowait(_event(zone="filler"))
    q.put_nowait(_event(zone="filler"))

    emitter = QueueEmitter(queue=q, loop=loop)
    metric_before = _sample_search("dropped")
    drops_before = emitter.drop_count

    emitter.emit(kind=EventKind.SEARCH, result=Result.OK, subject_zone="dropped")

    # Drop accounted, but no SEARCH_REQUESTS{zone="dropped"} counter increment.
    assert emitter.drop_count == drops_before + 1
    assert _sample_search("dropped") == metric_before


@pytest.mark.asyncio
async def test_emit_after_quiesce_is_dropped() -> None:
    """Once quiesce_pending starts, late off-loop emits must be counted as
    drops instead of being scheduled — otherwise they could land in an
    orphaned queue after shutdown closes the worker."""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    emitter = QueueEmitter(queue=q, loop=loop)

    await emitter.quiesce_pending(timeout=0.05)

    drops_before = emitter.drop_count
    # A late emit from another thread must not enqueue.
    done = threading.Event()

    def _late_emit() -> None:
        emitter.emit(kind=EventKind.SEARCH, result=Result.OK)
        done.set()

    threading.Thread(target=_late_emit).start()
    done.wait(timeout=1.0)
    # Yield once to let any (incorrectly) scheduled callback run.
    await asyncio.sleep(0.02)

    assert q.qsize() == 0, "late emit must not enqueue after quiesce"
    assert emitter.drop_count == drops_before + 1
