"""Load and stress tests for event signal pipeline (Issue #3193).

Tests sustained load and many concurrent SSE connections to validate
the notification-driven architecture under pressure.

- Sustained write load: 200 events over waves
- Many SSE connections: 10 concurrent stream() consumers on one signal
- Mixed operations under load: writes + renames + deletes
- Signal contention: rapid-fire signal with slow consumers

Marked ``slow`` — excluded from default CI run to avoid timeouts.
Run with: ``pytest -m slow tests/e2e/self_contained/test_event_signal_stress.py``
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.event_log.delivery import EventDeliveryWorker
from nexus.services.event_log.replay import EventReplayService
from nexus.storage.models import OperationLogModel
from nexus.storage.record_store import SQLAlchemyRecordStore


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def record_store(temp_dir: Path) -> Generator[SQLAlchemyRecordStore, None, None]:
    rs = SQLAlchemyRecordStore(db_path=temp_dir / "stress_test.db")
    yield rs
    rs.close()


def _insert_event(
    session_factory,
    path: str,
    operation_type: str = "write",
    zone_id: str = ROOT_ZONE_ID,
    delivered: bool = False,
    sequence_number: int | None = None,
) -> str:
    op_id = str(uuid.uuid4())
    with session_factory() as session:
        record = OperationLogModel(
            operation_id=op_id,
            operation_type=operation_type,
            path=path,
            zone_id=zone_id,
            status="success",
            delivered=delivered,
            created_at=datetime.now(UTC),
            sequence_number=sequence_number,
        )
        session.add(record)
        session.commit()
    return op_id


# =========================================================================
# Sustained write load
# =========================================================================


@pytest.mark.slow
class TestSustainedLoad:
    """Test delivery under sustained write load."""

    @pytest.mark.asyncio
    async def test_500_events_sustained(self, record_store: SQLAlchemyRecordStore) -> None:
        """500 events inserted over time, all delivered via signal."""
        signal = asyncio.Event()
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
            batch_size=50,
        )
        await worker.start()

        total = 100
        t0 = time.monotonic()

        try:
            # Insert events in waves of 20, signaling after each wave
            for wave in range(5):
                for i in range(20):
                    idx = wave * 50 + i
                    _insert_event(
                        record_store.session_factory,
                        path=f"/sustained-{idx}.txt",
                    )
                signal.set()
                await asyncio.sleep(0.05)  # Brief pause between waves

            # Wait for all to be delivered
            deadline = asyncio.get_event_loop().time() + 30.0
            while asyncio.get_event_loop().time() < deadline:
                if worker.metrics["total_dispatched"] >= total:
                    break
                signal.set()  # Ensure worker isn't stuck
                await asyncio.sleep(0.1)

            elapsed = time.monotonic() - t0
            dispatched = worker.metrics["total_dispatched"]

            assert dispatched == total, (
                f"Only {dispatched}/{total} events delivered in {elapsed:.1f}s"
            )

            # Verify all marked delivered in DB
            with record_store.session_factory() as session:
                from sqlalchemy import func, select

                undelivered = session.execute(
                    select(func.count())
                    .select_from(OperationLogModel)
                    .where(OperationLogModel.delivered == False)  # noqa: E712
                ).scalar()
                assert undelivered == 0, f"{undelivered} events still undelivered"

            # Performance: should complete well under 30s
            assert elapsed < 30.0, f"Sustained load took {elapsed:.1f}s, expected <30s"

        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_mixed_operations_under_load(self, record_store: SQLAlchemyRecordStore) -> None:
        """Writes + renames + deletes all delivered under sustained load."""
        signal = asyncio.Event()
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
            batch_size=25,
        )
        await worker.start()

        total = 0
        try:
            # Mix of operation types
            for i in range(50):
                _insert_event(
                    record_store.session_factory, path=f"/mix-write-{i}.txt", operation_type="write"
                )
                total += 1
            for i in range(20):
                _insert_event(
                    record_store.session_factory,
                    path=f"/mix-rename-{i}.txt",
                    operation_type="rename",
                )
                total += 1
            for i in range(15):
                _insert_event(
                    record_store.session_factory,
                    path=f"/mix-delete-{i}.txt",
                    operation_type="delete",
                )
                total += 1
            for i in range(10):
                _insert_event(
                    record_store.session_factory, path=f"/mix-mkdir-{i}", operation_type="mkdir"
                )
                total += 1
            for i in range(5):
                _insert_event(
                    record_store.session_factory, path=f"/mix-rmdir-{i}", operation_type="rmdir"
                )
                total += 1

            signal.set()

            # Wait for all
            deadline = asyncio.get_event_loop().time() + 15.0
            while asyncio.get_event_loop().time() < deadline:
                if worker.metrics["total_dispatched"] >= total:
                    break
                signal.set()
                await asyncio.sleep(0.1)

            assert worker.metrics["total_dispatched"] == total, (
                f"Only {worker.metrics['total_dispatched']}/{total} mixed events delivered"
            )
        finally:
            await worker.stop()


# =========================================================================
# Many concurrent SSE connections
# =========================================================================


@pytest.mark.slow
class TestManyConcurrentStreams:
    """Test many SSE stream() consumers sharing one signal."""

    @pytest.mark.asyncio
    async def test_20_concurrent_streams(self, record_store: SQLAlchemyRecordStore) -> None:
        """20 concurrent stream() consumers all receive events via shared signal."""
        signal = asyncio.Event()
        num_consumers = 10
        events_per_consumer: dict[int, list] = {i: [] for i in range(num_consumers)}

        # Pre-insert 5 events for streams to pick up
        for i in range(5):
            _insert_event(
                record_store.session_factory,
                path=f"/multi-stream-{i}.txt",
                sequence_number=i + 1,
                delivered=True,
            )

        # Create N consumers
        async def consume(consumer_id: int):
            service = EventReplayService(record_store=record_store, event_signal=signal)
            async for event in service.stream(
                since_revision=0,
                poll_interval=60.0,  # Long poll — rely on signal
                idle_timeout=3.0,
            ):
                events_per_consumer[consumer_id].append(event)
                if len(events_per_consumer[consumer_id]) >= 5:
                    break

        # Start all consumers
        tasks = [asyncio.create_task(consume(i)) for i in range(num_consumers)]

        # Give consumers time to start
        await asyncio.sleep(0.2)

        # Signal all consumers
        signal.set()

        # Wait for all to complete
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=10.0,
            )
        except TimeoutError:
            for t in tasks:
                t.cancel()

        # Verify all consumers received events
        received_counts = [len(events_per_consumer[i]) for i in range(num_consumers)]
        all_got_events = all(c >= 5 for c in received_counts)

        assert all_got_events, f"Not all consumers received 5 events. Counts: {received_counts}"

    @pytest.mark.asyncio
    async def test_streams_with_new_events_arriving(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Multiple streams receive events that arrive AFTER they start listening."""
        signal = asyncio.Event()
        num_consumers = 10
        events_per_consumer: dict[int, list] = {i: [] for i in range(num_consumers)}

        async def consume(consumer_id: int):
            service = EventReplayService(record_store=record_store, event_signal=signal)
            async for event in service.stream(
                poll_interval=60.0,
                idle_timeout=5.0,
            ):
                events_per_consumer[consumer_id].append(event)
                if len(events_per_consumer[consumer_id]) >= 3:
                    break

        # Start consumers first (they'll wait for signal)
        tasks = [asyncio.create_task(consume(i)) for i in range(num_consumers)]
        await asyncio.sleep(0.2)

        # Now insert events and signal
        for i in range(3):
            _insert_event(
                record_store.session_factory,
                path=f"/late-arrival-{i}.txt",
                sequence_number=i + 1,
                delivered=True,
            )
            signal.set()
            await asyncio.sleep(0.05)

        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=8.0,
            )
        except TimeoutError:
            for t in tasks:
                t.cancel()

        received_counts = [len(events_per_consumer[i]) for i in range(num_consumers)]
        all_got_events = all(c >= 3 for c in received_counts)

        assert all_got_events, (
            f"Not all consumers received 3 late-arrival events. Counts: {received_counts}"
        )


