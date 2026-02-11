"""Playbook REST API endpoints.

Provides 6 endpoints for playbook management:
- GET    /api/v2/playbooks           - List playbooks
- POST   /api/v2/playbooks           - Create playbook
- GET    /api/v2/playbooks/{id}      - Get playbook
- PUT    /api/v2/playbooks/{id}      - Update playbook
- DELETE /api/v2/playbooks/{id}      - Delete playbook
- POST   /api/v2/playbooks/{id}/usage - Record usage
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from nexus.server.api.v2.dependencies import get_playbook_manager
from nexus.server.api.v2.models import (
    PlaybookCreateRequest,
    PlaybookCreateResponse,
    PlaybookGetResponse,
    PlaybookUpdateRequest,
    PlaybookUsageRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/playbooks", tags=["playbooks"])


# =============================================================================
# Endpoints
# =============================================================================


@router.get("")
async def list_playbooks(
    scope: str | None = Query(None, description="Filter by scope"),
    name_pattern: str | None = Query(None, description="Filter by name (SQL LIKE pattern)"),
    limit: int = Query(50, ge=1, le=100, description="Maximum results"),
    offset: int = Query(0, ge=0, description="Number of results to skip"),  # noqa: ARG001
    playbook_manager: Any = Depends(get_playbook_manager),
) -> dict[str, Any]:
    """List playbooks with optional filters."""
    try:
        playbooks = playbook_manager.query_playbooks(
            scope=scope,
            name_pattern=name_pattern,
            limit=limit,
        )

        return {
            "playbooks": playbooks,
            "total": len(playbooks),
            "filters": {
                "scope": scope,
                "name_pattern": name_pattern,
            },
        }

    except Exception as e:
        logger.error(f"Playbook list error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list playbooks") from e


@router.post("", response_model=PlaybookCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_playbook(
    request: PlaybookCreateRequest,
    playbook_manager: Any = Depends(get_playbook_manager),
) -> PlaybookCreateResponse:
    """Create a new playbook."""
    try:
        playbook_id = playbook_manager.create_playbook(
            name=request.name,
            description=request.description,
            scope=request.scope,
            visibility=request.visibility,
            initial_strategies=request.initial_strategies,
        )

        return PlaybookCreateResponse(playbook_id=playbook_id, status="created")

    except Exception as e:
        logger.error(f"Playbook create error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create playbook") from e


@router.get("/{playbook_id}", response_model=PlaybookGetResponse)
async def get_playbook(
    playbook_id: str,
    playbook_manager: Any = Depends(get_playbook_manager),
) -> dict[str, Any]:
    """Get playbook by ID with full content."""
    try:
        playbook = playbook_manager.get_playbook(playbook_id)

        if playbook is None:
            raise HTTPException(status_code=404, detail=f"Playbook not found: {playbook_id}")

        return {"playbook": playbook}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Playbook get error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve playbook") from e


@router.put("/{playbook_id}")
async def update_playbook(
    playbook_id: str,
    request: PlaybookUpdateRequest,
    playbook_manager: Any = Depends(get_playbook_manager),
) -> dict[str, Any]:
    """Update a playbook."""
    try:
        existing = playbook_manager.get_playbook(playbook_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"Playbook not found: {playbook_id}")

        playbook_manager.update_playbook(
            playbook_id=playbook_id,
            strategies=request.strategies,
            metadata=request.metadata,
            increment_version=request.increment_version,
        )

        return {
            "playbook_id": playbook_id,
            "status": "updated",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Playbook update error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update playbook") from e


@router.delete("/{playbook_id}")
async def delete_playbook(
    playbook_id: str,
    playbook_manager: Any = Depends(get_playbook_manager),
) -> dict[str, Any]:
    """Delete a playbook."""
    try:
        deleted = playbook_manager.delete_playbook(playbook_id)

        if not deleted:
            raise HTTPException(status_code=404, detail=f"Playbook not found: {playbook_id}")

        return {
            "playbook_id": playbook_id,
            "deleted": True,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Playbook delete error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete playbook") from e


@router.post("/{playbook_id}/usage")
async def record_usage(
    playbook_id: str,
    request: PlaybookUsageRequest,
    playbook_manager: Any = Depends(get_playbook_manager),
) -> dict[str, Any]:
    """Record playbook usage."""
    try:
        existing = playbook_manager.get_playbook(playbook_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"Playbook not found: {playbook_id}")

        playbook_manager.record_usage(
            playbook_id=playbook_id,
            success=request.success,
            improvement_score=request.improvement_score,
        )

        return {
            "playbook_id": playbook_id,
            "recorded": True,
            "success": request.success,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Playbook usage error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to record playbook usage") from e
