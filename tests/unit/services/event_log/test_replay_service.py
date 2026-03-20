"""Unit tests for EventReplayService — cursor pagination, filtering, and streaming.

Issue #1139: Event Replay.
Issue #3193: Notification-driven streaming.

Tests cover:
- Empty table returns empty result
- Single event replay
- Cursor-based pagination (encode/decode)
- Multiple event types filtering
- Path pattern glob matching
- since_revision gaps
- Invalid cursor handling
- Boundary conditions
- stream() async generator: historical + live tail
- stream() with event signal (notification-driven)
- stream() idle timeout
- stream() lost-wakeup prevention
"""

import asyncio
import tempfile
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.event_subsystem.log.replay import (
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

    def test_invalid_cursor_returns_none(self) -> None:
        assert _decode_cursor("not-a-valid-cursor") is None

    def test_empty_cursor_returns_none(self) -> None:
        assert _decode_cursor("") is None


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

    def test_invalid_cursor_returns_all(
        self, service: EventReplayService, record_store: SQLAlchemyRecordStore
    ) -> None:
        _insert_event(record_store.session_factory, sequence_number=1)

        # Invalid cursor should be ignored (decoded as None)
        result = service.replay(cursor="invalid-cursor-value")
        assert len(result.events) == 1


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
# stream() — async generator (Issue #3193)
# =========================================================================


class TestStream:
    """Test the stream() async generator for SSE streaming."""

    @pytest.mark.asyncio
    async def test_stream_yields_historical_events(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """stream() should yield historical events first."""
        for i in range(3):
            _insert_event(
                record_store.session_factory,
                path=f"/stream{i}.txt",
                sequence_number=i + 1,
            )

        service = EventReplayService(record_store=record_store)
        events = []
        async for event in service.stream(idle_timeout=0.1, poll_interval=0.05):
            events.append(event)

        assert len(events) == 3
        assert events[0].path == "/stream0.txt"
        assert events[2].path == "/stream2.txt"

    @pytest.mark.asyncio
    async def test_stream_idle_timeout_returns(self, record_store: SQLAlchemyRecordStore) -> None:
        """stream() should return after idle_timeout when no new events."""
        service = EventReplayService(record_store=record_store)
        events = []
        async for event in service.stream(idle_timeout=0.2, poll_interval=0.05):
            events.append(event)

        assert len(events) == 0  # No events, timed out

    @pytest.mark.asyncio
    async def test_stream_last_seq_tracks_correctly(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """stream() should track last_seq and not re-yield old events."""
        _insert_event(record_store.session_factory, path="/a.txt", sequence_number=1)

        service = EventReplayService(record_store=record_store)
        events = []
        async for event in service.stream(since_revision=0, idle_timeout=0.2, poll_interval=0.05):
            events.append(event)

        assert len(events) == 1
        assert events[0].sequence_number == 1

    @pytest.mark.asyncio
    async def test_stream_since_timestamp_used_only_first_iteration(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """since_timestamp should only apply on first iteration, not subsequent polls."""
        base = datetime(2024, 1, 1, tzinfo=UTC)
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
            idle_timeout=0.2,
            poll_interval=0.05,
        ):
            events.append(event)

        # Should only get the event after the timestamp
        assert len(events) == 1
        assert events[0].path == "/new.txt"


# =========================================================================
# stream() with event_signal (Issue #3193, notification-driven)
# =========================================================================


class TestStreamWithSignal:
    """Test stream() using asyncio.Event notification for live tail."""

    @pytest.mark.asyncio
    async def test_stream_wakes_on_signal(self, record_store: SQLAlchemyRecordStore) -> None:
        """stream() should wake immediately when event_signal is set."""
        signal = asyncio.Event()
        _insert_event(record_store.session_factory, path="/first.txt", sequence_number=1)

        service = EventReplayService(record_store=record_store, event_signal=signal)
        events = []

        async def consume():
            async for event in service.stream(idle_timeout=2.0, poll_interval=10.0):
                events.append(event)
                if len(events) >= 2:
                    return

        # Start consuming in background
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.1)  # Let it pick up the first event

        # Insert second event and signal
        _insert_event(record_store.session_factory, path="/second.txt", sequence_number=2)
        signal.set()

        # Should complete quickly (not wait for 10s poll)
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except TimeoutError:
            task.cancel()
            pytest.fail("stream() did not wake on signal — took too long")

        assert len(events) == 2
        assert events[1].path == "/second.txt"

    @pytest.mark.asyncio
    async def test_stream_no_lost_wakeup(self, record_store: SQLAlchemyRecordStore) -> None:
        """Signal between clear() and wait() should not cause lost events."""
        signal = asyncio.Event()
        service = EventReplayService(record_store=record_store, event_signal=signal)
        events = []

        async def consume():
            async for event in service.stream(idle_timeout=2.0, poll_interval=10.0):
                events.append(event)
                if len(events) >= 3:
                    return

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.1)  # Let it start

        # Rapid-fire: insert + signal three times
        for i in range(3):
            _insert_event(
                record_store.session_factory,
                path=f"/rapid{i}.txt",
                sequence_number=i + 1,
            )
            signal.set()
            await asyncio.sleep(0)  # Yield to event loop

        try:
            await asyncio.wait_for(task, timeout=3.0)
        except TimeoutError:
            task.cancel()
            pytest.fail("stream() missed events — possible lost wakeup")

        assert len(events) == 3


# =========================================================================
# Observer Event notification (Issue #3193, Issue 10/14)
# =========================================================================


class TestObserverEventNotification:
    """Test PipedRecordStoreWriteObserver signals event_signal after commit."""

    @pytest.mark.asyncio
    async def test_event_signal_set_after_flush(self, record_store: SQLAlchemyRecordStore) -> None:
        """_flush_batch should signal the event after successful DB commit."""
        from nexus.storage.piped_record_store_write_observer import (
            PipedRecordStoreWriteObserver,
        )

        signal = asyncio.Event()
        observer = PipedRecordStoreWriteObserver(record_store, event_signal=signal)

        # Simulate a batch flush with a write event
        events = [
            {
                "op": "write",
                "path": "/notify.txt",
                "is_new": True,
                "zone_id": "root",
                "agent_id": None,
                "snapshot_hash": None,
                "metadata_snapshot": None,
                "metadata": {
                    "path": "/notify.txt",
                    "backend_name": "local",
                    "physical_path": "abc",
                    "size": 100,
                    "etag": "abc",
                    "mime_type": "text/plain",
                    "created_at": datetime.now(UTC).isoformat(),
                    "modified_at": datetime.now(UTC).isoformat(),
                    "version": 1,
                    "zone_id": "root",
                    "created_by": "test",
                    "owner_id": "test",
                },
            }
        ]

        assert not signal.is_set()
        await observer._flush_batch(events)
        assert signal.is_set(), "Event signal should be set after successful flush"

    @pytest.mark.asyncio
    async def test_event_signal_not_set_on_flush_failure(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """_flush_batch should NOT signal the event if DB commit fails."""

        from nexus.storage.piped_record_store_write_observer import (
            PipedRecordStoreWriteObserver,
        )

        signal = asyncio.Event()
        observer = PipedRecordStoreWriteObserver(record_store, event_signal=signal)

        # Create an event that will fail during flush (bad metadata)
        events = [
            {
                "op": "write",
                "path": "/fail.txt",
                "is_new": True,
                "zone_id": "root",
                "agent_id": None,
                "snapshot_hash": None,
                "metadata_snapshot": None,
                "metadata": {},  # Invalid — will fail on _metadata_from_dict
            }
        ]

        # Flush should fail (bad metadata), event should NOT be signaled
        await observer._flush_batch(events)  # Retries then drops
        assert not signal.is_set(), "Event signal should NOT be set after failed flush"
