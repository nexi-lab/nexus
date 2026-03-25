"""Workflows RPC Service — workflow lifecycle management.

Issue #1522.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class WorkflowsRPCService:
    """RPC surface for workflow operations."""

    def __init__(self, workflow_engine: Any) -> None:
        self._engine = workflow_engine

    @rpc_expose(description="List all workflows")
    async def workflows_list(self) -> dict[str, Any]:
        workflows = await self._engine.list_workflows()
        return {
            "workflows": [
                {"name": w.name, "enabled": w.enabled, "trigger": w.trigger} for w in workflows
            ],
            "count": len(workflows),
        }

    @rpc_expose(description="Create a workflow")
    async def workflows_create(
        self,
        name: str,
        trigger: str,
        steps: list[dict[str, Any]],
        description: str = "",
    ) -> dict[str, Any]:
        wf = await self._engine.create_workflow(
            name=name,
            trigger=trigger,
            steps=steps,
            description=description,
        )
        return {"name": wf.name, "created": True}

    @rpc_expose(description="Get workflow details")
    async def workflows_get(self, name: str) -> dict[str, Any]:
        wf = await self._engine.get_workflow(name)
        if wf is None:
            return {"error": f"Workflow '{name}' not found"}
        return {
            "name": wf.name,
            "trigger": wf.trigger,
            "enabled": wf.enabled,
            "steps": wf.steps,
            "description": wf.description,
        }

    @rpc_expose(description="Delete a workflow")
    async def workflows_delete(self, name: str) -> dict[str, Any]:
        deleted = await self._engine.delete_workflow(name)
        return {"deleted": deleted, "name": name}

    @rpc_expose(description="Enable a workflow")
    async def workflows_enable(self, name: str) -> dict[str, Any]:
        await self._engine.enable_workflow(name)
        return {"name": name, "enabled": True}

    @rpc_expose(description="Disable a workflow")
    async def workflows_disable(self, name: str) -> dict[str, Any]:
        await self._engine.disable_workflow(name)
        return {"name": name, "enabled": False}

    @rpc_expose(description="Execute a workflow manually")
    async def workflows_execute(
        self, name: str, inputs: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        result = await self._engine.execute(name, inputs=inputs or {})
        return {
            "name": name,
            "execution_id": result.execution_id,
            "status": result.status,
        }

    @rpc_expose(description="List workflow executions")
    async def workflows_executions(self, name: str, limit: int = 20) -> dict[str, Any]:
        executions = await self._engine.list_executions(name, limit=limit)
        return {
            "name": name,
            "executions": [
                {
                    "execution_id": e.execution_id,
                    "status": e.status,
                    "started_at": str(e.started_at) if e.started_at else None,
                }
                for e in executions
            ],
        }
