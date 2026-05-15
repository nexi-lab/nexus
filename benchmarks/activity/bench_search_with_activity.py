"""Bench: confirm activity wrapper does not add measurable overhead.

Synthetic — invokes the emit + timing wrapper around a no-op coroutine
to isolate activity overhead from real search work.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from nexus.services.activity import EventKind, Result, set_emitter
from nexus.services.activity.emitter import NoopEmitter, QueueEmitter
from nexus.services.activity.events import ActivityEvent


async def _wrapped_search(emit_fn) -> int:
    start = time.monotonic()
    result = 1
    emit_fn(kind=EventKind.SEARCH, result=Result.OK, latency_ms=int((time.monotonic() - start) * 1000))
    return result


@pytest.mark.benchmark(group="activity-search-overhead")
def test_search_with_noop_emitter(benchmark) -> None:
    set_emitter(NoopEmitter())
    from nexus.services.activity import emit

    def _run() -> None:
        asyncio.run(_wrapped_search(emit))

    benchmark(_run)


@pytest.mark.benchmark(group="activity-search-overhead")
def test_search_with_queue_emitter(benchmark) -> None:
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue(maxsize=10_000)
    set_emitter(QueueEmitter(queue=queue))
    from nexus.services.activity import emit

    def _run() -> None:
        asyncio.run(_wrapped_search(emit))

    benchmark(_run)
