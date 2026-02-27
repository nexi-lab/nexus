"""Unit tests for A2A request handlers.

TDD-first tests for the extracted handler module.  Tests verify
pure async handler functions in isolation (no HTTP layer).
"""

from typing import Any

import pytest

from nexus.bricks.a2a.exceptions import (
    InvalidParamsError,
    MethodNotFoundError,
    PushNotificationNotSupportedError,
    TaskNotCancelableError,
    TaskNotFoundError,
)
from nexus.bricks.a2a.handlers import dispatch, handle_cancel, handle_get, handle_send
from nexus.bricks.a2a.models import Message, TaskState, TextPart
from nexus.bricks.a2a.stores.in_memory import CacheBackedTaskStore
from nexus.bricks.a2a.task_manager import TaskManager
from nexus.cache.inmemory import InMemoryCacheStore


@pytest.fixture
def tm() -> TaskManager:
    return TaskManager(store=CacheBackedTaskStore(InMemoryCacheStore()))


def _user_msg(text: str = "hello") -> dict[str, Any]:
    """Build a raw message dict (as comes from JSON-RPC params)."""
    return {
        "message": {"role": "user", "parts": [{"type": "text", "text": text}]},
    }


def _user_message(text: str = "hello") -> Message:
    return Message(role="user", parts=[TextPart(text=text)])


class TestHandleSend:
    @pytest.mark.asyncio
    async def test_creates_task(self, tm: TaskManager) -> None:
        result = await handle_send(_user_msg(), zone_id="root", agent_id=None, task_manager=tm)
        assert result["id"]
        assert result["status"]["state"] == "submitted"

    @pytest.mark.asyncio
    async def test_invalid_params_raises(self, tm: TaskManager) -> None:
        with pytest.raises(InvalidParamsError):
            await handle_send({"bad": "params"}, zone_id="root", agent_id=None, task_manager=tm)

    @pytest.mark.asyncio
    async def test_continuation_updates_state(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        params = {**_user_msg("followup"), "taskId": task.id}
        result = await handle_send(params, zone_id="root", agent_id=None, task_manager=tm)
        assert result["status"]["state"] == "working"

    @pytest.mark.asyncio
    async def test_continuation_merges_metadata(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message(), metadata={"key1": "original"})
        params = {
            **_user_msg("followup"),
            "taskId": task.id,
            "metadata": {"key2": "new"},
        }
        result = await handle_send(params, zone_id="root", agent_id=None, task_manager=tm)
        # Metadata should be merged
        loaded = await tm.get_task(result["id"])
        assert loaded.metadata is not None
        assert loaded.metadata["key1"] == "original"
        assert loaded.metadata["key2"] == "new"

    @pytest.mark.asyncio
    async def test_continuation_terminal_raises(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        await tm.update_task_state(task.id, TaskState.CANCELED)
        params = {**_user_msg("followup"), "taskId": task.id}
        with pytest.raises(InvalidParamsError):
            await handle_send(params, zone_id="root", agent_id=None, task_manager=tm)


class TestHandleGet:
    @pytest.mark.asyncio
    async def test_returns_task(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        result = await handle_get({"taskId": task.id}, zone_id="root", task_manager=tm)
        assert result["id"] == task.id

    @pytest.mark.asyncio
    async def test_nonexistent_raises(self, tm: TaskManager) -> None:
        with pytest.raises(TaskNotFoundError):
            await handle_get({"taskId": "nonexistent"}, zone_id="root", task_manager=tm)

    @pytest.mark.asyncio
    async def test_with_history_length(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message("m1"))
        await tm.update_task_state(task.id, TaskState.WORKING, message=_user_message("m2"))
        result = await handle_get(
            {"taskId": task.id, "historyLength": 1},
            zone_id="root",
            task_manager=tm,
        )
        assert len(result["history"]) == 1


class TestHandleCancel:
    @pytest.mark.asyncio
    async def test_cancels_task(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        result = await handle_cancel({"taskId": task.id}, zone_id="root", task_manager=tm)
        assert result["status"]["state"] == "canceled"

    @pytest.mark.asyncio
    async def test_terminal_raises(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        await tm.update_task_state(task.id, TaskState.CANCELED)
        with pytest.raises(TaskNotCancelableError):
            await handle_cancel({"taskId": task.id}, zone_id="root", task_manager=tm)


class TestDispatch:
    @pytest.mark.asyncio
    async def test_routes_to_send(self, tm: TaskManager) -> None:
        result = await dispatch(
            method="a2a.tasks.send",
            params=_user_msg(),
            zone_id="root",
            agent_id=None,
            task_manager=tm,
        )
        assert result["status"]["state"] == "submitted"

    @pytest.mark.asyncio
    async def test_routes_to_get(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        result = await dispatch(
            method="a2a.tasks.get",
            params={"taskId": task.id},
            zone_id="root",
            agent_id=None,
            task_manager=tm,
        )
        assert result["id"] == task.id

    @pytest.mark.asyncio
    async def test_routes_to_cancel(self, tm: TaskManager) -> None:
        task = await tm.create_task(_user_message())
        result = await dispatch(
            method="a2a.tasks.cancel",
            params={"taskId": task.id},
            zone_id="root",
            agent_id=None,
            task_manager=tm,
        )
        assert result["status"]["state"] == "canceled"

    @pytest.mark.asyncio
    async def test_unknown_method_raises(self, tm: TaskManager) -> None:
        with pytest.raises(MethodNotFoundError):
            await dispatch(
                method="a2a.tasks.unknown",
                params={},
                zone_id="root",
                agent_id=None,
                task_manager=tm,
            )

    @pytest.mark.asyncio
    async def test_push_notification_raises(self, tm: TaskManager) -> None:
        with pytest.raises(PushNotificationNotSupportedError):
            await dispatch(
                method="a2a.tasks.createPushNotificationConfig",
                params={},
                zone_id="root",
                agent_id=None,
                task_manager=tm,
            )
