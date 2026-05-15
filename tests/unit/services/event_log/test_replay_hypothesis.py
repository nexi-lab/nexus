"""Hypothesis property-based tests for EventReplayService (Issue #1139).

Tests properties:
- No duplicates across pages
- No gaps in sequence ordering
- Monotonic sequence_number ordering
- Cursor stability (re-running same cursor yields same results)
"""

import tempfile
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("hypothesis")

from hypothesis import given, settings
from hypothesis import strategies as st

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.event_log.replay import EventReplayService
from nexus.storage.models import OperationLogModel
from nexus.storage.record_store import SQLAlchemyRecordStore


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def record_store(temp_dir: Path) -> Generator[SQLAlchemyRecordStore, None, None]:
    rs = SQLAlchemyRecordStore(db_path=temp_dir / "hypothesis_test.db")
    yield rs
    rs.close()


def _seed_events(session_factory, count: int) -> list[str]:
    """Insert count events with sequential sequence_numbers."""
    op_ids = []
    with session_factory() as session:
        for i in range(count):
            op_id = str(uuid.uuid4())
            record = OperationLogModel(
                operation_id=op_id,
                operation_type="write",
                path=f"/file{i}.txt",
                zone_id=ROOT_ZONE_ID,
                status="success",
                delivered=True,
                created_at=datetime.now(UTC),
                sequence_number=i + 1,
            )
            session.add(record)
            op_ids.append(op_id)
        session.commit()
    return op_ids


class TestReplayProperties:
    """Property-based tests for replay pagination."""

    @given(
        n_events=st.integers(min_value=0, max_value=50),
        page_size=st.integers(min_value=1, max_value=20),
    )
    @settings(max_examples=10, deadline=10000)
    def test_no_duplicates_across_all_pages(self, n_events: int, page_size: int) -> None:
        """Every event_id appears exactly once across all pages."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rs = SQLAlchemyRecordStore(db_path=Path(tmpdir) / "test.db")
            try:
                _seed_events(rs.session_factory, n_events)
                service = EventReplayService(record_store=rs)

                all_ids: list[str] = []
                cursor = None
                for _ in range(n_events + 5):  # Safety bound
                    result = service.replay(limit=page_size, cursor=cursor)
                    for ev in result.events:
                        all_ids.append(ev.event_id)
                    if not result.has_more:
                        break
                    cursor = result.next_cursor

                assert len(all_ids) == len(set(all_ids)), "Duplicate event_ids found"
                assert len(all_ids) == n_events
            finally:
                rs.close()

    @given(
        n_events=st.integers(min_value=1, max_value=50),
        page_size=st.integers(min_value=1, max_value=20),
    )
    @settings(max_examples=10, deadline=10000)
    def test_monotonic_sequence_ordering(self, n_events: int, page_size: int) -> None:
        """Sequence numbers are always strictly increasing across pages."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rs = SQLAlchemyRecordStore(db_path=Path(tmpdir) / "test.db")
            try:
                _seed_events(rs.session_factory, n_events)
                service = EventReplayService(record_store=rs)

                all_seqs: list[int] = []
                cursor = None
                for _ in range(n_events + 5):
                    result = service.replay(limit=page_size, cursor=cursor)
                    for ev in result.events:
                        if ev.sequence_number is not None:
                            all_seqs.append(ev.sequence_number)
                    if not result.has_more:
                        break
                    cursor = result.next_cursor

                # Verify strictly increasing
                for i in range(1, len(all_seqs)):
                    assert all_seqs[i] > all_seqs[i - 1], (
                        f"Non-monotonic: seq[{i - 1}]={all_seqs[i - 1]} >= seq[{i}]={all_seqs[i]}"
                    )
            finally:
                rs.close()

    @given(
        n_events=st.integers(min_value=2, max_value=30),
        page_size=st.integers(min_value=1, max_value=10),
    )
    @settings(max_examples=10, deadline=10000)
    def test_cursor_stability(self, n_events: int, page_size: int) -> None:
        """Running the same cursor twice yields the same results."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rs = SQLAlchemyRecordStore(db_path=Path(tmpdir) / "test.db")
            try:
                _seed_events(rs.session_factory, n_events)
                service = EventReplayService(record_store=rs)

                # Get first page
                result1 = service.replay(limit=page_size)
                if result1.next_cursor is None:
                    return  # Only one page, no cursor to test

                # Run same cursor twice
                result_a = service.replay(limit=page_size, cursor=result1.next_cursor)
                result_b = service.replay(limit=page_size, cursor=result1.next_cursor)

                ids_a = [ev.event_id for ev in result_a.events]
                ids_b = [ev.event_id for ev in result_b.events]
                assert ids_a == ids_b, "Same cursor produced different results"
            finally:
                rs.close()
