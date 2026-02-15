"""In-memory task store for testing and embedded mode.

Tasks are stored in a plain ``dict``.  Data is lost when the process
terminates.  This is the default store when no persistent backend is
configured.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from nexus.a2a.models import Task, TaskState


class InMemoryTaskStore:
    """Dict-backed task store.

    Each record is stored as::

        {
            "task": <Task dict>,
            "zone_id": str,
            "agent_id": str | None,
            "created_at": str,   # ISO 8601
        }
    """

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    async def save(
        self,
        task: Task,
        *,
        zone_id: str,
        agent_id: str | None = None,
    ) -> None:
        key = self._key(task.id, zone_id)
        existing = self._store.get(key)
        created_at = (
            existing["created_at"] if existing is not None else datetime.now(UTC).isoformat()
        )
        self._store[key] = {
            "task": task.model_dump(mode="json"),
            "zone_id": zone_id,
            "agent_id": agent_id,
            "created_at": created_at,
        }

    async def get(self, task_id: str, *, zone_id: str) -> Task | None:
        record = self._store.get(self._key(task_id, zone_id))
        if record is None:
            return None
        return Task.model_validate(record["task"])

    async def delete(self, task_id: str, *, zone_id: str) -> bool:
        key = self._key(task_id, zone_id)
        if key not in self._store:
            return False
        del self._store[key]
        return True

    async def list_tasks(
        self,
        *,
        zone_id: str,
        agent_id: str | None = None,
        state: TaskState | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        results: list[tuple[str, Task]] = []
        for record in self._store.values():
            if record["zone_id"] != zone_id:
                continue
            if agent_id is not None and record.get("agent_id") != agent_id:
                continue
            task = Task.model_validate(record["task"])
            if state is not None and task.status.state != state:
                continue
            results.append((record["created_at"], task))

        # Sort by created_at descending (newest first)
        results.sort(key=lambda pair: pair[0], reverse=True)
        tasks = [task for _, task in results]
        return tasks[offset : offset + limit]

    @staticmethod
    def _key(task_id: str, zone_id: str) -> str:
        """Composite key ensuring zone isolation."""
        return f"{zone_id}:{task_id}"
