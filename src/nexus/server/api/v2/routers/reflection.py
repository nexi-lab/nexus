"""Reflection & Curation REST API endpoints.

Provides 3 endpoints for reflection and curation:
- POST /api/v2/reflect     - Trigger reflection on trajectory
- POST /api/v2/curate      - Curate memories into playbook
- POST /api/v2/curate/bulk - Bulk curation from trajectories
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from nexus.server.api.v2.models import (
    CurateBulkRequest,
    CurateRequest,
    CurationResponse,
    ReflectionResponse,
    ReflectRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reflection"])


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


def _get_reflector(auth_result: dict[str, Any]) -> Any:
    """Create Reflector with user context."""
    from nexus.core.ace.reflection import Reflector
    from nexus.core.ace.trajectory import TrajectoryManager

    app_state = _get_app_state()
    context = _get_operation_context(auth_result)
    session = app_state.nexus_fs.memory.session
    backend = app_state.nexus_fs.memory.backend
    llm_provider = getattr(app_state.nexus_fs, "_llm_provider", None)

    # Create trajectory manager for reflector
    traj_manager = TrajectoryManager(
        session=session,
        backend=backend,
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        zone_id=context.zone_id,
        context=context,
    )

    return Reflector(
        session=session,
        backend=backend,
        llm_provider=llm_provider,  # type: ignore[arg-type]
        trajectory_manager=traj_manager,
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        zone_id=context.zone_id,
    )


def _get_curator(auth_result: dict[str, Any]) -> Any:
    """Create Curator with user context."""
    from nexus.core.ace.curation import Curator
    from nexus.core.ace.playbook import PlaybookManager

    app_state = _get_app_state()
    context = _get_operation_context(auth_result)
    session = app_state.nexus_fs.memory.session
    backend = app_state.nexus_fs.memory.backend

    # Create playbook manager for curator
    playbook_manager = PlaybookManager(
        session=session,
        backend=backend,
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        zone_id=context.zone_id,
        context=context,
    )

    return Curator(
        session=session,
        backend=backend,
        playbook_manager=playbook_manager,
    )


# =============================================================================
# Endpoints
# =============================================================================


@router.post("/api/v2/reflect", response_model=ReflectionResponse)
async def reflect_on_trajectory(
    request: ReflectRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> ReflectionResponse:
    """Trigger reflection on a trajectory.

    Analyzes a completed trajectory using LLM to extract:
    - Helpful strategies that worked well
    - Harmful patterns to avoid
    - General observations

    Requires an LLM provider to be configured.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    # Check if LLM provider is available
    llm_provider = getattr(app_state.nexus_fs, "_llm_provider", None)
    if llm_provider is None:
        raise HTTPException(
            status_code=503,
            detail="LLM provider not configured. Reflection requires an LLM.",
        )

    try:
        reflector = _get_reflector(auth_result)

        result = await reflector.reflect_async(
            trajectory_id=request.trajectory_id,
            context=request.context,
            reflection_prompt=request.reflection_prompt,
        )

        return ReflectionResponse(
            memory_id=result.get("memory_id", ""),
            trajectory_id=result.get("trajectory_id", request.trajectory_id),
            helpful_strategies=result.get("helpful_strategies", []),
            harmful_patterns=result.get("harmful_patterns", []),
            observations=result.get("observations", []),
            confidence=result.get("confidence", 0.0),
        )

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Reflection error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Reflection error: {e}") from e


@router.post("/api/v2/curate", response_model=CurationResponse)
async def curate_memories(
    request: CurateRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> CurationResponse:
    """Curate reflection memories into a playbook.

    Takes reflection memories and extracts strategies to add to
    or merge with existing strategies in the target playbook.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        curator = _get_curator(auth_result)

        result = curator.curate_playbook(
            playbook_id=request.playbook_id,
            reflection_memory_ids=request.reflection_memory_ids,
            merge_threshold=request.merge_threshold,
        )

        return CurationResponse(
            playbook_id=result.get("playbook_id", request.playbook_id),
            strategies_added=result.get("strategies_added", 0),
            strategies_merged=result.get("strategies_merged", 0),
            strategies_total=result.get("strategies_total", 0),
        )

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Curation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Curation error: {e}") from e


@router.post("/api/v2/curate/bulk")
async def curate_bulk(
    request: CurateBulkRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Bulk curation from trajectories.

    Processes multiple trajectories and curates their reflections
    into the target playbook.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        curator = _get_curator(auth_result)

        results = []
        errors = []

        for trajectory_id in request.trajectory_ids:
            try:
                result = curator.curate_from_trajectory(
                    playbook_id=request.playbook_id,
                    trajectory_id=trajectory_id,
                )
                if result:
                    results.append(
                        {
                            "trajectory_id": trajectory_id,
                            **result,
                        }
                    )
            except Exception as e:
                errors.append(
                    {
                        "trajectory_id": trajectory_id,
                        "error": str(e),
                    }
                )

        return {
            "playbook_id": request.playbook_id,
            "processed": len(results),
            "failed": len(errors),
            "results": results,
            "errors": errors if errors else None,
        }

    except Exception as e:
        logger.error(f"Bulk curation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Bulk curation error: {e}") from e
