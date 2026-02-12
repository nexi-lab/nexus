"""Unit tests for conflict resolution module (Issue #1129, #1130).

Tests the pure functions: detect_conflict() and resolve_conflict().
No database or I/O required — all data is constructed in-memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from nexus.services.change_log_store import ChangeLogEntry
from nexus.services.conflict_resolution import (
    ConflictAbortError,
    ConflictContext,
    ConflictRecord,
    ConflictStatus,
    ConflictStrategy,
    ResolutionOutcome,
    detect_conflict,
    resolve_conflict,
)

# =============================================================================
# Test Fixtures
# =============================================================================


@dataclass
class FakeFileInfo:
    """Minimal FileInfo stand-in for tests."""

    size: int = 1024
    mtime: datetime | None = None
    backend_version: str | None = None
    content_hash: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _entry(
    *,
    mtime: datetime | None = None,
    content_hash: str | None = None,
    backend_version: str | None = None,
    size_bytes: int | None = 1024,
) -> ChangeLogEntry:
    return ChangeLogEntry(
        path="/test/file.txt",
        backend_name="test_backend",
        mtime=mtime,
        content_hash=content_hash,
        backend_version=backend_version,
        size_bytes=size_bytes,
    )


def _ctx(
    *,
    nexus_mtime: datetime | None = None,
    nexus_size: int | None = None,
    backend_mtime: datetime | None = None,
    backend_size: int | None = None,
) -> ConflictContext:
    return ConflictContext(
        nexus_mtime=nexus_mtime,
        nexus_size=nexus_size,
        nexus_content_hash="nexus_hash",
        backend_mtime=backend_mtime,
        backend_size=backend_size,
        backend_content_hash="backend_hash",
        path="/test/file.txt",
        backend_name="test_backend",
        zone_id="default",
    )


# =============================================================================
# detect_conflict Tests
# =============================================================================


class TestDetectConflict:
    """Tests for detect_conflict()."""

    def test_no_conflict_when_only_nexus_changed(self):
        """Backend unchanged, Nexus changed -> no conflict."""
        base_time = _now() - timedelta(hours=1)

        # Nexus changed (new hash), backend unchanged (same version)
        result = detect_conflict(
            nexus_mtime=_now(),
            nexus_content_hash="bbb",
            backend_file_info=FakeFileInfo(
                mtime=base_time,
                backend_version="v1",
                content_hash="aaa",
            ),
            last_synced=_entry(
                mtime=base_time,
                backend_version="v1",
                content_hash="aaa",
            ),
        )
        assert result is False

    def test_no_conflict_when_only_backend_changed(self):
        """Nexus unchanged, backend changed -> no conflict."""
        base_time = _now() - timedelta(hours=1)

        result = detect_conflict(
            nexus_mtime=base_time,
            nexus_content_hash="aaa",
            backend_file_info=FakeFileInfo(
                backend_version="v2",  # Changed
            ),
            last_synced=_entry(
                mtime=base_time,
                content_hash="aaa",
                backend_version="v1",
            ),
        )
        assert result is False

    def test_conflict_when_both_changed(self):
        """Both Nexus and backend changed since last sync -> conflict."""
        base_time = _now() - timedelta(hours=1)

        result = detect_conflict(
            nexus_mtime=_now(),
            nexus_content_hash="bbb",  # Nexus changed
            backend_file_info=FakeFileInfo(
                backend_version="v2",  # Backend changed
            ),
            last_synced=_entry(
                mtime=base_time,
                content_hash="aaa",
                backend_version="v1",
            ),
        )
        assert result is True

    def test_no_conflict_when_neither_changed(self):
        """Neither side changed -> no conflict."""
        base_time = _now() - timedelta(hours=1)

        result = detect_conflict(
            nexus_mtime=base_time,
            nexus_content_hash="aaa",
            backend_file_info=FakeFileInfo(
                backend_version="v1",
            ),
            last_synced=_entry(
                mtime=base_time,
                content_hash="aaa",
                backend_version="v1",
            ),
        )
        assert result is False

    def test_no_last_synced_entry_assumes_first_sync(self):
        """No previous sync record -> first sync, no conflict possible."""
        result = detect_conflict(
            nexus_mtime=_now(),
            nexus_content_hash="abc",
            backend_file_info=FakeFileInfo(backend_version="v1"),
            last_synced=None,
        )
        assert result is False

    def test_conflict_with_null_mtime_falls_back_to_hash(self):
        """When mtime is None, use content_hash for comparison."""
        result = detect_conflict(
            nexus_mtime=None,
            nexus_content_hash="new_hash",  # Changed
            backend_file_info=FakeFileInfo(
                mtime=None,
                backend_version="v2",  # Changed
            ),
            last_synced=_entry(
                mtime=None,
                content_hash="old_hash",
                backend_version="v1",
            ),
        )
        assert result is True


# =============================================================================
# resolve_conflict Tests — Strategy Matrix
# =============================================================================


class TestResolveConflict:
    """Tests for resolve_conflict() with all 6 strategies."""

    # --- ABORT ---
    def test_abort_returns_abort_outcome(self):
        result = resolve_conflict(_ctx(), ConflictStrategy.ABORT)
        assert result == ResolutionOutcome.ABORT

    # --- KEEP_REMOTE ---
    def test_keep_remote_always_returns_backend_wins(self):
        result = resolve_conflict(
            _ctx(nexus_mtime=_now(), backend_mtime=_now() - timedelta(hours=1)),
            ConflictStrategy.KEEP_REMOTE,
        )
        assert result == ResolutionOutcome.BACKEND_WINS

    # --- KEEP_LOCAL ---
    def test_keep_local_always_returns_nexus_wins(self):
        result = resolve_conflict(
            _ctx(backend_mtime=_now(), nexus_mtime=_now() - timedelta(hours=1)),
            ConflictStrategy.KEEP_LOCAL,
        )
        assert result == ResolutionOutcome.NEXUS_WINS

    # --- KEEP_NEWER ---
    def test_keep_newer_nexus_wins_when_newer(self):
        result = resolve_conflict(
            _ctx(nexus_mtime=_now(), backend_mtime=_now() - timedelta(hours=1)),
            ConflictStrategy.KEEP_NEWER,
        )
        assert result == ResolutionOutcome.NEXUS_WINS

    def test_keep_newer_backend_wins_when_newer(self):
        result = resolve_conflict(
            _ctx(nexus_mtime=_now() - timedelta(hours=1), backend_mtime=_now()),
            ConflictStrategy.KEEP_NEWER,
        )
        assert result == ResolutionOutcome.BACKEND_WINS

    def test_keep_newer_tie_nexus_wins(self):
        same_time = _now()
        result = resolve_conflict(
            _ctx(nexus_mtime=same_time, backend_mtime=same_time),
            ConflictStrategy.KEEP_NEWER,
        )
        assert result == ResolutionOutcome.NEXUS_WINS

    def test_keep_newer_nexus_none_backend_wins(self):
        result = resolve_conflict(
            _ctx(nexus_mtime=None, backend_mtime=_now()),
            ConflictStrategy.KEEP_NEWER,
        )
        assert result == ResolutionOutcome.BACKEND_WINS

    def test_keep_newer_backend_none_nexus_wins(self):
        result = resolve_conflict(
            _ctx(nexus_mtime=_now(), backend_mtime=None),
            ConflictStrategy.KEEP_NEWER,
        )
        assert result == ResolutionOutcome.NEXUS_WINS

    def test_keep_newer_both_none_nexus_wins(self):
        result = resolve_conflict(
            _ctx(nexus_mtime=None, backend_mtime=None),
            ConflictStrategy.KEEP_NEWER,
        )
        assert result == ResolutionOutcome.NEXUS_WINS

    # --- KEEP_LARGER ---
    def test_keep_larger_nexus_bigger(self):
        result = resolve_conflict(
            _ctx(nexus_size=2000, backend_size=1000),
            ConflictStrategy.KEEP_LARGER,
        )
        assert result == ResolutionOutcome.NEXUS_WINS

    def test_keep_larger_backend_bigger(self):
        result = resolve_conflict(
            _ctx(nexus_size=500, backend_size=2000),
            ConflictStrategy.KEEP_LARGER,
        )
        assert result == ResolutionOutcome.BACKEND_WINS

    def test_keep_larger_equal_sizes_nexus_wins(self):
        result = resolve_conflict(
            _ctx(nexus_size=1000, backend_size=1000),
            ConflictStrategy.KEEP_LARGER,
        )
        assert result == ResolutionOutcome.NEXUS_WINS

    def test_keep_larger_nexus_none_treated_as_zero(self):
        result = resolve_conflict(
            _ctx(nexus_size=None, backend_size=100),
            ConflictStrategy.KEEP_LARGER,
        )
        assert result == ResolutionOutcome.BACKEND_WINS

    def test_keep_larger_backend_none_treated_as_zero(self):
        result = resolve_conflict(
            _ctx(nexus_size=100, backend_size=None),
            ConflictStrategy.KEEP_LARGER,
        )
        assert result == ResolutionOutcome.NEXUS_WINS

    def test_keep_larger_both_none_nexus_wins(self):
        result = resolve_conflict(
            _ctx(nexus_size=None, backend_size=None),
            ConflictStrategy.KEEP_LARGER,
        )
        assert result == ResolutionOutcome.NEXUS_WINS

    def test_keep_larger_zero_byte_files_nexus_wins(self):
        result = resolve_conflict(
            _ctx(nexus_size=0, backend_size=0),
            ConflictStrategy.KEEP_LARGER,
        )
        assert result == ResolutionOutcome.NEXUS_WINS

    # --- RENAME_CONFLICT ---
    def test_rename_conflict_returns_rename_outcome(self):
        result = resolve_conflict(_ctx(), ConflictStrategy.RENAME_CONFLICT)
        assert result == ResolutionOutcome.RENAME_CONFLICT


# =============================================================================
# ConflictContext Tests
# =============================================================================


class TestConflictContext:
    """Tests for ConflictContext dataclass."""

    def test_context_is_frozen(self):
        ctx = _ctx()
        with pytest.raises(AttributeError):
            ctx.path = "/changed"  # type: ignore[misc]

    def test_context_stores_all_fields(self):
        now = _now()
        ctx = ConflictContext(
            nexus_mtime=now,
            nexus_size=100,
            nexus_content_hash="abc",
            backend_mtime=now,
            backend_size=200,
            backend_content_hash="def",
            path="/test.txt",
            backend_name="gcs",
            zone_id="z1",
        )
        assert ctx.nexus_size == 100
        assert ctx.backend_size == 200
        assert ctx.backend_name == "gcs"
        assert ctx.zone_id == "z1"


# =============================================================================
# ConflictRecord Tests
# =============================================================================


class TestConflictRecord:
    """Tests for ConflictRecord dataclass."""

    def test_record_is_frozen(self):
        now = _now()
        record = ConflictRecord(
            id="test-id",
            path="/test.txt",
            backend_name="gcs",
            zone_id="default",
            strategy=ConflictStrategy.KEEP_NEWER,
            outcome=ResolutionOutcome.NEXUS_WINS,
            nexus_content_hash="abc",
            nexus_mtime=now,
            nexus_size=100,
            backend_content_hash="def",
            backend_mtime=now,
            backend_size=200,
            conflict_copy_path=None,
            status=ConflictStatus.AUTO_RESOLVED,
            resolved_at=now,
        )
        with pytest.raises(AttributeError):
            record.status = "changed"  # type: ignore[misc]


# =============================================================================
# ConflictAbortError Tests
# =============================================================================


class TestConflictAbortError:
    """Tests for ConflictAbortError exception."""

    def test_is_exception(self):
        err = ConflictAbortError("test")
        assert isinstance(err, Exception)
        assert str(err) == "test"
