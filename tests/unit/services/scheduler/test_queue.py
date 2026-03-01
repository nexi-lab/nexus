"""Tests for TaskQueue (Issue #2360 — count_pending coverage).

Unit tests for the SQL-backed TaskQueue, using mocked asyncpg connections.
"""

from unittest.mock import AsyncMock

import pytest

from nexus.system_services.scheduler.queue import TaskQueue


@pytest.fixture()
def queue() -> TaskQueue:
    return TaskQueue()


@pytest.fixture()
def mock_conn() -> AsyncMock:
    return AsyncMock()


# ---------------------------------------------------------------------------
# count_pending tests (#9A)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCountPending:
    """Verify TaskQueue.count_pending() SQL dispatch and fallback."""

    async def test_count_pending_no_zone(self, queue: TaskQueue, mock_conn: AsyncMock) -> None:
        """Global count dispatches _SQL_COUNT_PENDING (no zone filter)."""
        mock_conn.fetchrow.return_value = {"cnt": 5}
        result = await queue.count_pending(mock_conn)
        assert result == 5
        mock_conn.fetchrow.assert_called_once()
        # Should NOT pass any args (no zone_id)
        args = mock_conn.fetchrow.call_args
        assert len(args.args) == 1  # just the SQL string

    async def test_count_pending_with_zone(self, queue: TaskQueue, mock_conn: AsyncMock) -> None:
        """Zone-filtered count dispatches _SQL_COUNT_PENDING_BY_ZONE."""
        mock_conn.fetchrow.return_value = {"cnt": 2}
        result = await queue.count_pending(mock_conn, zone_id="zone-1")
        assert result == 2
        mock_conn.fetchrow.assert_called_once()
        # Should pass zone_id as second arg
        args = mock_conn.fetchrow.call_args
        assert args.args[1] == "zone-1"

    async def test_count_pending_none_row(self, queue: TaskQueue, mock_conn: AsyncMock) -> None:
        """Returns 0 when fetchrow returns None (empty table edge case)."""
        mock_conn.fetchrow.return_value = None
        result = await queue.count_pending(mock_conn)
        assert result == 0

    async def test_count_pending_none_row_with_zone(
        self, queue: TaskQueue, mock_conn: AsyncMock
    ) -> None:
        """Returns 0 when fetchrow returns None for a zone filter."""
        mock_conn.fetchrow.return_value = None
        result = await queue.count_pending(mock_conn, zone_id="empty-zone")
        assert result == 0

    async def test_count_pending_zero(self, queue: TaskQueue, mock_conn: AsyncMock) -> None:
        """Correctly returns 0 when cnt is 0 (not None)."""
        mock_conn.fetchrow.return_value = {"cnt": 0}
        result = await queue.count_pending(mock_conn)
        assert result == 0
