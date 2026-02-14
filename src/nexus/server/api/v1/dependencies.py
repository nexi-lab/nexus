"""Shared dependencies for API v1 endpoints (#1288).

Provides typed FastAPI dependency injection functions that read from
``request.app.state`` instead of the legacy ``_app_state`` global.

Note: This module intentionally does NOT use ``from __future__ import annotations``
because FastAPI uses ``eval_str=True`` on dependency signatures at import time,
which fails for TYPE_CHECKING-only imports.

Issue #1288: Decompose FastAPI server monolith into domain routers.
"""

import logging
from typing import Any

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


# =============================================================================
# Core service dependencies
# =============================================================================


def get_nexus_fs(request: Request) -> Any:
    """Get NexusFS instance from app.state, raising 503 if not initialized."""
    fs = getattr(request.app.state, "nexus_fs", None)
    if fs is None:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")
    return fs


def get_async_nexus_fs(request: Request) -> Any:
    """Get AsyncNexusFS instance from app.state, raising 503 if not initialized."""
    fs = getattr(request.app.state, "async_nexus_fs", None)
    if fs is None:
        raise HTTPException(status_code=503, detail="AsyncNexusFS not initialized")
    return fs


def get_lock_manager(request: Request) -> Any:
    """Get the distributed lock manager from NexusFS, raising 503 if not configured."""
    fs = get_nexus_fs(request)
    if not fs._has_distributed_locks():
        raise HTTPException(
            status_code=503,
            detail="Distributed lock manager not configured. "
            "Enable Redis/Dragonfly for distributed locking.",
        )
    return fs._lock_manager


def get_subscription_manager(request: Request) -> Any:
    """Get SubscriptionManager from app.state, raising 503 if not available."""
    mgr = getattr(request.app.state, "subscription_manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="Subscription manager not available")
    return mgr


def get_search_daemon(request: Request) -> Any:
    """Get SearchDaemon from app.state, raising 503 if not enabled."""
    daemon = getattr(request.app.state, "search_daemon", None)
    if daemon is None:
        raise HTTPException(
            status_code=503,
            detail="Search daemon not enabled (set NEXUS_SEARCH_DAEMON=true)",
        )
    return daemon


def get_websocket_manager(request: Request) -> Any:
    """Get WebSocketManager from app.state, raising 503 if not available."""
    mgr = getattr(request.app.state, "websocket_manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="WebSocket manager not available")
    return mgr


def get_key_service(request: Request) -> Any:
    """Get KeyService from app.state, raising 503 if not available."""
    svc = getattr(request.app.state, "key_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="Identity service not available")
    return svc


def get_database_url(request: Request) -> str:
    """Get database URL from app.state, raising 503 if not configured."""
    url = getattr(request.app.state, "database_url", None)
    if not url:
        raise HTTPException(status_code=503, detail="Database URL not configured")
    return url


def get_operation_timeout(request: Request) -> float:
    """Get operation timeout from app.state (default: 30.0s)."""
    return getattr(request.app.state, "operation_timeout", 30.0)


# =============================================================================
# Optional service dependencies (return None instead of 503)
# =============================================================================


def get_optional_search_daemon(request: Request) -> Any:
    """Get SearchDaemon from app.state, returning None if not enabled."""
    return getattr(request.app.state, "search_daemon", None)


def get_optional_subscription_manager(request: Request) -> Any:
    """Get SubscriptionManager from app.state, returning None if not available."""
    return getattr(request.app.state, "subscription_manager", None)


def get_optional_websocket_manager(request: Request) -> Any:
    """Get WebSocketManager from app.state, returning None if not available."""
    return getattr(request.app.state, "websocket_manager", None)
