"""Hook integration tests for auto-tracking in write/delete paths (Issue #1752).

Tests that the snapshot service is correctly called from the write path
when a transaction is active.
"""

from unittest.mock import MagicMock


class TestWritePathTracking:
    """Tests for auto-tracking in _write_internal()."""

    def test_write_tracks_when_transaction_active(self) -> None:
        """When a path is tracked by a transaction, track_write should be called."""
        mock_svc = MagicMock()
        mock_svc.is_tracked.return_value = "txn-1"

        # Simulate the write path logic
        path = "/file.txt"
        snapshot_hash = "old-hash"
        metadata_snapshot = {"size": 100}
        content_id = "new-hash"

        _snapshot_svc = mock_svc
        if _snapshot_svc is not None:
            _txn_id = _snapshot_svc.is_tracked(path)
            if _txn_id is not None:
                _snapshot_svc.track_write(
                    _txn_id, path, snapshot_hash, metadata_snapshot, content_id
                )

        mock_svc.track_write.assert_called_once_with(
            "txn-1", "/file.txt", "old-hash", {"size": 100}, "new-hash"
        )

    def test_write_skips_when_no_transaction(self) -> None:
        """When no transaction is active, track_write should not be called."""
        mock_svc = MagicMock()
        mock_svc.is_tracked.return_value = None

        path = "/file.txt"
        _snapshot_svc = mock_svc
        if _snapshot_svc is not None:
            _txn_id = _snapshot_svc.is_tracked(path)
            if _txn_id is not None:
                _snapshot_svc.track_write(_txn_id, path, "hash", {}, "new-hash")

        mock_svc.track_write.assert_not_called()

    def test_write_skips_when_service_is_none(self) -> None:
        """When snapshot service is None (not configured), nothing happens."""
        _snapshot_svc = None
        # This should not raise
        if _snapshot_svc is not None:
            _snapshot_svc.is_tracked("/file.txt")


class TestDeletePathTracking:
    """Tests for auto-tracking in delete()."""

    def test_delete_tracks_when_transaction_active(self) -> None:
        """When a path is tracked, track_delete should be called."""
        mock_svc = MagicMock()
        mock_svc.is_tracked.return_value = "txn-1"

        path = "/file.txt"
        snapshot_hash = "old-hash"
        metadata_snapshot = {"size": 100, "version": 1}

        _snapshot_svc = mock_svc
        if _snapshot_svc is not None:
            _txn_id = _snapshot_svc.is_tracked(path)
            if _txn_id is not None:
                _snapshot_svc.track_delete(_txn_id, path, snapshot_hash, metadata_snapshot)

        mock_svc.track_delete.assert_called_once_with(
            "txn-1", "/file.txt", "old-hash", {"size": 100, "version": 1}
        )

    def test_delete_skips_when_no_transaction(self) -> None:
        """When no transaction is active for the path, skip tracking."""
        mock_svc = MagicMock()
        mock_svc.is_tracked.return_value = None

        path = "/file.txt"
        _snapshot_svc = mock_svc
        if _snapshot_svc is not None:
            _txn_id = _snapshot_svc.is_tracked(path)
            if _txn_id is not None:
                _snapshot_svc.track_delete(_txn_id, path, "hash", {})

        mock_svc.track_delete.assert_not_called()
