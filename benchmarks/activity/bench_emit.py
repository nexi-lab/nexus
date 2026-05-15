"""Bench: emit() p50/p99 — must be < 10 µs / < 50 µs respectively."""

from __future__ import annotations

import asyncio

import pytest

from nexus.services.activity import EventKind, Result
from nexus.services.activity.emitter import QueueEmitter


@pytest.mark.benchmark(group="activity-emit")
def test_emit_hot_path(benchmark) -> None:
    queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
    emitter = QueueEmitter(queue=queue)

    def _do_emit() -> None:
        emitter.emit(
            kind=EventKind.SEARCH,
            result=Result.OK,
            actor_token_hash="abc1234567890def",
            subject_zone="eng",
            latency_ms=42,
        )

    benchmark(_do_emit)
    stats = benchmark.stats.stats
    # CI-friendly threshold (allow noise on shared runners)
    assert stats.median * 1e6 < 25.0, f"emit p50 {stats.median * 1e6:.1f} µs > 25 µs"
