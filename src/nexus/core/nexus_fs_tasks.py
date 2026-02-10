"""Task queue operations for NexusFS.

This module provides a thin facade over TaskQueueService:
- Submit durable tasks to the Rust-backed task queue
- Query task status and results
- Cancel tasks
- List tasks with filters
- Get queue statistics

All business logic is in TaskQueueService. This mixin only:
1. Provides @rpc_expose decorated methods
2. Instantiates service via cached_property
3. Delegates to service
"""

from __future__ import annotations

import logging
import os
from functools import cached_property
from typing import TYPE_CHECKING, Any

from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.services.task_queue_service import TaskQueueService

logger = logging.getLogger(__name__)


class NexusFSTasksMixin:
    """Mixin providing task queue RPC methods for NexusFS.

    Exposes durable task queue operations via @rpc_expose for
    automatic discovery by the FastAPI server.
    """

    # =========================================================================
    # Service Property (lazy initialization)
    # =========================================================================

    @cached_property
    def _task_queue_service(self) -> TaskQueueService:
        """Get or create TaskQueueService."""
        from nexus.services.task_queue_service import TaskQueueService

        # Determine data directory for task queue storage
        db_path = self._resolve_tasks_db_path()
        return TaskQueueService(db_path=db_path)

    def _resolve_tasks_db_path(self) -> str:
        """Resolve the path for the tasks fjall database.

        Priority:
        1. NEXUS_TASKS_DB_PATH environment variable
        2. NEXUS_DATA_DIR/tasks-db
        3. backend.root_path/../tasks-db (alongside backend storage)
        4. .nexus-data/tasks-db (fallback)

        Returns:
            Absolute path string for tasks database
        """
        # 1. Explicit env var
        env_path = os.environ.get("NEXUS_TASKS_DB_PATH")
        if env_path:
            return env_path

        # 2. Data dir env var
        data_dir = os.environ.get("NEXUS_DATA_DIR")
        if data_dir:
            return os.path.join(data_dir, "tasks-db")

        # 3. Backend root path (sibling to backend storage)
        backend = getattr(self, "backend", None)
        if backend is not None:
            root_path = getattr(backend, "root_path", None)
            if root_path is not None:
                return os.path.join(str(root_path), "tasks-db")

        # 4. Fallback
        return os.path.join(".nexus-data", "tasks-db")

    # =========================================================================
    # RPC-Exposed Task Queue Methods
    # =========================================================================

    @rpc_expose(description="Submit a task to the durable task queue")
    def submit_task(
        self,
        task_type: str,
        params_json: str = "{}",
        priority: int = 2,
        max_retries: int = 3,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Submit a task to the durable task queue.

        Args:
            task_type: Task type identifier (e.g. "data.export", "agent.run")
            params_json: JSON string of task parameters
            priority: Priority (0=critical, 1=high, 2=normal, 3=low, 4=best_effort)
            max_retries: Max retry attempts before dead letter
            context: Operation context (injected by server)

        Returns:
            Dict with task_id, status, task_type
        """
        return self._task_queue_service.submit_task(
            task_type=task_type,
            params_json=params_json,
            priority=priority,
            max_retries=max_retries,
        )

    @rpc_expose(description="Get task status and result")
    def get_task(
        self,
        task_id: int,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        """Get task status, progress, and result.

        Args:
            task_id: Task ID to look up
            context: Operation context (injected by server)

        Returns:
            Task details dict or None if not found
        """
        return self._task_queue_service.get_task(task_id)

    @rpc_expose(description="Cancel a pending or running task")
    def cancel_task(
        self,
        task_id: int,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Cancel a pending or running task.

        Args:
            task_id: Task ID to cancel
            context: Operation context (injected by server)

        Returns:
            Dict with success status and message
        """
        return self._task_queue_service.cancel_task(task_id)

    @rpc_expose(description="List tasks with optional filters")
    def list_queue_tasks(
        self,
        task_type: str | None = None,
        status: int | None = None,
        limit: int = 50,
        offset: int = 0,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        """List tasks with optional filters.

        Args:
            task_type: Filter by task type
            status: Filter by status code (0=pending, 1=running, 2=completed,
                    3=failed, 4=dead_letter, 5=cancelled)
            limit: Maximum tasks to return
            offset: Pagination offset
            context: Operation context (injected by server)

        Returns:
            List of task dicts
        """
        return self._task_queue_service.list_tasks(
            task_type=task_type,
            status=status,
            limit=limit,
            offset=offset,
        )

    @rpc_expose(description="Get task queue statistics")
    def get_task_stats(
        self,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Get task queue statistics.

        Args:
            context: Operation context (injected by server)

        Returns:
            Dict with pending, running, completed, failed, dead_letter counts
        """
        return self._task_queue_service.get_task_stats()
