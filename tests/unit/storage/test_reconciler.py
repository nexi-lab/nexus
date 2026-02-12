"""Unit tests for the background reconciler (Phase 4.4).

Tests drift detection and repair between SQL SSOT and redb cache.
Uses mock stores to test reconciliation logic without real databases.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from nexus.core._metadata_generated import FileMetadata
from nexus.storage.reconciler import Reconciler, ReconciliationStats


def _meta(path: str, etag: str = "abc", size: int = 100) -> FileMetadata:
    return FileMetadata(
        path=path,
        backend_name="local",
        physical_path=path,
        size=size,
        etag=etag,
        mime_type="text/plain",
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
        version=1,
    )


class TestReconcileOnce:
    def test_no_drift(self) -> None:
        """Both stores have identical entries — no repairs."""
        sql = MagicMock()
        raft = MagicMock()

        entries = [_meta("/a.txt", "h1"), _meta("/b.txt", "h2")]
        sql.list.return_value = entries
        raft.list.return_value = entries

        r = Reconciler(sql, raft)
        stats = r.reconcile_once()

        assert stats.stale_cache_entries == 0
        assert stats.orphaned_sql_entries == 0
        assert stats.field_mismatches == 0
        assert stats.repairs_applied == 0
        assert stats.errors == 0

    def test_stale_cache_entry(self) -> None:
        """File deleted from SQL but still in redb — remove from cache."""
        sql = MagicMock()
        raft = MagicMock()

        sql.list.return_value = [_meta("/a.txt")]
        raft.list.return_value = [_meta("/a.txt"), _meta("/stale.txt")]

        r = Reconciler(sql, raft)
        stats = r.reconcile_once()

        assert stats.stale_cache_entries == 1
        assert stats.repairs_applied == 1
        raft.delete.assert_called_once_with("/stale.txt")

    def test_orphaned_sql_entries(self) -> None:
        """File in SQL but not in redb — log but don't repair (cache miss)."""
        sql = MagicMock()
        raft = MagicMock()

        sql.list.return_value = [_meta("/a.txt"), _meta("/orphan.txt")]
        raft.list.return_value = [_meta("/a.txt")]

        r = Reconciler(sql, raft)
        stats = r.reconcile_once()

        assert stats.orphaned_sql_entries == 1
        assert stats.repairs_applied == 0  # No repair for orphans

    def test_field_mismatch_etag(self) -> None:
        """Etag differs — SQL wins, redb cache is updated."""
        sql = MagicMock()
        raft = MagicMock()

        sql_meta = _meta("/a.txt", etag="sql_hash")
        raft_meta = _meta("/a.txt", etag="stale_hash")

        sql.list.return_value = [sql_meta]
        raft.list.return_value = [raft_meta]

        r = Reconciler(sql, raft)
        stats = r.reconcile_once()

        assert stats.field_mismatches == 1
        assert stats.repairs_applied == 1
        raft.put.assert_called_once_with(sql_meta)

    def test_field_mismatch_size(self) -> None:
        """Size differs — SQL wins."""
        sql = MagicMock()
        raft = MagicMock()

        sql.list.return_value = [_meta("/a.txt", size=200)]
        raft.list.return_value = [_meta("/a.txt", size=100)]

        r = Reconciler(sql, raft)
        stats = r.reconcile_once()

        assert stats.field_mismatches == 1
        assert stats.repairs_applied == 1

    def test_no_raft_store_skips(self) -> None:
        """No raft_store — reconciliation is a no-op."""
        sql = MagicMock()
        r = Reconciler(sql, raft_store=None)
        stats = r.reconcile_once()

        assert stats.repairs_applied == 0
        assert "skipping" in stats.details[0].lower()

    def test_system_paths_skipped(self) -> None:
        """System paths (/__sys__/) are excluded from reconciliation."""
        sql = MagicMock()
        raft = MagicMock()

        sql.list.return_value = []
        # System path in raft should NOT be flagged as stale
        raft.list.return_value = [_meta("/__sys__/zone_rev/default")]

        r = Reconciler(sql, raft)
        stats = r.reconcile_once()

        assert stats.stale_cache_entries == 0
        assert stats.repairs_applied == 0

    def test_meta_keys_skipped(self) -> None:
        """Extended metadata keys (meta:...) are excluded."""
        sql = MagicMock()
        raft = MagicMock()

        sql.list.return_value = []
        raft.list.return_value = [_meta("meta:/a.txt:parsed_text")]

        r = Reconciler(sql, raft)
        stats = r.reconcile_once()

        assert stats.stale_cache_entries == 0

    def test_repair_error_counted(self) -> None:
        """If redb delete fails, error is counted."""
        sql = MagicMock()
        raft = MagicMock()

        sql.list.return_value = []
        raft.list.return_value = [_meta("/stale.txt")]
        raft.delete.side_effect = RuntimeError("redb error")

        r = Reconciler(sql, raft)
        stats = r.reconcile_once()

        assert stats.stale_cache_entries == 1
        assert stats.errors == 1
        assert stats.repairs_applied == 0


class TestReconcilerLifecycle:
    def test_start_stop(self) -> None:
        """Reconciler starts and stops without error."""
        sql = MagicMock()
        sql.list.return_value = []
        raft = MagicMock()
        raft.list.return_value = []

        r = Reconciler(sql, raft, interval_seconds=0.1)
        r.start()
        assert r._thread is not None
        assert r._thread.is_alive()
        r.stop(timeout=2.0)
        assert r._thread is None

    def test_last_stats(self) -> None:
        """last_stats is updated after reconcile_once."""
        sql = MagicMock()
        sql.list.return_value = []
        raft = MagicMock()
        raft.list.return_value = []

        r = Reconciler(sql, raft)
        assert r.last_stats is None
        r.reconcile_once()
        assert r.last_stats is not None
        assert isinstance(r.last_stats, ReconciliationStats)
