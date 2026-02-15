"""A2A task storage protocol.

Defines the pluggable storage interface for A2A tasks.  Implementations
include in-memory (testing), VFS-backed (file-based), and database-backed
(PostgreSQL/SQLite).

Follows the same pattern as Google's ``a2a-python`` SDK ``TaskStore``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.a2a.models import Task, TaskState


@runtime_checkable
class TaskStoreProtocol(Protocol):
    """Pluggable storage backend for A2A tasks.

    All methods enforce zone isolation: a task stored in zone "alpha"
    is invisible from zone "beta".
    """

    async def save(
        self,
        task: Task,
        *,
        zone_id: str,
        agent_id: str | None = None,
    ) -> None:
        """Persist a new task or update an existing one.

        Parameters
        ----------
        task:
            The task to persist.  If a task with the same ``id`` already
            exists in the given zone, it is overwritten.
        zone_id:
            Multi-tenant zone identifier.
        agent_id:
            Optional agent that owns this task.
        """
        ...

    async def get(self, task_id: str, *, zone_id: str) -> Task | None:
        """Load a task by ID with zone isolation.

        Returns *None* if the task does not exist or belongs to a
        different zone.
        """
        ...

    async def delete(self, task_id: str, *, zone_id: str) -> bool:
        """Delete a task.

        Returns *True* if the task was deleted, *False* if it was not
        found (or belongs to a different zone).
        """
        ...

    async def list_tasks(
        self,
        *,
        zone_id: str,
        agent_id: str | None = None,
        state: TaskState | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        """List tasks with optional filters.

        Results are ordered by creation time descending (newest first).
        """
        ...
