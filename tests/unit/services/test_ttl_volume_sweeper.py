"""Tests for TTL volume sweeper (Issue #3405).

Tests the TTLVolumeSweeper background service including:
- Normal sweep operation
- Failure injection (transport errors)
- Start/stop lifecycle
- Idempotent behavior
- Metastore cleanup
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from nexus.services.ttl_volume_sweeper import TTLVolumeSweeper


@pytest.fixture
def mock_transport():
    transport = MagicMock()
    transport.expire_ttl_volumes.return_value = []
    transport.rotate_ttl_volumes.return_value = 0
    transport.flush_expired_index.return_value = 0
    return transport


class TestSweeperLifecycle:
    """Test start/stop behavior."""

    @pytest.mark.asyncio
    async def test_start_stop(self, mock_transport) -> None:
        sweeper = TTLVolumeSweeper(mock_transport, interval=0.1)
        assert not sweeper.is_running

        await sweeper.start()
        assert sweeper.is_running

        await sweeper.stop()
        assert not sweeper.is_running

    @pytest.mark.asyncio
    async def test_double_start_is_noop(self, mock_transport) -> None:
        sweeper = TTLVolumeSweeper(mock_transport, interval=0.1)
        await sweeper.start()
        await sweeper.start()  # should not crash or create extra tasks
        assert sweeper.is_running
        await sweeper.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start(self, mock_transport) -> None:
        sweeper = TTLVolumeSweeper(mock_transport, interval=0.1)
        await sweeper.stop()  # should not crash
        assert not sweeper.is_running


class TestSweepOnce:
    """Test single sweep cycle."""

    @pytest.mark.asyncio
    async def test_sweep_calls_expire_and_rotate(self, mock_transport) -> None:
        mock_transport.expire_ttl_volumes.return_value = [("1m", 5)]
        mock_transport.rotate_ttl_volumes.return_value = 1

        sweeper = TTLVolumeSweeper(mock_transport)
        entries, sealed = await sweeper.sweep_once()

        assert entries == 5
        assert sealed == 1
        mock_transport.expire_ttl_volumes.assert_called_once()
        mock_transport.rotate_ttl_volumes.assert_called_once()

    @pytest.mark.asyncio
    async def test_sweep_no_entries(self, mock_transport) -> None:
        sweeper = TTLVolumeSweeper(mock_transport)
        entries, sealed = await sweeper.sweep_once()
        assert entries == 0
        assert sealed == 0

    @pytest.mark.asyncio
    async def test_sweep_multiple_buckets(self, mock_transport) -> None:
        mock_transport.expire_ttl_volumes.return_value = [
            ("1m", 10),
            ("5m", 5),
            ("1h", 2),
        ]
        sweeper = TTLVolumeSweeper(mock_transport)
        entries, sealed = await sweeper.sweep_once()
        assert entries == 17


class TestSweeperFailureInjection:
    """Test sweeper behavior under failures."""

    @pytest.mark.asyncio
    async def test_expire_failure_doesnt_crash(self, mock_transport) -> None:
        """Expiry failure should not prevent rotation from running."""
        mock_transport.expire_ttl_volumes.side_effect = OSError("Permission denied")
        mock_transport.rotate_ttl_volumes.return_value = 1

        sweeper = TTLVolumeSweeper(mock_transport)
        entries, sealed = await sweeper.sweep_once()

        assert entries == 0  # failed
        assert sealed == 1  # rotation still ran

    @pytest.mark.asyncio
    async def test_rotate_failure_doesnt_crash(self, mock_transport) -> None:
        """Rotation failure should not prevent sweep_once from returning."""
        mock_transport.expire_ttl_volumes.return_value = [("1m", 3)]
        mock_transport.rotate_ttl_volumes.side_effect = OSError("Disk full")

        sweeper = TTLVolumeSweeper(mock_transport)
        entries, sealed = await sweeper.sweep_once()

        assert entries == 3
        assert sealed == 0  # failed

    @pytest.mark.asyncio
    async def test_both_fail_still_returns(self, mock_transport) -> None:
        mock_transport.expire_ttl_volumes.side_effect = RuntimeError("boom")
        mock_transport.rotate_ttl_volumes.side_effect = RuntimeError("also boom")

        sweeper = TTLVolumeSweeper(mock_transport)
        entries, sealed = await sweeper.sweep_once()
        assert entries == 0
        assert sealed == 0

    @pytest.mark.asyncio
    async def test_sweep_loop_continues_after_failure(self, mock_transport) -> None:
        """The background loop should continue even after sweep_once fails."""
        call_count = 0

        def expire_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first call fails")
            return [("1m", 1)]

        mock_transport.expire_ttl_volumes.side_effect = expire_side_effect

        sweeper = TTLVolumeSweeper(mock_transport, interval=0.05)
        await sweeper.start()
        await asyncio.sleep(0.2)  # wait for a few cycles
        await sweeper.stop()

        # Should have called expire multiple times (recovered from failure)
        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_idempotent_expire(self, mock_transport) -> None:
        """Multiple sweeps should be safe — expire is idempotent."""
        mock_transport.expire_ttl_volumes.return_value = [("1m", 5)]

        sweeper = TTLVolumeSweeper(mock_transport)
        await sweeper.sweep_once()
        await sweeper.sweep_once()
        await sweeper.sweep_once()

        assert mock_transport.expire_ttl_volumes.call_count == 3


class TestMetastoreCleanup:
    """Test metastore cleanup for expired TTL entries."""

    def _make_meta(self, path: str, ttl: float, modified_minutes_ago: float):
        """Create a mock FileMetadata with TTL and modified_at."""
        meta = MagicMock()
        meta.path = path
        meta.ttl_seconds = ttl
        meta.modified_at = datetime.now(UTC) - timedelta(minutes=modified_minutes_ago)
        return meta

    @pytest.mark.asyncio
    async def test_cleanup_deletes_expired_entries(self, mock_transport) -> None:
        metastore = MagicMock()
        # Two expired entries (ttl=60s, modified 10 min ago) + one live
        metastore.list_iter.return_value = [
            self._make_meta("/tmp/a.txt", ttl=60.0, modified_minutes_ago=10),
            self._make_meta("/tmp/b.txt", ttl=60.0, modified_minutes_ago=10),
            self._make_meta(
                "/tmp/c.txt", ttl=3600.0, modified_minutes_ago=10
            ),  # 1h TTL, still live
        ]

        sweeper = TTLVolumeSweeper(mock_transport, metastore=metastore)
        await sweeper.sweep_once()

        metastore.delete_batch.assert_called_once()
        deleted_paths = metastore.delete_batch.call_args[0][0]
        assert "/tmp/a.txt" in deleted_paths
        assert "/tmp/b.txt" in deleted_paths
        assert "/tmp/c.txt" not in deleted_paths

    @pytest.mark.asyncio
    async def test_cleanup_skips_permanent_entries(self, mock_transport) -> None:
        metastore = MagicMock()
        metastore.list_iter.return_value = [
            self._make_meta("/docs/readme.md", ttl=0.0, modified_minutes_ago=999),  # permanent
        ]

        sweeper = TTLVolumeSweeper(mock_transport, metastore=metastore)
        await sweeper.sweep_once()

        metastore.delete_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_no_metastore(self, mock_transport) -> None:
        """No metastore = no crash, no cleanup."""
        sweeper = TTLVolumeSweeper(mock_transport, metastore=None)
        entries, sealed = await sweeper.sweep_once()
        assert entries == 0

    @pytest.mark.asyncio
    async def test_cleanup_failure_doesnt_crash(self, mock_transport) -> None:
        metastore = MagicMock()
        metastore.list_iter.side_effect = RuntimeError("db down")

        sweeper = TTLVolumeSweeper(mock_transport, metastore=metastore)
        entries, sealed = await sweeper.sweep_once()
        # Should not crash — metastore cleanup is best-effort
        assert entries == 0

    @pytest.mark.asyncio
    async def test_set_metastore_deferred(self, mock_transport) -> None:
        """Metastore can be injected after construction."""
        sweeper = TTLVolumeSweeper(mock_transport)
        assert sweeper._metastore is None

        metastore = MagicMock()
        metastore.list_iter.return_value = []
        sweeper.set_metastore(metastore)

        await sweeper.sweep_once()
        metastore.list_iter.assert_called_once()
