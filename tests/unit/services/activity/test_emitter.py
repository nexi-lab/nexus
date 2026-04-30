"""Unit tests for the Emitter singleton and NoopEmitter."""

from __future__ import annotations

import pytest

from nexus.services.activity import EventKind, Result, emit, get_emitter, set_emitter
from nexus.services.activity.emitter import NoopEmitter


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
