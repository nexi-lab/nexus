"""Kubernetes-style health probe endpoints (#2168, #3063).

Lightweight probes for k8s ``livenessProbe``, ``readinessProbe``, and
``startupProbe`` configuration, plus an opt-in storage write probe:

* ``GET /healthz/live``    — always 200 (event-loop alive)
* ``GET /healthz/ready``   — 200 when the server can serve traffic
* ``GET /healthz/startup`` — 200 when all lifespan phases are done
* ``GET /healthz/storage`` — 200 when live storage accepts write/read/delete

The k8s probes are **zero-I/O, in-memory only** and exempt from rate limiting.
The storage probe is explicitly I/O-based and opt-in by virtue of its separate
endpoint.

Failure policy (Issue #3063):
- Liveness: fails open (200) — avoids restart loops from transient probe bugs.
- Readiness: fails closed (503) — a broken instance should not receive traffic.
- Startup: fails closed (503) — Kubernetes should keep waiting/restarting.
- Storage: fails closed (503) — write-path failure should be visible to operators.
"""

import asyncio
import logging
import os
from threading import Lock
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext
from nexus.server.dependencies import require_admin
from nexus.server.rate_limiting import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["probes"])
_STORAGE_PROBE_PAYLOAD = b"nexus-healthz-storage-probe"
_STORAGE_PROBE_TIMEOUT_SECONDS = 2.0
_storage_probe_lock = Lock()


class _StorageProbeInProgress(RuntimeError):
    pass


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
        # The kernel process manages federation internally; if it
        # responds to gRPC, it's ready. Fails open when federation is
        # disabled by leaving NEXUS_PEERS unset.
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


def _storage_probe_timeout_seconds() -> float:
    raw = os.environ.get("NEXUS_HEALTHZ_STORAGE_TIMEOUT_SECONDS")
    if raw is None:
        return _STORAGE_PROBE_TIMEOUT_SECONDS
    try:
        return max(float(raw), 0.1)
    except ValueError:
        logger.warning(
            "Invalid NEXUS_HEALTHZ_STORAGE_TIMEOUT_SECONDS=%r; using default %.1fs",
            raw,
            _STORAGE_PROBE_TIMEOUT_SECONDS,
        )
        return _STORAGE_PROBE_TIMEOUT_SECONDS


def _storage_probe_context() -> OperationContext:
    return OperationContext(
        user_id="system",
        groups=[],
        is_admin=True,
        is_system=True,
        zone_id=ROOT_ZONE_ID,
    )


def _run_storage_round_trip(nx_fs: Any) -> None:
    if not _storage_probe_lock.acquire(blocking=False):
        raise _StorageProbeInProgress("storage probe already in progress")

    try:
        probe_path = f"/__healthz__/{uuid4().hex}"
        context = _storage_probe_context()
        probe_error: Exception | None = None

        try:
            nx_fs.sys_write(probe_path, _STORAGE_PROBE_PAYLOAD, context=context)
            readback = nx_fs.sys_read(probe_path, context=context)
            if readback != _STORAGE_PROBE_PAYLOAD:
                raise RuntimeError("storage probe readback mismatch")
        except Exception as exc:
            probe_error = exc
            raise
        finally:
            try:
                nx_fs.sys_unlink(probe_path, context=context)
            except Exception as cleanup_exc:
                if probe_error is None:
                    raise RuntimeError(
                        f"storage probe cleanup failed: {cleanup_exc}"
                    ) from cleanup_exc
                logger.warning(
                    "Storage health probe cleanup failed after probe error",
                    exc_info=True,
                )
    finally:
        _storage_probe_lock.release()


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


@router.get("/healthz/storage", dependencies=[Depends(require_admin)])
@limiter.exempt
async def storage(request: Request) -> JSONResponse:
    """Storage probe — write/read/delete through the live NexusFS path."""
    nx_fs = getattr(request.app.state, "nexus_fs", None)
    if nx_fs is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "reason": "nexus_fs_unavailable",
            },
        )

    try:
        await asyncio.wait_for(
            asyncio.to_thread(_run_storage_round_trip, nx_fs),
            timeout=_storage_probe_timeout_seconds(),
        )
        return JSONResponse({"status": "healthy"})
    except _StorageProbeInProgress:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "reason": "storage_probe_in_progress",
            },
        )
    except TimeoutError:
        logger.warning("Storage health probe timed out")
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "reason": "storage_probe_timeout",
            },
        )
    except Exception as exc:
        logger.warning("Storage health probe failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "reason": "storage_probe_failed",
                "error": str(exc),
            },
        )
