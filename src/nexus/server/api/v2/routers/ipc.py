"""IPC REST API — IPC send + SSE endpoints.

Inbox/query endpoints have been migrated to RPC services.
Compatibility send + SSE endpoints remain on REST.

    POST /api/v2/ipc/send              — enqueue a message in an agent inbox
    GET /api/v2/ipc/stream/{agent_id}  — SSE stream for real-time inbox notifications
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from nexus.bricks.ipc.conventions import inbox_path, validate_agent_id
from nexus.bricks.ipc.delivery import MessageSender
from nexus.bricks.ipc.envelope import MessageEnvelope, MessageType
from nexus.bricks.ipc.exceptions import (
    EnvelopeValidationError,
    InboxFullError,
    InboxNotFoundError,
)
from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/ipc", tags=["ipc"])

# ---------------------------------------------------------------------------
# Lazy imports (avoid circular imports with fastapi_server)
# ---------------------------------------------------------------------------


def _get_require_auth() -> Any:
    from nexus.server.dependencies import require_auth

    return require_auth


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _validate_agent_id(agent_id: str) -> str:
    """Validate agent_id at the REST boundary — rejects path traversal."""
    try:
        return validate_agent_id(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _check_agent_access(auth_result: dict[str, Any], agent_id: str) -> None:
    """Verify the caller is authorized to access the given agent's IPC resources.

    Admin users can access any agent. Non-admin callers must match the
    agent_id (via X-Agent-ID header or subject_id from auth).
    """
    if auth_result.get("is_admin", False):
        return
    caller_agent = auth_result.get("x_agent_id") or auth_result.get("subject_id")
    if caller_agent != agent_id:
        raise HTTPException(
            status_code=403,
            detail=f"Access denied: caller {caller_agent!r} cannot access agent {agent_id!r}",
        )


def _get_ipc_cache_store(request: Request) -> Any:
    """Get IPC cache store from app.state for TTL scheduling (Issue #3197)."""
    return getattr(request.app.state, "ipc_cache_store", None)


class SendMessageRequest(BaseModel):
    """Compatibility REST payload for IPC send."""

    model_config = ConfigDict(populate_by_name=True)

    sender: str
    recipient: str
    type: MessageType = MessageType.TASK
    payload: dict[str, Any]
    correlation_id: str | None = None
    ttl_seconds: int | None = None
    message_id: str | None = None


def _get_ipc_nexus_fs(request: Request) -> Any:
    return getattr(request.app.state, "ipc_nexus_fs", None)


def _get_ipc_event_publisher(request: Request) -> Any:
    return getattr(request.app.state, "ipc_event_publisher", None)


def _get_auth_zone_id(auth_result: dict[str, Any]) -> str:
    return auth_result.get("zone_id") or ROOT_ZONE_ID


# ---------------------------------------------------------------------------
# Send + SSE endpoints
# ---------------------------------------------------------------------------


@router.post("/send")
async def send_message(
    body: SendMessageRequest,
    _request: Request,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    vfs: Any = Depends(_get_ipc_nexus_fs),
    event_publisher: Any = Depends(_get_ipc_event_publisher),
    cache_store: Any = Depends(_get_ipc_cache_store),
) -> dict[str, Any]:
    """Compatibility REST endpoint for enqueueing IPC inbox messages."""
    _validate_agent_id(body.sender)
    _validate_agent_id(body.recipient)
    _check_agent_access(auth_result, body.sender)
    zone_id = _get_auth_zone_id(auth_result)

    if vfs is None:
        raise HTTPException(status_code=503, detail="IPC storage is not available")

    try:
        envelope_data: dict[str, Any] = {
            "from": body.sender,
            "to": body.recipient,
            "type": body.type,
            "payload": body.payload,
            "correlation_id": body.correlation_id,
            "ttl_seconds": body.ttl_seconds,
        }
        if body.message_id is not None:
            envelope_data["id"] = body.message_id
        envelope = MessageEnvelope.model_validate(envelope_data)
        sender = MessageSender(
            vfs=vfs,
            event_publisher=event_publisher,
            zone_id=zone_id,
            cache_store=cache_store,
        )
        message_path = await sender.send(envelope)
    except EnvelopeValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except InboxNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InboxFullError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "message_id": envelope.id,
        "path": message_path,
        "sender": envelope.sender,
        "recipient": envelope.recipient,
        "type": envelope.type.value,
    }


