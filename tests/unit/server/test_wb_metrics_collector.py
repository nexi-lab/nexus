"""Tests for WriteBufferCollector Prometheus bridge (Issue #1370).

Unit tests use a mocked write observer; integration tests use a real
WriteBuffer backed by an in-memory SQLite database.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.core._metadata_generated import FileMetadata
from nexus.server.wb_metrics_collector import WriteBufferCollector

# Expected metric family names emitted by the collector
_EXPECTED_FAMILIES = {
    "nexus_write_buffer_events_enqueued_total",
    "nexus_write_buffer_events_flushed_total",
    "nexus_write_buffer_events_failed_total",
    "nexus_write_buffer_retries_total",
    "nexus_write_buffer_pending_events",
    "nexus_write_buffer_flush_duration_seconds_sum",
    "nexus_write_buffer_flush_duration_seconds_count",
    "nexus_write_buffer_flush_batch_size_sum",
    "nexus_write_buffer_flush_batch_size_count",
}


def _make_observer(**overrides: object) -> MagicMock:
    """Build a mock write observer returning a metrics dict."""
    defaults: dict[str, object] = {
        "total_enqueued": 0,
        "total_flushed": 0,
        "total_failed": 0,
        "total_retries": 0,
        "pending": 0,
        "flush_count": 0,
        "flush_duration_sum": 0.0,
        "flush_batch_size_sum": 0,
        "enqueued_by_type": {"write": 0, "delete": 0, "rename": 0},
    }
    defaults.update(overrides)
    mock = MagicMock()
    mock.metrics = defaults
    return mock


# ---------------------------------------------------------------------------
# Unit tests (mocked write observer)
# ---------------------------------------------------------------------------


class TestWriteBufferCollectorUnit:
    """Unit tests with mocked WriteBuffer metrics."""

    def test_collect_yields_all_metric_families(self) -> None:
        """Collector should yield all 9 expected metric families."""
        collector = WriteBufferCollector(_make_observer())
        families = list(collector.collect())
        names = {f.name for f in families}
        assert names == _EXPECTED_FAMILIES

    def test_enqueued_has_event_type_labels(self) -> None:
        """Enqueued metric should have write/delete/rename labels."""
        observer = _make_observer(
            enqueued_by_type={"write": 10, "delete": 3, "rename": 1},
        )
        collector = WriteBufferCollector(observer)
        families = {f.name: f for f in collector.collect()}

        enqueued = families["nexus_write_buffer_events_enqueued_total"]
        label_values = {s.labels["event_type"]: s.value for s in enqueued.samples}
        assert label_values == {"write": 10, "delete": 3, "rename": 1}

    def test_zero_values_when_idle(self) -> None:
        """All metrics should be zero when no events have been processed."""
        collector = WriteBufferCollector(_make_observer())
        families = list(collector.collect())
        for family in families:
            for sample in family.samples:
                assert sample.value == 0

    def test_describe_returns_empty(self) -> None:
        """Custom collector convention: describe returns empty."""
        collector = WriteBufferCollector(_make_observer())
        assert list(collector.describe()) == []

    def test_collector_reads_observer_values(self) -> None:
        """Collector should faithfully mirror observer metric values."""
        observer = _make_observer(
            total_flushed=50,
            total_failed=2,
            total_retries=5,
            pending=3,
            flush_count=10,
            flush_duration_sum=1.23,
            flush_batch_size_sum=48,
        )
        collector = WriteBufferCollector(observer)
        families = {f.name: f for f in collector.collect()}

        assert families["nexus_write_buffer_events_flushed_total"].samples[0].value == 50
        assert families["nexus_write_buffer_events_failed_total"].samples[0].value == 2
        assert families["nexus_write_buffer_retries_total"].samples[0].value == 5
        assert families["nexus_write_buffer_pending_events"].samples[0].value == 3
        assert families["nexus_write_buffer_flush_duration_seconds_sum"].samples[0].value == 1.23
        assert families["nexus_write_buffer_flush_duration_seconds_count"].samples[0].value == 10
        assert families["nexus_write_buffer_flush_batch_size_sum"].samples[0].value == 48
        assert families["nexus_write_buffer_flush_batch_size_count"].samples[0].value == 10


# ---------------------------------------------------------------------------
# Integration tests (real WriteBuffer)
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
    from nexus.storage.models import Base

    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine)


class TestWriteBufferCollectorIntegration:
    """Integration tests with real WriteBuffer + in-memory SQLite."""

    def test_collector_reads_real_buffer_after_enqueue(self, session_factory) -> None:
        """Enqueuing events should update per-type counters visible via collector."""
        from nexus.storage.record_store_syncer import BufferedRecordStoreSyncer

        syncer = BufferedRecordStoreSyncer(session_factory, flush_interval_ms=10000)
        collector = WriteBufferCollector(syncer)

        syncer.on_write(_make_metadata(), is_new=True, path="/a.txt")
        syncer.on_delete(path="/b.txt", zone_id="default")
        syncer.on_rename(old_path="/c.txt", new_path="/d.txt")

        families = {f.name: f for f in collector.collect()}
        enqueued = families["nexus_write_buffer_events_enqueued_total"]
        label_values = {s.labels["event_type"]: s.value for s in enqueued.samples}
        assert label_values == {"write": 1, "delete": 1, "rename": 1}
        assert families["nexus_write_buffer_pending_events"].samples[0].value == 3

    def test_collector_reads_real_buffer_after_flush(self, session_factory) -> None:
        """Flushing should update flushed/duration/batch metrics."""
        from nexus.storage.record_store_syncer import BufferedRecordStoreSyncer

        syncer = BufferedRecordStoreSyncer(session_factory, flush_interval_ms=10000)
        syncer.start()
        collector = WriteBufferCollector(syncer)

        syncer.on_write(_make_metadata(path="/f1.txt", etag="h1"), is_new=True, path="/f1.txt")
        syncer.on_write(_make_metadata(path="/f2.txt", etag="h2"), is_new=True, path="/f2.txt")
        syncer.stop(timeout=5.0)

        families = {f.name: f for f in collector.collect()}
        assert families["nexus_write_buffer_events_flushed_total"].samples[0].value == 2
        assert families["nexus_write_buffer_flush_duration_seconds_count"].samples[0].value >= 1
        assert families["nexus_write_buffer_flush_duration_seconds_sum"].samples[0].value > 0
        assert families["nexus_write_buffer_flush_batch_size_sum"].samples[0].value == 2

    def test_collector_tracks_retries_on_failure(self, session_factory) -> None:
        """Transient failures should increment the retry counter."""
        from nexus.storage.write_buffer import WriteBuffer

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

        collector = WriteBufferCollector(MagicMock(metrics=buf.metrics))
        families = {f.name: f for f in collector.collect()}
        assert families["nexus_write_buffer_retries_total"].samples[0].value >= 1

    def test_collector_tracks_failed_events(self) -> None:
        """Exhausting retries should increment the failed counter."""
        from nexus.storage.write_buffer import WriteBuffer

        def always_fail():
            raise RuntimeError("Permanent failure")

        buf = WriteBuffer(always_fail, flush_interval_ms=10000, max_retries=2)
        buf.enqueue_write(_make_metadata(), is_new=True, path="/fail.txt")
        buf._flush_buffer()

        collector = WriteBufferCollector(MagicMock(metrics=buf.metrics))
        families = {f.name: f for f in collector.collect()}
        assert families["nexus_write_buffer_events_failed_total"].samples[0].value == 1
        assert families["nexus_write_buffer_retries_total"].samples[0].value == 2
