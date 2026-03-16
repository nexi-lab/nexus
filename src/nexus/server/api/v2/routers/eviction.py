"""Agent eviction REST API endpoint (Issue #2170, #2A).

Provides:
- POST /api/v2/agents/{agent_id}/evict — Manually evict a connected agent

All endpoints are authenticated via existing auth middleware.

Note: This module intentionally does NOT use ``from __future__ import annotations``
because FastAPI uses ``eval_str=True`` on dependency signatures at import time.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from nexus.server.api.v2.dependencies import _get_require_auth
from nexus.server.dependencies import get_operation_context

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agents"])


class EvictResponse(BaseModel):
    """Response body for agent eviction."""

    agent_id: str
    evicted: bool
    reason: str


async def _get_eviction_manager(request: Request) -> Any:
    """Get EvictionManager from app state."""
    em = getattr(request.app.state, "eviction_manager", None)
    if em is None:
        raise HTTPException(status_code=503, detail="Eviction manager not available")
    return em


@router.post(
    "/api/v2/agents/{agent_id}/evict",
    response_model=EvictResponse,
    summary="Manually evict a connected agent",
    description=(
        "Evicts a CONNECTED agent by checkpointing its state and "
        "transitioning it to SUSPENDED. Bypasses pressure checks and cooldown."
    ),
)
async def evict_agent(
    agent_id: str,
    auth_result: dict = Depends(_get_require_auth()),
    eviction_manager: Any = Depends(_get_eviction_manager),
) -> EvictResponse:
    """Manually evict a specific agent."""
    # Only admins or the agent itself can trigger eviction
    ctx = get_operation_context(auth_result)
    if not ctx.is_admin and ctx.subject_id != agent_id:
        raise HTTPException(status_code=403, detail=f"Not authorized to evict agent '{agent_id}'")
    try:
        result = await eviction_manager.evict_agent(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return EvictResponse(
        agent_id=agent_id,
        evicted=result.evicted > 0,
        reason=str(result.reason),
    )