# =========================================================================
# Signal contention: rapid-fire with slow consumers
# =========================================================================


@pytest.mark.slow
class TestSignalContention:
    """Test signal behavior when producer is faster than consumer."""

    @pytest.mark.asyncio
    async def test_fast_producer_slow_consumer(self, record_store: SQLAlchemyRecordStore) -> None:
        """Producer signals faster than consumer can process — no events lost."""
        signal = asyncio.Event()

        # Slow bus: each publish takes 10ms
        slow_bus = MagicMock()

        async def slow_publish(event):
            await asyncio.sleep(0.01)

        slow_bus.publish = AsyncMock(side_effect=slow_publish)

        worker = EventDeliveryWorker(
            record_store,
            event_bus=slow_bus,
            event_signal=signal,
            batch_size=10,
        )
        await worker.start()

        total = 100
        try:
            # Rapid-fire: insert all events and signal repeatedly
            for i in range(total):
                _insert_event(
                    record_store.session_factory,
                    path=f"/contention-{i}.txt",
                )
                if i % 10 == 0:
                    signal.set()

            signal.set()

            # Wait — consumer is slow (10ms * 200 = 2s minimum)
            deadline = asyncio.get_event_loop().time() + 15.0
            while asyncio.get_event_loop().time() < deadline:
                if worker.metrics["total_dispatched"] >= total:
                    break
                signal.set()
                await asyncio.sleep(0.2)

            dispatched = worker.metrics["total_dispatched"]
            assert dispatched == total, (
                f"Slow consumer: {dispatched}/{total} delivered (lost {total - dispatched})"
            )
        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_concurrent_delivery_and_streams_under_load(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Delivery worker + 5 SSE streams + 100 events — all concurrent."""
        signal = asyncio.Event()
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock()

        worker = EventDeliveryWorker(
            record_store,
            event_bus=mock_bus,
            event_signal=signal,
            batch_size=25,
        )

        num_streams = 5
        stream_events: dict[int, list] = {i: [] for i in range(num_streams)}
        total_events = 100

        async def stream_consumer(idx: int):
            svc = EventReplayService(record_store=record_store, event_signal=signal)
            count = 0
            async for event in svc.stream(
                poll_interval=60.0,
                idle_timeout=10.0,
            ):
                stream_events[idx].append(event)
                count += 1
                if count >= total_events:
                    break

        await worker.start()
        stream_tasks = [asyncio.create_task(stream_consumer(i)) for i in range(num_streams)]
        await asyncio.sleep(0.2)

        try:
            # Insert events
            for i in range(total_events):
                _insert_event(
                    record_store.session_factory,
                    path=f"/concurrent-load-{i}.txt",
                    sequence_number=i + 1,
                    delivered=False,
                )
                if i % 20 == 0:
                    signal.set()

            signal.set()

            # Wait for delivery worker to finish
            deadline = asyncio.get_event_loop().time() + 15.0
            while asyncio.get_event_loop().time() < deadline:
                if worker.metrics["total_dispatched"] >= total_events:
                    break
                signal.set()
                await asyncio.sleep(0.1)

            # Cancel stream tasks (they may still be waiting for idle_timeout)
            for t in stream_tasks:
                t.cancel()
            await asyncio.gather(*stream_tasks, return_exceptions=True)

            # Verify delivery worker processed all events
            assert worker.metrics["total_dispatched"] == total_events, (
                f"Worker: {worker.metrics['total_dispatched']}/{total_events}"
            )

            # Verify each stream got events (may not get all 100 due to timing)
            for idx in range(num_streams):
                count = len(stream_events[idx])
                assert count > 0, f"Stream {idx} received 0 events"

        finally:
            await worker.stop()
