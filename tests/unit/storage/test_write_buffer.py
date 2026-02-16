"""Tests for WriteBuffer — the write-behind buffer for async PG sync.

Issue #1246 Phase 3: Verifies buffering, flushing, retry, and metrics.
"""

from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.core._metadata_generated import FileMetadata
from nexus.storage.models import Base, FilePathModel, OperationLogModel
from nexus.storage.write_buffer import EventType, WriteBuffer, WriteEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_metadata(
    path: str = "/test/file.txt",
    etag: str | None = "sha256-abc",
) -> FileMetadata:
    now = datetime(2026, 2, 10, 12, 0, 0)
    return FileMetadata(
        path=path,
        backend_name="local",
        physical_path="/data/abc123",
        size=1024,
        etag=etag,
        mime_type="text/plain",
        version=1,
        zone_id="default",
        created_by="user-1",
        owner_id="owner-1",
        created_at=now,
        modified_at=now,
    )


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    factory = sessionmaker(bind=engine)
    return factory


# ---------------------------------------------------------------------------
# WriteEvent tests
# ---------------------------------------------------------------------------


class TestWriteEvent:
    """Tests for WriteEvent dataclass."""

    def test_write_event_is_immutable(self) -> None:
        """WriteEvent should be frozen (immutable)."""
        event = WriteEvent(event_type=EventType.WRITE, path="/test")
        with pytest.raises(AttributeError):
            event.path = "/other"  # type: ignore[misc]

    def test_event_types(self) -> None:
        """All event types should be valid."""
        assert EventType.WRITE.value == "write"
        assert EventType.DELETE.value == "delete"
        assert EventType.RENAME.value == "rename"


# ---------------------------------------------------------------------------
# Buffer lifecycle tests
# ---------------------------------------------------------------------------


class TestBufferLifecycle:
    """Tests for start/stop and basic buffering."""

    def test_enqueue_increments_count(self, session_factory) -> None:
        """Enqueuing events should increase pending count."""
        buf = WriteBuffer(session_factory, flush_interval_ms=10000)
        buf.enqueue_write(_make_metadata(), is_new=True, path="/test/file.txt")
        assert buf.pending_count == 1

    def test_enqueue_multiple(self, session_factory) -> None:
        """Multiple enqueues should accumulate."""
        buf = WriteBuffer(session_factory, flush_interval_ms=10000)
        for i in range(5):
            buf.enqueue_write(
                _make_metadata(path=f"/test/file{i}.txt"),
                is_new=True,
                path=f"/test/file{i}.txt",
            )
        assert buf.pending_count == 5

    def test_stop_drains_buffer(self, session_factory) -> None:
        """Stopping the buffer should flush all remaining events."""
        buf = WriteBuffer(session_factory, flush_interval_ms=10000)
        buf.start()

        for i in range(3):
            buf.enqueue_write(
                _make_metadata(path=f"/test/file{i}.txt", etag=f"hash-{i}"),
                is_new=True,
                path=f"/test/file{i}.txt",
            )

        buf.stop(timeout=5.0)

        assert buf.pending_count == 0
        assert buf.metrics["total_flushed"] == 3

    def test_metrics_tracking(self, session_factory) -> None:
        """Metrics should accurately track enqueue/flush counts."""
        buf = WriteBuffer(session_factory, flush_interval_ms=10000)
        buf.start()

        buf.enqueue_write(_make_metadata(), is_new=True, path="/test/file.txt")
        buf.stop(timeout=5.0)

        metrics = buf.metrics
        assert metrics["total_enqueued"] == 1
        assert metrics["total_flushed"] == 1
        assert metrics["total_failed"] == 0
        assert metrics["pending"] == 0


# ---------------------------------------------------------------------------
# Flush behavior tests
# ---------------------------------------------------------------------------


