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


@pytest.mark.asyncio
async def test_worker_unexpected_death_surfaces_via_metrics() -> None:
    """If the consumer task dies without stop() being called, the
    component health flag must drop and ACTIVITY_SINK_ERRORS must
    increment so /metrics carries an alertable signal — otherwise
    activity persistence can be dead while the registry reports OK."""
    from prometheus_client import REGISTRY

    from nexus.services.activity.metrics import ACTIVITY_SINK_ERRORS

    def _sample_worker_errors() -> float:
        for fam in REGISTRY.collect():
            for s in fam.samples:
                if (
                    s.name.startswith(ACTIVITY_SINK_ERRORS._name)
                    and s.labels.get("sink") == "ActivityWorker"
                ):
                    return s.value
        return 0.0

    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()

    class _Crasher:
        async def write_batch(self, events: Sequence[ActivityEvent]) -> None:
            raise BaseException("worker-killing failure")

        async def close(self) -> None:
            return None

    worker = ActivityWorker(queue=queue, sinks=[_Crasher()], batch_size=1, batch_timeout_s=0.01)
    before = _sample_worker_errors()
    await worker.start()
    queue.put_nowait(_ev(0))
    # Wait for the BaseException to take down the consumer task.
    for _ in range(100):
        if worker._task is not None and worker._task.done():  # noqa: SLF001
            break
        await asyncio.sleep(0.01)

    assert not worker.is_healthy()
    assert _sample_worker_errors() == before + 1


@pytest.mark.asyncio
async def test_stop_waits_for_inflight_write_instead_of_cancelling() -> None:
    """A wedged write must not be cancelled — cancellation cannot stop a
    running executor thread, so close() would race the same connection.
    Instead stop() must wait for the in-flight write to finish naturally,
    then close the sink only after the write completes.
    """
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
    write_started = asyncio.Event()
    release_write = asyncio.Event()
    close_observations: list[bool] = []

    class _SlowSink:
        def __init__(self) -> None:
            self.write_done = False

        async def write_batch(self, events: Sequence[ActivityEvent]) -> None:
            write_started.set()
            await release_write.wait()
            self.write_done = True

        async def close(self) -> None:
            close_observations.append(self.write_done)

    sink = _SlowSink()
    queue.put_nowait(_ev(0))
    worker = ActivityWorker(queue=queue, sinks=[sink], batch_size=10, batch_timeout_s=0.01)
    await worker.start()
    await write_started.wait()

    stop_task = asyncio.create_task(worker.stop(timeout=0.05))
    await asyncio.sleep(0.1)  # let soft timeout elapse
    assert not stop_task.done(), "stop() must wait for the in-flight write"
    release_write.set()
    await stop_task

    # close() must have observed the completed write — never the in-flight state.
    assert close_observations == [True]


@pytest.mark.asyncio
async def test_double_stop_is_safe() -> None:
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
    sink = RecordingSink()
    worker = ActivityWorker(queue=queue, sinks=[sink], batch_size=10, batch_timeout_s=0.01)
    await worker.start()
    queue.put_nowait(_ev(0))
    await worker.stop(timeout=1.0)
    await worker.stop(timeout=1.0)  # second stop must not raise
    assert len(sink.events) == 1


@pytest.mark.asyncio
async def test_start_after_stop_resumes() -> None:
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
    sink = RecordingSink()
    worker = ActivityWorker(queue=queue, sinks=[sink], batch_size=10, batch_timeout_s=0.01)
    await worker.start()
    queue.put_nowait(_ev(1))
    await asyncio.sleep(0.1)
    await worker.stop(timeout=1.0)
    # Restart and verify it actually consumes again
    await worker.start()
    queue.put_nowait(_ev(2))
    await asyncio.sleep(0.1)
    await worker.stop(timeout=1.0)
    assert len(sink.events) == 2


@pytest.mark.asyncio
async def test_stop_before_start_is_safe() -> None:
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
    sink = RecordingSink()
    worker = ActivityWorker(queue=queue, sinks=[sink], batch_size=10, batch_timeout_s=0.01)
    # stop() before start() must be a no-op, not raise
    await worker.stop(timeout=1.0)


@pytest.mark.asyncio
async def test_double_start_is_idempotent() -> None:
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
    sink = RecordingSink()
    worker = ActivityWorker(queue=queue, sinks=[sink], batch_size=10, batch_timeout_s=0.01)
    await worker.start()
    await worker.start()  # second start must not create a duplicate task
    queue.put_nowait(_ev(0))
    await asyncio.sleep(0.1)
    await worker.stop(timeout=1.0)
    assert len(sink.events) == 1
