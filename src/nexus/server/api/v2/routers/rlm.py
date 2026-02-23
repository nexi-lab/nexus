"""RLM inference REST endpoints.

Provides SSE streaming and non-streaming endpoints for Recursive Language
Model inference. Follows the pay.py router pattern.

Endpoints:
    POST /api/v2/rlm/infer — Run RLM inference (SSE streaming by default)

Architecture Decisions:
    - Issue 4C: SSE streaming (per-iteration events)
    - Issue 13A: Dedicated thread pool (returns 503 when full)
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.dependencies import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/rlm", tags=["rlm"], dependencies=[Depends(require_auth)])

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class RLMInferenceRequestModel(BaseModel):
    """REST API request model for RLM inference."""

    query: str = Field(..., description="The question to answer using RLM inference")
    context_paths: list[str] = Field(
        default_factory=list,
        description="Nexus VFS paths to relevant files",
    )
    zone_id: str = Field(default=ROOT_ZONE_ID, description="Zone ID for scoping")
    model: str = Field(
        default="claude-sonnet-4-20250514",
        description="LLM model to use for reasoning",
    )
    sub_model: str | None = Field(
        default=None,
        description="Model for sub-LM calls (cheaper, optional)",
    )
    max_iterations: int = Field(default=15, ge=1, le=50, description="Maximum REPL iterations")
    max_duration_seconds: int = Field(
        default=120, ge=10, le=600, description="Maximum total duration"
    )
    max_total_tokens: int = Field(
        default=100_000, ge=1_000, le=1_000_000, description="Maximum total tokens"
    )
    sandbox_provider: str | None = Field(
        default=None,
        description="Sandbox provider (docker, e2b, monty). Auto-selects if None.",
    )
    stream: bool = Field(
        default=True,
        description="If true, returns SSE stream. If false, returns JSON.",
    )


class RLMInferenceResponseModel(BaseModel):
    """REST API response model for non-streaming RLM inference."""

    status: str
    answer: str | None = None
    total_tokens: int = 0
    total_duration_seconds: float = 0.0
    iterations: int = 0
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/infer")
async def infer(
    request: RLMInferenceRequestModel,
    req: Request,
) -> Any:
    """Run RLM inference over Nexus VFS data.

    The model recursively analyzes context by writing Python code in a
    sandboxed REPL. Pre-loaded tools (nexus_read, nexus_search) allow
    the model to lazily fetch data as needed.

    If stream=true (default), returns an SSE stream with per-iteration
    progress events. If stream=false, blocks until completion.

    Returns:
        SSE stream or JSON response with inference results.

    Raises:
        503: If the RLM thread pool is full (try again later).
        401: If not authenticated.
    """
    # Get RLM service from app state
    rlm_service = getattr(req.app.state, "rlm_service", None)
    if rlm_service is None:
        raise HTTPException(
            status_code=503,
            detail="RLM inference service not available. Ensure sandbox and LLM providers are configured.",
        )

    # Get auth context
    api_key = getattr(req.state, "api_key", None) or getattr(req.app.state, "api_key", "")
    user_id = getattr(req.state, "agent_id", None) or "anonymous"

    # Build internal request
    from nexus.bricks.rlm.types import RLMInferenceRequest

    rlm_request = RLMInferenceRequest(
        query=request.query,
        context_paths=tuple(request.context_paths),
        zone_id=request.zone_id,
        model=request.model,
        sub_model=request.sub_model,
        max_iterations=request.max_iterations,
        max_duration_seconds=request.max_duration_seconds,
        max_total_tokens=request.max_total_tokens,
        sandbox_provider=request.sandbox_provider,
        stream=request.stream,
    )

    if request.stream:
        # SSE streaming response
        async def event_generator() -> AsyncIterator[str]:
            async for event in rlm_service.infer_stream(
                rlm_request, user_id=user_id, api_key=api_key
            ):
                yield f"event: {event.event}\ndata: {json.dumps(event.data, default=str)}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        # Non-streaming: block until completion
        result = await rlm_service.infer(rlm_request, user_id=user_id, api_key=api_key)
        return RLMInferenceResponseModel(
            status=result.status,
            answer=result.answer,
            total_tokens=result.total_tokens,
            total_duration_seconds=result.total_duration_seconds,
            iterations=len(result.iterations),
            error_message=result.error_message,
        )
