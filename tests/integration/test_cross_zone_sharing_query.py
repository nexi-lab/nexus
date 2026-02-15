"""Integration tests for cross-zone sharing query (Issue #904).

Tests the _get_cross_zone_shared_paths() method on SearchService which queries
rebac_tuples for files shared across zone boundaries.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Minimal ReBAC manager fake for cross-zone sharing query
# ---------------------------------------------------------------------------


class FakeReBACManager:
    """Minimal fake that provides _connection(), _create_cursor(), _fix_sql_placeholders().

    Uses an in-memory SQLite database with a rebac_tuples table matching
    the schema used by the real ReBAC manager.
    """

    def __init__(self) -> None:
        self._db = sqlite3.connect(":memory:")
        self._db.row_factory = sqlite3.Row
        self._db.execute("""
            CREATE TABLE rebac_tuples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zone_id TEXT NOT NULL,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                expires_at TEXT
            )
        """)
        self._db.commit()

    @contextmanager
    def _connection(self) -> Any:
        yield self._db

    def _create_cursor(self, conn: Any) -> Any:
        return conn.cursor()

    def _fix_sql_placeholders(self, sql: str) -> str:
        return sql  # SQLite uses ? already

    def add_tuple(
        self,
        zone_id: str,
        subject_type: str,
        subject_id: str,
        relation: str,
        object_type: str,
        object_id: str,
        expires_at: str | None = None,
    ) -> None:
        """Insert a rebac tuple for testing."""
        self._db.execute(
            "INSERT INTO rebac_tuples (zone_id, subject_type, subject_id, "
            "relation, object_type, object_id, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (zone_id, subject_type, subject_id, relation, object_type, object_id, expires_at),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_search_service(rebac_manager: FakeReBACManager | None = None) -> Any:
    """Create a minimal SearchService with a fake metadata store and ReBAC manager."""
    from nexus.storage.raft_metadata_store import RaftMetadataStore

    with tempfile.TemporaryDirectory() as tmpdir:
        metadata_store = RaftMetadataStore.embedded(str(Path(tmpdir) / "meta"), zone_id="default")

        from nexus.services.search_service import SearchService

        svc = SearchService(
            metadata_store=metadata_store,
            rebac_manager=rebac_manager,
            enforce_permissions=False,
        )
        yield svc
        metadata_store.close()


@pytest.fixture
def rebac() -> Generator[FakeReBACManager, None, None]:
    mgr = FakeReBACManager()
    yield mgr
    mgr.close()


@pytest.fixture
def search_svc(rebac: FakeReBACManager) -> Generator[Any, None, None]:
    yield from _make_search_service(rebac)


# ===========================================================================
# Tests
# ===========================================================================


class TestCrossZoneSharingQuery:
    """Tests for SearchService._get_cross_zone_shared_paths()."""

    def test_no_shares_returns_empty(self, search_svc: Any) -> None:
        """Query with no ReBAC tuples returns empty list."""
        result = search_svc._get_cross_zone_shared_paths(
            subject_type="user",
            subject_id="alice",
            zone_id="zone_a",
        )
        assert result == []

    def test_single_cross_zone_share(self, search_svc: Any, rebac: FakeReBACManager) -> None:
        """A shared-viewer tuple from zone_b should return the shared path."""
        rebac.add_tuple(
            zone_id="zone_b",
            subject_type="user",
            subject_id="alice",
            relation="shared-viewer",
            object_type="file",
            object_id="/zone_b/shared/report.pdf",
        )
        result = search_svc._get_cross_zone_shared_paths(
            subject_type="user",
            subject_id="alice",
            zone_id="zone_a",
        )
        assert "/zone_b/shared/report.pdf" in result

    def test_same_zone_share_excluded(self, search_svc: Any, rebac: FakeReBACManager) -> None:
        """Shares within the same zone should NOT be returned (zone_id != condition)."""
        rebac.add_tuple(
            zone_id="zone_a",
            subject_type="user",
            subject_id="alice",
            relation="shared-viewer",
            object_type="file",
            object_id="/zone_a/local/file.txt",
        )
        result = search_svc._get_cross_zone_shared_paths(
            subject_type="user",
            subject_id="alice",
            zone_id="zone_a",
        )
        assert result == []

    def test_expired_share_excluded(self, search_svc: Any, rebac: FakeReBACManager) -> None:
        """Expired tuples should not be returned."""
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        rebac.add_tuple(
            zone_id="zone_b",
            subject_type="user",
            subject_id="alice",
            relation="shared-viewer",
            object_type="file",
            object_id="/zone_b/expired.txt",
            expires_at=past,
        )
        result = search_svc._get_cross_zone_shared_paths(
            subject_type="user",
            subject_id="alice",
            zone_id="zone_a",
        )
        assert result == []

    def test_future_expiry_included(self, search_svc: Any, rebac: FakeReBACManager) -> None:
        """Tuples with future expiry should be returned."""
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        rebac.add_tuple(
            zone_id="zone_b",
            subject_type="user",
            subject_id="alice",
            relation="shared-viewer",
            object_type="file",
            object_id="/zone_b/valid.txt",
            expires_at=future,
        )
        result = search_svc._get_cross_zone_shared_paths(
            subject_type="user",
            subject_id="alice",
            zone_id="zone_a",
        )
        assert "/zone_b/valid.txt" in result

    def test_prefix_filter(self, search_svc: Any, rebac: FakeReBACManager) -> None:
        """Prefix parameter should filter results."""
        rebac.add_tuple(
            zone_id="zone_b",
            subject_type="user",
            subject_id="alice",
            relation="shared-viewer",
            object_type="file",
            object_id="/zone_b/docs/file.txt",
        )
        rebac.add_tuple(
            zone_id="zone_b",
            subject_type="user",
            subject_id="alice",
            relation="shared-viewer",
            object_type="file",
            object_id="/zone_b/other/file.txt",
        )
        result = search_svc._get_cross_zone_shared_paths(
            subject_type="user",
            subject_id="alice",
            zone_id="zone_a",
            prefix="/zone_b/docs/",
        )
        assert "/zone_b/docs/file.txt" in result
        assert "/zone_b/other/file.txt" not in result

    def test_db_error_returns_empty_with_log(
        self, search_svc: Any, rebac: FakeReBACManager
    ) -> None:
        """Database error should be caught and return empty list."""
        # Drop the table to simulate DB error
        rebac._db.execute("DROP TABLE rebac_tuples")
        rebac._db.commit()

        result = search_svc._get_cross_zone_shared_paths(
            subject_type="user",
            subject_id="alice",
            zone_id="zone_a",
        )
        assert result == []

    def test_no_rebac_manager_returns_empty(self) -> None:
        """No ReBAC manager should return empty list immediately."""
        svc_gen = _make_search_service(rebac_manager=None)
        svc = next(svc_gen)
        try:
            result = svc._get_cross_zone_shared_paths(
                subject_type="user",
                subject_id="alice",
                zone_id="zone_a",
            )
            assert result == []
        finally:
            try:
                next(svc_gen)
            except StopIteration:
                pass

    def test_cache_returns_same_result(self, search_svc: Any, rebac: FakeReBACManager) -> None:
        """Second call within TTL should return cached result."""
        rebac.add_tuple(
            zone_id="zone_b",
            subject_type="user",
            subject_id="alice",
            relation="shared-viewer",
            object_type="file",
            object_id="/zone_b/cached.txt",
        )
        result1 = search_svc._get_cross_zone_shared_paths(
            subject_type="user",
            subject_id="alice",
            zone_id="zone_a",
        )
        # Delete the tuple — cached result should still be returned
        rebac._db.execute("DELETE FROM rebac_tuples")
        rebac._db.commit()

        result2 = search_svc._get_cross_zone_shared_paths(
            subject_type="user",
            subject_id="alice",
            zone_id="zone_a",
        )
        assert result1 == result2 == ["/zone_b/cached.txt"]
