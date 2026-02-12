"""Unit tests for ConflictLogStore (Issue #1130).

Tests CRUD operations, pagination, filtering, manual resolution,
expiry, and stats using an in-memory SQLite database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.services.conflict_log_store import ConflictLogStore
from nexus.services.conflict_resolution import (
    ConflictRecord,
    ConflictStatus,
    ConflictStrategy,
    ResolutionOutcome,
)
from nexus.storage.models import Base


def _now() -> datetime:
    return datetime.now(UTC)


def _make_record(
    *,
    record_id: str = "test-id-1",
    path: str = "/test/file.txt",
    backend_name: str = "gcs",
    zone_id: str = "default",
    strategy: ConflictStrategy = ConflictStrategy.KEEP_NEWER,
    outcome: ResolutionOutcome = ResolutionOutcome.NEXUS_WINS,
    status: ConflictStatus = ConflictStatus.AUTO_RESOLVED,
    resolved_at: datetime | None = None,
) -> ConflictRecord:
    now = resolved_at or _now()
    return ConflictRecord(
        id=record_id,
        path=path,
        backend_name=backend_name,
        zone_id=zone_id,
        strategy=strategy,
        outcome=outcome,
        nexus_content_hash="abc123",
        nexus_mtime=now,
        nexus_size=1024,
        backend_content_hash="def456",
        backend_mtime=now - timedelta(hours=1),
        backend_size=2048,
        conflict_copy_path=None,
        status=status,
        resolved_at=now,
    )


@pytest.fixture
def store():
    """ConflictLogStore backed by in-memory SQLite."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    gw = MagicMock()
    gw.session_factory = sessionmaker(bind=engine)
    return ConflictLogStore(gw)


# =============================================================================
# log_conflict Tests
# =============================================================================


class TestLogConflict:
    """Tests for log_conflict()."""

    def test_log_creates_entry(self, store: ConflictLogStore):
        record = _make_record()
        result_id = store.log_conflict(record)
        assert result_id == "test-id-1"

        # Verify we can read it back
        fetched = store.get_conflict("test-id-1")
        assert fetched is not None
        assert fetched.path == "/test/file.txt"
        assert fetched.strategy == ConflictStrategy.KEEP_NEWER
        assert fetched.outcome == ResolutionOutcome.NEXUS_WINS

    def test_log_preserves_all_fields(self, store: ConflictLogStore):
        now = _now()
        record = ConflictRecord(
            id="full-record",
            path="/deep/path/file.txt",
            backend_name="s3",
            zone_id="zone-42",
            strategy=ConflictStrategy.RENAME_CONFLICT,
            outcome=ResolutionOutcome.RENAME_CONFLICT,
            nexus_content_hash="nx_hash",
            nexus_mtime=now,
            nexus_size=500,
            backend_content_hash="bk_hash",
            backend_mtime=now - timedelta(minutes=30),
            backend_size=600,
            conflict_copy_path="/deep/path/file.sync-conflict-20260211-s3.txt",
            status=ConflictStatus.AUTO_RESOLVED,
            resolved_at=now,
        )
        store.log_conflict(record)

        fetched = store.get_conflict("full-record")
        assert fetched is not None
        assert fetched.nexus_content_hash == "nx_hash"
        assert fetched.nexus_size == 500
        assert fetched.backend_content_hash == "bk_hash"
        assert fetched.backend_size == 600
        assert fetched.conflict_copy_path == "/deep/path/file.sync-conflict-20260211-s3.txt"
        assert fetched.zone_id == "zone-42"


# =============================================================================
# list_conflicts Tests
# =============================================================================


