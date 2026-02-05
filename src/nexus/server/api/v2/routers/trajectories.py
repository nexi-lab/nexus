"""Trajectory REST API endpoints.

Provides 5 endpoints for trajectory tracking:
- POST /api/v2/trajectories              - Start trajectory
- POST /api/v2/trajectories/{id}/steps   - Log step
- POST /api/v2/trajectories/{id}/complete - Complete trajectory
- GET  /api/v2/trajectories              - Query trajectories
- GET  /api/v2/trajectories/{id}         - Get trajectory
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from nexus.server.api.v2.models import (
    TrajectoryCompleteRequest,
    TrajectoryStartRequest,
    TrajectoryStartResponse,
    TrajectoryStepRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/trajectories", tags=["trajectories"])


def _get_require_auth() -> Any:
    """Lazy import to avoid circular imports."""
    from nexus.server.fastapi_server import require_auth

    return require_auth


def _get_app_state() -> Any:
    """Lazy import to avoid circular imports."""
    from nexus.server.fastapi_server import _app_state

    return _app_state


def _get_operation_context(auth_result: dict[str, Any]) -> Any:
    """Get operation context from auth result."""
    from nexus.server.fastapi_server import get_operation_context

    return get_operation_context(auth_result)


def _get_trajectory_manager(auth_result: dict[str, Any]) -> Any:
    """Create TrajectoryManager with user context."""
    from nexus.core.ace.trajectory import TrajectoryManager

    app_state = _get_app_state()
    context = _get_operation_context(auth_result)
    session = app_state.nexus_fs.memory.session
    backend = app_state.nexus_fs.memory.backend

    return TrajectoryManager(
        session=session,
        backend=backend,
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        zone_id=context.zone_id,
        context=context,
    )


# =============================================================================
# Endpoints
# =============================================================================


@router.post("", response_model=TrajectoryStartResponse, status_code=status.HTTP_201_CREATED)
async def start_trajectory(
    request: TrajectoryStartRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> TrajectoryStartResponse:
    """Start a new trajectory for task tracking.

    Creates a new trajectory to track an agent's execution of a task.
    Steps can be logged during execution and completed when done.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        traj_manager = _get_trajectory_manager(auth_result)

        trajectory_id = traj_manager.start_trajectory(
            task_description=request.task_description,
            task_type=request.task_type,
            parent_trajectory_id=request.parent_trajectory_id,
            metadata=request.metadata,
            path=request.path,
        )

        return TrajectoryStartResponse(trajectory_id=trajectory_id, status="in_progress")

    except Exception as e:
        logger.error(f"Trajectory start error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Trajectory start error: {e}") from e


@router.post("/{trajectory_id}/steps")
async def log_trajectory_step(
    trajectory_id: str,
    request: TrajectoryStepRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Log a step in an active trajectory.

    Records an action, decision, observation, or tool call
    during trajectory execution.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        traj_manager = _get_trajectory_manager(auth_result)

        traj_manager.log_step(
            trajectory_id=trajectory_id,
            step_type=request.step_type,
            description=request.description,
            result=request.result,
            metadata=request.metadata,
        )

        return {"status": "logged", "trajectory_id": trajectory_id}

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Trajectory step error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Trajectory step error: {e}") from e


@router.post("/{trajectory_id}/complete")
async def complete_trajectory(
    trajectory_id: str,
    request: TrajectoryCompleteRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Complete a trajectory with final status.

    Marks a trajectory as completed with success/failure status,
    optional score, and metrics.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        traj_manager = _get_trajectory_manager(auth_result)

        completed_id = traj_manager.complete_trajectory(
            trajectory_id=trajectory_id,
            status=request.status,
            success_score=request.success_score,
            error_message=request.error_message,
            metrics=request.metrics,
        )

        return {
            "trajectory_id": completed_id,
            "status": request.status,
            "completed": True,
        }

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Trajectory complete error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Trajectory complete error: {e}") from e


@router.get("")
async def query_trajectories(
    agent_id: str | None = Query(None, description="Filter by agent ID"),
    task_type: str | None = Query(None, description="Filter by task type"),
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=100, description="Maximum results"),
    path: str | None = Query(None, description="Filter by path"),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Query trajectories with filters.

    Returns a list of trajectories matching the specified filters.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        traj_manager = _get_trajectory_manager(auth_result)

        trajectories = traj_manager.query_trajectories(
            agent_id=agent_id,
            task_type=task_type,
            status=status,
            limit=limit,
            path=path,
        )

        return {
            "trajectories": trajectories,
            "total": len(trajectories),
            "filters": {
                "agent_id": agent_id,
                "task_type": task_type,
                "status": status,
                "path": path,
            },
        }

    except Exception as e:
        logger.error(f"Trajectory query error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Trajectory query error: {e}") from e


@router.get("/{trajectory_id}", response_model=dict[str, Any])
async def get_trajectory(
    trajectory_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Get trajectory by ID with full trace.

    Returns the complete trajectory including all logged steps.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        traj_manager = _get_trajectory_manager(auth_result)
        result = traj_manager.get_trajectory(trajectory_id)

        if result is None:
            raise HTTPException(status_code=404, detail=f"Trajectory not found: {trajectory_id}")

        return {"trajectory": result}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Trajectory get error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Trajectory get error: {e}") from e
