"""Task Manager RPC Service — mission and task lifecycle.

Covers all task_manager.py endpoints except SSE streaming.
"""

import logging
from typing import Any, cast

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class TaskManagerRPCService:
    """RPC surface for task/mission management operations."""

    def __init__(self, task_manager_service: Any) -> None:
        self._svc = task_manager_service

    # --- Missions ---

    @rpc_expose(description="Create a mission")
    async def mission_create(
        self,
        title: str,
        description: str = "",
        goal: str = "",
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._svc.create_mission(
                title=title,
                description=description,
                goal=goal,
            ),
        )

    @rpc_expose(description="List missions")
    async def mission_list(
        self,
        status: str | None = None,
        archived: bool = False,
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._svc.list_missions(
                status=status,
                archived=archived,
                page=page,
                limit=limit,
            ),
        )

    @rpc_expose(description="Get mission details")
    async def mission_get(self, mission_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self._svc.get_mission(mission_id))

    @rpc_expose(description="Update a mission")
    async def mission_update(self, mission_id: str, **kwargs: Any) -> dict[str, Any]:
        return cast(dict[str, Any], await self._svc.update_mission(mission_id, **kwargs))

    # --- Tasks ---

    @rpc_expose(description="Create a task")
    async def task_create(
        self,
        title: str,
        mission_id: str | None = None,
        description: str = "",
        worker_type: str | None = None,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._svc.create_task(
                title=title,
                mission_id=mission_id,
                description=description,
                worker_type=worker_type,
            ),
        )

    @rpc_expose(description="List tasks")
    async def task_list(self, worker_type: str | None = None) -> dict[str, Any]:
        return cast(dict[str, Any], await self._svc.list_tasks(worker_type=worker_type))

    @rpc_expose(description="Get task details")
    async def task_get(self, task_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self._svc.get_task(task_id))

    @rpc_expose(description="Update a task")
    async def task_update(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        return cast(dict[str, Any], await self._svc.update_task(task_id, **kwargs))

    @rpc_expose(description="Get task history timeline")
    async def task_history(self, task_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self._svc.get_task_history(task_id))

    @rpc_expose(description="Create a task audit entry")
    async def task_audit_create(
        self,
        task_id: str,
        action: str,
        details: str = "",
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._svc.create_audit_entry(
                task_id=task_id,
                action=action,
                details=details,
            ),
        )

    # --- Comments & Artifacts ---

    @rpc_expose(description="Create a comment")
    async def comment_create(
        self,
        task_id: str,
        content: str,
        author: str = "",
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._svc.create_comment(
                task_id=task_id,
                content=content,
                author=author,
            ),
        )

    @rpc_expose(description="List comments for a task")
    async def comment_list(self, task_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self._svc.list_comments(task_id=task_id))

    @rpc_expose(description="Create an artifact")
    async def artifact_create(
        self,
        task_id: str,
        name: str,
        artifact_type: str = "file",
        url: str = "",
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._svc.create_artifact(
                task_id=task_id,
                name=name,
                artifact_type=artifact_type,
                url=url,
            ),
        )
