"""Smoke test for issue #4081: drive 1k mixed events through the sink and
verify (a) no infinite loop, (b) bytes stay under cap, (c) the recursion
guard counter increments for self-observation events.

Phase B (FS + ReBAC integration) is deferred. This test exercises everything
downstream of dispatch: the sink, store, ring buffer, and metrics counters.
"""

import json

import pytest

from nexus.contracts.protocols.activity import EventKind, Result
from nexus.services.activity.agent_log_store import MemoryBackend
from nexus.services.activity.events import ActivityEvent, Actor
from nexus.services.activity.sinks.jsonl import JsonlActivitySink


def _evt(
    *, agent: str, op: str, path: str, bytes_count: int = 100, ts: str = "2026-05-09T12:00:00.000Z"
) -> ActivityEvent:
    return ActivityEvent(
        id=f"e-{agent}-{op}-{path}-{bytes_count}",
        ts=ts,
        kind=EventKind.OP,
        result=Result.OK,
        latency_ms=10,
        actor=Actor(agent=agent),
        meta={"op": op, "path": path, "bytes": bytes_count},
    )


@pytest.mark.asyncio
async def test_thousand_mixed_events_no_runaway_no_recursion():
    """Drive 1k events with a small cap. Half attempt to write to /.activity/
    paths (the recursion-trap class). Verify:
    - bytes stay <= cap (ring eviction works under load)
    - recursion_skipped counter == number of /.activity/ events
    - sink does not raise or hang
    """
    cap = 64 * 1024  # 64 KB
    store = MemoryBackend(cap_bytes=cap)
    sink = JsonlActivitySink(store=store)

    # Build 1000 events: 500 normal, 500 attempted self-observations.
    events = []
    for i in range(500):
        events.append(_evt(agent="alice", op="read", path=f"/local/file{i}.txt"))
        events.append(_evt(agent="alice", op="write", path="/.activity/2026-05-09/alice.jsonl"))

    # Single batch — exercises the per-event try/except and the loop hot path.
    await sink.write_batch(events)

    raw = store.read_path("/.activity/2026-05-09/alice.jsonl")

    # 1. No runaway: byte count is bounded by cap (allow some slack for last line).
    assert len(raw) <= cap + 1024, f"buffer exceeded cap: len={len(raw)}"

    # 2. Recursion guard fired for every /.activity/ attempt.
    assert sink.recursion_skipped == 500, sink.recursion_skipped

    # 3. Lines that did land are well-formed JSONL referencing only normal paths.
    lines = [json.loads(line) for line in raw.strip().split(b"\n") if line]
    assert lines, "no lines landed despite 500 valid events"
    for rec in lines:
        assert rec["kind"] == "op"
        assert not rec["path"].startswith("/.activity/"), rec
        assert rec["op"] in {"read", "write"}


@pytest.mark.asyncio
async def test_recursion_guard_holds_under_pure_self_observation_storm():
    """If every event is a self-observation, the store ends up empty and
    the counter equals the input count. Pathological case: confirms no line
    ever leaks past the guard."""
    store = MemoryBackend(cap_bytes=64 * 1024)
    sink = JsonlActivitySink(store=store)

    events = [
        _evt(agent="alice", op="read", path="/.activity/2026-05-09/alice.jsonl", bytes_count=i)
        for i in range(200)
    ]
    await sink.write_batch(events)

    assert store.read_path("/.activity/2026-05-09/alice.jsonl") == b""
    assert sink.recursion_skipped == 200
