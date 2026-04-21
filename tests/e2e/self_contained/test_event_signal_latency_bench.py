"""Latency benchmark for event signal pipeline (Issue #3193).

Measures actual latency numbers:
1. Signal → delivery worker processes event (end-to-end)
2. Signal → SSE stream yields event
3. Throughput: events/sec under signal-driven delivery
4. Comparison: signal-driven vs fallback polling latency
"""

from __future__ import annotations

import asyncio
import statistics
import tempfile
import time
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.services.event_log.delivery import EventDeliveryWorker
from nexus.services.event_log.replay import EventReplayService
from nexus.storage.models import OperationLogModel
from nexus.storage.record_store import SQLAlchemyRecordStore


def _percentile(values: list[float], p: float) -> float:
    """Compute percentile with linear interpolation.

    The prior index-based approach treated p95 as max for 20 samples
    (index 19), which made this benchmark flaky under normal CI jitter.
    """
    if not values:
        raise ValueError("values must not be empty")
    if p <= 0:
        return min(values)
    if p >= 100:
        return max(values)

    ordered = sorted(values)
    k = (len(ordered) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (k - f) * (ordered[c] - ordered[f])


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def record_store(temp_dir: Path) -> Generator[SQLAlchemyRecordStore, None, None]:
    rs = SQLAlchemyRecordStore(db_path=temp_dir / "latency_bench.db")
    yield rs
    rs.close()


def _insert_undelivered(session_factory, path: str, seq: int | None = None) -> str:
    op_id = str(uuid.uuid4())
    with session_factory() as session:
        record = OperationLogModel(
            operation_id=op_id,
            operation_type="write",
            path=path,
            zone_id="root",
            status="success",
            delivered=False,
            created_at=datetime.now(UTC),
            sequence_number=seq,
        )
        session.add(record)
        session.commit()
    return op_id


def _insert_delivered(session_factory, path: str, seq: int) -> str:
    op_id = str(uuid.uuid4())
    with session_factory() as session:
        record = OperationLogModel(
            operation_id=op_id,
            operation_type="write",
            path=path,
            zone_id="root",
            status="success",
            delivered=True,
            created_at=datetime.now(UTC),
            sequence_number=seq,
        )
        session.add(record)
        session.commit()
    return op_id


@pytest.mark.slow
class TestDeliveryLatency:
    """Measure signal → delivery latency."""

    @pytest.mark.asyncio
    async def test_signal_to_delivery_latency(self, record_store: SQLAlchemyRecordStore) -> None:
        """Measure time from signal.set() to event marked delivered in DB."""
        signal = asyncio.Event()
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
        )
        await worker.start()

        latencies_ms: list[float] = []

        try:
            for i in range(20):
                op_id = _insert_undelivered(record_store.session_factory, f"/lat-{i}.txt")

                t0 = time.monotonic()
                signal.set()

                # Poll for delivery
                for _ in range(500):
                    with record_store.session_factory() as session:
                        row = session.get(OperationLogModel, op_id)
                        if row and row.delivered:
                            latency = (time.monotonic() - t0) * 1000
                            latencies_ms.append(latency)
                            break
                    await asyncio.sleep(0.001)

                # Reset for next iteration
                await asyncio.sleep(0.01)

            assert len(latencies_ms) == 20, f"Only {len(latencies_ms)}/20 delivered"

            p50 = statistics.median(latencies_ms)
            p95 = _percentile(latencies_ms, 95)
            p99 = _percentile(latencies_ms, 99)
            avg = statistics.mean(latencies_ms)
            mn = min(latencies_ms)
            mx = max(latencies_ms)

            print(f"\n{'=' * 60}")
            print("  SIGNAL → DELIVERY LATENCY (20 samples)")
            print(f"{'=' * 60}")
            print(f"  min:  {mn:>8.2f} ms")
            print(f"  avg:  {avg:>8.2f} ms")
            print(f"  p50:  {p50:>8.2f} ms")
            print(f"  p95:  {p95:>8.2f} ms")
            print(f"  p99:  {p99:>8.2f} ms")
            print(f"  max:  {mx:>8.2f} ms")
            print(f"{'=' * 60}")
            print("  (old polling baseline: 200ms min, up to 5000ms with backoff)")
            print()

            # Assert: signal-driven should be well under 200ms (old poll interval)
            assert p50 < 50, f"p50 latency {p50:.1f}ms too high"
            assert p95 < 100, f"p95 latency {p95:.1f}ms too high"

        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_fallback_poll_latency_comparison(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Measure fallback polling latency to show the improvement."""
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        # Fallback polling at 200ms (the old default)
        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=None,  # No signal — fallback polling
            fallback_poll_interval_s=0.2,
        )
        await worker.start()

        latencies_ms: list[float] = []

        try:
            for i in range(10):
                op_id = _insert_undelivered(record_store.session_factory, f"/poll-{i}.txt")
                t0 = time.monotonic()

                for _ in range(500):
                    with record_store.session_factory() as session:
                        row = session.get(OperationLogModel, op_id)
                        if row and row.delivered:
                            latency = (time.monotonic() - t0) * 1000
                            latencies_ms.append(latency)
                            break
                    await asyncio.sleep(0.005)

                await asyncio.sleep(0.01)

            assert len(latencies_ms) == 10, f"Only {len(latencies_ms)}/10 delivered"

            p50 = statistics.median(latencies_ms)
            avg = statistics.mean(latencies_ms)

            print(f"\n{'=' * 60}")
            print("  FALLBACK POLL LATENCY (200ms interval, 10 samples)")
            print(f"{'=' * 60}")
            print(f"  min:  {min(latencies_ms):>8.2f} ms")
            print(f"  avg:  {avg:>8.2f} ms")
            print(f"  p50:  {p50:>8.2f} ms")
            print(f"  max:  {max(latencies_ms):>8.2f} ms")
            print(f"{'=' * 60}")
            print()

        finally:
            await worker.stop()


@pytest.mark.slow
class TestStreamLatency:
    """Measure signal → SSE stream yield latency."""

    @pytest.mark.asyncio
    async def test_signal_to_stream_latency(self, record_store: SQLAlchemyRecordStore) -> None:
        """Measure time from signal.set() to stream() yielding the event."""
        signal = asyncio.Event()
        service = EventReplayService(record_store=record_store, event_signal=signal)

        latencies_ms: list[float] = []

        async def _measure_one(seq: int) -> float | None:
            received = asyncio.Event()
            received_time: list[float] = []

            async def _consume():
                async for _event in service.stream(
                    poll_interval=60.0,
                    idle_timeout=5.0,
                    since_revision=seq - 1,
                ):
                    received_time.append(time.monotonic())
                    received.set()
                    break

            task = asyncio.create_task(_consume())
            await asyncio.sleep(0.05)

            _insert_delivered(record_store.session_factory, f"/stream-lat-{seq}.txt", seq=seq)

            t0 = time.monotonic()
            signal.set()

            try:
                await asyncio.wait_for(received.wait(), timeout=3.0)
                if received_time:
                    return (received_time[0] - t0) * 1000
            except TimeoutError:
                pass
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            return None

        for i in range(20):
            latency = await _measure_one(i + 1)
            if latency is not None:
                latencies_ms.append(latency)

        assert len(latencies_ms) >= 15, f"Only {len(latencies_ms)}/20 received"

        p50 = statistics.median(latencies_ms)
        p95 = _percentile(latencies_ms, 95)
        avg = statistics.mean(latencies_ms)

        print(f"\n{'=' * 60}")
        print(f"  SIGNAL → STREAM YIELD LATENCY ({len(latencies_ms)} samples)")
        print(f"{'=' * 60}")
        print(f"  min:  {min(latencies_ms):>8.2f} ms")
        print(f"  avg:  {avg:>8.2f} ms")
        print(f"  p50:  {p50:>8.2f} ms")
        print(f"  p95:  {p95:>8.2f} ms")
        print(f"  max:  {max(latencies_ms):>8.2f} ms")
        print(f"{'=' * 60}")
        print("  (old polling baseline: 1000ms)")
        print()

        assert p50 < 50, f"p50 stream latency {p50:.1f}ms too high"


@pytest.mark.slow
class TestThroughput:
    """Measure events/sec throughput."""

    @pytest.mark.asyncio
    async def test_delivery_throughput(self, record_store: SQLAlchemyRecordStore) -> None:
        """Measure max events/sec the delivery worker can process."""
        signal = asyncio.Event()
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
            batch_size=100,
        )

        # Pre-insert 500 events
        total = 100
        for i in range(total):
            _insert_undelivered(record_store.session_factory, f"/throughput-{i}.txt")

        await worker.start()

        try:
            t0 = time.monotonic()
            signal.set()

            # Wait for all to be delivered
            deadline = asyncio.get_event_loop().time() + 30.0
            while asyncio.get_event_loop().time() < deadline:
                if worker.metrics["total_dispatched"] >= total:
                    break
                signal.set()
                await asyncio.sleep(0.05)

            elapsed = time.monotonic() - t0
            dispatched = worker.metrics["total_dispatched"]
            rate = dispatched / elapsed if elapsed > 0 else 0

            print(f"\n{'=' * 60}")
            print("  DELIVERY THROUGHPUT")
            print(f"{'=' * 60}")
            print(f"  events:     {dispatched}")
            print(f"  elapsed:    {elapsed:.2f}s")
            print(f"  throughput: {rate:.0f} events/sec")
            print(f"{'=' * 60}")
            print()

            assert dispatched == total

        finally:
            await worker.stop()
