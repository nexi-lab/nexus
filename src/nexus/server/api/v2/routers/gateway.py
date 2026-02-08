"""Message Gateway REST API endpoints.

Provides endpoints for message handling:
- POST /api/v2/gateway/messages - Send a message through the gateway
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from nexus.message_gateway import Message, append_message
from nexus.message_gateway.dedup import Deduplicator
from nexus.server.api.v2.models import (
    GatewayMessageRequest,
    GatewayMessageResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/gateway", tags=["gateway"])

# Global deduplicator instance (single Gateway instance for MVP)
_deduplicator: Deduplicator | None = None


def _get_deduplicator() -> Deduplicator:
    """Get or create the global deduplicator."""
    global _deduplicator
    if _deduplicator is None:
        _deduplicator = Deduplicator()
    return _deduplicator


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

    Returns:
        - 201 Created with message_id if new message
        - 200 OK with status="duplicate" if already processed
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    # Generate message ID
    message_id = f"msg_{uuid.uuid4().hex[:16]}"
    ts = datetime.now(UTC).isoformat()

    # Check for duplicate
    dedup = _get_deduplicator()
    if not dedup.check_and_mark(message_id):
        # This shouldn't happen with UUID, but handle gracefully
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
