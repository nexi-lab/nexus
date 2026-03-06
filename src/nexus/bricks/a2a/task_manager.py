"""A2A task lifecycle manager.

Handles task CRUD and state machine transitions.  All persistent state
goes through a pluggable ``TaskStoreProtocol``; active SSE streams are
managed by a ``StreamRegistry`` (injected via DI).
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.bricks.a2a.exceptions import (
    InvalidStateTransitionError,
    StaleTaskVersionError,
    TaskNotCancelableError,
    TaskNotFoundError,
)
from nexus.bricks.a2a.models import (
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
from nexus.contracts.constants import ROOT_ZONE_ID

# Callback type: (artifact, task_id, zone_id) -> None
ArtifactCallback = Callable[[Any, str, str], Awaitable[None]]

if TYPE_CHECKING:
    import asyncio

    from nexus.bricks.a2a.stream_registry import StreamRegistry
    from nexus.bricks.a2a.task_store import TaskStoreProtocol

logger = logging.getLogger(__name__)


class TaskManager:
    """Manages A2A task lifecycle and persistence.

    Parameters
    ----------
    store:
        A ``TaskStoreProtocol`` implementation.  When *None* a
        ``CacheBackedTaskStore(InMemoryCacheStore())`` is used (useful
        for testing and embedded mode).
    stream_registry:
        A ``StreamRegistry`` for managing active SSE streams.  When
        *None* a default instance is created.
    """

    def __init__(
        self,
        store: "TaskStoreProtocol | None" = None,
        stream_registry: "StreamRegistry | None" = None,
        artifact_observers: "list[ArtifactCallback] | None" = None,
    ) -> None:
        if store is not None:
            self._store: TaskStoreProtocol = store
        else:
            from nexus.bricks.a2a.stores.in_memory import CacheBackedTaskStore
            from nexus.contracts.cache_store import InMemoryCacheStore

            self._store = CacheBackedTaskStore(InMemoryCacheStore())

        if stream_registry is not None:
            self._stream_registry = stream_registry
        else:
            from nexus.bricks.a2a.stream_registry import StreamRegistry as _SR

            self._stream_registry = _SR()

        self._artifact_observers = artifact_observers or []

    @property
    def stream_registry(self) -> "StreamRegistry":
        """Public access to the stream registry."""
        return self._stream_registry

    @property
    def store(self) -> "TaskStoreProtocol":
        """Public access to the underlying task store."""
        return self._store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_task(
        self,
        message: Message,
        *,
        zone_id: str = ROOT_ZONE_ID,
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
        zone_id: str = ROOT_ZONE_ID,
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
        zone_id: str = ROOT_ZONE_ID,
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
        zone_id: str = ROOT_ZONE_ID,
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
        zone_id: str = ROOT_ZONE_ID,
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

        current_version = task.version
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
        updates: dict[str, Any] = {
            "status": new_status,
            "version": current_version + 1,
        }

        if message is not None:
            updates["history"] = [*task.history, message]

        task = task.model_copy(update=updates)

        try:
            await self._store.save(task, zone_id=zone_id, expected_version=current_version)
        except StaleTaskVersionError:
            raise InvalidStateTransitionError(
                message=(
                    f"Concurrent modification detected on task {task_id} "
                    f"(expected version {current_version})"
                ),
                data={
                    "taskId": task_id,
                    "currentState": current_state.value,
                    "requestedState": new_state.value,
                    "expectedVersion": current_version,
                },
            ) from None

        # Push status update to active streams
        event = TaskStatusUpdateEvent(
            taskId=task_id,
            status=new_status,
            final=new_state in TERMINAL_STATES,
        )
        self._stream_registry.push_event(task_id, {"statusUpdate": event.model_dump(mode="json")})

        return task

    async def add_artifact(
        self,
        task_id: str,
        artifact: Artifact,
        *,
        zone_id: str = ROOT_ZONE_ID,
        append: bool = True,
    ) -> Task:
        """Add an artifact to a task.

        Pushes an artifact update event to any active SSE streams,
        then notifies artifact observers for downstream indexing
        (Issue #1861).
        """
        task = await self._store.get(task_id, zone_id=zone_id)
        if task is None:
            raise TaskNotFoundError(data={"taskId": task_id})

        current_version = task.version
        new_artifacts = [*task.artifacts, artifact]
        task = task.model_copy(update={"artifacts": new_artifacts, "version": current_version + 1})

        try:
            await self._store.save(task, zone_id=zone_id, expected_version=current_version)
        except StaleTaskVersionError:
            raise InvalidStateTransitionError(
                message=(
                    f"Concurrent modification detected on task {task_id} "
                    f"(expected version {current_version})"
                ),
                data={
                    "taskId": task_id,
                    "expectedVersion": current_version,
                },
            ) from None

        event = TaskArtifactUpdateEvent(
            taskId=task_id,
            artifact=artifact,
            append=append,
        )
        self._stream_registry.push_event(task_id, {"artifactUpdate": event.model_dump(mode="json")})

        # Notify artifact observers (Issue #1861)
        for _obs in self._artifact_observers:
            try:
                await _obs(artifact, task_id, zone_id)
            except Exception as exc:
                logger.warning("Artifact observer failed: %s", exc)

        return task

    # ------------------------------------------------------------------
    # Stream management (delegates to StreamRegistry)
    # ------------------------------------------------------------------

    def register_stream(self, task_id: str) -> "asyncio.Queue[dict[str, Any] | None]":
        """Register a new SSE stream for a task."""
        return self._stream_registry.register(task_id)

    def unregister_stream(
        self, task_id: str, queue: "asyncio.Queue[dict[str, Any] | None]"
    ) -> None:
        """Remove an SSE stream registration."""
        self._stream_registry.unregister(task_id, queue)
