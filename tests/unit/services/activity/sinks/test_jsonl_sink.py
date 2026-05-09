import json

import pytest

from nexus.contracts.protocols.activity import EventKind, Result
from nexus.services.activity.agent_log_store import MemoryBackend
from nexus.services.activity.events import ActivityEvent, Actor
from nexus.services.activity.sinks.jsonl import JsonlActivitySink


def _evt(
    *,
    kind,
    agent="alice",
    ts="2026-05-09T12:00:00.000Z",
    meta=None,
    result=Result.OK,
    latency_ms=10,
):
    return ActivityEvent(
        id="e1",
        ts=ts,
        kind=kind,
        result=result,
        latency_ms=latency_ms,
        actor=Actor(agent=agent),
        meta=meta or {},
    )


@pytest.mark.asyncio
async def test_op_event_writes_line():
    store = MemoryBackend(cap_bytes=1024)
    sink = JsonlActivitySink(store=store)
    evt = _evt(kind=EventKind.OP, meta={"op": "read", "path": "/s3/foo.txt", "bytes": 1234})
    await sink.write_batch([evt])
    raw = store.read_path("/.activity/2026-05-09/alice.jsonl")
    assert raw
    rec = json.loads(raw.strip())
    assert rec == {
        "ts": "2026-05-09T12:00:00.000Z",
        "kind": "op",
        "op": "read",
        "path": "/s3/foo.txt",
        "bytes": 1234,
        "ms": 10,
    }


@pytest.mark.asyncio
async def test_exec_event_writes_line():
    store = MemoryBackend(cap_bytes=1024)
    sink = JsonlActivitySink(store=store)
    evt = _evt(kind=EventKind.EXEC, meta={"cmd": "grep x /a", "exit_code": 0})
    await sink.write_batch([evt])
    raw = store.read_path("/.activity/2026-05-09/alice.jsonl")
    rec = json.loads(raw.strip())
    assert rec == {
        "ts": "2026-05-09T12:00:00.000Z",
        "kind": "exec",
        "cmd": "grep x /a",
        "exit_code": 0,
        "ms": 10,
    }


@pytest.mark.asyncio
async def test_recursion_guard_drops_op_under_activity_prefix():
    store = MemoryBackend(cap_bytes=1024)
    sink = JsonlActivitySink(store=store)
    evt = _evt(
        kind=EventKind.OP,
        meta={"op": "read", "path": "/.activity/2026-05-09/alice.jsonl", "bytes": 0},
    )
    await sink.write_batch([evt])
    assert store.read_path("/.activity/2026-05-09/alice.jsonl") == b""


@pytest.mark.asyncio
async def test_no_agent_drops_event():
    store = MemoryBackend(cap_bytes=1024)
    sink = JsonlActivitySink(store=store)
    evt = _evt(kind=EventKind.OP, agent=None, meta={"op": "read", "path": "/x"})
    await sink.write_batch([evt])
    assert list(store.list_dir("/.activity/")) == []


@pytest.mark.asyncio
async def test_non_op_non_exec_kinds_skipped():
    store = MemoryBackend(cap_bytes=1024)
    sink = JsonlActivitySink(store=store)
    evt = _evt(kind=EventKind.SEARCH)
    await sink.write_batch([evt])
    assert list(store.list_dir("/.activity/")) == []


@pytest.mark.asyncio
async def test_cmd_truncation_marker():
    store = MemoryBackend(cap_bytes=64 * 1024)
    sink = JsonlActivitySink(store=store, cmd_max_bytes=8)
    evt = _evt(kind=EventKind.EXEC, meta={"cmd": "0123456789ABCDEF", "exit_code": 0})
    await sink.write_batch([evt])
    rec = json.loads(store.read_path("/.activity/2026-05-09/alice.jsonl").strip())
    assert rec["cmd_truncated"] is True
    assert rec["cmd"].endswith("…")
    assert len(rec["cmd"].encode("utf-8")) <= 8 + len("…".encode())


@pytest.mark.asyncio
async def test_close_is_noop():
    store = MemoryBackend(cap_bytes=1024)
    sink = JsonlActivitySink(store=store)
    await sink.close()  # must not raise
