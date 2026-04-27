"""Unit tests for SnapshotCleanupWorker (Issue #1752).

Tests: Init, Lifecycle, Sweep, Errors.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.snapshot.cleanup import SnapshotCleanupWorker


@pytest.fixture
def mock_snapshot_service() -> MagicMock:
    """Mock TransactionalSnapshotService."""
    svc = MagicMock()
    svc.cleanup_expired = AsyncMock(return_value=0)
    return svc


class TestInit:
    """Tests for SnapshotCleanupWorker initialization."""

    def test_default_config(self, mock_snapshot_service: MagicMock) -> None:
        worker = SnapshotCleanupWorker(mock_snapshot_service)
        assert worker._sweep_interval == 300.0
        assert worker._batch_limit == 100
        assert not worker.is_running

    def test_custom_config(self, mock_snapshot_service: MagicMock) -> None:
        worker = SnapshotCleanupWorker(mock_snapshot_service, sweep_interval=60.0, batch_limit=50)
        assert worker._sweep_interval == 60.0
        assert worker._batch_limit == 50


class TestLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_creates_task(self, mock_snapshot_service: MagicMock) -> None:
        worker = SnapshotCleanupWorker(mock_snapshot_service, sweep_interval=0.01)
        await worker.start()
        assert worker.is_running
        await worker.stop()
        assert not worker.is_running

    @pytest.mark.asyncio
    async def test_start_idempotent(self, mock_snapshot_service: MagicMock) -> None:
        worker = SnapshotCleanupWorker(mock_snapshot_service, sweep_interval=0.01)
        await worker.start()
        task1 = worker._task
        await worker.start()  # should not create a new task
        assert worker._task is task1
        await worker.stop()

    @pytest.mark.asyncio
    async def test_stop_idempotent(self, mock_snapshot_service: MagicMock) -> None:
        worker = SnapshotCleanupWorker(mock_snapshot_service)
        await worker.stop()  # should not raise
        assert not worker.is_running


class TestSweep:
    """Tests for cleanup sweep behavior."""

    @pytest.mark.asyncio
    async def test_sweep_calls_cleanup_expired(self, mock_snapshot_service: MagicMock) -> None:
        mock_snapshot_service.cleanup_expired = AsyncMock(return_value=3)
        worker = SnapshotCleanupWorker(mock_snapshot_service, sweep_interval=0.01, batch_limit=50)
        await worker.start()
        # Wait for at least one sweep
        await asyncio.sleep(0.05)
        await worker.stop()

        mock_snapshot_service.cleanup_expired.assert_called_with(limit=50)


class TestErrors:
    """Tests for error handling during sweep."""

    @pytest.mark.asyncio
    async def test_sweep_error_does_not_stop_worker(self, mock_snapshot_service: MagicMock) -> None:
        """Worker should continue running even if a sweep fails."""
        call_count = 0
        continued = asyncio.Event()

        async def failing_cleanup(limit: int = 100) -> int:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("DB error")
            continued.set()
            return 0

        mock_snapshot_service.cleanup_expired = failing_cleanup
        worker = SnapshotCleanupWorker(mock_snapshot_service, sweep_interval=0.01)
        try:
            await worker.start()
            await asyncio.wait_for(continued.wait(), timeout=2.0)
            assert worker.is_running
            assert call_count >= 2  # continued past the error
        finally:
            await worker.stop()
