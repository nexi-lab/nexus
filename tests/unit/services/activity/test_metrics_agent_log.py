"""Unit tests for agent_log metrics."""

from __future__ import annotations

import pytest

from nexus.contracts.protocols.activity import EventKind, Result
from nexus.services.activity import metrics
from nexus.services.activity.agent_log_store import MemoryBackend
from nexus.services.activity.events import ActivityEvent, Actor
from nexus.services.activity.sinks.jsonl import JsonlActivitySink


def test_agent_log_lines_dropped_counter_present():
    assert metrics.AGENT_LOG_LINES_DROPPED is not None
    assert "reason" in metrics.AGENT_LOG_LINES_DROPPED._labelnames


def test_agent_log_bytes_gauge_present():
    assert metrics.AGENT_LOG_BYTES is not None
    assert "agent_id" in metrics.AGENT_LOG_BYTES._labelnames


def _evt(*, kind, agent="alice", ts="2026-05-09T12:00:00.000Z", meta=None):
    return ActivityEvent(
        id="e1",
        ts=ts,
        kind=kind,
        result=Result.OK,
        latency_ms=10,
        actor=Actor(agent=agent),
        meta=meta or {},
    )


def _counter_value(reason: str) -> float:
    return metrics.AGENT_LOG_LINES_DROPPED.labels(reason=reason)._value.get()


def _gauge_value(agent_id: str) -> float:
    return metrics.AGENT_LOG_BYTES.labels(agent_id=agent_id)._value.get()


@pytest.mark.asyncio
async def test_no_agent_drop_increments_counter():
    before = _counter_value("no_agent")
    store = MemoryBackend(cap_bytes=1024)
    sink = JsonlActivitySink(store=store)
    evt = _evt(kind=EventKind.OP, agent=None, meta={"op": "read", "path": "/x"})
    await sink.write_batch([evt])
    assert _counter_value("no_agent") == before + 1


@pytest.mark.asyncio
async def test_recursion_drop_increments_counter():
    before = _counter_value("recursion")
    store = MemoryBackend(cap_bytes=1024)
    sink = JsonlActivitySink(store=store)
    evt = _evt(kind=EventKind.OP, meta={"op": "read", "path": "/.activity/foo", "bytes": 0})
    await sink.write_batch([evt])
    assert _counter_value("recursion") == before + 1


def test_ring_evict_increments_counter_and_updates_bytes_gauge():
    before_evict = _counter_value("ring_evict")
    store = MemoryBackend(cap_bytes=8)
    store.append_line("alice", "2026-05-09", b"line1\n")
    store.append_line("alice", "2026-05-09", b"line2\n")  # triggers eviction
    assert _counter_value("ring_evict") == before_evict + 1
    # Bytes gauge reflects current size (one 6-byte line after eviction).
    assert _gauge_value("alice") == 6


def test_drop_date_zeroes_bytes_gauge():
    store = MemoryBackend(cap_bytes=1024)
    store.append_line("dropme", "2026-05-09", b"x\n")
    assert _gauge_value("dropme") == 2
    store.drop_date("2026-05-09")
    assert _gauge_value("dropme") == 0
