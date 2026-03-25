"""Scheduler RPC Service — task scheduling.

Issue #1212.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class SchedulerRPCService:
    """RPC surface for task scheduling operations."""

    def __init__(self, scheduler_service: Any) -> None:
        self._scheduler = scheduler_service

    @rpc_expose(description="Submit a task to the scheduler")
    async def scheduler_submit(
        self,
        executor: str,
        task_type: str = "default",
        payload: dict[str, Any] | None = None,
        priority: str = "normal",
        deadline: str | None = None,
    ) -> dict[str, Any]:
        result = await self._scheduler.submit(
            executor=executor,
            task_type=task_type,
            payload=payload or {},
            priority=priority,
            deadline=deadline,
        )
        return {
            "task_id": result.task_id,
            "status": result.status,
            "priority": result.priority,
        }

    @rpc_expose(description="Get task status")
    async def scheduler_task_status(self, task_id: str) -> dict[str, Any]:
        result = await self._scheduler.get_task(task_id)
        if result is None:
            return {"error": f"Task {task_id} not found"}
        return {
            "task_id": result.task_id,
            "status": result.status,
            "priority": result.priority,
            "created_at": result.created_at.isoformat() if result.created_at else None,
        }

    @rpc_expose(description="Cancel a scheduled task")
    async def scheduler_cancel(self, task_id: str) -> dict[str, Any]:
        cancelled = await self._scheduler.cancel(task_id)
        return {"cancelled": cancelled, "task_id": task_id}
