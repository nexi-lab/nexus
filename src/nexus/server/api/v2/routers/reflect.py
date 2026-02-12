"""Reflection REST API endpoint.

Provides 1 endpoint for reflection analysis:
- POST /api/v2/reflect - Trigger reflection on trajectory
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from nexus.server.api.v2.dependencies import get_llm_provider, get_reflector
from nexus.server.api.v2.error_handling import api_error_handler
from nexus.server.api.v2.models import ReflectionResponse, ReflectRequest

router = APIRouter(prefix="/api/v2/reflect", tags=["reflection"])


# =============================================================================
# Endpoints
# =============================================================================


@router.post("", response_model=ReflectionResponse)
@api_error_handler(context="perform reflection")
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
