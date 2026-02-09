"""Unit tests for A2A task manager.

Includes exhaustive state transition matrix tests.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus.a2a.exceptions import (
    InvalidStateTransitionError,
    TaskNotCancelableError,
    TaskNotFoundError,
)
from nexus.a2a.models import (
    VALID_TRANSITIONS,
    Artifact,
    Message,
    TaskState,
    TextPart,
)
from nexus.a2a.task_manager import TaskManager

# ======================================================================
# Fixtures
# ======================================================================


@pytest.fixture
def manager() -> TaskManager:
    """Create a TaskManager with in-memory storage."""
    return TaskManager(session_factory=None)


@pytest.fixture
def user_message() -> Message:
    return Message(role="user", parts=[TextPart(text="hello")])


@pytest.fixture
def agent_message() -> Message:
    return Message(role="agent", parts=[TextPart(text="response")])


# ======================================================================
# Task Creation
# ======================================================================


class TestCreateTask:
    @pytest.mark.asyncio
    async def test_creates_task_in_submitted_state(
        self, manager: TaskManager, user_message: Message
    ) -> None:
        task = await manager.create_task(user_message)
        assert task.status.state == TaskState.SUBMITTED

    @pytest.mark.asyncio
    async def test_assigns_unique_id(self, manager: TaskManager, user_message: Message) -> None:
        t1 = await manager.create_task(user_message)
        t2 = await manager.create_task(user_message)
        assert t1.id != t2.id

    @pytest.mark.asyncio
    async def test_assigns_context_id(self, manager: TaskManager, user_message: Message) -> None:
        task = await manager.create_task(user_message, context_id="ctx-1")
        assert task.contextId == "ctx-1"

    @pytest.mark.asyncio
    async def test_auto_generates_context_id(
        self, manager: TaskManager, user_message: Message
    ) -> None:
        task = await manager.create_task(user_message)
        assert task.contextId is not None

    @pytest.mark.asyncio
    async def test_stores_initial_message_in_history(
        self, manager: TaskManager, user_message: Message
    ) -> None:
        task = await manager.create_task(user_message)
        assert len(task.history) == 1
        assert task.history[0].role == "user"

    @pytest.mark.asyncio
    async def test_stores_metadata(self, manager: TaskManager, user_message: Message) -> None:
        task = await manager.create_task(user_message, metadata={"key": "val"})
        assert task.metadata == {"key": "val"}

    @pytest.mark.asyncio
    async def test_respects_zone_id(self, manager: TaskManager, user_message: Message) -> None:
        task = await manager.create_task(user_message, zone_id="zone-a")
        # Can retrieve with matching zone
        retrieved = await manager.get_task(task.id, zone_id="zone-a")
        assert retrieved.id == task.id

    @pytest.mark.asyncio
    async def test_zone_isolation(self, manager: TaskManager, user_message: Message) -> None:
        task = await manager.create_task(user_message, zone_id="zone-a")
        # Cannot retrieve with wrong zone
        with pytest.raises(TaskNotFoundError):
            await manager.get_task(task.id, zone_id="zone-b")


# ======================================================================
# Task Retrieval
# ======================================================================


class TestGetTask:
    @pytest.mark.asyncio
    async def test_get_existing_task(self, manager: TaskManager, user_message: Message) -> None:
        task = await manager.create_task(user_message)
        retrieved = await manager.get_task(task.id)
        assert retrieved.id == task.id
        assert retrieved.status.state == TaskState.SUBMITTED

    @pytest.mark.asyncio
    async def test_get_nonexistent_task(self, manager: TaskManager) -> None:
        with pytest.raises(TaskNotFoundError):
            await manager.get_task("nonexistent-id")

    @pytest.mark.asyncio
    async def test_history_length_truncation(
        self, manager: TaskManager, user_message: Message
    ) -> None:
        task = await manager.create_task(user_message)
        # Add more messages via state transitions
        msg2 = Message(role="agent", parts=[TextPart(text="working")])
        await manager.update_task_state(task.id, TaskState.WORKING, message=msg2)

        # Full history
        full = await manager.get_task(task.id, history_length=None)
        assert len(full.history) == 2

        # Truncated history
        truncated = await manager.get_task(task.id, history_length=1)
        assert len(truncated.history) == 1

    @pytest.mark.asyncio
    async def test_history_length_zero(self, manager: TaskManager, user_message: Message) -> None:
        task = await manager.create_task(user_message)
        result = await manager.get_task(task.id, history_length=0)
        assert result.history == []


# ======================================================================
# Task Listing
# ======================================================================


class TestListTasks:
    @pytest.mark.asyncio
    async def test_list_empty(self, manager: TaskManager) -> None:
        tasks = await manager.list_tasks()
        assert tasks == []

    @pytest.mark.asyncio
    async def test_list_returns_tasks(self, manager: TaskManager, user_message: Message) -> None:
        await manager.create_task(user_message)
        await manager.create_task(user_message)
        tasks = await manager.list_tasks()
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_list_filters_by_zone(self, manager: TaskManager, user_message: Message) -> None:
        await manager.create_task(user_message, zone_id="zone-a")
        await manager.create_task(user_message, zone_id="zone-b")
        tasks_a = await manager.list_tasks(zone_id="zone-a")
        assert len(tasks_a) == 1

    @pytest.mark.asyncio
    async def test_list_filters_by_state(self, manager: TaskManager, user_message: Message) -> None:
        t1 = await manager.create_task(user_message)
        await manager.create_task(user_message)
        await manager.update_task_state(t1.id, TaskState.WORKING)

        working = await manager.list_tasks(state=TaskState.WORKING)
        assert len(working) == 1
        assert working[0].id == t1.id

    @pytest.mark.asyncio
    async def test_list_pagination(self, manager: TaskManager, user_message: Message) -> None:
        for _ in range(5):
            await manager.create_task(user_message)
        page1 = await manager.list_tasks(limit=2, offset=0)
        page2 = await manager.list_tasks(limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0].id != page2[0].id


# ======================================================================
# Task Cancellation
# ======================================================================


class TestCancelTask:
    @pytest.mark.asyncio
    async def test_cancel_submitted_task(self, manager: TaskManager, user_message: Message) -> None:
        task = await manager.create_task(user_message)
        canceled = await manager.cancel_task(task.id)
        assert canceled.status.state == TaskState.CANCELED

    @pytest.mark.asyncio
    async def test_cancel_working_task(self, manager: TaskManager, user_message: Message) -> None:
        task = await manager.create_task(user_message)
        await manager.update_task_state(task.id, TaskState.WORKING)
        canceled = await manager.cancel_task(task.id)
        assert canceled.status.state == TaskState.CANCELED

    @pytest.mark.asyncio
    async def test_cancel_completed_task_fails(
        self, manager: TaskManager, user_message: Message
    ) -> None:
        task = await manager.create_task(user_message)
        await manager.update_task_state(task.id, TaskState.WORKING)
        await manager.update_task_state(task.id, TaskState.COMPLETED)
        with pytest.raises(TaskNotCancelableError):
            await manager.cancel_task(task.id)

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_task(self, manager: TaskManager) -> None:
        with pytest.raises(TaskNotFoundError):
            await manager.cancel_task("nonexistent")


# ======================================================================
# State Transition Matrix (Exhaustive)
# ======================================================================


# Generate all (from_state, to_state) pairs
ALL_TRANSITIONS = [
    (from_state, to_state)
    for from_state in TaskState
    for to_state in TaskState
    if from_state != to_state  # Self-transitions excluded
]


class TestStateTransitionMatrix:
    """Test every possible (from_state, to_state) pair."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("from_state,to_state", ALL_TRANSITIONS)
    async def test_transition(
        self,
        manager: TaskManager,
        user_message: Message,
        from_state: TaskState,
        to_state: TaskState,
    ) -> None:
        """Test a single state transition."""
        # Create a task and navigate to from_state
        task = await manager.create_task(user_message)
        task = await self._navigate_to_state(manager, task.id, from_state)

        valid = to_state in VALID_TRANSITIONS[from_state]

        if valid:
            result = await manager.update_task_state(task.id, to_state)
            assert result.status.state == to_state
        else:
            with pytest.raises(InvalidStateTransitionError):
                await manager.update_task_state(task.id, to_state)

    @staticmethod
    async def _navigate_to_state(manager: TaskManager, task_id: str, target: TaskState) -> object:
        """Navigate a task from SUBMITTED to the target state.

        Uses the shortest valid path.
        """
        # Path to reach each state from SUBMITTED
        paths: dict[TaskState, list[TaskState]] = {
            TaskState.SUBMITTED: [],
            TaskState.WORKING: [TaskState.WORKING],
            TaskState.INPUT_REQUIRED: [TaskState.WORKING, TaskState.INPUT_REQUIRED],
            TaskState.COMPLETED: [TaskState.WORKING, TaskState.COMPLETED],
            TaskState.FAILED: [TaskState.WORKING, TaskState.FAILED],
            TaskState.CANCELED: [TaskState.CANCELED],
            TaskState.REJECTED: [TaskState.REJECTED],
        }

        task = await manager.get_task(task_id)
        for state in paths[target]:
            task = await manager.update_task_state(task_id, state)
        return task


