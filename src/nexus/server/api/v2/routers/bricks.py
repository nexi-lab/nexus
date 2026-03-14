"""Brick lifecycle REST API (Issue #1704, #2363).

Endpoints for brick health monitoring and runtime hot-swap.
Requires admin authentication for mount/unmount/unregister operations.

Endpoints:
    GET  /api/v2/bricks/health — Aggregated brick health report
    GET  /api/v2/bricks/{name} — Individual brick status
    POST /api/v2/bricks/{name}/mount — Mount a registered/unmounted brick at runtime
    POST /api/v2/bricks/{name}/unmount — Unmount an active brick at runtime
    POST /api/v2/bricks/{name}/remount — Re-mount an unmounted brick
    POST /api/v2/bricks/{name}/unregister — Unregister an unmounted brick (terminal)
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from nexus.server.dependencies import require_admin

logger = logging.getLogger(__name__)

# Public router for K8s probes — no auth required
health_router = APIRouter(prefix="/api/v2/bricks", tags=["bricks"])

# Admin router for lifecycle management — requires admin auth
router = APIRouter(prefix="/api/v2/bricks", tags=["bricks"], dependencies=[Depends(require_admin)])

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
    unmounted_at: float | None = None


class BrickDetailResponse(BrickStatusResponse):
    """Extended brick detail with spec and dependency info (Issue #2980)."""

    enabled: bool = True
    depends_on: list[str] = []
    depended_by: list[str] = []


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


class DriftReportItem(BaseModel):
    """Single brick drift report."""

    brick_name: str
    spec_state: str
    actual_state: str
    action: str
    detail: str = ""


class BrickReconcileOutcomeItem(BaseModel):
    """Per-brick reconcile outcome (Issue #2059)."""

    brick_name: str
    requeue: bool
    requeue_after_seconds: float | None = None
    error: str | None = None


class DriftReportResponse(BaseModel):
    """Aggregated drift report from reconciliation."""

    total_bricks: int
    drifted: int
    actions_taken: int
    errors: int
    drifts: list[DriftReportItem]
    last_reconcile_at: float | None = None
    reconcile_count: int = 0
    reconcile_outcomes: list[BrickReconcileOutcomeItem] = []


class ResetBrickResponse(BaseModel):
    """Response for brick reset action."""

    name: str
    action: str = "reset"
    state: str
    retry_count: int = 0


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _get_system_service(request: Request, attr: str, label: str) -> Any:
    """Resolve a system service from the NexusFS instance on app state.

    Raises HTTPException(503) if NexusFS, system services, or the requested
    service is unavailable.
    """
    nx = getattr(request.app.state, "nexus_fs", None)
    if nx is None:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    _sys = getattr(nx, "_system_services", None)
    if _sys is None:
        raise HTTPException(status_code=503, detail="System services not available")

    service = getattr(_sys, attr, None)
    if service is None:
        raise HTTPException(status_code=503, detail=f"{label} not available")

    return service


def _get_lifecycle_manager(request: Request) -> Any:
    """Get BrickLifecycleManager from app state."""
    return _get_system_service(request, "brick_lifecycle_manager", "Brick lifecycle manager")


def _get_reconciler(request: Request) -> Any:
    """Get BrickReconciler from app state."""
    return _get_system_service(request, "brick_reconciler", "Brick reconciler")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# NOTE: /drift must be registered BEFORE /{name} to avoid path parameter capture.


@router.get("/drift", response_model=DriftReportResponse)
async def brick_drift(
    reconciler: Any = Depends(_get_reconciler),
) -> DriftReportResponse:
    """Current drift report: compare spec vs status for all bricks.

    Read-only: no corrective actions taken, no state transitions.
    The reconciler's periodic loop handles auto-healing separately.
    """
    result = reconciler.detect_drift()

    # Per-brick reconcile outcomes (Issue #2059)
    outcomes: list[BrickReconcileOutcomeItem] = []
    if hasattr(reconciler, "last_reconcile_outcomes"):
        for brick_name, outcome in reconciler.last_reconcile_outcomes:
            outcomes.append(
                BrickReconcileOutcomeItem(
                    brick_name=brick_name,
                    requeue=outcome.requeue,
                    requeue_after_seconds=(
                        outcome.requeue_after.total_seconds()
                        if outcome.requeue_after is not None
                        else None
                    ),
                    error=outcome.error,
                )
            )

    return DriftReportResponse(
        total_bricks=result.total_bricks,
        drifted=result.drifted,
        actions_taken=result.actions_taken,
        errors=result.errors,
        drifts=[
            DriftReportItem(
                brick_name=d.brick_name,
                spec_state=d.spec_state,
                actual_state=d.actual_state.value,
                action=d.action.value,
                detail=d.detail,
            )
            for d in result.drifts
        ],
        last_reconcile_at=reconciler.last_reconcile_at,
        reconcile_count=reconciler.reconcile_count,
        reconcile_outcomes=outcomes,
    )


@health_router.get("/health", response_model=BrickHealthResponse)
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
                unmounted_at=s.unmounted_at,
            )
            for s in report.bricks
        ],
    )


