"""Message Gateway REST API endpoints.

Provides endpoints for message handling:
- POST /api/v2/gateway/messages - Send a message through the gateway
- POST /api/v2/gateway/sync - Sync conversation history from channel
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from nexus.message_gateway import Message, append_message, sync_messages
from nexus.message_gateway.dedup import Deduplicator
from nexus.server.api.v2.models import (
    GatewayMessageRequest,
    GatewayMessageResponse,
    GatewaySyncRequest,
    GatewaySyncResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/gateway", tags=["gateway"])

# Global deduplicator instance (single Gateway instance for MVP)
_deduplicator: Deduplicator | None = None

# Registry of channel adapters for sync (populated at runtime)
_channel_adapters: dict[str, Any] = {}


def _get_deduplicator() -> Deduplicator:
    """Get or create the global deduplicator."""
    global _deduplicator
    if _deduplicator is None:
        _deduplicator = Deduplicator()
    return _deduplicator


def register_channel_adapter(channel: str, adapter: Any) -> None:
    """Register a channel adapter for sync operations.

    Args:
        channel: Channel name (e.g., "discord", "slack")
        adapter: ChannelAdapter instance
    """
    _channel_adapters[channel] = adapter
    logger.info(f"Registered channel adapter for {channel}")


def _get_require_auth() -> Any:
    """Lazy import to avoid circular imports."""
    from nexus.server.fastapi_server import require_auth

    return require_auth


def _get_app_state() -> Any:
    """Lazy import to avoid circular imports."""
    from nexus.server.fastapi_server import _app_state

    return _app_state


def _get_operation_context(auth_result: dict[str, Any]) -> Any:
    """Lazy import to avoid circular imports."""
    from nexus.server.fastapi_server import get_operation_context

    return get_operation_context(auth_result)


@router.post(
    "/messages", response_model=GatewayMessageResponse, status_code=status.HTTP_201_CREATED
)
async def send_message(
    request: GatewayMessageRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> GatewayMessageResponse:
    """Send a message through the gateway.

    Validates the message, checks for duplicates, and appends to the
    conversation file. All conversations are treated as "boardrooms".

    If `id` is provided (channel's native message ID), it will be used.
    Otherwise, a new ID is generated.

    Returns:
        - 201 Created with message_id if new message
        - 200 OK with status="duplicate" if already processed
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    # Use provided ID or generate one
    message_id = request.id or f"msg_{uuid.uuid4().hex[:16]}"
    ts = request.ts or datetime.now(UTC).isoformat()

    # Check for duplicate (using the actual message ID)
    dedup = _get_deduplicator()
    if not dedup.check_and_mark(message_id):
        return GatewayMessageResponse(
            message_id=message_id,
            status="duplicate",
            ts=ts,
        )

    try:
        # Create Message object
        message = Message(
            id=message_id,
            text=request.text,
            user=request.user,
            role=request.role,
            session_id=request.session_id,
            channel=request.channel,
            ts=ts,
            parent_id=request.parent_id,
            target=request.target,
            metadata=request.metadata,
        )

        # Get operation context for NexusFS
        context = _get_operation_context(auth_result)

        # Append to conversation
        append_message(
            nx=app_state.nexus_fs,
            session_id=request.session_id,
            message=message,
            context=context,
        )

        logger.info(f"Message {message_id} appended to session {request.session_id}")

        return GatewayMessageResponse(
            message_id=message_id,
            status="created",
            ts=ts,
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Gateway message error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Gateway error: {e}") from e


@router.post("/sync", response_model=GatewaySyncResponse)
async def sync_history(
    request: GatewaySyncRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> GatewaySyncResponse:
    """Sync conversation history from a channel.

    Fetches messages from the channel adapter and syncs them to the
    conversation file. Duplicate messages (by ID) are skipped.

    Requires a registered channel adapter for the specified channel.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    # Get channel adapter
    adapter = _channel_adapters.get(request.channel)
    if not adapter:
        raise HTTPException(
            status_code=400,
            detail=f"No adapter registered for channel: {request.channel}. "
            f"Available: {list(_channel_adapters.keys())}",
        )

    try:
        # Fetch history from channel
        messages = await adapter.fetch_history(
            session_id=request.session_id,
            limit=request.limit,
            before_id=request.before_id,
            after_id=request.after_id,
        )

        # Get operation context for NexusFS
        context = _get_operation_context(auth_result)

        # Sync messages to conversation file
        added, skipped = sync_messages(
            nx=app_state.nexus_fs,
            session_id=request.session_id,
            messages=messages,
            context=context,
        )

        logger.info(
            f"Synced {request.session_id}: fetched={len(messages)}, added={added}, skipped={skipped}"
        )

        return GatewaySyncResponse(
            session_id=request.session_id,
            added=added,
            skipped=skipped,
            total_fetched=len(messages),
        )

    except Exception as e:
        logger.error(f"Gateway sync error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Sync error: {e}") from e
