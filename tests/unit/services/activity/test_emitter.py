"""Unit tests for the Emitter singleton and NoopEmitter."""

from __future__ import annotations

import asyncio

import pytest

from nexus.services.activity import EventKind, Result, emit, get_emitter, set_emitter
from nexus.services.activity.emitter import NoopEmitter, QueueEmitter


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