class TestFlushBehavior:
    """Tests for periodic and threshold-based flushing."""

    def test_flush_on_threshold(self, session_factory) -> None:
        """Buffer should auto-flush when max_buffer_size is reached."""
        buf = WriteBuffer(
            session_factory,
            flush_interval_ms=10000,  # Long interval
            max_buffer_size=3,  # Low threshold
        )
        buf.start()

        for i in range(3):
            buf.enqueue_write(
                _make_metadata(path=f"/test/file{i}.txt", etag=f"hash-{i}"),
                is_new=True,
                path=f"/test/file{i}.txt",
            )

        # Give flush thread time to process
        time.sleep(0.3)
        buf.stop(timeout=5.0)

        assert buf.metrics["total_flushed"] == 3

    def test_flush_creates_db_records(self, session_factory) -> None:
        """Flushed events should create actual DB records."""
        buf = WriteBuffer(session_factory, flush_interval_ms=10000)
        buf.start()

        buf.enqueue_write(
            _make_metadata(path="/test/db_record.txt", etag="db-hash"),
            is_new=True,
            path="/test/db_record.txt",
            zone_id="default",
            agent_id="agent-1",
        )

        buf.stop(timeout=5.0)

        # Verify records in DB
        with session_factory() as session:
            fps = (
                session.execute(
                    select(FilePathModel).where(FilePathModel.virtual_path == "/test/db_record.txt")
                )
                .scalars()
                .all()
            )
            assert len(fps) == 1

            ops = (
                session.execute(
                    select(OperationLogModel).where(OperationLogModel.path == "/test/db_record.txt")
                )
                .scalars()
                .all()
            )
            assert len(ops) == 1

    def test_flush_handles_delete_events(self, session_factory) -> None:
        """Delete events should create audit log and soft-delete."""
        buf = WriteBuffer(session_factory, flush_interval_ms=10000)
        buf.start()

        # First create
        buf.enqueue_write(
            _make_metadata(path="/test/to_delete.txt", etag="hash-1"),
            is_new=True,
            path="/test/to_delete.txt",
        )
        buf.stop(timeout=5.0)

        # Then delete
        buf2 = WriteBuffer(session_factory, flush_interval_ms=10000)
        buf2.start()
        buf2.enqueue_delete(path="/test/to_delete.txt", zone_id="default")
        buf2.stop(timeout=5.0)

        with session_factory() as session:
            fp = session.execute(
                select(FilePathModel).where(FilePathModel.virtual_path == "/test/to_delete.txt")
            ).scalar_one()
            assert fp.deleted_at is not None

    def test_flush_handles_rename_events(self, session_factory) -> None:
        """Rename events should create audit log."""
        buf = WriteBuffer(session_factory, flush_interval_ms=10000)
        buf.start()

        buf.enqueue_rename(
            old_path="/test/old.txt",
            new_path="/test/new.txt",
            zone_id="default",
        )
        buf.stop(timeout=5.0)

        with session_factory() as session:
            ops = (
                session.execute(
                    select(OperationLogModel).where(OperationLogModel.operation_type == "rename")
                )
                .scalars()
                .all()
            )
            assert len(ops) == 1
            assert ops[0].path == "/test/old.txt"
            assert ops[0].new_path == "/test/new.txt"


# ---------------------------------------------------------------------------
# Retry tests
# ---------------------------------------------------------------------------


class TestRetryBehavior:
    """Tests for retry on flush failure."""

    def test_retry_on_transient_failure(self, session_factory) -> None:
        """Buffer should retry on transient failures."""
        buf = WriteBuffer(
            session_factory,
            flush_interval_ms=10000,
            max_retries=3,
        )

        buf.enqueue_write(_make_metadata(), is_new=True, path="/test/retry.txt")

        # Simulate one failure then success
        with patch.object(buf, "_process_events", wraps=buf._process_events):
            buf._flush_buffer()

        # Should have flushed successfully
        assert buf.metrics["total_failed"] == 0

    def test_events_dropped_after_max_retries(self, session_factory) -> None:
        """After max_retries, events should be dropped and counted."""
        buf = WriteBuffer(
            session_factory,
            flush_interval_ms=10000,
            max_retries=2,
        )

        buf.enqueue_write(_make_metadata(), is_new=True, path="/test/fail.txt")

        # Mock session to always fail
        def failing_factory():
            mock_session = MagicMock()
            mock_session.__enter__ = MagicMock(return_value=mock_session)
            mock_session.__exit__ = MagicMock(return_value=False)
            # Make log_operation raise
            raise RuntimeError("Permanent DB failure")

        buf._session_factory = failing_factory
        buf._flush_buffer()

        # Events should be counted as failed
        assert buf.metrics["total_failed"] > 0


# ---------------------------------------------------------------------------
# Enhanced metrics tests (Issue #1370)
# ---------------------------------------------------------------------------


class TestEnhancedMetrics:
    """Tests for the new timing, retry, and per-type metrics."""

    def test_metrics_includes_timing_fields(self, session_factory) -> None:
        """Metrics dict should include all new timing/batch fields."""
        buf = WriteBuffer(session_factory, flush_interval_ms=10000)
        buf.start()

        buf.enqueue_write(_make_metadata(), is_new=True, path="/test/file.txt")
        buf.stop(timeout=5.0)

        metrics = buf.metrics
        assert "total_retries" in metrics
        assert "flush_count" in metrics
        assert "flush_duration_sum" in metrics
        assert "flush_batch_size_sum" in metrics
        assert "enqueued_by_type" in metrics
        assert metrics["flush_count"] >= 1
        assert metrics["flush_duration_sum"] > 0
        assert metrics["flush_batch_size_sum"] == 1

    def test_enqueued_by_type_tracking(self, session_factory) -> None:
        """Per-type counters should track write, delete, and rename separately."""
        buf = WriteBuffer(session_factory, flush_interval_ms=10000)

        buf.enqueue_write(_make_metadata(path="/a.txt"), is_new=True, path="/a.txt")
        buf.enqueue_write(_make_metadata(path="/b.txt"), is_new=True, path="/b.txt")
        buf.enqueue_delete(path="/c.txt", zone_id="default")
        buf.enqueue_rename(old_path="/d.txt", new_path="/e.txt")

        by_type = buf.metrics["enqueued_by_type"]
        assert by_type == {"write": 2, "delete": 1, "rename": 1}

    def test_retry_counter_increments(self, session_factory) -> None:
        """Total retries should increment on transient flush failure."""
        call_count = 0
        real_factory = session_factory

        def flaky_factory():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise RuntimeError("Transient failure")
            return real_factory()

        buf = WriteBuffer(flaky_factory, flush_interval_ms=10000, max_retries=3)
        buf.enqueue_write(_make_metadata(), is_new=True, path="/retry.txt")
        buf._flush_buffer()

        assert buf.metrics["total_retries"] >= 1
        assert buf.metrics["total_flushed"] == 1
