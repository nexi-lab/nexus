"""Health and metrics endpoints.

Extracted from fastapi_server.py (#1602).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from nexus.server.rate_limiting import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    service: str
    enforce_permissions: bool | None = None
    enforce_zone_isolation: bool | None = None
    has_auth: bool | None = None


@router.get("/health", response_model=HealthResponse)
@limiter.exempt
async def health_check(request: Request) -> HealthResponse | Any:
    """Basic health check (always accessible)."""
    enforce_permissions = None
    enforce_zone_isolation = None
    has_auth = None

    nx_fs = request.app.state.nexus_fs
    if nx_fs:
        enforce_permissions = getattr(nx_fs, "_enforce_permissions", None)
        enforce_zone_isolation = getattr(nx_fs, "_enforce_zone_isolation", None)

        # Federation mode: ensure topology is initialized (standard Raft lifecycle).
        zone_mgr = getattr(nx_fs, "_zone_mgr", None)
        if zone_mgr is not None and not zone_mgr.ensure_topology():
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=503,
                content={
                    "status": "starting",
                    "service": "nexus-rpc",
                    "detail": "Waiting for Raft leader election and topology initialization",
                },
            )

    has_auth = bool(request.app.state.api_key or request.app.state.auth_provider)

    return HealthResponse(
        status="healthy",
        service="nexus-rpc",
        enforce_permissions=enforce_permissions,
        enforce_zone_isolation=enforce_zone_isolation,
        has_auth=has_auth,
    )


@router.get("/health/detailed")
@limiter.exempt
async def health_check_detailed(request: Request) -> dict[str, Any]:
    """Detailed health check including all components.

    Returns status of:
    - Core service
    - Search daemon (if enabled)
    - Database connection
    - Background tasks
    - Mounted backends (Issue #708)
    """
    state = request.app.state
    health: dict[str, Any] = {
        "status": "healthy",
        "service": "nexus-rpc",
        "components": {},
    }

    # Check search daemon (Issue #951)
    if state.search_daemon:
        daemon_health = state.search_daemon.get_health()
        health["components"]["search_daemon"] = daemon_health
    else:
        health["components"]["search_daemon"] = {
            "status": "disabled",
            "message": "Set NEXUS_SEARCH_DAEMON=true to enable",
        }

    # Check ReBAC + circuit breaker (Issue #726)
    rebac_health: dict[str, Any] = {"status": "disabled"}
    has_rebac = state.async_rebac_manager or getattr(state.nexus_fs, "_rebac_manager", None)
    if has_rebac:
        cb = getattr(state, "rebac_circuit_breaker", None)
        if cb:
            from nexus.services.permissions.circuit_breaker import CircuitState

            cb_state = cb.state
            if cb_state == CircuitState.CLOSED:
                rebac_status = "healthy"
            elif cb_state == CircuitState.HALF_OPEN:
                rebac_status = "degraded"
            else:
                rebac_status = "unhealthy"
            rebac_health = {
                "status": rebac_status,
                "circuit_state": cb_state.value,
                "failure_count": cb.failure_count,
                "open_count": cb.open_count,
                "last_failure_time": cb.last_failure_time,
            }
        else:
            rebac_health = {"status": "healthy"}
    health["components"]["rebac"] = rebac_health

    # Check subscription manager
    health["components"]["subscriptions"] = {
        "status": "healthy" if state.subscription_manager else "disabled",
    }

    # Check WebSocket manager (Issue #1116)
    if state.websocket_manager:
        ws_stats = state.websocket_manager.get_stats()
        health["components"]["websocket"] = {
            "status": "healthy",
            "current_connections": ws_stats["current_connections"],
            "total_connections": ws_stats["total_connections"],
            "total_messages_sent": ws_stats["total_messages_sent"],
            "connections_by_zone": ws_stats["connections_by_zone"],
        }
    else:
        health["components"]["websocket"] = {"status": "disabled"}

    # Check Reactive Subscription Manager (Issue #1167)
    if state.reactive_subscription_manager:
        try:
            reactive_stats = state.reactive_subscription_manager.get_stats()
            health["components"]["reactive_subscriptions"] = {
                "status": "healthy",
                "total_subscriptions": reactive_stats["total_subscriptions"],
                "read_set_subscriptions": reactive_stats["read_set_subscriptions"],
                "pattern_subscriptions": reactive_stats["pattern_subscriptions"],
                "avg_lookup_ms": reactive_stats["avg_lookup_ms"],
                "registry": reactive_stats["registry"],
            }
        except Exception as e:
            health["components"]["reactive_subscriptions"] = {
                "status": "error",
                "error": str(e),
            }
    else:
        health["components"]["reactive_subscriptions"] = {"status": "disabled"}

    # Check mounted backends (Issue #708)
    backends_health: dict[str, Any] = {}
    if state.nexus_fs and hasattr(state.nexus_fs, "path_router"):
        mounts = state.nexus_fs.path_router.list_mounts()
        for mount in mounts:
            backend = mount.backend
            mount_point = mount.mount_point

            try:
                status = backend.check_connection()
                backends_health[mount_point] = {
                    "backend": backend.name,
                    "healthy": status.success,
                    "latency_ms": status.latency_ms,
                    "user_scoped": backend.user_scoped,
                    "thread_safe": backend.thread_safe,
                }
                if status.error_message:
                    backends_health[mount_point]["error"] = status.error_message
                if status.details:
                    backends_health[mount_point]["details"] = status.details
            except Exception as e:
                backends_health[mount_point] = {
                    "backend": backend.name,
                    "healthy": False,
                    "error": str(e),
                }

    health["components"]["backends"] = backends_health

    # Update overall status if any backend is unhealthy
    unhealthy_backends = [k for k, v in backends_health.items() if not v.get("healthy", True)]
    if unhealthy_backends:
        health["status"] = "degraded"
        health["unhealthy_backends"] = unhealthy_backends

    # Circuit breaker health (Issue #1366)
    _resiliency_mgr = (
        state.nexus_fs._service_extras.get("resiliency_manager")
        if state.nexus_fs and hasattr(state.nexus_fs, "_service_extras")
        else None
    )
    if _resiliency_mgr is not None:
        health["components"]["resiliency"] = _resiliency_mgr.health_check()
        if health["components"]["resiliency"]["status"] == "degraded":
            health["status"] = "degraded"

    return health


@router.get("/metrics/pool")
@limiter.exempt
async def pool_metrics(request: Request) -> dict[str, Any]:
    """Get database connection pool metrics."""
    state = request.app.state
    metrics: dict[str, Any] = {}

    # PostgreSQL pool stats from metadata store
    if state.nexus_fs and hasattr(state.nexus_fs, "metadata"):
        try:
            pg_stats = state.nexus_fs.metadata.get_pool_stats()
            metrics["postgres"] = pg_stats
        except Exception as e:
            metrics["postgres"] = {"error": str(e)}
    else:
        metrics["postgres"] = {"status": "not_available"}

    # Redis/Dragonfly pool stats from cache factory
    try:
        from nexus.cache.factory import get_cache_factory

        cache_factory = get_cache_factory()
        if cache_factory.has_cache_store and cache_factory._cache_client:
            dragonfly_stats = cache_factory._cache_client.get_pool_stats()
            dragonfly_info = await cache_factory._cache_client.get_info()
            metrics["dragonfly"] = {
                **dragonfly_stats,
                "server_info": dragonfly_info,
            }
        else:
            metrics["dragonfly"] = {"status": "not_configured"}
    except RuntimeError:
        metrics["dragonfly"] = {"status": "not_initialized"}
    except Exception as e:
        metrics["dragonfly"] = {"error": str(e)}

    return metrics
