"""Kubernetes-style health probe endpoints (#2168, #3063).

Three lightweight probes for k8s ``livenessProbe``, ``readinessProbe``,
and ``startupProbe`` configuration:

* ``GET /healthz/live``    — always 200 (event-loop alive)
* ``GET /healthz/ready``   — 200 when the server can serve traffic
* ``GET /healthz/startup`` — 200 when all lifespan phases are done

All probes are **zero-I/O, in-memory only** and exempt from rate limiting.

Failure policy (Issue #3063):
- Liveness: fails open (200) — avoids restart loops from transient probe bugs.
- Readiness: fails closed (503) — a broken instance should not receive traffic.
- Startup: fails closed (503) — Kubernetes should keep waiting/restarting.
"""

import logging
import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from nexus.server.rate_limiting import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["probes"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_raft_topology(request: Request) -> tuple[bool, str]:
    """Return (ok, reason) for Raft topology readiness.

    Fails open: any exception → (True, "").
    """
    try:
        nx_fs = getattr(request.app.state, "nexus_fs", None)
        if nx_fs is None:
            return True, ""
        # Phase H: federation readiness is observed through the
        # DistributedCoordinator HAL trait via the
        # ``nexus_runtime.federation_is_initialized`` module helper —
        # the kernel itself no longer exposes a ``mount_reconciliation_done``
        # PyO3 method (zone lifecycle is kernel-internal HAL state).
        # Fails open when the helper is unavailable (slim builds) or
        # federation is disabled by leaving NEXUS_PEERS unset.
        kernel = getattr(nx_fs, "_kernel", None)
        if kernel is None:
            return True, ""
        if not os.environ.get("NEXUS_PEERS"):
            return True, ""
        try:
            import nexus_runtime as _nr

            ready = bool(_nr.federation_is_initialized(kernel))
        except Exception:
            return True, ""
        if not ready:
            return False, "Raft topology not ready"
        return True, ""
    except Exception:
        return True, ""


def _check_db_pool(request: Request) -> tuple[bool, str]:
    """Return (ok, reason) based on in-memory DB pool stats.

    Reads the pool ``size()`` / ``freesize()`` attributes exposed by
    asyncpg pools.  Fails open on any exception.
    """
    try:
        nx_fs = getattr(request.app.state, "nexus_fs", None)
        if nx_fs is None:
            return True, ""
        metadata = getattr(nx_fs, "metadata", None)
        if metadata is None:
            return True, ""
        pool_stats: dict[str, Any] = {}
        if hasattr(metadata, "get_pool_stats"):
            pool_stats = metadata.get_pool_stats()
        if not pool_stats:
            return True, ""
        idle = pool_stats.get("idle", pool_stats.get("freesize", -1))
        if idle == 0:
            return False, "DB pool exhausted (0 idle connections)"
        return True, ""
    except Exception:
        return True, ""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/healthz/live")
@limiter.exempt
async def liveness(request: Request) -> JSONResponse:  # noqa: ARG001
    """Liveness probe — unconditional 200.

    If the event loop can execute this handler the process is alive.
    """
    return JSONResponse({"status": "alive"})


@router.get("/healthz/ready")
@limiter.exempt
async def readiness(request: Request) -> JSONResponse:
    """Readiness probe — 200 when the server can accept traffic.

    Checks:
    1. Required startup phases completed (StartupTracker)
    2. Raft topology initialised and root zone is leader-writeable
    3. DB pool has at least one idle connection
    """
    try:
        tracker = getattr(request.app.state, "startup_tracker", None)
        if tracker is not None and not tracker.is_ready:
            pending = sorted(p.value for p in tracker.pending_phases)
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "reason": "startup_incomplete",
                    "pending_phases": pending,
                },
            )

        raft_ok, raft_reason = _check_raft_topology(request)
        if not raft_ok:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reason": raft_reason},
            )

        db_ok, db_reason = _check_db_pool(request)
        if not db_ok:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reason": db_reason},
            )

        uptime = tracker.elapsed_seconds if tracker else 0.0
        return JSONResponse({"status": "ready", "uptime_seconds": round(uptime, 2)})
    except Exception:
        # Fail closed — a broken instance should not receive traffic (Issue #3063)
        logger.exception("Readiness probe error — returning 503")
        return JSONResponse(
            status_code=503,
            content={"status": "error", "reason": "unexpected_probe_error"},
        )


@router.get("/healthz/startup")
@limiter.exempt
async def startup(request: Request) -> JSONResponse:
    """Startup probe — 200 when all lifespan phases are complete.

    Kubernetes uses this to know when to switch from ``startupProbe``
    to ``livenessProbe``.
    """
    try:
        tracker = getattr(request.app.state, "startup_tracker", None)
        if tracker is None or tracker.is_complete:
            return JSONResponse({"status": "started"})

        completed = sorted(p.value for p in tracker.completed_phases)
        pending = sorted(p.value for p in tracker.pending_phases)
        return JSONResponse(
            status_code=503,
            content={
                "status": "starting",
                "completed_phases": completed,
                "pending_phases": pending,
            },
        )
    except Exception:
        # Fail closed — Kubernetes should keep waiting/restarting (Issue #3063)
        logger.exception("Startup probe error — returning 503")
        return JSONResponse(
            status_code=503,
            content={"status": "error", "reason": "unexpected_probe_error"},
        )
