"""Unit tests for conflict resolution module (Issue #1129).

Tests the pure functions: detect_conflict() and resolve_conflict().
No database or I/O required — all data is constructed in-memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from nexus.services.change_log_store import ChangeLogEntry
from nexus.services.conflict_resolution import detect_conflict, resolve_conflict

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
# resolve_conflict Tests
# =============================================================================


class TestResolveConflict:
    """Tests for resolve_conflict()."""

    def test_lww_nexus_wins_when_newer(self):
        """Nexus mtime is newer -> nexus_wins."""
        result = resolve_conflict(
            nexus_mtime=_now(),
            backend_mtime=_now() - timedelta(hours=1),
            policy="lww",
        )
        assert result == "nexus_wins"

    def test_lww_backend_wins_when_newer(self):
        """Backend mtime is newer -> backend_wins."""
        result = resolve_conflict(
            nexus_mtime=_now() - timedelta(hours=1),
            backend_mtime=_now(),
            policy="lww",
        )
        assert result == "backend_wins"

    def test_lww_same_timestamp_nexus_wins(self):
        """Same mtime -> tie-breaker: nexus_wins."""
        same_time = _now()
        result = resolve_conflict(
            nexus_mtime=same_time,
            backend_mtime=same_time,
            policy="lww",
        )
        assert result == "nexus_wins"

    def test_fork_resolution_always_forks(self):
        """Fork policy always returns fork regardless of timestamps."""
        result = resolve_conflict(
            nexus_mtime=_now(),
            backend_mtime=_now() - timedelta(hours=1),
            policy="fork",
        )
        assert result == "fork"

    def test_lww_nexus_none_mtime_backend_wins(self):
        """Nexus mtime is None -> backend_wins."""
        result = resolve_conflict(
            nexus_mtime=None,
            backend_mtime=_now(),
            policy="lww",
        )
        assert result == "backend_wins"

    def test_lww_backend_none_mtime_nexus_wins(self):
        """Backend mtime is None -> nexus_wins."""
        result = resolve_conflict(
            nexus_mtime=_now(),
            backend_mtime=None,
            policy="lww",
        )
        assert result == "nexus_wins"

    def test_lww_both_none_mtime_nexus_wins(self):
        """Both mtimes None -> nexus_wins (default)."""
        result = resolve_conflict(
            nexus_mtime=None,
            backend_mtime=None,
            policy="lww",
        )
        assert result == "nexus_wins"
