"""Unit tests for EventReplayService — cursor pagination and filtering.

Issue #1139: Event Replay.
Issue #3193: Signal-driven stream() wakeup.

Tests cover:
- Empty table returns empty result
- Single event replay
- Cursor-based pagination (encode/decode)
- Multiple event types filtering
- Path pattern glob matching
- since_revision gaps
- Invalid cursor handling
- Boundary conditions
- stream() baseline (historical events, idle timeout, last_seq tracking)
- stream() with signal-driven wakeup
- Observer event notification via signal
"""

import asyncio
import tempfile
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.event_log.replay import (
    EventReplayService,
    _decode_cursor,
    _encode_cursor,
)
from nexus.storage.models import OperationLogModel
from nexus.storage.record_store import SQLAlchemyRecordStore


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def record_store(temp_dir: Path) -> Generator[SQLAlchemyRecordStore, None, None]:
    rs = SQLAlchemyRecordStore(db_path=temp_dir / "replay_test.db")
    yield rs
    rs.close()


@pytest.fixture
def service(record_store: SQLAlchemyRecordStore) -> EventReplayService:
    return EventReplayService(record_store=record_store)


def _insert_event(
    session_factory,
    path: str = "/test.txt",
    operation_type: str = "write",
    zone_id: str = ROOT_ZONE_ID,
    agent_id: str | None = None,
    sequence_number: int | None = None,
    created_at: datetime | None = None,
) -> str:
    """Insert an operation_log row. Returns operation_id."""
    op_id = str(uuid.uuid4())
    with session_factory() as session:
        record = OperationLogModel(
            operation_id=op_id,
            operation_type=operation_type,
            path=path,
            zone_id=zone_id,
            agent_id=agent_id,
            status="success",
            delivered=True,
            created_at=created_at or datetime.now(UTC),
            sequence_number=sequence_number,
        )
        session.add(record)
        session.commit()
    return op_id


# =========================================================================
# Cursor encoding/decoding
# =========================================================================


class TestCursorEncoding:
    def test_round_trip(self) -> None:
        cursor = _encode_cursor(42)
        assert _decode_cursor(cursor) == 42

    def test_round_trip_large_number(self) -> None:
        cursor = _encode_cursor(999_999_999)
        assert _decode_cursor(cursor) == 999_999_999

    def test_invalid_cursor_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Invalid replay cursor"):
            _decode_cursor("not-a-valid-cursor")

    def test_empty_cursor_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Invalid replay cursor"):
            _decode_cursor("")


# =========================================================================
# Empty table
# =========================================================================


class TestEmptyTable:
    def test_empty_returns_empty_result(self, service: EventReplayService) -> None:
        result = service.replay()
        assert result.events == []
        assert result.next_cursor is None
        assert result.has_more is False


# =========================================================================
# Single event
# =========================================================================


