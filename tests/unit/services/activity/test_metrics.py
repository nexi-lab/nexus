"""Unit tests for the activity metrics catalog."""

from __future__ import annotations

import asyncio

import pytest
from prometheus_client import REGISTRY

from nexus.services.activity import EventKind, Result
from nexus.services.activity.emitter import QueueEmitter
from nexus.services.activity.metrics import (
    ACTIVITY_DROPS,
    APPROVALS_PENDING,
    MCP_TOOL_CALLS,
    POLICY_BLOCKS,
    SEARCH_LATENCY,
    SEARCH_REQUESTS,
    record_metrics,
)


def _sample(metric, **labels) -> float:
    """Return the current value of a Prom metric for the given label set."""
    for fam in REGISTRY.collect():
        for s in fam.samples:
            if s.name.startswith(metric._name) and all(
                s.labels.get(k) == v for k, v in labels.items()
            ):
                return s.value
    return 0.0


def test_search_request_increments_counter() -> None:
    before = _sample(SEARCH_REQUESTS, zone="eng", status="ok")
    SEARCH_REQUESTS.labels(zone="eng", status="ok").inc()
    after = _sample(SEARCH_REQUESTS, zone="eng", status="ok")
    assert after == before + 1


def test_search_latency_observed() -> None:
    SEARCH_LATENCY.labels(zone="eng").observe(0.05)


def test_mcp_tool_calls_counter_present() -> None:
    MCP_TOOL_CALLS.labels(tool="search", status="ok").inc()


def test_policy_blocks_counter_present() -> None:
    POLICY_BLOCKS.labels(kind="zone_access").inc()


def test_approvals_pending_gauge_inc_dec_balanced() -> None:
    APPROVALS_PENDING.inc()
    APPROVALS_PENDING.dec()


def test_activity_drops_counter_present() -> None:
    ACTIVITY_DROPS.inc()


@pytest.mark.asyncio
async def test_queue_emitter_records_metrics() -> None:
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    emitter = QueueEmitter(queue=q)
    before_search = _sample(SEARCH_REQUESTS, zone="eng", status="ok")
    emitter.emit(
        kind=EventKind.SEARCH,
        result=Result.OK,
        actor_token_hash="abc",
        subject_zone="eng",
        latency_ms=42,
    )
    after_search = _sample(SEARCH_REQUESTS, zone="eng", status="ok")
    assert after_search == before_search + 1


@pytest.mark.asyncio
async def test_queue_emitter_drops_increment_drop_metric() -> None:
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    emitter = QueueEmitter(queue=q)
    before = _sample(ACTIVITY_DROPS)
    emitter.emit(kind=EventKind.SEARCH, result=Result.OK)
    emitter.emit(kind=EventKind.SEARCH, result=Result.OK)  # overflow → drop
    after = _sample(ACTIVITY_DROPS)
    assert after == before + 1


def test_record_metrics_dispatches_search() -> None:
    before = _sample(SEARCH_REQUESTS, zone="d-eng", status="ok")
    record_metrics(
        kind=EventKind.SEARCH,
        result=Result.OK,
        actor_token_hash="d-tok",
        subject_zone="d-eng",
        subject_extra=None,
        latency_ms=10,
    )
    after = _sample(SEARCH_REQUESTS, zone="d-eng", status="ok")
    assert after == before + 1


def test_record_metrics_dispatches_mcp_tool_call() -> None:
    before = _sample(MCP_TOOL_CALLS, tool="d-tool", status="ok")
    record_metrics(
        kind=EventKind.MCP_TOOL_CALL,
        result=Result.OK,
        actor_token_hash=None,
        subject_zone=None,
        subject_extra={"tool": "d-tool"},
        latency_ms=None,
    )
    after = _sample(MCP_TOOL_CALLS, tool="d-tool", status="ok")
    assert after == before + 1


def test_record_metrics_dispatches_zone_access_blocked() -> None:
    before = _sample(POLICY_BLOCKS, kind="zone_access")
    record_metrics(
        kind=EventKind.ZONE_ACCESS,
        result=Result.BLOCKED,
        actor_token_hash=None,
        subject_zone="d-eng",
        subject_extra=None,
        latency_ms=None,
    )
    after = _sample(POLICY_BLOCKS, kind="zone_access")
    assert after == before + 1


def test_record_metrics_dispatches_policy_block_blocked() -> None:
    before = _sample(POLICY_BLOCKS, kind="policy_block")
    record_metrics(
        kind=EventKind.POLICY_BLOCK,
        result=Result.BLOCKED,
        actor_token_hash=None,
        subject_zone=None,
        subject_extra=None,
        latency_ms=None,
    )
    after = _sample(POLICY_BLOCKS, kind="policy_block")
    assert after == before + 1


def test_record_metrics_zone_access_ok_does_not_increment_blocks() -> None:
    """ZONE_ACCESS with result=OK must NOT increment POLICY_BLOCKS."""
    before = _sample(POLICY_BLOCKS, kind="zone_access")
    record_metrics(
        kind=EventKind.ZONE_ACCESS,
        result=Result.OK,
        actor_token_hash=None,
        subject_zone="d-eng",
        subject_extra=None,
        latency_ms=None,
    )
    after = _sample(POLICY_BLOCKS, kind="zone_access")
    assert after == before


def test_record_metrics_does_not_mutate_approvals_pending() -> None:
    """APPROVALS_PENDING is reseeded from durable state by ApprovalService at
    every transition (and at startup); record_metrics must not touch it.
    Otherwise per-process delta accounting drifts on cross-worker decisions.
    """
    before = _sample(APPROVALS_PENDING)
    for result in (Result.PENDING_APPROVAL, Result.OK, Result.BLOCKED):
        record_metrics(
            kind=EventKind.APPROVAL,
            result=result,
            actor_token_hash=None,
            subject_zone=None,
            subject_extra=None,
            latency_ms=None,
        )
    after = _sample(APPROVALS_PENDING)
    assert after == before


def test_reseed_approvals_pending_sets_gauge_authoritatively() -> None:
    """The contracts-layer reseed function must overwrite the gauge with the
    durable count, not increment/decrement."""
    from nexus.contracts.protocols.activity import reseed_approvals_pending

    reseed_approvals_pending(0)
    assert _sample(APPROVALS_PENDING) == 0
    reseed_approvals_pending(7)
    assert _sample(APPROVALS_PENDING) == 7
    reseed_approvals_pending(3)
    assert _sample(APPROVALS_PENDING) == 3
    reseed_approvals_pending(0)


def test_record_metrics_fetch_is_noop() -> None:
    """FETCH currently has no metric — record_metrics must not raise."""
    record_metrics(
        kind=EventKind.FETCH,
        result=Result.OK,
        actor_token_hash=None,
        subject_zone=None,
        subject_extra=None,
        latency_ms=None,
    )
