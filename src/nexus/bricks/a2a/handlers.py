"""A2A JSON-RPC method handlers.

Pure async functions operating on domain types — no FastAPI / HTTP
imports.  Extracted from ``router.py`` for testability and separation
of concerns (Decision 1 / #1585).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from nexus.bricks.a2a.exceptions import (
    InvalidParamsError,
    MethodNotFoundError,
    PushNotificationNotSupportedError,
)
from nexus.bricks.a2a.models import (
    TERMINAL_STATES,
    SendParams,
    TaskIdParams,
    TaskQueryParams,
    TaskState,
)
from nexus.bricks.a2a.task_manager import TaskManager


async def dispatch(
    *,
    method: str,
    params: dict[str, Any],
    zone_id: str,
    agent_id: str | None,
    task_manager: TaskManager,
    handle_extended_card: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> Any:
    """Route a JSON-RPC method to its handler."""

    if method == "a2a.tasks.send":
        return await handle_send(params, zone_id, agent_id, task_manager)
    elif method == "a2a.tasks.get":
        return await handle_get(params, zone_id, task_manager)
    elif method == "a2a.tasks.cancel":
        return await handle_cancel(params, zone_id, task_manager)
    elif method == "a2a.agent.getExtendedAgentCard":
        if handle_extended_card is not None:
            return await handle_extended_card()
        raise MethodNotFoundError(data={"method": method})
    elif method in (
        "a2a.tasks.createPushNotificationConfig",
        "a2a.tasks.getPushNotificationConfig",
        "a2a.tasks.deletePushNotificationConfig",
        "a2a.tasks.listPushNotificationConfigs",
    ):
        raise PushNotificationNotSupportedError()
    else:
        raise MethodNotFoundError(data={"method": method})


async def handle_send(
    params: dict[str, Any],
    zone_id: str,
    agent_id: str | None,
    task_manager: TaskManager,
) -> dict[str, Any]:
    """Handle ``a2a.tasks.send`` — create or continue a task."""
    # Extract taskId before validation (SendParams forbids extra fields)
    task_id = params.get("taskId")
    validate_params = {k: v for k, v in params.items() if k != "taskId"}

    try:
        send_params = SendParams.model_validate(validate_params)
    except ValidationError as e:
        raise InvalidParamsError(data={"detail": str(e)}) from e
    if task_id:
        task = await task_manager.get_task(task_id, zone_id=zone_id)
        # Reject continuation of terminal tasks
        if task.status.state in TERMINAL_STATES:
            raise InvalidParamsError(
                data={
                    "taskId": task_id,
                    "currentState": task.status.state.value,
                    "detail": "Cannot continue a task in terminal state.",
                }
            )

        # Merge metadata (shallow) — Decision 17
        merged_metadata = {**(task.metadata or {}), **(send_params.metadata or {})}
        if merged_metadata:
            task = task.model_copy(update={"metadata": merged_metadata})
            await task_manager.store.save(task, zone_id=zone_id)

        # Add message to history and transition to working
        task = await task_manager.update_task_state(
            task_id,
            TaskState.WORKING,
            zone_id=zone_id,
            message=send_params.message,
        )
        return task.model_dump(mode="json")

    # New task
    task = await task_manager.create_task(
        send_params.message,
        zone_id=zone_id,
        agent_id=agent_id,
        metadata=send_params.metadata,
    )
    return task.model_dump(mode="json")


async def handle_get(
    params: dict[str, Any],
    zone_id: str,
    task_manager: TaskManager,
) -> dict[str, Any]:
    """Handle ``a2a.tasks.get`` — retrieve a task by ID."""
    try:
        query_params = TaskQueryParams.model_validate(params)
    except ValidationError as e:
        raise InvalidParamsError(data={"detail": str(e)}) from e

    task = await task_manager.get_task(
        query_params.taskId,
        zone_id=zone_id,
        history_length=query_params.historyLength,
    )
    return task.model_dump(mode="json")


async def handle_cancel(
    params: dict[str, Any],
    zone_id: str,
    task_manager: TaskManager,
) -> dict[str, Any]:
    """Handle ``a2a.tasks.cancel`` — cancel a running task."""
    try:
        cancel_params = TaskIdParams.model_validate(params)
    except ValidationError as e:
        raise InvalidParamsError(data={"detail": str(e)}) from e

    task = await task_manager.cancel_task(cancel_params.taskId, zone_id=zone_id)
    return task.model_dump(mode="json")