class TestListConflicts:
    """Tests for list_conflicts()."""

    def test_list_returns_all_by_default(self, store: ConflictLogStore):
        for i in range(3):
            store.log_conflict(_make_record(record_id=f"id-{i}"))

        results = store.list_conflicts()
        assert len(results) == 3

    def test_list_with_pagination(self, store: ConflictLogStore):
        for i in range(5):
            store.log_conflict(_make_record(record_id=f"id-{i}"))

        page1 = store.list_conflicts(limit=2, offset=0)
        page2 = store.list_conflicts(limit=2, offset=2)
        page3 = store.list_conflicts(limit=2, offset=4)

        assert len(page1) == 2
        assert len(page2) == 2
        assert len(page3) == 1

    def test_list_filter_by_status(self, store: ConflictLogStore):
        store.log_conflict(_make_record(record_id="auto-1", status=ConflictStatus.AUTO_RESOLVED))
        store.log_conflict(
            _make_record(record_id="pending-1", status=ConflictStatus.MANUAL_PENDING)
        )

        auto = store.list_conflicts(status=ConflictStatus.AUTO_RESOLVED)
        pending = store.list_conflicts(status=ConflictStatus.MANUAL_PENDING)

        assert len(auto) == 1
        assert auto[0].id == "auto-1"
        assert len(pending) == 1
        assert pending[0].id == "pending-1"

    def test_list_filter_by_backend_name(self, store: ConflictLogStore):
        store.log_conflict(_make_record(record_id="gcs-1", backend_name="gcs"))
        store.log_conflict(_make_record(record_id="s3-1", backend_name="s3"))

        gcs = store.list_conflicts(backend_name="gcs")
        s3 = store.list_conflicts(backend_name="s3")

        assert len(gcs) == 1
        assert gcs[0].backend_name == "gcs"
        assert len(s3) == 1
        assert s3[0].backend_name == "s3"

    def test_list_filter_by_zone_id(self, store: ConflictLogStore):
        store.log_conflict(_make_record(record_id="z1-1", zone_id="zone-1"))
        store.log_conflict(_make_record(record_id="z2-1", zone_id="zone-2"))

        z1 = store.list_conflicts(zone_id="zone-1")
        assert len(z1) == 1
        assert z1[0].zone_id == "zone-1"


# =============================================================================
# get_conflict Tests
# =============================================================================


class TestGetConflict:
    """Tests for get_conflict()."""

    def test_get_found(self, store: ConflictLogStore):
        store.log_conflict(_make_record(record_id="found-id"))

        result = store.get_conflict("found-id")
        assert result is not None
        assert result.id == "found-id"

    def test_get_not_found(self, store: ConflictLogStore):
        result = store.get_conflict("nonexistent-id")
        assert result is None


# =============================================================================
# resolve_conflict_manually Tests
# =============================================================================


class TestResolveConflictManually:
    """Tests for resolve_conflict_manually()."""

    def test_resolve_updates_status(self, store: ConflictLogStore):
        store.log_conflict(
            _make_record(record_id="pending-1", status=ConflictStatus.MANUAL_PENDING)
        )

        result = store.resolve_conflict_manually("pending-1", ResolutionOutcome.NEXUS_WINS)
        assert result is True

        fetched = store.get_conflict("pending-1")
        assert fetched is not None
        assert fetched.status == "manually_resolved"
        assert fetched.outcome == ResolutionOutcome.NEXUS_WINS

    def test_resolve_returns_false_for_non_pending(self, store: ConflictLogStore):
        store.log_conflict(_make_record(record_id="auto-1", status=ConflictStatus.AUTO_RESOLVED))

        result = store.resolve_conflict_manually("auto-1", ResolutionOutcome.BACKEND_WINS)
        assert result is False

    def test_resolve_returns_false_for_not_found(self, store: ConflictLogStore):
        result = store.resolve_conflict_manually("ghost-id", ResolutionOutcome.NEXUS_WINS)
        assert result is False


# =============================================================================
# expire_stale Tests
# =============================================================================


class TestExpireStale:
    """Tests for expire_stale()."""

    def test_expire_by_ttl(self, store: ConflictLogStore):
        old_time = _now() - timedelta(days=60)
        store.log_conflict(_make_record(record_id="old-1", resolved_at=old_time))
        store.log_conflict(_make_record(record_id="new-1"))

        expired = store.expire_stale(ttl_seconds=86400)  # 1 day
        assert expired == 1

        # Old record should be gone
        assert store.get_conflict("old-1") is None
        # New record should remain
        assert store.get_conflict("new-1") is not None

    def test_expire_by_cap(self, store: ConflictLogStore):
        for i in range(5):
            store.log_conflict(_make_record(record_id=f"cap-{i}"))

        expired = store.expire_stale(ttl_seconds=999999999, max_entries=3)
        assert expired == 2

        remaining = store.list_conflicts()
        assert len(remaining) == 3


# =============================================================================
# get_stats Tests
# =============================================================================


class TestGetStats:
    """Tests for get_stats()."""

    def test_stats_counts_by_status(self, store: ConflictLogStore):
        store.log_conflict(_make_record(record_id="a1", status=ConflictStatus.AUTO_RESOLVED))
        store.log_conflict(_make_record(record_id="a2", status=ConflictStatus.AUTO_RESOLVED))
        store.log_conflict(_make_record(record_id="p1", status=ConflictStatus.MANUAL_PENDING))

        stats = store.get_stats()
        assert stats["auto_resolved"] == 2
        assert stats["manual_pending"] == 1
        assert stats["total"] == 3

    def test_stats_empty_store(self, store: ConflictLogStore):
        stats = store.get_stats()
        assert stats["total"] == 0
