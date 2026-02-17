"""Brick lifecycle REST API (Issue #1704).

Endpoints for brick health monitoring and runtime hot-swap.
Requires admin authentication for mount/unmount operations.

Endpoints:
    GET  /api/v2/bricks/health — Aggregated brick health report
    GET  /api/v2/bricks/{name} — Individual brick status
    POST /api/v2/bricks/{name}/mount — Mount a registered brick at runtime
    POST /api/v2/bricks/{name}/unmount — Unmount an active brick at runtime
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/bricks", tags=["bricks"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class BrickStatusResponse(BaseModel):
    """Individual brick status."""

    name: str
    state: str
    protocol_name: str
    error: str | None = None
    started_at: float | None = None
    stopped_at: float | None = None


class BrickHealthResponse(BaseModel):
    """Aggregated brick health report."""

    total: int
    active: int
    failed: int
    bricks: list[BrickStatusResponse]


class BrickActionResponse(BaseModel):
    """Response for mount/unmount actions."""

    name: str
    action: str
    state: str
    error: str | None = None


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _get_lifecycle_manager(request: Request) -> Any:
    """Get BrickLifecycleManager from app state."""
    nx = getattr(request.app.state, "nexus_fs", None)
    if nx is None:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    services = getattr(nx, "services", None)
    if services is None:
        raise HTTPException(status_code=503, detail="Kernel services not available")

    manager = getattr(services, "brick_lifecycle_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Brick lifecycle manager not available")

    return manager


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/health", response_model=BrickHealthResponse)
async def brick_health(
    manager: Any = Depends(_get_lifecycle_manager),
) -> BrickHealthResponse:
    """Aggregated health report for all managed bricks.

    Returns counts of total, active, and failed bricks with per-brick status.
    Useful for Kubernetes liveness/readiness probes.
    """
    report = manager.health()
    return BrickHealthResponse(
        total=report.total,
        active=report.active,
        failed=report.failed,
        bricks=[
            BrickStatusResponse(
                name=s.name,
                state=s.state.value,
                protocol_name=s.protocol_name,
                error=s.error,
                started_at=s.started_at,
                stopped_at=s.stopped_at,
            )
            for s in report.bricks
        ],
    )


@router.get("/{name}", response_model=BrickStatusResponse)
async def brick_status(
    name: str,
    manager: Any = Depends(_get_lifecycle_manager),
) -> BrickStatusResponse:
    """Individual brick lifecycle status."""
    status = manager.get_status(name)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Brick {name!r} not found")
    return BrickStatusResponse(
        name=status.name,
        state=status.state.value,
        protocol_name=status.protocol_name,
        error=status.error,
        started_at=status.started_at,
        stopped_at=status.stopped_at,
    )


@router.post("/{name}/mount", response_model=BrickActionResponse)
async def mount_brick(
    name: str,
    manager: Any = Depends(_get_lifecycle_manager),
) -> BrickActionResponse:
    """Mount a registered brick at runtime (hot-swap).

    The brick must be in REGISTERED state. Fires PRE_MOUNT/POST_MOUNT hooks.
    """
    status = manager.get_status(name)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Brick {name!r} not found")

    await manager.mount(name)

    new_status = manager.get_status(name)
    return BrickActionResponse(
        name=name,
        action="mount",
        state=new_status.state.value if new_status else "unknown",
        error=new_status.error if new_status else None,
    )


@router.post("/{name}/unmount", response_model=BrickActionResponse)
async def unmount_brick(
    name: str,
    manager: Any = Depends(_get_lifecycle_manager),
) -> BrickActionResponse:
    """Unmount an active brick at runtime (hot-swap).

    The brick must be in ACTIVE state. Fires PRE_UNMOUNT/POST_UNMOUNT hooks.
    """
    status = manager.get_status(name)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Brick {name!r} not found")

    await manager.unmount(name)

    new_status = manager.get_status(name)
    return BrickActionResponse(
        name=name,
        action="unmount",
        state=new_status.state.value if new_status else "unknown",
        error=new_status.error if new_status else None,
    )
