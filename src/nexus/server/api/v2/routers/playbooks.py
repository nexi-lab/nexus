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

from nexus.server.api.v2.models import (
    PlaybookCreateRequest,
    PlaybookCreateResponse,
    PlaybookUpdateRequest,
    PlaybookUsageRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/playbooks", tags=["playbooks"])


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


def _get_playbook_manager(auth_result: dict[str, Any]) -> Any:
    """Create PlaybookManager with user context."""
    from nexus.core.ace.playbook import PlaybookManager

    app_state = _get_app_state()
    context = _get_operation_context(auth_result)
    session = app_state.nexus_fs.memory.session
    backend = app_state.nexus_fs.memory.backend

    return PlaybookManager(
        session=session,
        backend=backend,
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        tenant_id=context.zone_id,
        context=context,
    )


# =============================================================================
# Endpoints
# =============================================================================


@router.get("")
async def list_playbooks(
    scope: str | None = Query(None, description="Filter by scope"),
    name_pattern: str | None = Query(None, description="Filter by name (SQL LIKE pattern)"),
    limit: int = Query(50, ge=1, le=100, description="Maximum results"),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """List playbooks with optional filters.

    Returns playbooks accessible to the current user/agent.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        playbook_manager = _get_playbook_manager(auth_result)

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
        raise HTTPException(status_code=500, detail=f"Playbook list error: {e}") from e


@router.post("", response_model=PlaybookCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_playbook(
    request: PlaybookCreateRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> PlaybookCreateResponse:
    """Create a new playbook.

    Creates a playbook with the specified name, scope, and visibility.
    Initial strategies can be provided.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        playbook_manager = _get_playbook_manager(auth_result)

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
        raise HTTPException(status_code=500, detail=f"Playbook create error: {e}") from e


@router.get("/{playbook_id}", response_model=dict[str, Any])
async def get_playbook(
    playbook_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Get playbook by ID with full content.

    Returns the playbook including all strategies.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        playbook_manager = _get_playbook_manager(auth_result)
        playbook = playbook_manager.get_playbook(playbook_id)

        if playbook is None:
            raise HTTPException(status_code=404, detail=f"Playbook not found: {playbook_id}")

        return {"playbook": playbook}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Playbook get error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Playbook get error: {e}") from e


@router.put("/{playbook_id}")
async def update_playbook(
    playbook_id: str,
    request: PlaybookUpdateRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Update a playbook.

    Updates playbook strategies and/or metadata.
    Optionally increments version number.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        playbook_manager = _get_playbook_manager(auth_result)

        # First verify playbook exists
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
        raise HTTPException(status_code=500, detail=f"Playbook update error: {e}") from e


@router.delete("/{playbook_id}")
async def delete_playbook(
    playbook_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Delete a playbook.

    Permanently removes the playbook.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        playbook_manager = _get_playbook_manager(auth_result)
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
        raise HTTPException(status_code=500, detail=f"Playbook delete error: {e}") from e


@router.post("/{playbook_id}/usage")
async def record_usage(
    playbook_id: str,
    request: PlaybookUsageRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Record playbook usage.

    Records a usage event with success/failure status and optional
    improvement score. Used to track playbook effectiveness.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        playbook_manager = _get_playbook_manager(auth_result)

        # First verify playbook exists
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
        raise HTTPException(status_code=500, detail=f"Playbook usage error: {e}") from e
