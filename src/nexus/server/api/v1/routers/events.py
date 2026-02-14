"""Events API router (Issue #1116, #1117, #1288).

Provides real-time event streaming and long-polling watch endpoints:
- WS  /ws/events/{subscription_id}  -- real-time events via WebSocket
- WS  /ws/events                    -- all zone events (no subscription filter)
- GET /api/watch                    -- long-polling watch for file changes

WebSocket endpoints access services via ``websocket.app.state`` directly
because FastAPI ``Depends()`` is not supported on WebSocket routes.

Extracted from ``fastapi_server.py`` during monolith decomposition (#1288).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi import status as http_status

from nexus.core.exceptions import NexusFileNotFoundError, NexusPermissionError
from nexus.server.api.v1.dependencies import get_nexus_fs
from nexus.server.dependencies import get_auth_result, get_operation_context

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events"])


# =============================================================================
# WebSocket endpoints (no Depends — use websocket.app.state directly)
# =============================================================================


@router.websocket("/ws/events/{subscription_id}")
async def websocket_events(
    websocket: WebSocket,
    subscription_id: str,
    token: str = Query(None, description="Authentication token"),
) -> None:
    """WebSocket endpoint for real-time file system events."""
    app_state = websocket.app.state

    if not getattr(app_state, "websocket_manager", None):
        await websocket.close(
            code=http_status.WS_1011_INTERNAL_ERROR,
            reason="WebSocket manager not available",
        )
        return

    # Authenticate via token query parameter
    auth_result: dict[str, Any] | None = None
    if token:
        auth_result = await get_auth_result(request=websocket, authorization=f"Bearer {token}")

    # Allow unauthenticated if no auth configured (open access mode)
    if not auth_result and (
        getattr(app_state, "api_key", None) or getattr(app_state, "auth_provider", None)
    ):
        await websocket.close(
            code=http_status.WS_1008_POLICY_VIOLATION,
            reason="Authentication required",
        )
        return

    zone_id = (auth_result or {}).get("zone_id") or "default"
    user_id = (auth_result or {}).get("subject_id")

    # Resolve subscription filters (patterns / event_types)
    patterns: list[str] = []
    event_types: list[str] = []

    subscription_manager = getattr(app_state, "subscription_manager", None)
    if subscription_manager and subscription_id != "all":
        subscription = subscription_manager.get(subscription_id, zone_id)
        if subscription:
            patterns = subscription.patterns or []
            event_types = subscription.event_types or []
        else:
            logger.debug(
                "Subscription %s not found, allowing dynamic subscription",
                subscription_id,
            )

    connection_id = f"{subscription_id}:{uuid.uuid4().hex[:8]}"

    _conn_info = await app_state.websocket_manager.connect(
        websocket=websocket,
        zone_id=zone_id,
        connection_id=connection_id,
        user_id=user_id,
        subscription_id=subscription_id if subscription_id != "all" else None,
        patterns=patterns,
        event_types=event_types,
    )

    await websocket.send_json(
        {
            "type": "connected",
            "connection_id": connection_id,
            "zone_id": zone_id,
            "patterns": patterns,
            "event_types": event_types,
        }
    )

    try:
        await app_state.websocket_manager.handle_client(websocket, connection_id)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("WebSocket error for %s: %s", connection_id, e)
    finally:
        await app_state.websocket_manager.disconnect(connection_id)


@router.websocket("/ws/events")
async def websocket_events_all(
    websocket: WebSocket,
    token: str = Query(None, description="Authentication token"),
) -> None:
    """WebSocket endpoint for all zone events (no subscription filter)."""
    await websocket_events(websocket, "all", token)


# =============================================================================
# Long-polling watch endpoint
# =============================================================================


@router.get("/api/watch", tags=["watch"])
async def watch_for_changes(
    path: str = Query("/**/*", description="Path or glob pattern to watch"),
    timeout: float = Query(30.0, ge=0.1, le=300.0, description="Maximum time to wait in seconds"),
    nexus_fs: Any = Depends(get_nexus_fs),
    _auth_result: dict[str, Any] | None = Depends(get_auth_result),
) -> dict[str, Any]:
    """Long-polling endpoint to wait for file system changes.

    Returns the first change matching *path* within *timeout* seconds,
    or ``{"changes": [], "timeout": true}`` if no change occurs.
    """
    context = None
    if _auth_result:
        context = get_operation_context(_auth_result)

    try:
        change = await nexus_fs.wait_for_changes(path=path, timeout=timeout, _context=context)
        if change is None:
            return {"changes": [], "timeout": True}
        return {"changes": [change], "timeout": False}
    except NotImplementedError as e:
        raise HTTPException(
            status_code=501,
            detail=(f"Watch not available: {e}. Requires Redis event bus or same-box backend."),
        ) from None
    except NexusFileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Path not found: {path}") from None
    except NexusPermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from None
    except Exception as e:
        logger.error("Watch error for %s: %s", path, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Watch error: {e}") from e