# ======================================================================
# Artifacts
# ======================================================================


class TestAddArtifact:
    @pytest.mark.asyncio
    async def test_add_artifact(self, manager: TaskManager, user_message: Message) -> None:
        task = await manager.create_task(user_message)
        artifact = Artifact(artifactId="a1", parts=[TextPart(text="result")])
        updated = await manager.add_artifact(task.id, artifact)
        assert len(updated.artifacts) == 1
        assert updated.artifacts[0].artifactId == "a1"

    @pytest.mark.asyncio
    async def test_add_multiple_artifacts(
        self, manager: TaskManager, user_message: Message
    ) -> None:
        task = await manager.create_task(user_message)
        a1 = Artifact(artifactId="a1", parts=[TextPart(text="r1")])
        a2 = Artifact(artifactId="a2", parts=[TextPart(text="r2")])
        await manager.add_artifact(task.id, a1)
        updated = await manager.add_artifact(task.id, a2)
        assert len(updated.artifacts) == 2

    @pytest.mark.asyncio
    async def test_add_artifact_nonexistent_task(self, manager: TaskManager) -> None:
        artifact = Artifact(artifactId="a1", parts=[TextPart(text="r")])
        with pytest.raises(TaskNotFoundError):
            await manager.add_artifact("nonexistent", artifact)


