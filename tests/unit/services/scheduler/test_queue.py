"""Tests for TaskQueue (Issues #2360, #2747).

Unit tests for the SQL-backed TaskQueue, using mocked asyncpg connections.
Covers count_pending, deadline enforcement in dequeue SQL, nearest_deadline,
and notify payload format.
"""

import json
from datetime import UTC, datetime, timedelta
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


# ---------------------------------------------------------------------------
# Deadline enforcement in dequeue SQL (Issue #2747)
# ---------------------------------------------------------------------------


class TestDeadlineEnforcement:
    """Verify dequeue SQL includes deadline filter."""

    async def test_dequeue_sql_contains_deadline_filter(self) -> None:
        """All dequeue SQL statements include deadline enforcement."""
        from nexus.system_services.scheduler.queue import (
            _SQL_DEQUEUE,
            _SQL_DEQUEUE_BY_EXECUTOR,
            _SQL_DEQUEUE_HRRN,
            _SQL_DEQUEUE_HRRN_BY_EXECUTOR,
        )

        deadline_clause = "deadline IS NULL OR deadline <= now()"
        for name, sql in [
            ("_SQL_DEQUEUE", _SQL_DEQUEUE),
            ("_SQL_DEQUEUE_BY_EXECUTOR", _SQL_DEQUEUE_BY_EXECUTOR),
            ("_SQL_DEQUEUE_HRRN", _SQL_DEQUEUE_HRRN),
            ("_SQL_DEQUEUE_HRRN_BY_EXECUTOR", _SQL_DEQUEUE_HRRN_BY_EXECUTOR),
        ]:
            assert deadline_clause in sql, f"{name} is missing deadline enforcement clause"

    async def test_dequeue_sql_uses_shared_columns(self) -> None:
        """All dequeue SQL statements use the shared _TASK_COLUMNS constant."""
        from nexus.system_services.scheduler.queue import (
            _SQL_DEQUEUE,
            _SQL_DEQUEUE_BY_EXECUTOR,
            _SQL_DEQUEUE_HRRN,
            _SQL_DEQUEUE_HRRN_BY_EXECUTOR,
            _SQL_GET_TASK,
            _TASK_COLUMNS,
        )

        # The f-string interpolation means _TASK_COLUMNS content is embedded
        for name, sql in [
            ("_SQL_DEQUEUE", _SQL_DEQUEUE),
            ("_SQL_DEQUEUE_BY_EXECUTOR", _SQL_DEQUEUE_BY_EXECUTOR),
            ("_SQL_DEQUEUE_HRRN", _SQL_DEQUEUE_HRRN),
            ("_SQL_DEQUEUE_HRRN_BY_EXECUTOR", _SQL_DEQUEUE_HRRN_BY_EXECUTOR),
            ("_SQL_GET_TASK", _SQL_GET_TASK),
        ]:
            assert _TASK_COLUMNS.strip() in sql, f"{name} does not contain _TASK_COLUMNS content"


# ---------------------------------------------------------------------------
# nearest_deadline tests (Issue #2747)
# ---------------------------------------------------------------------------


class TestNearestDeadline:
    """Verify TaskQueue.nearest_deadline() for timer gate cold-start."""

    async def test_returns_deadline_when_exists(
        self, queue: TaskQueue, mock_conn: AsyncMock
    ) -> None:
        """Returns the nearest future deadline from the query."""
        future = datetime.now(UTC) + timedelta(hours=1)
        mock_conn.fetchrow.return_value = {"nearest": future}
        result = await queue.nearest_deadline(mock_conn)
        assert result == future

    async def test_returns_none_when_no_deadlines(
        self, queue: TaskQueue, mock_conn: AsyncMock
    ) -> None:
        """Returns None when no queued tasks have future deadlines."""
        mock_conn.fetchrow.return_value = {"nearest": None}
        result = await queue.nearest_deadline(mock_conn)
        assert result is None

    async def test_returns_none_when_row_is_none(
        self, queue: TaskQueue, mock_conn: AsyncMock
    ) -> None:
        """Returns None when fetchrow returns None (empty table)."""
        mock_conn.fetchrow.return_value = None
        result = await queue.nearest_deadline(mock_conn)
        assert result is None


# ---------------------------------------------------------------------------
# Notify payload format (Issue #2747)
# ---------------------------------------------------------------------------


class TestNotifyPayload:
    """Verify enqueue sends JSON pg_notify payload with deadline."""

    async def test_notify_payload_without_deadline(
        self, queue: TaskQueue, mock_conn: AsyncMock
    ) -> None:
        """Enqueue without deadline sends JSON with task_id and executor_id."""
        mock_conn.fetchval.return_value = "task-123"
        await queue.enqueue(
            mock_conn,
            agent_id="a1",
            executor_id="e1",
            task_type="test",
            payload={},
            priority_tier=2,
            effective_tier=2,
        )
        # Second call should be the NOTIFY
        notify_call = mock_conn.execute.call_args
        raw_payload = notify_call.args[1]
        data = json.loads(raw_payload)
        assert data["task_id"] == "task-123"
        assert data["executor_id"] == "e1"
        assert "deadline" not in data

    async def test_notify_payload_with_deadline(
        self, queue: TaskQueue, mock_conn: AsyncMock
    ) -> None:
        """Enqueue with deadline includes deadline ISO in JSON payload."""
        deadline = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        mock_conn.fetchval.return_value = "task-456"
        await queue.enqueue(
            mock_conn,
            agent_id="a1",
            executor_id="e1",
            task_type="test",
            payload={},
            priority_tier=2,
            effective_tier=2,
            deadline=deadline,
        )
        notify_call = mock_conn.execute.call_args
        raw_payload = notify_call.args[1]
        data = json.loads(raw_payload)
        assert data["task_id"] == "task-456"
        assert data["executor_id"] == "e1"
        assert data["deadline"] == deadline.isoformat()
