"""Unit tests for ActivityWorker."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from nexus.services.activity.events import ActivityEvent, EventKind, Result
from nexus.services.activity.sinks import RecordingSink
from nexus.services.activity.worker import ActivityWorker


def _ev(i: int) -> ActivityEvent:
    return ActivityEvent(id=str(i), ts=f"t{i}", kind=EventKind.SEARCH, result=Result.OK)


@pytest.mark.asyncio
async def test_drains_queue_to_sink() -> None:
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
    sink = RecordingSink()
    worker = ActivityWorker(queue=queue, sinks=[sink], batch_size=10, batch_timeout_s=0.01)
    await worker.start()
    for i in range(5):
        queue.put_nowait(_ev(i))
    await asyncio.sleep(0.1)
    await worker.stop(timeout=1.0)
    assert len(sink.events) == 5


@pytest.mark.asyncio
async def test_batches_up_to_batch_size() -> None:
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
    write_call_sizes: list[int] = []

    class _CountingSink(RecordingSink):
        async def write_batch(self, events: Sequence[ActivityEvent]) -> None:
            write_call_sizes.append(len(events))
            await super().write_batch(events)

    sink = _CountingSink()
    worker = ActivityWorker(queue=queue, sinks=[sink], batch_size=3, batch_timeout_s=1.0)
    await worker.start()
    for i in range(7):
        queue.put_nowait(_ev(i))
    await asyncio.sleep(0.1)
    await worker.stop(timeout=1.0)
    assert all(s <= 3 for s in write_call_sizes)
    assert sum(write_call_sizes) == 7


@pytest.mark.asyncio
async def test_flushes_partial_batch_on_timeout() -> None:
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
    sink = RecordingSink()
    worker = ActivityWorker(queue=queue, sinks=[sink], batch_size=100, batch_timeout_s=0.05)
    await worker.start()
    queue.put_nowait(_ev(1))
    await asyncio.sleep(0.2)
    assert len(sink.events) == 1
    await worker.stop(timeout=1.0)


@pytest.mark.asyncio
async def test_sink_error_isolated() -> None:
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()

    class _Flaky:
        calls = 0

        async def write_batch(self, events: Sequence[ActivityEvent]) -> None:
            type(self).calls += 1
            raise RuntimeError("boom")

        async def close(self) -> None:
            return None

    flaky = _Flaky()
    sink_ok = RecordingSink()
    worker = ActivityWorker(
        queue=queue, sinks=[flaky, sink_ok], batch_size=10, batch_timeout_s=0.01
    )
    await worker.start()
    for i in range(3):
        queue.put_nowait(_ev(i))
    await asyncio.sleep(0.1)
    await worker.stop(timeout=1.0)
    assert _Flaky.calls > 0
    assert len(sink_ok.events) == 3


@pytest.mark.asyncio
async def test_stop_drains_remaining_queue() -> None:
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
    sink = RecordingSink()
    worker = ActivityWorker(queue=queue, sinks=[sink], batch_size=2, batch_timeout_s=10.0)
    await worker.start()
    for i in range(5):
        queue.put_nowait(_ev(i))
    await worker.stop(timeout=1.0)
    assert len(sink.events) == 5
