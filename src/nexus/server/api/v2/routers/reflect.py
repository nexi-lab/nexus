"""Reflection REST API endpoint.

Provides 1 endpoint for reflection analysis:
- POST /api/v2/reflect - Trigger reflection on trajectory
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from nexus.server.api.v2.dependencies import get_llm_provider, get_reflector
from nexus.server.api.v2.models import ReflectionResponse, ReflectRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reflection"])


# =============================================================================
# Endpoints
# =============================================================================


@router.post("/api/v2/reflect", response_model=ReflectionResponse)
async def reflect_on_trajectory(
    request: ReflectRequest,
    reflector: Any = Depends(get_reflector),
    llm_provider: Any = Depends(get_llm_provider),
) -> ReflectionResponse:
    """Trigger reflection on a trajectory.

    Analyzes a completed trajectory using LLM to extract:
    - Helpful strategies that worked well
    - Harmful patterns to avoid
    - General observations

    Requires an LLM provider to be configured.
    """
    if llm_provider is None:
        raise HTTPException(
            status_code=503,
            detail="LLM provider not configured. Reflection requires an LLM.",
        )

    try:
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
        raise HTTPException(status_code=500, detail="Failed to perform reflection") from e
