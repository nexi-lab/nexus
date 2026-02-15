"""Unit tests for TaskManager.

Tests the business logic layer: state machine transitions, SSE streaming,
task lifecycle, and edge cases.  Uses InMemoryTaskStore to isolate from
storage concerns.
"""

from __future__ import annotations

import pytest

from nexus.a2a.models import (
    Artifact,
    DataPart,
    Message,
    TaskState,
    TextPart,
)
from nexus.a2a.stores.in_memory import InMemoryTaskStore
from nexus.a2a.task_manager import TaskManager


@pytest.fixture
def store() -> InMemoryTaskStore:
    return InMemoryTaskStore()


@pytest.fixture
def tm(store: InMemoryTaskStore) -> TaskManager:
    return TaskManager(store=store)


def _user_message(text: str = "hello") -> Message:
    return Message(role="user", parts=[TextPart(text=text)])


def _agent_message(text: str = "response") -> Message:
    return Message(role="agent", parts=[TextPart(text=text)])


class TestCreateTask:
    @pytest.mark.asyncio
    async def test_creates_task_in_submitted_state(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        assert task.status.state == TaskState.SUBMITTED

    @pytest.mark.asyncio
    async def test_assigns_uuid_id(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        assert len(task.id) > 0
        assert "-" in task.id

    @pytest.mark.asyncio
    async def test_includes_message_in_history(self, tm: TaskManager) -> None:
        msg = _user_message("test content")
        task = await tm.create_task(msg)
        assert len(task.history) == 1
        assert task.history[0].parts[0].text == "test content"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_assigns_context_id(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message(), context_id="ctx-123")
        assert task.contextId == "ctx-123"

    @pytest.mark.asyncio
    async def test_auto_generates_context_id(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        assert task.contextId is not None
        assert len(task.contextId) > 0

    @pytest.mark.asyncio
    async def test_stores_metadata(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message(), metadata={"key": "val"})
        assert task.metadata == {"key": "val"}

    @pytest.mark.asyncio
    async def test_task_persisted_to_store(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        loaded = await tm.get_task(task.id)
        assert loaded.id == task.id


class TestGetTask:
    @pytest.mark.asyncio
    async def test_get_existing_task(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        loaded = await tm.get_task(task.id)
        assert loaded.id == task.id
        assert loaded.status.state == TaskState.SUBMITTED

    @pytest.mark.asyncio
    async def test_get_nonexistent_raises(self, tm: TaskManager) -> None:
        from nexus.a2a.exceptions import TaskNotFoundError

        with pytest.raises(TaskNotFoundError):
            await tm.get_task("nonexistent")

    @pytest.mark.asyncio
    async def test_history_length_truncation(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message("m1"))
        await tm.update_task_state(task.id, TaskState.WORKING, message=_agent_message("m2"))
        loaded = await tm.get_task(task.id, history_length=1)
        assert len(loaded.history) == 1

    @pytest.mark.asyncio
    async def test_history_length_zero(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        loaded = await tm.get_task(task.id, history_length=0)
        assert loaded.history == []


class TestStateTransitions:
    @pytest.mark.asyncio
    async def test_submitted_to_working(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        updated = await tm.update_task_state(task.id, TaskState.WORKING)
        assert updated.status.state == TaskState.WORKING

    @pytest.mark.asyncio
    async def test_working_to_completed(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        await tm.update_task_state(task.id, TaskState.WORKING)
        updated = await tm.update_task_state(task.id, TaskState.COMPLETED)
        assert updated.status.state == TaskState.COMPLETED

    @pytest.mark.asyncio
    async def test_working_to_failed(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        await tm.update_task_state(task.id, TaskState.WORKING)
        updated = await tm.update_task_state(task.id, TaskState.FAILED)
        assert updated.status.state == TaskState.FAILED

    @pytest.mark.asyncio
    async def test_submitted_to_canceled(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        updated = await tm.update_task_state(task.id, TaskState.CANCELED)
        assert updated.status.state == TaskState.CANCELED

    @pytest.mark.asyncio
    async def test_submitted_to_rejected(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        updated = await tm.update_task_state(task.id, TaskState.REJECTED)
        assert updated.status.state == TaskState.REJECTED

    @pytest.mark.asyncio
    async def test_working_to_input_required(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        await tm.update_task_state(task.id, TaskState.WORKING)
        updated = await tm.update_task_state(task.id, TaskState.INPUT_REQUIRED)
        assert updated.status.state == TaskState.INPUT_REQUIRED

    @pytest.mark.asyncio
    async def test_input_required_to_working(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        await tm.update_task_state(task.id, TaskState.WORKING)
        await tm.update_task_state(task.id, TaskState.INPUT_REQUIRED)
        updated = await tm.update_task_state(task.id, TaskState.WORKING)
        assert updated.status.state == TaskState.WORKING

    @pytest.mark.asyncio
    async def test_invalid_transition_raises(self, tm: TaskManager) -> None:
        from nexus.a2a.exceptions import InvalidStateTransitionError

        task = await tm.create_task(_user_message())
        with pytest.raises(InvalidStateTransitionError):
            await tm.update_task_state(task.id, TaskState.COMPLETED)

    @pytest.mark.asyncio
    async def test_terminal_state_no_further_transitions(self, tm: TaskManager) -> None:
        from nexus.a2a.exceptions import InvalidStateTransitionError

        task = await tm.create_task(_user_message())
        await tm.update_task_state(task.id, TaskState.CANCELED)
        with pytest.raises(InvalidStateTransitionError):
            await tm.update_task_state(task.id, TaskState.WORKING)

    @pytest.mark.asyncio
    async def test_update_appends_message_to_history(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message("first"))
        await tm.update_task_state(task.id, TaskState.WORKING, message=_agent_message("second"))
        loaded = await tm.get_task(task.id)
        assert len(loaded.history) == 2
        assert loaded.history[1].parts[0].text == "second"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_nonexistent_task_raises(self, tm: TaskManager) -> None:
        from nexus.a2a.exceptions import TaskNotFoundError

        with pytest.raises(TaskNotFoundError):
            await tm.update_task_state("nonexistent", TaskState.WORKING)


class TestCancelTask:
    @pytest.mark.asyncio
    async def test_cancel_submitted_task(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        canceled = await tm.cancel_task(task.id)
        assert canceled.status.state == TaskState.CANCELED

    @pytest.mark.asyncio
    async def test_cancel_working_task(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        await tm.update_task_state(task.id, TaskState.WORKING)
        canceled = await tm.cancel_task(task.id)
        assert canceled.status.state == TaskState.CANCELED

    @pytest.mark.asyncio
    async def test_cancel_terminal_task_raises(self, tm: TaskManager) -> None:
        from nexus.a2a.exceptions import TaskNotCancelableError

        task = await tm.create_task(_user_message())
        await tm.update_task_state(task.id, TaskState.CANCELED)
        with pytest.raises(TaskNotCancelableError):
            await tm.cancel_task(task.id)

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_raises(self, tm: TaskManager) -> None:
        from nexus.a2a.exceptions import TaskNotFoundError

        with pytest.raises(TaskNotFoundError):
            await tm.cancel_task("nonexistent")


class TestAddArtifact:
    @pytest.mark.asyncio
    async def test_add_artifact(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        artifact = Artifact(
            artifactId="art-1",
            name="output.json",
            parts=[DataPart(data={"result": 42})],
        )
        updated = await tm.add_artifact(task.id, artifact)
        assert len(updated.artifacts) == 1
        assert updated.artifacts[0].artifactId == "art-1"

    @pytest.mark.asyncio
    async def test_add_multiple_artifacts(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        for i in range(3):
            await tm.add_artifact(
                task.id,
                Artifact(artifactId=f"art-{i}", parts=[DataPart(data={"i": i})]),
            )
        loaded = await tm.get_task(task.id)
        assert len(loaded.artifacts) == 3

    @pytest.mark.asyncio
    async def test_add_artifact_nonexistent_raises(self, tm: TaskManager) -> None:
        from nexus.a2a.exceptions import TaskNotFoundError

        with pytest.raises(TaskNotFoundError):
            await tm.add_artifact(
                "nonexistent",
                Artifact(artifactId="x", parts=[]),
            )


class TestSSEStreams:
    @pytest.mark.asyncio
    async def test_register_stream(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        queue = tm.register_stream(task.id)
        assert queue is not None

    @pytest.mark.asyncio
    async def test_state_update_pushes_to_stream(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        queue = tm.register_stream(task.id)
        await tm.update_task_state(task.id, TaskState.WORKING)
        event = queue.get_nowait()
        assert "statusUpdate" in event
        assert event["statusUpdate"]["status"]["state"] == "working"

    @pytest.mark.asyncio
    async def test_artifact_pushes_to_stream(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        queue = tm.register_stream(task.id)
        await tm.add_artifact(
            task.id,
            Artifact(artifactId="a1", parts=[DataPart(data={"x": 1})]),
        )
        event = queue.get_nowait()
        assert "artifactUpdate" in event

    @pytest.mark.asyncio
    async def test_terminal_state_marks_final(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        queue = tm.register_stream(task.id)
        await tm.update_task_state(task.id, TaskState.CANCELED)
        event = queue.get_nowait()
        assert event["statusUpdate"]["final"] is True

    @pytest.mark.asyncio
    async def test_unregister_stream(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        queue = tm.register_stream(task.id)
        tm.unregister_stream(task.id, queue)
        await tm.update_task_state(task.id, TaskState.WORKING)
        assert queue.empty()


class TestListTasks:
    @pytest.mark.asyncio
    async def test_list_tasks(self, tm: TaskManager) -> None:
        for i in range(3):
            await tm.create_task(_user_message(f"msg-{i}"))
        tasks = await tm.list_tasks()
        assert len(tasks) == 3

    @pytest.mark.asyncio
    async def test_list_filter_by_state(self, tm: TaskManager) -> None:
        t1 = await tm.create_task(_user_message())
        await tm.create_task(_user_message())
        await tm.update_task_state(t1.id, TaskState.WORKING)
        working = await tm.list_tasks(state=TaskState.WORKING)
        assert len(working) == 1
        assert working[0].id == t1.id
