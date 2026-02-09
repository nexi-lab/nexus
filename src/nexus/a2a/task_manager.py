"""A2A task lifecycle manager.

Handles task CRUD, state machine transitions, and active SSE stream
tracking.  All persistent state goes through SQLAlchemy; active streams
are held in an in-memory dict of ``asyncio.Queue`` objects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

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

logger = logging.getLogger(__name__)


class TaskManager:
    """Manages A2A task lifecycle, persistence, and streaming.

    Parameters
    ----------
    session_factory:
        A callable that returns a SQLAlchemy ``Session`` (sync) or an
        async session factory.  When *None* an in-memory dict is used
        instead (useful for testing and embedded mode).
    """

    def __init__(self, session_factory: Any = None) -> None:
        self._session_factory = session_factory
        # In-memory fallback store: task_id -> task dict
        self._memory_store: dict[str, dict[str, Any]] = {}
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

        await self._persist_task(task, zone_id=zone_id, agent_id=agent_id)
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
        task = await self._load_task(task_id, zone_id=zone_id)
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
        if self._session_factory is not None:
            return await self._list_tasks_db(
                zone_id=zone_id,
                agent_id=agent_id,
                state=state,
                limit=limit,
                offset=offset,
            )
        # In-memory fallback
        tasks: list[Task] = []
        for record in self._memory_store.values():
            if record["zone_id"] != zone_id:
                continue
            if agent_id is not None and record.get("agent_id") != agent_id:
                continue
            task = self._record_to_task(record)
            if state is not None and task.status.state != state:
                continue
            tasks.append(task)
        # Sort by created_at descending
        tasks.sort(key=lambda t: t.status.timestamp or datetime.min, reverse=True)
        return tasks[offset : offset + limit]

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
        task = await self._load_task(task_id, zone_id=zone_id)
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
        task = await self._load_task(task_id, zone_id=zone_id)
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

        await self._update_task_in_store(task, zone_id=zone_id)

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
        task = await self._load_task(task_id, zone_id=zone_id)
        if task is None:
            raise TaskNotFoundError(data={"taskId": task_id})

        new_artifacts = [*task.artifacts, artifact]
        task = task.model_copy(update={"artifacts": new_artifacts})
        await self._update_task_in_store(task, zone_id=zone_id)

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

    # ------------------------------------------------------------------
    # Persistence helpers (in-memory or DB)
    # ------------------------------------------------------------------

    async def _persist_task(
        self,
        task: Task,
        *,
        zone_id: str,
        agent_id: str | None,
    ) -> None:
        """Save a new task to the store."""
        if self._session_factory is not None:
            await self._persist_task_db(task, zone_id=zone_id, agent_id=agent_id)
            return

        self._memory_store[task.id] = {
            "task": task.model_dump(mode="json"),
            "zone_id": zone_id,
            "agent_id": agent_id,
            "created_at": datetime.now(UTC).isoformat(),
        }

    async def _load_task(self, task_id: str, *, zone_id: str) -> Task | None:
        """Load a task from the store with zone isolation."""
        if self._session_factory is not None:
            return await self._load_task_db(task_id, zone_id=zone_id)

        record = self._memory_store.get(task_id)
        if record is None or record["zone_id"] != zone_id:
            return None
        return self._record_to_task(record)

    async def _update_task_in_store(self, task: Task, *, zone_id: str) -> None:
        """Update an existing task in the store."""
        if self._session_factory is not None:
            await self._update_task_db(task, zone_id=zone_id)
            return

        record = self._memory_store.get(task.id)
        if record is not None:
            record["task"] = task.model_dump(mode="json")

    @staticmethod
    def _record_to_task(record: dict[str, Any]) -> Task:
        """Deserialize a task from an in-memory record."""
        return Task.model_validate(record["task"])

    # ------------------------------------------------------------------
    # Database persistence (SQLAlchemy)
    # ------------------------------------------------------------------

    async def _persist_task_db(
        self,
        task: Task,
        *,
        zone_id: str,
        agent_id: str | None,
    ) -> None:
        from nexus.a2a.db import A2ATaskModel

        model = A2ATaskModel(
            id=task.id,
            context_id=task.contextId,
            zone_id=zone_id,
            agent_id=agent_id,
            state=task.status.state.value,
            messages_json=json.dumps([m.model_dump(mode="json") for m in task.history]),
            artifacts_json=json.dumps([a.model_dump(mode="json") for a in task.artifacts]),
            metadata_json=json.dumps(task.metadata) if task.metadata else None,
        )

        session = self._session_factory()
        try:
            session.add(model)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    async def _load_task_db(self, task_id: str, *, zone_id: str) -> Task | None:
        from nexus.a2a.db import A2ATaskModel

        session = self._session_factory()
        try:
            row = session.get(A2ATaskModel, task_id)
            if row is None or row.zone_id != zone_id:
                return None
            return self._db_row_to_task(row)
        finally:
            session.close()

    async def _update_task_db(self, task: Task, *, zone_id: str) -> None:
        from nexus.a2a.db import A2ATaskModel

        session = self._session_factory()
        try:
            row = session.get(A2ATaskModel, task.id)
            if row is None or row.zone_id != zone_id:
                raise TaskNotFoundError(data={"taskId": task.id})
            row.state = task.status.state.value
            row.messages_json = json.dumps([m.model_dump(mode="json") for m in task.history])
            row.artifacts_json = json.dumps([a.model_dump(mode="json") for a in task.artifacts])
            row.metadata_json = json.dumps(task.metadata) if task.metadata else None
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    async def _list_tasks_db(
        self,
        *,
        zone_id: str,
        agent_id: str | None,
        state: TaskState | None,
        limit: int,
        offset: int,
    ) -> list[Task]:
        from sqlalchemy import select

        from nexus.a2a.db import A2ATaskModel

        session = self._session_factory()
        try:
            stmt = (
                select(A2ATaskModel)
                .where(A2ATaskModel.zone_id == zone_id)
                .order_by(A2ATaskModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            if agent_id is not None:
                stmt = stmt.where(A2ATaskModel.agent_id == agent_id)
            if state is not None:
                stmt = stmt.where(A2ATaskModel.state == state.value)

            rows = session.execute(stmt).scalars().all()
            return [self._db_row_to_task(row) for row in rows]
        finally:
            session.close()

    @staticmethod
    def _db_row_to_task(row: Any) -> Task:
        """Convert a database row to a Task model."""
        messages = json.loads(row.messages_json) if row.messages_json else []
        artifacts = json.loads(row.artifacts_json) if row.artifacts_json else []
        metadata = json.loads(row.metadata_json) if row.metadata_json else None

        return Task(
            id=row.id,
            contextId=row.context_id,
            status=TaskStatus(
                state=TaskState(row.state),
                timestamp=row.updated_at,
            ),
            history=[Message.model_validate(m) for m in messages],
            artifacts=[Artifact.model_validate(a) for a in artifacts],
            metadata=metadata,
        )


# ------------------------------------------------------------------
# Utility
# ------------------------------------------------------------------

from contextlib import suppress as _suppress  # noqa: E402
