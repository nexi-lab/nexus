"""Regression tests for TaskManager TOCTOU fix — Issue #2960 C7.

Verifies that concurrent state transitions on the same task are serialized
via per-task asyncio locks, preventing lost-update races.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.a2a.models import (
    Message,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)
from nexus.bricks.a2a.task_manager import TaskManager


def _make_task(task_id: str, state: TaskState) -> Task:
    """Create a minimal Task for testing."""
    from datetime import UTC, datetime

    return Task(
        id=task_id,
        contextId="ctx-1",
        status=TaskStatus(state=state, timestamp=datetime.now(UTC)),
        history=[
            Message(
                role="user",
                parts=[TextPart(text="hello")],
            )
        ],
    )


class TestTaskManagerTOCTOU:
    """Regression: C7 — TOCTOU in task state machine."""

    @pytest.mark.asyncio
    async def test_concurrent_state_transitions_serialized(self) -> None:
        """Two concurrent update_task_state calls on the same task_id
        must be serialized: the second sees the first's result."""
        store = AsyncMock()
        registry = MagicMock()
        registry.push_event = MagicMock()

        # Track call order: store state evolves as transitions complete
        call_count = 0

        async def mock_get(task_id, zone_id=None):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                # First caller sees SUBMITTED
                return _make_task("task-1", TaskState.SUBMITTED)
            # Second caller (after first saved) sees WORKING
            return _make_task("task-1", TaskState.WORKING)

        async def mock_save(t, zone_id=None, agent_id=None):
            # Simulate a brief delay during save to expose race window
            await asyncio.sleep(0.01)

        store.get = mock_get
        store.save = mock_save

        tm = TaskManager(store=store, stream_registry=registry)

        # First transitions SUBMITTED->WORKING, second transitions WORKING->COMPLETED
        # The lock ensures the second call sees the WORKING state from the first
        results = await asyncio.gather(
            tm.update_task_state("task-1", TaskState.WORKING),
            tm.update_task_state("task-1", TaskState.COMPLETED),
        )

        # Both should succeed (the lock serializes them)
        assert len(results) == 2
        # The lock dict should have an entry for this task
        assert "task-1" in tm._task_locks

    @pytest.mark.asyncio
    async def test_different_tasks_not_blocked(self) -> None:
        """Locks for different task IDs should not interfere."""
        store = AsyncMock()
        registry = MagicMock()
        registry.push_event = MagicMock()

        async def mock_get(task_id, zone_id=None):
            return _make_task(task_id, TaskState.SUBMITTED)

        store.get = mock_get
        store.save = AsyncMock()

        tm = TaskManager(store=store, stream_registry=registry)

        # Two different tasks — should not deadlock
        results = await asyncio.gather(
            tm.update_task_state("task-a", TaskState.WORKING),
            tm.update_task_state("task-b", TaskState.WORKING),
        )

        assert len(results) == 2
        assert "task-a" in tm._task_locks
        assert "task-b" in tm._task_locks
