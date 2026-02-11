"""Unit tests for SyncBacklogStore (Issue #1129).

Uses an in-memory SQLite database for fast, isolated testing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.services.sync_backlog_store import SyncBacklogStore
from nexus.storage.models import Base, SyncBacklogModel

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def db_session_factory():
    """Create an in-memory SQLite database with all tables."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def mock_gateway(db_session_factory):
    """Create a mock gateway with real SQLite session factory."""
    gw = MagicMock()
    gw.session_factory = db_session_factory
    return gw


@pytest.fixture
def store(mock_gateway):
    """Create a SyncBacklogStore backed by in-memory SQLite."""
    return SyncBacklogStore(mock_gateway)


# =============================================================================
# Tests
# =============================================================================


class TestEnqueue:
    """Tests for enqueue (upsert coalescing)."""

    def test_enqueue_creates_pending_entry(self, store, db_session_factory):
        """Enqueue creates a new pending entry."""
        result = store.enqueue(
            path="/test/file.txt",
            backend_name="gcs",
            zone_id="default",
            operation_type="write",
            content_hash="abc123",
        )
        assert result is True

        entries = store.fetch_pending("gcs", "default")
        assert len(entries) == 1
        assert entries[0].path == "/test/file.txt"
        assert entries[0].status == "pending"
        assert entries[0].operation_type == "write"
        assert entries[0].content_hash == "abc123"

    def test_enqueue_coalesces_duplicate_pending(self, store):
        """Multiple enqueues for same path coalesce into one entry."""
        store.enqueue("/test/file.txt", "gcs", "default", "write", content_hash="v1")
        store.enqueue("/test/file.txt", "gcs", "default", "write", content_hash="v2")

        entries = store.fetch_pending("gcs", "default")
        assert len(entries) == 1
        assert entries[0].content_hash == "v2"  # Latest wins

    def test_enqueue_different_paths_separate_entries(self, store):
        """Different paths create separate entries."""
        store.enqueue("/test/a.txt", "gcs", "default", "write")
        store.enqueue("/test/b.txt", "gcs", "default", "write")

        entries = store.fetch_pending("gcs", "default")
        assert len(entries) == 2


class TestFetchPending:
    """Tests for fetch_pending (FIFO ordering)."""

    def test_fetch_pending_returns_fifo_order(self, store):
        """Entries are returned in creation order (FIFO)."""
        store.enqueue("/test/first.txt", "gcs", "default", "write")
        store.enqueue("/test/second.txt", "gcs", "default", "write")
        store.enqueue("/test/third.txt", "gcs", "default", "write")

        entries = store.fetch_pending("gcs", "default")
        paths = [e.path for e in entries]
        assert paths == ["/test/first.txt", "/test/second.txt", "/test/third.txt"]

    def test_fetch_pending_respects_limit(self, store):
        """Limit caps the number of returned entries."""
        for i in range(10):
            store.enqueue(f"/test/file{i}.txt", "gcs", "default", "write")

        entries = store.fetch_pending("gcs", "default", limit=3)
        assert len(entries) == 3

    def test_fetch_pending_filters_by_backend(self, store):
        """Only entries for the requested backend are returned."""
        store.enqueue("/test/a.txt", "gcs", "default", "write")
        store.enqueue("/test/b.txt", "s3", "default", "write")

        gcs_entries = store.fetch_pending("gcs", "default")
        assert len(gcs_entries) == 1
        assert gcs_entries[0].backend_name == "gcs"


class TestStatusTransitions:
    """Tests for mark_in_progress, mark_completed, mark_failed."""

    def test_mark_in_progress_transitions_status(self, store):
        """Pending -> in_progress transition works."""
        store.enqueue("/test/file.txt", "gcs", "default", "write")
        entries = store.fetch_pending("gcs", "default")
        entry_id = entries[0].id

        result = store.mark_in_progress(entry_id)
        assert result is True

        # Should no longer appear in pending
        remaining = store.fetch_pending("gcs", "default")
        assert len(remaining) == 0

    def test_mark_completed_transitions_status(self, store):
        """In_progress -> completed transition works."""
        store.enqueue("/test/file.txt", "gcs", "default", "write")
        entries = store.fetch_pending("gcs", "default")
        entry_id = entries[0].id

        store.mark_in_progress(entry_id)
        result = store.mark_completed(entry_id)
        assert result is True

    def test_mark_failed_increments_retry_count(self, store, db_session_factory):
        """Failed attempt increments retry_count and returns to pending."""
        store.enqueue("/test/file.txt", "gcs", "default", "write")
        entries = store.fetch_pending("gcs", "default")
        entry_id = entries[0].id

        store.mark_in_progress(entry_id)
        store.mark_failed(entry_id, "Network timeout")

        # Should return to pending with incremented retry
        session = db_session_factory()
        row = session.query(SyncBacklogModel).filter_by(id=entry_id).first()
        assert row.retry_count == 1
        assert row.status == "pending"
        assert row.error_message == "Network timeout"
        session.close()

    def test_mark_failed_exceeds_max_retries(self, store, db_session_factory):
        """After max_retries, status changes to 'failed'."""
        store.enqueue("/test/file.txt", "gcs", "default", "write")
        entries = store.fetch_pending("gcs", "default")
        entry_id = entries[0].id

        # Default max_retries is 5, so fail 5 times
        for i in range(5):
            store.mark_in_progress(entry_id)
            store.mark_failed(entry_id, f"Attempt {i + 1}")

        session = db_session_factory()
        row = session.query(SyncBacklogModel).filter_by(id=entry_id).first()
        assert row.retry_count == 5
        assert row.status == "failed"
        session.close()


class TestExpireStale:
    """Tests for expire_stale (TTL + cap-based expiry)."""

    def test_expire_stale_by_ttl(self, store, db_session_factory):
        """Entries older than TTL are expired."""
        # Insert an old entry directly
        session = db_session_factory()
        old_time = datetime.now(UTC) - timedelta(days=2)
        entry = SyncBacklogModel(
            path="/test/old.txt",
            backend_name="gcs",
            zone_id="default",
            operation_type="write",
            status="pending",
            created_at=old_time,
            updated_at=old_time,
        )
        session.add(entry)
        session.commit()
        session.close()

        expired = store.expire_stale(ttl_seconds=86400)  # 1 day
        assert expired >= 1

    def test_expire_stale_by_max_entries(self, store, db_session_factory):
        """When pending count > max_entries, oldest are expired."""
        for i in range(5):
            store.enqueue(f"/test/file{i}.txt", "gcs", "default", "write")

        expired = store.expire_stale(ttl_seconds=999999, max_entries=2)
        assert expired == 3  # 5 - 2 = 3 expired

        remaining = store.fetch_pending("gcs", "default")
        assert len(remaining) == 2


class TestGetStats:
    """Tests for get_stats."""

    def test_stats_breakdown_by_status(self, store):
        """Stats correctly count entries by status."""
        store.enqueue("/test/a.txt", "gcs", "default", "write")
        store.enqueue("/test/b.txt", "gcs", "default", "write")

        entries = store.fetch_pending("gcs", "default")
        store.mark_in_progress(entries[0].id)

        stats = store.get_stats()
        assert stats.get("pending", 0) == 1
        assert stats.get("in_progress", 0) == 1