# ======================================================================
# SSE Stream Management
# ======================================================================


class TestStreamManagement:
    def test_register_stream(self, manager: TaskManager) -> None:
        queue = manager.register_stream("task-1")
        assert queue is not None
        assert "task-1" in manager._active_streams

    def test_unregister_stream(self, manager: TaskManager) -> None:
        queue = manager.register_stream("task-1")
        manager.unregister_stream("task-1", queue)
        assert "task-1" not in manager._active_streams

    def test_unregister_nonexistent_stream(self, manager: TaskManager) -> None:
        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        # Should not raise
        manager.unregister_stream("task-1", queue)

    def test_multiple_streams_per_task(self, manager: TaskManager) -> None:
        q1 = manager.register_stream("task-1")
        q2 = manager.register_stream("task-1")
        assert len(manager._active_streams["task-1"]) == 2
        manager.unregister_stream("task-1", q1)
        assert len(manager._active_streams["task-1"]) == 1
        manager.unregister_stream("task-1", q2)
        assert "task-1" not in manager._active_streams

    @pytest.mark.asyncio
    async def test_state_change_pushes_to_stream(
        self, manager: TaskManager, user_message: Message
    ) -> None:
        task = await manager.create_task(user_message)
        queue = manager.register_stream(task.id)

        await manager.update_task_state(task.id, TaskState.WORKING)

        event = queue.get_nowait()
        assert "statusUpdate" in event
        assert event["statusUpdate"]["status"]["state"] == "working"

    @pytest.mark.asyncio
    async def test_artifact_pushes_to_stream(
        self, manager: TaskManager, user_message: Message
    ) -> None:
        task = await manager.create_task(user_message)
        queue = manager.register_stream(task.id)

        artifact = Artifact(artifactId="a1", parts=[TextPart(text="data")])
        await manager.add_artifact(task.id, artifact)

        event = queue.get_nowait()
        assert "artifactUpdate" in event

    @pytest.mark.asyncio
    async def test_terminal_state_sends_final_event(
        self, manager: TaskManager, user_message: Message
    ) -> None:
        task = await manager.create_task(user_message)
        queue = manager.register_stream(task.id)

        await manager.update_task_state(task.id, TaskState.WORKING)
        await manager.update_task_state(task.id, TaskState.COMPLETED)

        # Drain events
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())

        final_event = events[-1]
        assert final_event["statusUpdate"]["final"] is True


# ======================================================================
# Edge Cases
# ======================================================================


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_update_state_with_message(
        self, manager: TaskManager, user_message: Message
    ) -> None:
        task = await manager.create_task(user_message)
        msg = Message(role="agent", parts=[TextPart(text="working")])
        updated = await manager.update_task_state(task.id, TaskState.WORKING, message=msg)
        assert len(updated.history) == 2
        assert updated.history[-1].role == "agent"

    @pytest.mark.asyncio
    async def test_multi_turn_conversation(
        self, manager: TaskManager, user_message: Message
    ) -> None:
        """Simulate: submitted → working → input_required → working → completed."""
        task = await manager.create_task(user_message)

        await manager.update_task_state(task.id, TaskState.WORKING)

        input_msg = Message(role="agent", parts=[TextPart(text="Need more info")])
        await manager.update_task_state(task.id, TaskState.INPUT_REQUIRED, message=input_msg)

        user_reply = Message(role="user", parts=[TextPart(text="Here it is")])
        await manager.update_task_state(task.id, TaskState.WORKING, message=user_reply)

        final_msg = Message(role="agent", parts=[TextPart(text="Done")])
        result = await manager.update_task_state(task.id, TaskState.COMPLETED, message=final_msg)

        assert result.status.state == TaskState.COMPLETED
        # original (1) + input_required (1) + working (1) + completed (1) = 4
        # The first WORKING transition had no message, so not in history
        assert len(result.history) == 4
