"""Task Queue Service - Durable task queue management via Rust TaskEngine.

This service wraps the nexus-tasks Rust crate (PyO3 bindings) with
Nexus conventions, returning JSON-serializable dicts.

Follows the SyncJobService pattern for consistent service integration.

Example:
    ```python
    service = TaskQueueService("/path/to/tasks-db")
    result = service.submit_task("test.echo", '{"msg": "hello"}')
    print(f"Task ID: {result['task_id']}")

    task = service.get_task(result["task_id"])
    print(f"Status: {task['status']}")
    ```
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from _nexus_tasks import TaskEngine

logger = logging.getLogger(__name__)

# Status code to human-readable name mapping
_STATUS_NAMES = {
    0: "pending",
    1: "running",
    2: "completed",
    3: "failed",
    4: "dead_letter",
    5: "cancelled",
}


def _task_record_to_dict(record: Any) -> dict[str, Any]:
    """Convert a PyTaskRecord to a JSON-serializable dict."""
    return {
        "task_id": record.task_id,
        "task_type": record.task_type,
        "params": record.params.decode("utf-8", errors="replace") if record.params else "",
        "priority": record.priority,
        "status": record.status,
        "status_name": _STATUS_NAMES.get(record.status, "unknown"),
        "result": record.result.decode("utf-8", errors="replace") if record.result else None,
        "error_message": record.error_message,
        "attempt": record.attempt,
        "max_retries": record.max_retries,
        "created_at": record.created_at,
        "run_at": record.run_at,
        "claimed_by": record.claimed_by,
        "progress_pct": record.progress_pct,
        "progress_message": record.progress_message,
        "completed_at": record.completed_at,
    }


class TaskQueueService:
    """Manages durable task queue operations via Rust TaskEngine.

    Provides a Nexus-convention wrapper that returns JSON-serializable dicts.
    The underlying TaskEngine is lazily initialized on first use.
    """

    def __init__(self, db_path: str, max_pending: int = 1000) -> None:
        """Initialize task queue service.

        Args:
            db_path: Path for fjall database storage
            max_pending: Maximum pending tasks (admission control)
        """
        self._db_path = db_path
        self._max_pending = max_pending
        self._engine: TaskEngine | None = None
        self._runner: Any = None  # AsyncTaskRunner, set by lifespan

    def _get_engine(self) -> TaskEngine:
        """Get or create TaskEngine (lazy init).

        Returns:
            TaskEngine instance
        """
        if self._engine is None:
            from _nexus_tasks import TaskEngine

            self._engine = TaskEngine(
                self._db_path,
                max_pending=self._max_pending,
            )
            logger.info(f"TaskEngine initialized at {self._db_path}")
        return self._engine

    def get_engine(self) -> TaskEngine:
        """Public accessor for TaskEngine (used by AsyncTaskRunner).

        Returns:
            TaskEngine instance
        """
        return self._get_engine()

    def set_runner(self, runner: Any) -> None:
        """Attach an AsyncTaskRunner to this service.

        Called by server lifespan to wire up the background task runner.

        Args:
            runner: AsyncTaskRunner instance
        """
        self._runner = runner

    def submit_task(
        self,
        task_type: str,
        params_json: str = "{}",
        priority: int = 2,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        """Submit a task to the durable queue.

        Args:
            task_type: Task type identifier (e.g. "test.echo")
            params_json: JSON string of task parameters
            priority: Priority level (0=critical, 4=best_effort)
            max_retries: Maximum retry attempts before dead letter

        Returns:
            Dict with task_id and status

        Raises:
            ValueError: If inputs fail validation
        """
        # Input validation
        if not task_type or not task_type.strip():
            raise ValueError("task_type must be a non-empty string")
        if len(task_type) > 256:
            raise ValueError("task_type must be <= 256 characters")
        if not 0 <= priority <= 4:
            raise ValueError(f"priority must be 0-4, got {priority}")
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
        if len(params_json) > 1_048_576:  # 1MB limit
            raise ValueError("params_json must be <= 1MB")

        engine = self._get_engine()
        params_bytes = params_json.encode("utf-8")
        task_id = engine.submit(task_type, params_bytes, priority=priority, max_retries=max_retries)
        return {
            "task_id": task_id,
            "status": "pending",
            "task_type": task_type,
        }

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        """Get task status and details.

        Args:
            task_id: Task ID to look up

        Returns:
            Task details dict or None if not found
        """
        engine = self._get_engine()
        record = engine.status(task_id)
        if record is None:
            return None
        return _task_record_to_dict(record)

    def cancel_task(self, task_id: int) -> dict[str, Any]:
        """Cancel a pending or running task.

        Args:
            task_id: Task ID to cancel

        Returns:
            Dict with success status
        """
        engine = self._get_engine()
        try:
            engine.cancel(task_id)
            return {
                "success": True,
                "task_id": task_id,
                "message": "Task cancelled",
            }
        except RuntimeError as e:
            return {
                "success": False,
                "task_id": task_id,
                "message": str(e),
            }

    def list_tasks(
        self,
        task_type: str | None = None,
        status: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List tasks with optional filters.

        Args:
            task_type: Filter by task type
            status: Filter by status code (0-5)
            limit: Maximum tasks to return
            offset: Pagination offset

        Returns:
            List of task dicts

        Raises:
            ValueError: If limit or offset are invalid
        """
        # Input validation
        limit = min(max(1, limit), 1000)  # Clamp to [1, 1000]
        offset = max(0, offset)
        if status is not None and not 0 <= status <= 5:
            raise ValueError(f"status must be 0-5, got {status}")

        engine = self._get_engine()
        records = engine.list_tasks(
            status=status,
            task_type=task_type,
            limit=limit,
            offset=offset,
        )
        return [_task_record_to_dict(r) for r in records]

    def get_task_stats(self) -> dict[str, Any]:
        """Get queue statistics.

        Returns:
            Dict with pending, running, completed, failed, dead_letter, cancelled counts
        """
        engine = self._get_engine()
        stats = engine.stats()
        return {
            "pending": stats.pending,
            "running": stats.running,
            "completed": stats.completed,
            "failed": stats.failed,
            "dead_letter": stats.dead_letter,
            "cancelled": getattr(stats, "cancelled", 0),
        }
