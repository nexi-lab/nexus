"""Unit tests for EventReplayService — cursor pagination and filtering.

Issue #1139: Event Replay.

Tests cover:
- Empty table returns empty result
- Single event replay
- Cursor-based pagination (encode/decode)
- Multiple event types filtering
- Path pattern glob matching
- since_revision gaps
- Invalid cursor handling
- Boundary conditions
"""

import tempfile
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.models import OperationLogModel
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.system_services.event_subsystem.log.replay import (
    EventReplayService,
    _decode_cursor,
    _encode_cursor,
)


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
