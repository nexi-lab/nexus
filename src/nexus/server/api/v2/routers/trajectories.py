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

from nexus.server.api.v2.dependencies import get_trajectory_manager
from nexus.server.api.v2.models import (
    TrajectoryCompleteRequest,
    TrajectoryGetResponse,
    TrajectoryStartRequest,
    TrajectoryStartResponse,
    TrajectoryStepRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/trajectories", tags=["trajectories"])


# =============================================================================
# Endpoints
# =============================================================================


@router.post("", response_model=TrajectoryStartResponse, status_code=status.HTTP_201_CREATED)
async def start_trajectory(
    request: TrajectoryStartRequest,
    traj_manager: Any = Depends(get_trajectory_manager),
) -> TrajectoryStartResponse:
    """Start a new trajectory for task tracking."""
    try:
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
        raise HTTPException(status_code=500, detail="Failed to start trajectory") from e


@router.post("/{trajectory_id}/steps")
async def log_trajectory_step(
    trajectory_id: str,
    request: TrajectoryStepRequest,
    traj_manager: Any = Depends(get_trajectory_manager),
) -> dict[str, Any]:
    """Log a step in an active trajectory."""
    try:
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
        raise HTTPException(status_code=500, detail="Failed to log trajectory step") from e


@router.post("/{trajectory_id}/complete")
async def complete_trajectory(
    trajectory_id: str,
    request: TrajectoryCompleteRequest,
    traj_manager: Any = Depends(get_trajectory_manager),
) -> dict[str, Any]:
    """Complete a trajectory with final status."""
    try:
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
        raise HTTPException(status_code=500, detail="Failed to complete trajectory") from e


@router.get("")
async def query_trajectories(
    agent_id: str | None = Query(None, description="Filter by agent ID"),
    task_type: str | None = Query(None, description="Filter by task type"),
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=100, description="Maximum results"),
    offset: int = Query(0, ge=0, description="Number of results to skip"),  # noqa: ARG001
    path: str | None = Query(None, description="Filter by path"),
    traj_manager: Any = Depends(get_trajectory_manager),
) -> dict[str, Any]:
    """Query trajectories with filters."""
    try:
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
        raise HTTPException(status_code=500, detail="Failed to query trajectories") from e


@router.get("/{trajectory_id}", response_model=TrajectoryGetResponse)
async def get_trajectory(
    trajectory_id: str,
    traj_manager: Any = Depends(get_trajectory_manager),
) -> dict[str, Any]:
    """Get trajectory by ID with full trace."""
    try:
        result = traj_manager.get_trajectory(trajectory_id)

        if result is None:
            raise HTTPException(status_code=404, detail=f"Trajectory not found: {trajectory_id}")

        return {"trajectory": result}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Trajectory get error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve trajectory") from e
