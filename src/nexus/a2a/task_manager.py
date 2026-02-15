"""A2A task lifecycle manager.

Handles task CRUD, state machine transitions, and active SSE stream
tracking.  All persistent state goes through a pluggable
``TaskStoreProtocol``; active streams are held in an in-memory dict of
``asyncio.Queue`` objects.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress as _suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.a2a.exceptions import (
    InvalidStateTransitionError,
    TaskNotCancelableError,
    TaskNotFoundError,
)
from nexus.a2a.models import (
    TERMINAL_STATES,
    Artifact,
    Message,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    is_valid_transition,
)

if TYPE_CHECKING:
    from nexus.a2a.task_store import TaskStoreProtocol

logger = logging.getLogger(__name__)


class TaskManager:
    """Manages A2A task lifecycle, persistence, and streaming.

    Parameters
    ----------
    store:
        A ``TaskStoreProtocol`` implementation.  When *None* an
        ``InMemoryTaskStore`` is used (useful for testing and embedded
        mode).
    session_factory:
        **Deprecated.**  If provided (and *store* is None), wraps the
        session factory in a ``DatabaseTaskStore`` for backwards
        compatibility.
    """

    def __init__(
        self,
        store: TaskStoreProtocol | None = None,
        session_factory: Any = None,
    ) -> None:
        if store is not None:
            self._store: TaskStoreProtocol = store
        elif session_factory is not None:
            from nexus.a2a.stores.database import DatabaseTaskStore

            self._store = DatabaseTaskStore(session_factory)
        else:
            from nexus.a2a.stores.in_memory import InMemoryTaskStore

            self._store = InMemoryTaskStore()

        # Active SSE streams: task_id -> list of asyncio.Queue
        self._active_streams: dict[str, list[asyncio.Queue[dict[str, Any] | None]]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_task(
        self,
        message: Message,
        *,
        zone_id: str = "default",
        agent_id: str | None = None,
        context_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        """Create a new task from an incoming message.

        The task starts in the ``submitted`` state.
        """
        task_id = str(uuid.uuid4())
        now = datetime.now(UTC)

        task = Task(
            id=task_id,
            contextId=context_id or str(uuid.uuid4()),
            status=TaskStatus(state=TaskState.SUBMITTED, timestamp=now),
            history=[message],
            metadata=metadata,
        )

        await self._store.save(task, zone_id=zone_id, agent_id=agent_id)
        return task

    async def get_task(
        self,
        task_id: str,
        *,
        zone_id: str = "default",
        history_length: int | None = None,
    ) -> Task:
        """Retrieve a task by ID.

        Raises ``TaskNotFoundError`` if the task does not exist or belongs
        to a different zone.
        """
        task = await self._store.get(task_id, zone_id=zone_id)
        if task is None:
            raise TaskNotFoundError(data={"taskId": task_id})

        if history_length is not None and history_length >= 0:
            if history_length == 0:
                task = task.model_copy(update={"history": []})
            else:
                task = task.model_copy(update={"history": task.history[-history_length:]})
        return task

    async def list_tasks(
        self,
        *,
        zone_id: str = "default",
        agent_id: str | None = None,
        state: TaskState | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        """List tasks with optional filters."""
        return await self._store.list_tasks(
            zone_id=zone_id,
            agent_id=agent_id,
            state=state,
            limit=limit,
            offset=offset,
        )

    async def cancel_task(
        self,
        task_id: str,
        *,
        zone_id: str = "default",
    ) -> Task:
        """Cancel a task.

        Raises ``TaskNotFoundError`` if the task does not exist.
        Raises ``TaskNotCancelableError`` if the task is in a terminal state.
        """
        task = await self._store.get(task_id, zone_id=zone_id)
        if task is None:
            raise TaskNotFoundError(data={"taskId": task_id})

        if task.status.state in TERMINAL_STATES:
            raise TaskNotCancelableError(
                data={
                    "taskId": task_id,
                    "currentState": task.status.state.value,
                }
            )

        return await self.update_task_state(task_id, TaskState.CANCELED, zone_id=zone_id)

    async def update_task_state(
        self,
        task_id: str,
        new_state: TaskState,
        *,
        zone_id: str = "default",
        message: Message | None = None,
    ) -> Task:
        """Transition a task to a new state.

        Validates the transition against the state machine.
        Pushes a status update event to any active SSE streams.

        Raises ``TaskNotFoundError`` if the task does not exist.
        Raises ``InvalidStateTransitionError`` if the transition is invalid.
        """
        task = await self._store.get(task_id, zone_id=zone_id)
        if task is None:
            raise TaskNotFoundError(data={"taskId": task_id})

        current_state = task.status.state
        if not is_valid_transition(current_state, new_state):
            raise InvalidStateTransitionError(
                message=f"Cannot transition from {current_state.value} to {new_state.value}",
                data={
                    "taskId": task_id,
                    "currentState": current_state.value,
                    "requestedState": new_state.value,
                },
            )

        now = datetime.now(UTC)
        new_status = TaskStatus(state=new_state, message=message, timestamp=now)
        task = task.model_copy(update={"status": new_status})

        if message is not None:
            task = task.model_copy(update={"history": [*task.history, message]})

        await self._store.save(task, zone_id=zone_id)

        # Push status update to active streams
        event = TaskStatusUpdateEvent(
            taskId=task_id,
            status=new_status,
            final=new_state in TERMINAL_STATES,
        )
        await self._push_event(task_id, {"statusUpdate": event.model_dump(mode="json")})

        return task

    async def add_artifact(
        self,
        task_id: str,
        artifact: Artifact,
        *,
        zone_id: str = "default",
        append: bool = True,
    ) -> Task:
        """Add an artifact to a task.

        Pushes an artifact update event to any active SSE streams.
        """
        task = await self._store.get(task_id, zone_id=zone_id)
        if task is None:
            raise TaskNotFoundError(data={"taskId": task_id})

        new_artifacts = [*task.artifacts, artifact]
        task = task.model_copy(update={"artifacts": new_artifacts})
        await self._store.save(task, zone_id=zone_id)

        event = TaskArtifactUpdateEvent(
            taskId=task_id,
            artifact=artifact,
            append=append,
        )
        await self._push_event(task_id, {"artifactUpdate": event.model_dump(mode="json")})

        return task

    # ------------------------------------------------------------------
    # Active stream management
    # ------------------------------------------------------------------

    def register_stream(self, task_id: str) -> asyncio.Queue[dict[str, Any] | None]:
        """Register a new SSE stream for a task.

        Returns an ``asyncio.Queue`` that will receive stream events.
        ``None`` is pushed as a sentinel to signal stream closure.
        """
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._active_streams.setdefault(task_id, []).append(queue)
        return queue

    def unregister_stream(self, task_id: str, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        """Remove an SSE stream registration."""
        streams = self._active_streams.get(task_id)
        if streams is not None:
            with _suppress(ValueError):
                streams.remove(queue)
            if not streams:
                del self._active_streams[task_id]

    async def _push_event(self, task_id: str, event: dict[str, Any]) -> None:
        """Push an event to all active streams for a task."""
        streams = self._active_streams.get(task_id)
        if not streams:
            return
        for queue in list(streams):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("SSE queue full for task %s, dropping event", task_id)
