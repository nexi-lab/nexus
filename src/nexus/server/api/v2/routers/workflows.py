"""Nexus Workflow REST API endpoints.

Provides 8 endpoints for workflow management:
- GET    /api/v2/workflows                       - List all workflows
- POST   /api/v2/workflows                       - Load workflow from JSON body
- GET    /api/v2/workflows/{name}                - Get workflow definition
- DELETE /api/v2/workflows/{name}                - Unload workflow
- POST   /api/v2/workflows/{name}/enable         - Enable workflow
- POST   /api/v2/workflows/{name}/disable        - Disable workflow
- POST   /api/v2/workflows/{name}/execute        - Manual execution
- GET    /api/v2/workflows/{name}/executions     - Execution history

Related: Issue #1522
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/workflows", tags=["workflows"])

# =============================================================================
# Pydantic Request/Response Models
# =============================================================================


class WorkflowTriggerModel(BaseModel):
    """A workflow trigger definition."""

    type: str = Field(..., description="Trigger type (e.g. 'file_write', 'file_delete')")
    config: dict[str, Any] = Field(default_factory=dict, description="Trigger configuration")


class WorkflowActionModel(BaseModel):
    """A workflow action definition."""

    name: str = Field(..., description="Action name")
    type: str = Field(..., description="Action type (e.g. 'python', 'bash', 'parse')")
    config: dict[str, Any] = Field(default_factory=dict, description="Action configuration")


class CreateWorkflowRequest(BaseModel):
    """Request to load a workflow definition."""

    name: str = Field(..., description="Workflow name")
    version: str = Field(default="1.0", description="Workflow version")
    description: str | None = Field(default=None, description="Workflow description")
    triggers: list[WorkflowTriggerModel] = Field(
        default_factory=list, description="Trigger definitions"
    )
    actions: list[WorkflowActionModel] = Field(..., description="Action definitions")
    variables: dict[str, Any] = Field(default_factory=dict, description="Default variables")
    enabled: bool = Field(default=True, description="Enable workflow after loading")


class ExecuteWorkflowRequest(BaseModel):
    """Request for manual workflow execution."""

    file_path: str | None = Field(
        default=None, description="Optional file path for trigger context"
    )
    context: dict[str, Any] = Field(
        default_factory=dict, description="Additional execution context"
    )


class WorkflowSummary(BaseModel):
    """Summary of a loaded workflow."""

    name: str
    version: str
    description: str | None = None
    enabled: bool
    triggers: int
    actions: int


class WorkflowDetail(BaseModel):
    """Full workflow definition."""

    name: str
    version: str
    description: str | None = None
    triggers: list[WorkflowTriggerModel] = Field(default_factory=list)
    actions: list[WorkflowActionModel] = Field(default_factory=list)
    variables: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ExecutionSummary(BaseModel):
    """Summary of a workflow execution."""

    execution_id: str
    workflow_id: str
    trigger_type: str
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    actions_completed: int = 0
    actions_total: int = 0
    error_message: str | None = None


class ExecutionResult(BaseModel):
    """Result from a manual execution."""

    execution_id: str
    status: str
    actions_completed: int = 0
    actions_total: int = 0
    error_message: str | None = None


# =============================================================================
# Dependencies
# =============================================================================


def _get_require_auth() -> Any:
    """Lazy import to avoid circular imports."""
    from nexus.server.dependencies import require_auth

    return require_auth


def _get_workflow_engine(request: Request) -> Any:
    """Get WorkflowEngine from app state or brick lifecycle."""
    engine = getattr(request.app.state, "workflow_engine", None)
    if not engine:
        # Fallback: check NexusFS for workflow_engine attribute, or brick lifecycle
        nx = getattr(request.app.state, "nexus_fs", None)
        if nx is not None:
            engine = getattr(nx, "_workflow_engine", None) or getattr(nx, "workflow_engine", None)
        if not engine:
            blm = getattr(request.app.state, "brick_lifecycle_manager", None)
            if blm is not None:
                for _name, _spec, _state, _retries, brick_inst in blm.iter_bricks():
                    if _name == "workflow_engine" and brick_inst is not None:
                        # Unwrap lifecycle adapter if needed
                        engine = getattr(brick_inst, "_engine", brick_inst)
                        break
        if engine:
            request.app.state.workflow_engine = engine
    if not engine:
        raise HTTPException(status_code=503, detail="Workflow engine not available")

    return engine


# =============================================================================
# Endpoints
# =============================================================================


@router.get("", response_model=list[WorkflowSummary])
async def list_workflows(
    engine: Any = Depends(_get_workflow_engine),
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> list[WorkflowSummary]:
    """List all loaded workflows."""
    workflows = engine.list_workflows()
    return [
        WorkflowSummary(
            name=w["name"],
            version=w["version"],
            description=w.get("description"),
            enabled=w["enabled"],
            triggers=w["triggers"],
            actions=w["actions"],
        )
        for w in workflows
    ]


@router.post("", response_model=WorkflowSummary, status_code=201)
async def create_workflow(
    body: CreateWorkflowRequest,
    engine: Any = Depends(_get_workflow_engine),
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> WorkflowSummary:
    """Load a workflow definition.

    Accepts a JSON workflow definition and loads it into the engine.
    """
    from nexus.bricks.workflows.loader import WorkflowLoader

    # Convert request to dict for WorkflowLoader
    workflow_dict: dict[str, Any] = {
        "name": body.name,
        "version": body.version,
        "description": body.description,
        "triggers": [{"type": t.type, **t.config} for t in body.triggers],
        "actions": [{"name": a.name, "type": a.type, **a.config} for a in body.actions],
        "variables": body.variables,
    }

    try:
        definition = WorkflowLoader.load_from_dict(workflow_dict)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid workflow definition: {e}") from e

    success = engine.load_workflow(definition, enabled=body.enabled)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to load workflow")

    return WorkflowSummary(
        name=definition.name,
        version=definition.version,
        description=definition.description,
        enabled=body.enabled,
        triggers=len(definition.triggers),
        actions=len(definition.actions),
    )


@router.get("/{name}", response_model=WorkflowDetail)
async def get_workflow(
    name: str,
    engine: Any = Depends(_get_workflow_engine),
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> WorkflowDetail:
    """Get a workflow definition by name."""
    definition = engine.workflows.get(name)
    if not definition:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")

    return WorkflowDetail(
        name=definition.name,
        version=definition.version,
        description=definition.description,
        triggers=[
            WorkflowTriggerModel(type=t.type.value, config=t.config) for t in definition.triggers
        ],
        actions=[
            WorkflowActionModel(name=a.name, type=a.type, config=a.config)
            for a in definition.actions
        ],
        variables=definition.variables or {},
        enabled=engine.enabled_workflows.get(name, False),
    )


@router.delete("/{name}", status_code=204)
async def delete_workflow(
    name: str,
    engine: Any = Depends(_get_workflow_engine),
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> None:
    """Unload a workflow by name."""
    success = engine.unload_workflow(name)
    if not success:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")


@router.post("/{name}/enable", status_code=204)
async def enable_workflow(
    name: str,
    engine: Any = Depends(_get_workflow_engine),
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> None:
    """Enable a workflow."""
    if name not in engine.workflows:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
    engine.enable_workflow(name)


@router.post("/{name}/disable", status_code=204)
async def disable_workflow(
    name: str,
    engine: Any = Depends(_get_workflow_engine),
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> None:
    """Disable a workflow."""
    if name not in engine.workflows:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
    engine.disable_workflow(name)


@router.post("/{name}/execute", response_model=ExecutionResult)
async def execute_workflow(
    name: str,
    body: ExecuteWorkflowRequest | None = None,
    engine: Any = Depends(_get_workflow_engine),
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> ExecutionResult:
    """Manually trigger a workflow execution."""
    event_context: dict[str, Any] = {}
    if body:
        event_context = dict(body.context)
        if body.file_path:
            event_context["file_path"] = body.file_path

    execution = await engine.trigger_workflow(name, event_context)
    if not execution:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow '{name}' not found or disabled",
        )

    return ExecutionResult(
        execution_id=str(execution.execution_id),
        status=execution.status.value,
        actions_completed=execution.actions_completed,
        actions_total=execution.actions_total,
        error_message=execution.error_message,
    )


@router.get("/{name}/executions", response_model=list[ExecutionSummary])
async def get_executions(
    name: str,
    limit: int = Query(default=10, ge=1, le=100, description="Max results to return"),
    engine: Any = Depends(_get_workflow_engine),
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> list[ExecutionSummary]:
    """Get execution history for a workflow."""
    if name not in engine.workflows:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")

    if not engine.workflow_store:
        return []

    executions = await engine.workflow_store.get_executions(name=name, limit=limit)
    return [
        ExecutionSummary(
            execution_id=e["execution_id"],
            workflow_id=e["workflow_id"],
            trigger_type=e["trigger_type"],
            status=e["status"],
            started_at=str(e["started_at"]) if e.get("started_at") else None,
            completed_at=str(e["completed_at"]) if e.get("completed_at") else None,
            actions_completed=e.get("actions_completed", 0),
            actions_total=e.get("actions_total", 0),
            error_message=e.get("error_message"),
        )
        for e in executions
    ]


# =============================================================================
# Module Exports
# =============================================================================

__all__ = ["router"]