@router.get("/inbox/{agent_id}")
async def list_inbox(
    agent_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    vfs: Any = Depends(_get_ipc_nexus_fs),
) -> dict[str, Any]:
    """Compatibility REST endpoint for listing inbox messages."""
    _validate_agent_id(agent_id)
    _check_agent_access(auth_result, agent_id)
    zone_id = _get_auth_zone_id(auth_result)

    if vfs is None:
        raise HTTPException(status_code=503, detail="IPC storage is not available")

    try:
        from nexus.contracts.types import OperationContext

        _ctx = OperationContext(user_id="system", groups=[], zone_id=zone_id, is_system=True)
        filenames = vfs.sys_readdir(inbox_path(agent_id), recursive=False, context=_ctx)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {
        "agent_id": agent_id,
        "messages": [{"filename": name} for name in filenames],
        "count": len(filenames),
    }


@router.get("/inbox/{agent_id}/count")
async def inbox_count(
    agent_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    vfs: Any = Depends(_get_ipc_nexus_fs),
) -> dict[str, Any]:
    """Compatibility REST endpoint for inbox depth."""
    _validate_agent_id(agent_id)
    _check_agent_access(auth_result, agent_id)
    zone_id = _get_auth_zone_id(auth_result)

    if vfs is None:
        raise HTTPException(status_code=503, detail="IPC storage is not available")

    try:
        from nexus.contracts.types import OperationContext

        _ctx = OperationContext(user_id="system", groups=[], zone_id=zone_id, is_system=True)
        _entries = vfs.sys_readdir(inbox_path(agent_id), recursive=False, context=_ctx)
        count = len(_entries)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {
        "agent_id": agent_id,
        "count": count,
    }


@router.get("/stream/{agent_id}")
async def stream_inbox(
    agent_id: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    cache_store: Any = Depends(_get_ipc_cache_store),
) -> StreamingResponse:
    """SSE stream for real-time inbox notifications (Issue #3197).

    Opens a long-lived Server-Sent Events connection. When a message is
    sent to this agent's inbox, the sender's ``CacheStoreEventPublisher``
    publishes to ``ipc.inbox.{agent_id}`` via Redis/Dragonfly pub/sub.
    This endpoint subscribes to that channel and pushes events to the
    client as SSE.

    The SSE event contains notification metadata (message_id, sender,
    type). To read the actual message content, the client calls
    ``GET /api/v2/ipc/inbox/{agent_id}`` after receiving the event.

    Supports automatic reconnection via the SSE ``Last-Event-ID`` header.

    Usage::

        curl -N -H "Authorization: Bearer sk-..." \\
          http://localhost:2026/api/v2/ipc/stream/agent:bob

    Event format::

        event: message_delivered
        data: {"message_id":"msg_123","sender":"agent:alice","type":"task"}

    """
    _validate_agent_id(agent_id)
    _check_agent_access(auth_result, agent_id)

    if cache_store is None:
        raise HTTPException(
            status_code=503,
            detail="SSE streaming requires CacheStore (Dragonfly/Redis)",
        )

    channel = f"ipc.inbox.{agent_id}"

    async def event_generator() -> AsyncIterator[str]:
        """Subscribe to pub/sub and yield SSE events."""
        # Initial connection event
        yield f"event: connected\ndata: {json.dumps({'agent_id': agent_id, 'channel': channel})}\n\n"

        try:
            async with cache_store.subscribe(channel) as messages:
                async for msg in messages:
                    # Check if client disconnected
                    if await request.is_disconnected():
                        break

                    try:
                        data = json.loads(msg)
                        event_type = data.get("event", "message")
                        yield f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
                    except Exception:
                        logger.debug("Invalid event on channel %s", channel)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning(
                "SSE stream error for agent %s",
                agent_id,
                exc_info=True,
            )
            yield f"event: error\ndata: {json.dumps({'error': 'stream_interrupted'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable NGINX buffering
        },
    )