@router.get("/{name}", response_model=BrickDetailResponse)
async def brick_status(
    name: str,
    manager: Any = Depends(_get_lifecycle_manager),
) -> BrickDetailResponse:
    """Individual brick lifecycle status with spec and dependency info."""
    status = manager.get_status(name)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Brick {name!r} not found")

    # Enrich with spec data (depends_on, enabled)
    spec = manager.get_spec(name)
    depends_on: list[str] = list(spec.depends_on) if spec else []
    enabled: bool = spec.enabled if spec else True

    # Compute reverse dependencies (which bricks depend on this one)
    depended_by: list[str] = []
    all_specs = manager.all_specs()
    for other_name, other_spec in all_specs.items():
        if name in other_spec.depends_on:
            depended_by.append(other_name)

    return BrickDetailResponse(
        name=status.name,
        state=status.state.value,
        protocol_name=status.protocol_name,
        error=status.error,
        started_at=status.started_at,
        stopped_at=status.stopped_at,
        unmounted_at=status.unmounted_at,
        enabled=enabled,
        depends_on=depends_on,
        depended_by=sorted(depended_by),
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


@router.post("/{name}/reset", response_model=ResetBrickResponse)
async def reset_brick(
    name: str,
    manager: Any = Depends(_get_lifecycle_manager),
) -> ResetBrickResponse:
    """Reset a FAILED brick to REGISTERED for retry.

    Clears error, timestamps, and retry counter. The reconciler will
    automatically attempt to remount the brick on its next pass.
    """
    status = manager.get_status(name)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Brick {name!r} not found")

    try:
        manager.reset(name)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    new_status = manager.get_status(name)
    return ResetBrickResponse(
        name=name,
        state=new_status.state.value if new_status else "unknown",
    )


@router.post("/{name}/remount", response_model=BrickActionResponse)
async def remount_brick(
    name: str,
    manager: Any = Depends(_get_lifecycle_manager),
) -> BrickActionResponse:
    """Re-mount an UNMOUNTED brick at runtime.

    The brick must be in UNMOUNTED state. Equivalent to calling mount on an
    unmounted brick.  Fires PRE_MOUNT/POST_MOUNT hooks.
    """
    status = manager.get_status(name)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Brick {name!r} not found")

    try:
        await manager.remount(name)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    new_status = manager.get_status(name)
    return BrickActionResponse(
        name=name,
        action="remount",
        state=new_status.state.value if new_status else "unknown",
        error=new_status.error if new_status else None,
    )


@router.post("/{name}/unregister", response_model=BrickActionResponse)
async def unregister_brick(
    name: str,
    manager: Any = Depends(_get_lifecycle_manager),
) -> BrickActionResponse:
    """Unregister an UNMOUNTED brick (remove from registry).

    The brick must be in UNMOUNTED state. This is a terminal action —
    the brick is removed and cannot be re-mounted.
    Fires PRE_UNREGISTER/POST_UNREGISTER hooks.
    """
    status = manager.get_status(name)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Brick {name!r} not found")

    try:
        await manager.unregister(name)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    return BrickActionResponse(
        name=name,
        action="unregister",
        state="unregistered",
    )
