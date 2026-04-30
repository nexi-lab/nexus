"""Unit tests for sink protocol + NoopSink + RecordingSink."""

from __future__ import annotations

import pytest

from nexus.services.activity.events import ActivityEvent, EventKind, Result
from nexus.services.activity.sinks import NoopSink, RecordingSink


def _ev(kind: EventKind = EventKind.SEARCH) -> ActivityEvent:
    return ActivityEvent(id="x", ts="t", kind=kind, result=Result.OK)


@pytest.mark.asyncio
async def test_noop_sink_accepts_writes() -> None:
    sink = NoopSink()
    await sink.write_batch([_ev(), _ev()])
    await sink.close()


@pytest.mark.asyncio
async def test_recording_sink_collects_events() -> None:
    sink = RecordingSink()
    await sink.write_batch([_ev(EventKind.SEARCH), _ev(EventKind.FETCH)])
    assert len(sink.events) == 2
    assert sink.events[0].kind is EventKind.SEARCH
    assert sink.events[1].kind is EventKind.FETCH


@pytest.mark.asyncio
async def test_recording_sink_clear() -> None:
    sink = RecordingSink()
    await sink.write_batch([_ev()])
    sink.clear()
    assert sink.events == []


@pytest.mark.asyncio
async def test_recording_sink_filter() -> None:
    sink = RecordingSink()
    await sink.write_batch([_ev(EventKind.SEARCH), _ev(EventKind.FETCH), _ev(EventKind.SEARCH)])
    matches = sink.events_of(EventKind.SEARCH)
    assert len(matches) == 2
