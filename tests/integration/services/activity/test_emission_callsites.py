"""Integration: each emission pattern produces the expected event in the sink.

This test verifies the emit() -> queue -> worker -> sink contract that all
five callsites (SearchService.grep, FederatedSearch.search, RebacChecker
deny path, ApprovalService request_and_wait/decide, MCPAuditLogMiddleware)
rely on. The instrumented services have heavy real dependencies; their
own unit/integration suites cover the wiring of these emit() calls.
Here we lock the downstream contract.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus.services.activity import EventKind, Result, set_emitter
from nexus.services.activity.emitter import QueueEmitter
from nexus.services.activity.events import ActivityEvent
from nexus.services.activity.sinks.recording import RecordingSink
from nexus.services.activity.worker import ActivityWorker


@pytest.fixture
async def recording():
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue(maxsize=1024)
    sink = RecordingSink()
    worker = ActivityWorker(queue=queue, sinks=[sink], batch_size=10, batch_timeout_s=0.01)
    await worker.start()
    set_emitter(QueueEmitter(queue=queue))
    try:
        yield sink
    finally:
        await worker.stop(timeout=2.0)


@pytest.mark.asyncio
async def test_search_emit_recorded(recording: RecordingSink) -> None:
    from nexus.services.activity import emit

    emit(
        kind=EventKind.SEARCH,
        result=Result.OK,
        actor_token_hash="tok",
        subject_zone="eng",
        latency_ms=12,
    )
    await asyncio.sleep(0.1)
    matches = recording.events_of(EventKind.SEARCH)
    assert len(matches) == 1
    assert matches[0].subject.zone == "eng"
    assert matches[0].latency_ms == 12


@pytest.mark.asyncio
async def test_mcp_tool_call_emit_recorded(recording: RecordingSink) -> None:
    from nexus.services.activity import emit

    emit(
        kind=EventKind.MCP_TOOL_CALL,
        result=Result.OK,
        actor_token_hash="tok",
        subject_extra={"tool": "search", "rpc_method": "tools/call"},
        latency_ms=8,
    )
    await asyncio.sleep(0.1)
    matches = recording.events_of(EventKind.MCP_TOOL_CALL)
    assert len(matches) == 1


@pytest.mark.asyncio
async def test_zone_access_block_emit_recorded(recording: RecordingSink) -> None:
    from nexus.services.activity import emit

    emit(
        kind=EventKind.ZONE_ACCESS,
        result=Result.BLOCKED,
        actor_user="alice",
        subject_zone="legal",
        subject_extra={"reason": "no_scope"},
    )
    await asyncio.sleep(0.1)
    matches = recording.events_of(EventKind.ZONE_ACCESS)
    assert len(matches) == 1
    assert matches[0].result is Result.BLOCKED


@pytest.mark.asyncio
async def test_approval_pending_then_decided_emit_recorded(recording: RecordingSink) -> None:
    from nexus.services.activity import emit

    emit(
        kind=EventKind.APPROVAL,
        result=Result.PENDING_APPROVAL,
        subject_zone="eng",
        subject_extra={"request_id": "r1"},
    )
    emit(
        kind=EventKind.APPROVAL,
        result=Result.OK,
        subject_zone="eng",
        subject_extra={"request_id": "r1", "decision": "approved"},
    )
    await asyncio.sleep(0.1)
    matches = recording.events_of(EventKind.APPROVAL)
    assert len(matches) == 2
    assert matches[0].result is Result.PENDING_APPROVAL
    assert matches[1].result is Result.OK