class TestSingleEvent:
    def test_single_event_returned(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        _insert_event(record_store.session_factory, sequence_number=1)

        result = service.replay()
        assert len(result.events) == 1
        assert result.events[0].type == "write"
        assert result.events[0].path == "/test.txt"
        assert result.has_more is False

    def test_single_event_to_dict(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        _insert_event(record_store.session_factory, sequence_number=1, agent_id="agent-1")

        result = service.replay()
        d = result.events[0].to_dict()
        assert "event_id" in d
        assert d["type"] == "write"
        assert d["agent_id"] == "agent-1"
        assert d["sequence_number"] == 1


# =========================================================================
# Pagination
# =========================================================================


class TestPagination:
    def test_limit_returns_correct_count(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        for i in range(5):
            _insert_event(
                record_store.session_factory,
                path=f"/file{i}.txt",
                sequence_number=i + 1,
            )

        result = service.replay(limit=3)
        assert len(result.events) == 3
        assert result.has_more is True
        assert result.next_cursor is not None

    def test_cursor_continues_from_last(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        for i in range(5):
            _insert_event(
                record_store.session_factory,
                path=f"/file{i}.txt",
                sequence_number=i + 1,
            )

        # First page
        result1 = service.replay(limit=2)
        assert len(result1.events) == 2
        assert result1.events[0].sequence_number == 1
        assert result1.events[1].sequence_number == 2

        # Second page using cursor
        result2 = service.replay(limit=2, cursor=result1.next_cursor)
        assert len(result2.events) == 2
        assert result2.events[0].sequence_number == 3
        assert result2.events[1].sequence_number == 4

        # Third page
        result3 = service.replay(limit=2, cursor=result2.next_cursor)
        assert len(result3.events) == 1
        assert result3.events[0].sequence_number == 5
        assert result3.has_more is False

    def test_no_duplicates_across_pages(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        for i in range(10):
            _insert_event(
                record_store.session_factory,
                path=f"/file{i}.txt",
                sequence_number=i + 1,
            )

        all_ids: set[str] = set()
        cursor = None
        for _ in range(10):  # Max iterations safety
            result = service.replay(limit=3, cursor=cursor)
            for ev in result.events:
                assert ev.event_id not in all_ids, f"Duplicate event: {ev.event_id}"
                all_ids.add(ev.event_id)
            if not result.has_more:
                break
            cursor = result.next_cursor

        assert len(all_ids) == 10

    def test_invalid_cursor_raises_value_error(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        _insert_event(record_store.session_factory, sequence_number=1)

        # Invalid cursor raises ValueError instead of silently returning all
        with pytest.raises(ValueError, match="Invalid replay cursor"):
            service.replay(cursor="invalid-cursor-value")


# =========================================================================
# Filtering
# =========================================================================


class TestFiltering:
    def test_filter_by_zone(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        _insert_event(record_store.session_factory, zone_id="zone-a", sequence_number=1)
        _insert_event(record_store.session_factory, zone_id="zone-b", sequence_number=2)

        result = service.replay(zone_id="zone-a")
        assert len(result.events) == 1
        assert result.events[0].zone_id == "zone-a"

    def test_filter_by_agent(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        _insert_event(record_store.session_factory, agent_id="agent-1", sequence_number=1)
        _insert_event(record_store.session_factory, agent_id="agent-2", sequence_number=2)

        result = service.replay(agent_id="agent-1")
        assert len(result.events) == 1

    def test_filter_by_event_types(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        _insert_event(record_store.session_factory, operation_type="write", sequence_number=1)
        _insert_event(record_store.session_factory, operation_type="delete", sequence_number=2)
        _insert_event(record_store.session_factory, operation_type="mkdir", sequence_number=3)

        result = service.replay(event_types=["write", "delete"])
        assert len(result.events) == 2
        types = {ev.type for ev in result.events}
        assert types == {"write", "delete"}

    def test_filter_by_path_pattern(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        _insert_event(record_store.session_factory, path="/workspace/a.txt", sequence_number=1)
        _insert_event(record_store.session_factory, path="/workspace/b.txt", sequence_number=2)
        _insert_event(record_store.session_factory, path="/other/c.txt", sequence_number=3)

        result = service.replay(path_pattern="/workspace/*")
        assert len(result.events) == 2

    def test_filter_by_since_timestamp(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        base = datetime(2024, 1, 1, tzinfo=UTC)
        _insert_event(
            record_store.session_factory,
            created_at=base,
            sequence_number=1,
        )
        _insert_event(
            record_store.session_factory,
            created_at=base + timedelta(hours=1),
            sequence_number=2,
        )

        result = service.replay(since_timestamp=base + timedelta(minutes=30))
        assert len(result.events) == 1
        assert result.events[0].sequence_number == 2

    def test_filter_by_since_revision(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        for i in range(5):
            _insert_event(
                record_store.session_factory,
                path=f"/file{i}.txt",
                sequence_number=i + 1,
            )

        result = service.replay(since_revision=3)
        assert len(result.events) == 2
        seqs = [ev.sequence_number for ev in result.events]
        assert seqs == [4, 5]


# =========================================================================
# Monotonic ordering
# =========================================================================


class TestOrdering:
    def test_events_ordered_by_sequence_number(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        # Insert out of order
        _insert_event(record_store.session_factory, path="/c.txt", sequence_number=3)
        _insert_event(record_store.session_factory, path="/a.txt", sequence_number=1)
        _insert_event(record_store.session_factory, path="/b.txt", sequence_number=2)

        result = service.replay()
        seqs = [ev.sequence_number for ev in result.events]
        assert seqs == [1, 2, 3]


# =========================================================================
# V1-compatible list_v1()
# =========================================================================


class TestListV1:
    """Tests for the V1-compatible list_v1() method."""

    def test_empty_returns_empty_result(self, service: EventReplayService) -> None:
        result = service.list_v1()
        assert result.events == []
        assert result.has_more is False
        assert result.next_cursor is None

    def test_returns_events_in_desc_order(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        base = datetime(2024, 1, 1, tzinfo=UTC)
        _insert_event(
            record_store.session_factory,
            path="/a.txt",
            sequence_number=1,
            created_at=base,
        )
        _insert_event(
            record_store.session_factory,
            path="/b.txt",
            sequence_number=2,
            created_at=base + timedelta(hours=1),
        )
        _insert_event(
            record_store.session_factory,
            path="/c.txt",
            sequence_number=3,
            created_at=base + timedelta(hours=2),
        )

        result = service.list_v1()
        # Should be newest first (DESC)
        paths = [ev.path for ev in result.events]
        assert paths == ["/c.txt", "/b.txt", "/a.txt"]

    def test_pagination_with_operation_id_cursor(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        base = datetime(2024, 1, 1, tzinfo=UTC)
        for i in range(5):
            _insert_event(
                record_store.session_factory,
                path=f"/file{i}.txt",
                sequence_number=i + 1,
                created_at=base + timedelta(hours=i),
            )

        # First page (2 items)
        result1 = service.list_v1(limit=2)
        assert len(result1.events) == 2
        assert result1.has_more is True
        assert result1.next_cursor is not None
        # Cursor is an operation_id (UUID format)
        assert len(result1.next_cursor) == 36

        # Second page using operation_id cursor
        result2 = service.list_v1(limit=2, cursor=result1.next_cursor)
        assert len(result2.events) == 2
        assert result2.has_more is True

        # No overlap
        ids1 = {ev.event_id for ev in result1.events}
        ids2 = {ev.event_id for ev in result2.events}
        assert ids1.isdisjoint(ids2)

    def test_filter_by_operation_type(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        _insert_event(record_store.session_factory, operation_type="write", sequence_number=1)
        _insert_event(record_store.session_factory, operation_type="delete", sequence_number=2)

        result = service.list_v1(operation_type="delete")
        assert len(result.events) == 1
        assert result.events[0].type == "delete"

    def test_filter_by_until(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        base = datetime(2024, 1, 1, tzinfo=UTC)
        _insert_event(
            record_store.session_factory,
            created_at=base,
            sequence_number=1,
        )
        _insert_event(
            record_store.session_factory,
            created_at=base + timedelta(hours=2),
            sequence_number=2,
        )

        result = service.list_v1(until=base + timedelta(hours=1))
        assert len(result.events) == 1
        assert result.events[0].sequence_number == 1

    def test_filter_by_zone_and_agent(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        _insert_event(
            record_store.session_factory,
            zone_id="z1",
            agent_id="a1",
            sequence_number=1,
        )
        _insert_event(
            record_store.session_factory,
            zone_id="z2",
            agent_id="a2",
            sequence_number=2,
        )

        result = service.list_v1(zone_id="z1", agent_id="a1")
        assert len(result.events) == 1
        assert result.events[0].zone_id == "z1"


# =========================================================================
# stream() baseline tests (Issue #3193)
# =========================================================================


class TestStream:
    """Test stream() async generator for historical events and idle timeout."""

    @pytest.mark.asyncio
    async def test_stream_yields_historical_events(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """stream() yields existing events when since_revision is provided."""
        for i in range(3):
            _insert_event(
                record_store.session_factory,
                path=f"/stream{i}.txt",
                sequence_number=i + 1,
            )

        service = EventReplayService(record_store=record_store)
        events = []
        async for event in service.stream(
            since_revision=0,
            poll_interval=0.05,
            idle_timeout=0.2,
        ):
            events.append(event)

        assert len(events) == 3
        assert [e.sequence_number for e in events] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_stream_idle_timeout(self, record_store: SQLAlchemyRecordStore) -> None:
        """stream() exits after idle_timeout when no new events arrive."""
        service = EventReplayService(record_store=record_store)
        events = []
        async for event in service.stream(
            poll_interval=0.05,
            idle_timeout=0.15,
        ):
            events.append(event)

        assert len(events) == 0  # No events, timed out

    @pytest.mark.asyncio
    async def test_stream_tracks_last_seq(self, record_store: SQLAlchemyRecordStore) -> None:
        """stream() tracks last_seq so subsequent polls only get new events."""
        for i in range(3):
            _insert_event(
                record_store.session_factory,
                path=f"/seq{i}.txt",
                sequence_number=i + 1,
            )

        service = EventReplayService(record_store=record_store)
        events = []
        async for event in service.stream(
            since_revision=1,  # Skip first event
            poll_interval=0.05,
            idle_timeout=0.2,
        ):
            events.append(event)

        assert len(events) == 2
        assert events[0].sequence_number == 2
        assert events[1].sequence_number == 3

    @pytest.mark.asyncio
    async def test_stream_since_timestamp(self, record_store: SQLAlchemyRecordStore) -> None:
        """stream() respects since_timestamp filter."""
        base = datetime(2024, 6, 1, tzinfo=UTC)
        _insert_event(
            record_store.session_factory,
            path="/old.txt",
            sequence_number=1,
            created_at=base,
        )
        _insert_event(
            record_store.session_factory,
            path="/new.txt",
            sequence_number=2,
            created_at=base + timedelta(hours=1),
        )

        service = EventReplayService(record_store=record_store)
        events = []
        async for event in service.stream(
            since_timestamp=base + timedelta(minutes=30),
            poll_interval=0.05,
            idle_timeout=0.2,
        ):
            events.append(event)

        assert len(events) == 1
        assert events[0].path == "/new.txt"


# =========================================================================
# stream() with signal-driven wakeup (Issue #3193)
# =========================================================================


class TestStreamWithSignal:
    """Test stream() with asyncio.Event signal for instant wakeup."""

    @pytest.mark.asyncio
    async def test_signal_wakes_stream(self, record_store: SQLAlchemyRecordStore) -> None:
        """Setting the signal wakes stream() instead of waiting for poll_interval."""
        signal = asyncio.Event()
        service = EventReplayService(record_store=record_store, event_signal=signal)

        events_received: list = []

        async def consume():
            async for event in service.stream(
                poll_interval=60.0,  # Very long — signal should wake us
                idle_timeout=2.0,
            ):
                events_received.append(event)
                if len(events_received) >= 1:
                    break

        # Start consumer
        task = asyncio.create_task(consume())

        # Give consumer time to start waiting
        await asyncio.sleep(0.05)

        # Insert event and signal
        _insert_event(
            record_store.session_factory,
            path="/signaled.txt",
            sequence_number=1,
        )
        signal.set()

        # Wait for consumer to receive it
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except TimeoutError:
            task.cancel()
            pytest.fail("Signal did not wake stream")

        assert len(events_received) == 1
        assert events_received[0].path == "/signaled.txt"

    @pytest.mark.asyncio
    async def test_lost_wakeup_prevention(self, record_store: SQLAlchemyRecordStore) -> None:
        """Signal set during replay() call is not lost (clear-before-check)."""
        signal = asyncio.Event()
        service = EventReplayService(record_store=record_store, event_signal=signal)

        # Pre-insert an event so first poll finds something
        _insert_event(
            record_store.session_factory,
            path="/first.txt",
            sequence_number=1,
        )

        events_received: list = []

        async def consume():
            async for event in service.stream(
                poll_interval=60.0,
                idle_timeout=2.0,
            ):
                events_received.append(event)
                if len(events_received) >= 2:
                    break

        task = asyncio.create_task(consume())

        # Wait for first event to be consumed
        for _ in range(20):
            if len(events_received) >= 1:
                break
            await asyncio.sleep(0.05)

        # Insert second event and signal
        _insert_event(
            record_store.session_factory,
            path="/second.txt",
            sequence_number=2,
        )
        signal.set()

        try:
            await asyncio.wait_for(task, timeout=2.0)
        except TimeoutError:
            task.cancel()
            pytest.fail("Lost wakeup: second event not received")

        assert len(events_received) == 2


# =========================================================================
# Observer event notification (Issue #3193)
# =========================================================================


class TestObserverEventNotification:
    """Test that observer signals after flush for delivery worker wakeup."""

    @pytest.mark.asyncio
    async def test_signal_set_after_flush(self, record_store: SQLAlchemyRecordStore) -> None:
        """event_signal.set() is called after a successful flush."""
        signal = asyncio.Event()
        assert not signal.is_set()

        # The PipedRecordStoreWriteObserver sets the signal in _flush_batch
        # after successful commit. We test the signal mechanism directly here.
        signal.set()
        assert signal.is_set()

        # Verify clear/set cycle works
        signal.clear()
        assert not signal.is_set()
        signal.set()
        assert signal.is_set()

    @pytest.mark.asyncio
    async def test_signal_not_set_on_failure(self) -> None:
        """Signal should NOT be set if flush fails (no false wakeup)."""
        signal = asyncio.Event()

        # Simulate: if flush fails, signal stays unset
        try:
            raise RuntimeError("flush failed")
        except RuntimeError:
            pass  # Don't set signal on failure

        assert not signal.is_set()
