"""Unit tests for SyncBacklogStore.

Tests the sync backlog CRUD operations: enqueue, fetch, status transitions,
TTL/cap expiry, and graceful degradation without a database.

Issue #2132: Previously 0% test coverage.
"""

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.services.sync.sync_backlog_store import (
    SyncBacklogEntry,
    SyncBacklogStore,
)
from nexus.storage.models._base import Base

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def engine():
    """Create an in-memory SQLite engine with all tables."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    """Create a session factory bound to the in-memory engine."""
    return sessionmaker(bind=engine)


@pytest.fixture
def record_store(session_factory):
    """Create a minimal record_store-like namespace with session_factory."""
    return SimpleNamespace(session_factory=session_factory)


@pytest.fixture
def store(record_store):
    """Create a SyncBacklogStore backed by in-memory SQLite."""
    return SyncBacklogStore(record_store, is_postgresql=False)


@pytest.fixture
def store_no_db():
    """Create a SyncBacklogStore with no database (record_store=None)."""
    return SyncBacklogStore(None)


# =============================================================================
# enqueue() tests
# =============================================================================


class TestEnqueue:
    """Tests for SyncBacklogStore.enqueue()."""

    def test_enqueue_creates_pending_entry(self, store):
        """Enqueue should create a pending backlog entry and return True."""
        result = store.enqueue(
            path="/docs/readme.md",
            backend_name="gcs",
            zone_id="root",
            operation_type="write",
            content_hash="abc123",
        )
        assert result is True

        entries = store.fetch_pending("gcs", zone_id="root")
        assert len(entries) == 1
        entry = entries[0]
        assert entry.path == "/docs/readme.md"
        assert entry.backend_name == "gcs"
        assert entry.operation_type == "write"
        assert entry.content_hash == "abc123"
        assert entry.status == "pending"
        assert entry.retry_count == 0
        assert entry.max_retries == 5

    def test_enqueue_uses_default_zone_and_operation(self, store):
        """Enqueue should default to ROOT_ZONE_ID and 'write' operation."""
        store.enqueue(path="/file.txt", backend_name="s3")
        entries = store.fetch_pending("s3", zone_id="root")
        assert len(entries) == 1
        assert entries[0].zone_id == "root"
        assert entries[0].operation_type == "write"

    def test_enqueue_coalesces_duplicate(self, store):
        """A second enqueue for the same (path, backend, zone) with status=pending
        should coalesce (upsert) rather than create a duplicate."""
        store.enqueue(path="/file.txt", backend_name="gcs", content_hash="hash1")
        store.enqueue(path="/file.txt", backend_name="gcs", content_hash="hash2")

        entries = store.fetch_pending("gcs", zone_id="root")
        assert len(entries) == 1
        assert entries[0].content_hash == "hash2"

    def test_enqueue_different_backends_separate(self, store):
        """Entries for different backends should not coalesce."""
        store.enqueue(path="/file.txt", backend_name="gcs")
        store.enqueue(path="/file.txt", backend_name="s3")

        gcs_entries = store.fetch_pending("gcs")
        s3_entries = store.fetch_pending("s3")
        assert len(gcs_entries) == 1
        assert len(s3_entries) == 1

    def test_enqueue_rename_operation(self, store):
        """Enqueue a rename operation with new_path."""
        result = store.enqueue(
            path="/old/path.txt",
            backend_name="gcs",
            operation_type="rename",
            new_path="/new/path.txt",
        )
        assert result is True
        entries = store.fetch_pending("gcs")
        assert entries[0].operation_type == "rename"
        assert entries[0].new_path == "/new/path.txt"

    def test_enqueue_returns_false_without_db(self, store_no_db):
        """Enqueue should return False when no database is available."""
        result = store_no_db.enqueue(path="/file.txt", backend_name="gcs")
        assert result is False


# =============================================================================
# fetch_pending() tests
# =============================================================================


class TestFetchPending:
    """Tests for SyncBacklogStore.fetch_pending()."""

    def test_fetch_pending_empty(self, store):
        """Fetch pending on empty store should return empty list."""
        entries = store.fetch_pending("gcs")
        assert entries == []

    def test_fetch_pending_filters_by_backend_and_zone(self, store):
        """Fetch pending should filter by backend_name and zone_id."""
        store.enqueue(path="/a.txt", backend_name="gcs", zone_id="zone1")
        store.enqueue(path="/b.txt", backend_name="gcs", zone_id="zone2")
        store.enqueue(path="/c.txt", backend_name="s3", zone_id="zone1")

        entries = store.fetch_pending("gcs", zone_id="zone1")
        assert len(entries) == 1
        assert entries[0].path == "/a.txt"

    def test_fetch_pending_returns_fifo_order(self, store):
        """Entries should be returned in FIFO order (oldest first)."""
        store.enqueue(path="/first.txt", backend_name="gcs")
        # Small delay to ensure ordering (SQLite datetime resolution)
        time.sleep(0.01)
        store.enqueue(path="/second.txt", backend_name="gcs")

        entries = store.fetch_pending("gcs")
        assert len(entries) == 2
        assert entries[0].path == "/first.txt"
        assert entries[1].path == "/second.txt"

    def test_fetch_pending_respects_limit(self, store):
        """Fetch pending should respect the limit parameter."""
        for i in range(5):
            store.enqueue(path=f"/file{i}.txt", backend_name="gcs")
            time.sleep(0.01)

        entries = store.fetch_pending("gcs", limit=3)
        assert len(entries) == 3

    def test_fetch_pending_returns_frozen_dataclasses(self, store):
        """Returned entries should be immutable SyncBacklogEntry dataclasses."""
        store.enqueue(path="/file.txt", backend_name="gcs")
        entries = store.fetch_pending("gcs")
        assert len(entries) == 1
        assert isinstance(entries[0], SyncBacklogEntry)

        with pytest.raises(AttributeError):
            entries[0].path = "/modified.txt"  # type: ignore[misc]

    def test_fetch_pending_returns_empty_without_db(self, store_no_db):
        """Fetch pending should return empty list without database."""
        assert store_no_db.fetch_pending("gcs") == []


# =============================================================================
# fetch_distinct_backend_zones() tests
# =============================================================================


class TestFetchDistinctBackendZones:
    """Tests for SyncBacklogStore.fetch_distinct_backend_zones()."""

    def test_returns_empty_when_no_entries(self, store):
        """Should return empty list with no pending entries."""
        assert store.fetch_distinct_backend_zones() == []

    def test_returns_distinct_pairs(self, store):
        """Should return distinct (backend, zone) pairs with pending work."""
        store.enqueue(path="/a.txt", backend_name="gcs", zone_id="z1")
        store.enqueue(path="/b.txt", backend_name="gcs", zone_id="z1")
        store.enqueue(path="/c.txt", backend_name="s3", zone_id="z2")

        pairs = store.fetch_distinct_backend_zones()
        assert len(pairs) == 2
        assert ("gcs", "z1") in pairs
        assert ("s3", "z2") in pairs

    def test_returns_empty_without_db(self, store_no_db):
        """Should return empty list when no database is available."""
        assert store_no_db.fetch_distinct_backend_zones() == []


# =============================================================================
# Status transition tests
# =============================================================================


class TestMarkInProgress:
    """Tests for SyncBacklogStore.mark_in_progress()."""

    def test_transitions_pending_to_in_progress(self, store):
        """mark_in_progress should transition a pending entry."""
        store.enqueue(path="/file.txt", backend_name="gcs")
        entries = store.fetch_pending("gcs")
        entry_id = entries[0].id

        result = store.mark_in_progress(entry_id)
        assert result is True

        # Should no longer appear in pending
        remaining = store.fetch_pending("gcs")
        assert len(remaining) == 0

    def test_rejects_non_pending_entry(self, store):
        """mark_in_progress should fail if entry is not in 'pending' status."""
        store.enqueue(path="/file.txt", backend_name="gcs")
        entries = store.fetch_pending("gcs")
        entry_id = entries[0].id

        store.mark_in_progress(entry_id)
        # Try to mark in_progress again (now it's already in_progress)
        result = store.mark_in_progress(entry_id)
        assert result is False

    def test_returns_false_for_nonexistent_id(self, store):
        """mark_in_progress should return False for nonexistent entry."""
        result = store.mark_in_progress("nonexistent-id")
        assert result is False

    def test_returns_false_without_db(self, store_no_db):
        """mark_in_progress should return False without database."""
        result = store_no_db.mark_in_progress("any-id")
        assert result is False


class TestMarkCompleted:
    """Tests for SyncBacklogStore.mark_completed()."""

    def test_transitions_in_progress_to_completed(self, store):
        """mark_completed should transition an in_progress entry."""
        store.enqueue(path="/file.txt", backend_name="gcs")
        entries = store.fetch_pending("gcs")
        entry_id = entries[0].id

        store.mark_in_progress(entry_id)
        result = store.mark_completed(entry_id)
        assert result is True

    def test_rejects_pending_entry(self, store):
        """mark_completed should fail if entry is still pending."""
        store.enqueue(path="/file.txt", backend_name="gcs")
        entries = store.fetch_pending("gcs")
        entry_id = entries[0].id

        result = store.mark_completed(entry_id)
        assert result is False

    def test_returns_false_without_db(self, store_no_db):
        """mark_completed should return False without database."""
        result = store_no_db.mark_completed("any-id")
        assert result is False


class TestMarkFailed:
    """Tests for SyncBacklogStore.mark_failed()."""

    def test_increments_retry_and_stays_pending(self, store):
        """mark_failed should increment retry_count and keep status as pending
        when under max_retries."""
        store.enqueue(path="/file.txt", backend_name="gcs")
        entries = store.fetch_pending("gcs")
        entry_id = entries[0].id

        result = store.mark_failed(entry_id, "connection timeout")
        assert result is True

        # Entry should still be fetchable as pending (retry_count < max_retries)
        entries_after = store.fetch_pending("gcs")
        assert len(entries_after) == 1
        assert entries_after[0].retry_count == 1
        assert entries_after[0].error_message == "connection timeout"

    def test_marks_failed_after_max_retries(self, store):
        """After max_retries are exceeded, status should become 'failed'."""
        store.enqueue(path="/file.txt", backend_name="gcs")
        entries = store.fetch_pending("gcs")
        entry_id = entries[0].id

        # Default max_retries is 5, so fail 5 times
        for i in range(5):
            store.mark_failed(entry_id, f"error {i}")

        # After 5 failures, should no longer appear as pending
        remaining = store.fetch_pending("gcs")
        assert len(remaining) == 0

        # Stats should show a 'failed' entry
        stats = store.get_stats()
        assert stats.get("failed", 0) == 1

    def test_returns_false_for_nonexistent_id(self, store):
        """mark_failed should return False for nonexistent entry."""
        result = store.mark_failed("nonexistent-id", "error")
        assert result is False

    def test_returns_false_without_db(self, store_no_db):
        """mark_failed should return False without database."""
        result = store_no_db.mark_failed("any-id", "error")
        assert result is False


# =============================================================================
# expire_stale() tests
# =============================================================================


class TestExpireStale:
    """Tests for SyncBacklogStore.expire_stale()."""

    def test_no_entries_returns_zero(self, store):
        """expire_stale on empty store should return 0."""
        assert store.expire_stale() == 0

    def test_ttl_expiry(self, store, session_factory):
        """Entries older than TTL should be expired."""
        from nexus.storage.models import SyncBacklogModel

        # Directly insert an old entry via session
        session = session_factory()
        old_time = datetime.now(UTC) - timedelta(hours=25)
        from nexus.lib.db_base import _generate_uuid

        entry = SyncBacklogModel(
            id=_generate_uuid(),
            path="/old.txt",
            backend_name="gcs",
            zone_id="root",
            operation_type="write",
            status="pending",
            retry_count=0,
            max_retries=5,
            created_at=old_time,
            updated_at=old_time,
        )
        session.add(entry)
        session.commit()
        session.close()

        # TTL of 1 hour should expire the 25-hour-old entry
        expired = store.expire_stale(ttl_seconds=3600)
        assert expired == 1

        # Entry should no longer be pending
        entries = store.fetch_pending("gcs")
        assert len(entries) == 0

    def test_fresh_entries_not_expired(self, store):
        """Entries younger than TTL should not be expired."""
        store.enqueue(path="/fresh.txt", backend_name="gcs")

        expired = store.expire_stale(ttl_seconds=86400)
        assert expired == 0

        entries = store.fetch_pending("gcs")
        assert len(entries) == 1

    def test_cap_based_expiry(self, store):
        """When pending count exceeds max_entries, oldest should be expired."""
        for i in range(5):
            store.enqueue(path=f"/file{i}.txt", backend_name="gcs")
            time.sleep(0.01)

        # Cap at 3 entries: oldest 2 should be expired
        expired = store.expire_stale(ttl_seconds=86400, max_entries=3)
        assert expired == 2

        remaining = store.fetch_pending("gcs")
        assert len(remaining) == 3

    def test_combined_ttl_and_cap(self, store, session_factory):
        """Both TTL and cap logic should run in the same call."""
        from nexus.lib.db_base import _generate_uuid
        from nexus.storage.models import SyncBacklogModel

        # Insert one old entry (will be TTL-expired)
        session = session_factory()
        old_time = datetime.now(UTC) - timedelta(hours=25)
        entry = SyncBacklogModel(
            id=_generate_uuid(),
            path="/ancient.txt",
            backend_name="gcs",
            zone_id="root",
            operation_type="write",
            status="pending",
            retry_count=0,
            max_retries=5,
            created_at=old_time,
            updated_at=old_time,
        )
        session.add(entry)
        session.commit()
        session.close()

        # Add 4 fresh entries
        for i in range(4):
            store.enqueue(path=f"/fresh{i}.txt", backend_name="gcs")
            time.sleep(0.01)

        # TTL=1h should expire the ancient entry, cap=3 should expire 1 more
        expired = store.expire_stale(ttl_seconds=3600, max_entries=3)
        assert expired >= 1  # At least the TTL-expired one

    def test_returns_zero_without_db(self, store_no_db):
        """expire_stale should return 0 without database."""
        assert store_no_db.expire_stale() == 0


# =============================================================================
# get_stats() tests
# =============================================================================


class TestGetStats:
    """Tests for SyncBacklogStore.get_stats()."""

    def test_empty_store_returns_empty_dict(self, store):
        """Stats on empty store should return empty dict."""
        assert store.get_stats() == {}

    def test_counts_by_status(self, store):
        """Stats should return count grouped by status."""
        store.enqueue(path="/a.txt", backend_name="gcs")
        store.enqueue(path="/b.txt", backend_name="gcs")

        entries = store.fetch_pending("gcs")
        store.mark_in_progress(entries[0].id)

        stats = store.get_stats()
        assert stats["pending"] == 1
        assert stats["in_progress"] == 1

    def test_filter_by_backend(self, store):
        """Stats should filter by backend_name when provided."""
        store.enqueue(path="/a.txt", backend_name="gcs")
        store.enqueue(path="/b.txt", backend_name="s3")

        gcs_stats = store.get_stats(backend_name="gcs")
        assert gcs_stats.get("pending", 0) == 1

        s3_stats = store.get_stats(backend_name="s3")
        assert s3_stats.get("pending", 0) == 1

    def test_returns_empty_dict_without_db(self, store_no_db):
        """Stats should return empty dict without database."""
        assert store_no_db.get_stats() == {}


# =============================================================================
# Graceful degradation tests
# =============================================================================


class TestGracefulDegradation:
    """Tests for graceful degradation when record_store is None."""

    def test_all_operations_safe_without_db(self, store_no_db):
        """All public methods should return safe defaults without a database."""
        assert store_no_db.enqueue(path="/x.txt", backend_name="gcs") is False
        assert store_no_db.fetch_pending("gcs") == []
        assert store_no_db.fetch_distinct_backend_zones() == []
        assert store_no_db.mark_in_progress("id") is False
        assert store_no_db.mark_completed("id") is False
        assert store_no_db.mark_failed("id", "err") is False
        assert store_no_db.expire_stale() == 0
        assert store_no_db.get_stats() == {}


# =============================================================================
# Full lifecycle test
# =============================================================================


class TestFullLifecycle:
    """End-to-end lifecycle test: enqueue -> in_progress -> completed."""

    def test_happy_path_lifecycle(self, store):
        """Test the full lifecycle of a backlog entry."""
        # 1. Enqueue
        assert store.enqueue(path="/sync-me.txt", backend_name="gcs") is True

        # 2. Fetch pending
        entries = store.fetch_pending("gcs")
        assert len(entries) == 1
        entry = entries[0]
        assert entry.status == "pending"

        # 3. Mark in progress
        assert store.mark_in_progress(entry.id) is True
        assert store.fetch_pending("gcs") == []

        # 4. Mark completed
        assert store.mark_completed(entry.id) is True

        # 5. Verify stats
        stats = store.get_stats()
        assert stats.get("completed", 0) == 1
        assert stats.get("pending", 0) == 0

    def test_retry_lifecycle(self, store):
        """Test the retry path: enqueue -> fail -> retry -> complete."""
        store.enqueue(path="/flaky.txt", backend_name="gcs")
        entries = store.fetch_pending("gcs")
        entry_id = entries[0].id

        # Fail twice (retry_count goes to 1, then 2; stays pending)
        store.mark_failed(entry_id, "timeout 1")
        store.mark_failed(entry_id, "timeout 2")

        # Should still be pending with retry_count=2
        entries = store.fetch_pending("gcs")
        assert len(entries) == 1
        assert entries[0].retry_count == 2

        # Transition to in_progress and complete
        store.mark_in_progress(entry_id)
        store.mark_completed(entry_id)

        stats = store.get_stats()
        assert stats.get("completed", 0) == 1
