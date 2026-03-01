"""TaskQueueService protocol (Issue #696).

Defines the contract for durable task-queue operations backed by Rust TaskEngine.

Existing implementation: ``nexus.services.task_queue_service.TaskQueueService``

References:
    - docs/design/KERNEL-ARCHITECTURE.md §1 (service DI)
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TaskQueueProtocol(Protocol):
    """Service contract for durable task-queue operations."""

    def submit_task(
        self,
        task_type: str,
        params_json: str = "{}",
        priority: int = 2,
        max_retries: int = 3,
    ) -> dict[str, Any]: ...

    def get_task(self, task_id: int) -> dict[str, Any] | None: ...

    def cancel_task(self, task_id: int) -> dict[str, Any]: ...

    def list_tasks(
        self,
        task_type: str | None = None,
        status: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...

    def get_task_stats(self) -> dict[str, Any]: ...

    def get_engine(self) -> Any: ...

    def set_runner(self, runner: Any) -> None: ...
