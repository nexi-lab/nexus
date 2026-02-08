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

from nexus.message_gateway import (
    Message,
    append_message,
    get_conversation_path,
    get_sync_cursor,
    sync_messages,
)
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


async def _fire_gateway_event(session_id: str, actor_id: str, context: Any) -> None:
    """Fire file_write event for subscription webhooks.

    Args:
        session_id: Session key
        actor_id: Who triggered the event
        context: Operation context with zone info
    """
    try:
        from nexus.server.subscriptions import get_subscription_manager

        manager = get_subscription_manager()
        if manager is None:
            return

        file_path = get_conversation_path(session_id)
        zone_id = getattr(context, "zone_id", None) or "default"
        subject_type = "agent" if actor_id.startswith("agent:") else "user"

        await manager.broadcast(
            event_type="file_write",
            data={
                "file_path": file_path,
                "zone_id": zone_id,
                "subject_id": actor_id,
                "subject_type": subject_type,
            },
            zone_id=zone_id,
        )
        logger.debug(f"Gateway fired file_write event for {file_path}")

    except Exception as e:
        logger.warning(f"Failed to fire gateway event: {e}")


@router.post(
    "/messages", response_model=GatewayMessageResponse, status_code=status.HTTP_201_CREATED
)
async def send_message(
    request: GatewayMessageRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> GatewayMessageResponse:
    """Send a message through the gateway.

    For agent messages (role="agent"):
    - Sends to the channel adapter FIRST (Discord, Slack, etc.)
    - Gets the channel's native message ID back
    - THEN stores in conversation.jsonl with that ID

    This ensures conversation.jsonl only contains messages that
    actually exist in the channel.

    For human messages with an ID (from sync):
    - Stores directly with the provided channel message ID

    Returns:
        - 201 Created with message_id if new message
        - 200 OK with status="duplicate" if already processed
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        # Get operation context for NexusFS
        context = _get_operation_context(auth_result)

        # For agent messages, send to channel FIRST
        if request.role == "agent":
            adapter = _channel_adapters.get(request.channel)
            if not adapter:
                raise HTTPException(
                    status_code=400,
                    detail=f"No adapter registered for channel: {request.channel}. "
                    f"Cannot send agent messages without a channel adapter. "
                    f"Available: {list(_channel_adapters.keys())}",
                )

            # Send to channel and get Message with native ID
            message = await adapter.send_message(
                session_id=request.session_id,
                text=request.text,
                parent_id=request.parent_id,
            )

            # Update message fields from request that adapter doesn't set
            message = Message(
                id=message.id,  # Channel's native ID
                text=message.text,
                user=request.user,  # Use requested user, not adapter's bot user
                role="agent",
                session_id=request.session_id,
                channel=request.channel,
                ts=message.ts,  # Channel's timestamp
                parent_id=message.parent_id,
                target=request.target,
                metadata={**message.metadata, **(request.metadata or {})},
            )

            logger.info(f"Agent message sent to {request.channel}: {message.id}")

        else:
            # Human message (from sync or direct) - use provided ID or generate
            message_id = request.id or f"msg_{uuid.uuid4().hex[:16]}"
            ts = request.ts or datetime.now(UTC).isoformat()

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

        # Check for duplicate (using the message ID)
        dedup = _get_deduplicator()
        if not dedup.check_and_mark(message.id):
            return GatewayMessageResponse(
                message_id=message.id,
                status="duplicate",
                ts=message.ts,
            )

        # Append to conversation
        append_message(
            nx=app_state.nexus_fs,
            session_id=request.session_id,
            message=message,
            context=context,
        )

        logger.info(f"Message {message.id} appended to session {request.session_id}")

        # Fire file_write event for subscription webhooks
        await _fire_gateway_event(request.session_id, request.user, context)

        return GatewayMessageResponse(
            message_id=message.id,
            status="created",
            ts=message.ts,
        )

    except HTTPException:
        raise
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

    If no `after_id` is provided, uses the last synced message ID from
    session metadata (incremental sync). The sync cursor is updated
    after each successful sync.

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

    # Get operation context for NexusFS
    context = _get_operation_context(auth_result)

    try:
        # Determine after_id with priority:
        # 1. history_message_id (explicit cursor from caller)
        # 2. after_id (explicit parameter)
        # 3. Stored cursor from session metadata
        after_id: str | None = None
        if request.history_message_id:
            after_id = request.history_message_id
            logger.debug(f"Using caller's history cursor: after_id={after_id}")
        elif request.after_id:
            after_id = request.after_id
        else:
            cursor = get_sync_cursor(app_state.nexus_fs, request.session_id, context)
            if cursor:
                after_id = cursor["last_synced_id"]
                logger.debug(f"Using stored sync cursor: after_id={after_id}")

        # Fetch history from channel
        messages = await adapter.fetch_history(
            session_id=request.session_id,
            limit=request.limit,
            before_id=request.before_id,
            after_id=after_id,
        )

        # Sync messages to conversation file (also updates sync cursor)
        added, skipped = sync_messages(
            nx=app_state.nexus_fs,
            session_id=request.session_id,
            messages=messages,
            context=context,
        )

        # Get the updated cursor (will be set if messages were synced)
        last_synced_id: str | None = None
        last_synced_ts: str | None = None
        if messages:
            # The cursor is set to the last message in the list
            last_synced_id = messages[-1].id
            last_synced_ts = messages[-1].ts

        logger.info(
            f"Synced {request.session_id}: fetched={len(messages)}, added={added}, skipped={skipped}"
        )

        return GatewaySyncResponse(
            session_id=request.session_id,
            added=added,
            skipped=skipped,
            total_fetched=len(messages),
            last_synced_id=last_synced_id,
            last_synced_ts=last_synced_ts,
        )

    except Exception as e:
        logger.error(f"Gateway sync error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Sync error: {e}") from e
