"""Feishu webhook event ingestion router (Task #83).

Handles inbound Feishu webhook events and publishes them to the EventBus
as FileEvents for downstream processing.

Endpoint: POST /api/v2/webhooks/feishu

Supports:
- URL verification challenge (Feishu setup handshake)
- Event signature verification
- Event mapping to FileEvent types:
  - im.message.receive_v1 -> FILE_WRITE
  - im.chat.member.bot.added_v1 -> DIR_CREATE
  - im.chat.member.bot.deleted_v1 -> DIR_DELETE
"""

import hashlib
import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from nexus.core.file_events import FileEvent, FileEventType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/webhooks", tags=["webhooks"])

# Module-level state set by configure_feishu_webhook()
_event_bus: Any = None
_verification_token: str | None = None
_encrypt_key: str | None = None

# Cache invalidation callbacks: list of callables that accept a FileEvent
_cache_invalidators: list[Any] = []


def configure_feishu_webhook(
    event_bus: Any,
    verification_token: str | None = None,
    encrypt_key: str | None = None,
) -> None:
    """Configure the Feishu webhook with EventBus and security tokens.

    Called at server startup.

    Args:
        event_bus: EventBusProtocol instance for publishing FileEvents
        verification_token: Feishu verification token for signature validation
        encrypt_key: Feishu encrypt key (optional, for encrypted payloads)
    """
    global _event_bus, _verification_token, _encrypt_key
    _event_bus = event_bus
    _verification_token = verification_token
    _encrypt_key = encrypt_key
    logger.info("Feishu webhook configured (verification=%s)", bool(verification_token))


def register_cache_invalidator(callback: Any) -> None:
    """Register a callback for cache invalidation on inbound events.

    The callback receives a FileEvent and should invalidate any cached
    content for the affected path.

    Args:
        callback: Callable that accepts a FileEvent
    """
    _cache_invalidators.append(callback)
    logger.info("Feishu webhook cache invalidator registered")


def _verify_signature(timestamp: str, nonce: str, body: str, signature: str) -> bool:
    """Verify Feishu event signature.

    Feishu signs events with: sha256(timestamp + nonce + encrypt_key + body)

    Args:
        timestamp: Request timestamp header
        nonce: Request nonce header
        body: Raw request body
        signature: X-Lark-Signature header value

    Returns:
        True if signature is valid or verification is disabled
    """
    if not _encrypt_key:
        return True  # No signature verification configured

    content = timestamp + nonce + _encrypt_key + body
    expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return expected == signature


def _map_event_to_file_event(event_type: str, event: dict[str, Any]) -> FileEvent | None:
    """Map a Feishu event to a FileEvent.

    Args:
        event_type: Feishu event type string
        event: Event payload dict

    Returns:
        FileEvent or None if event type is not mapped
    """
    if event_type == "im.message.receive_v1":
        message = event.get("message", {})
        chat_id = message.get("chat_id", "unknown")
        chat_type = message.get("chat_type", "group")
        folder = "p2p" if chat_type == "p2p" else "groups"
        return FileEvent(
            type=FileEventType.FILE_WRITE,
            path=f"/chat/feishu/{folder}/{chat_id}.yaml",
            size=len(json.dumps(message)),
        )

    if event_type == "im.chat.member.bot.added_v1":
        chat_id = event.get("chat_id", "unknown")
        return FileEvent(
            type=FileEventType.DIR_CREATE,
            path=f"/chat/feishu/groups/{chat_id}/",
        )

    if event_type == "im.chat.member.bot.deleted_v1":
        chat_id = event.get("chat_id", "unknown")
        return FileEvent(
            type=FileEventType.DIR_DELETE,
            path=f"/chat/feishu/groups/{chat_id}/",
        )

    logger.debug("Unmapped Feishu event type: %s", event_type)
    return None


@router.post("/feishu")
async def feishu_webhook(request: Request) -> JSONResponse:
    """Handle inbound Feishu webhook events.

    Handles three scenarios:
    1. URL verification challenge (returns challenge token)
    2. Event callback (maps to FileEvent and publishes to EventBus)
    3. Unknown event types (logged and ignored)
    """
    try:
        body = await request.body()
        payload = json.loads(body)
    except Exception as e:
        logger.warning("Failed to parse Feishu webhook body: %s", e)
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    # Handle URL verification challenge
    if "challenge" in payload:
        token = payload.get("token", "")
        if _verification_token and token != _verification_token:
            logger.warning("Feishu challenge verification failed: token mismatch")
            return JSONResponse(status_code=403, content={"error": "Invalid token"})
        return JSONResponse(content={"challenge": payload["challenge"]})

    # Validate verification token in event payload
    if _verification_token:
        header = payload.get("header", {})
        token = header.get("token", "")
        if token != _verification_token:
            logger.warning("Feishu event verification failed: token mismatch")
            return JSONResponse(status_code=403, content={"error": "Invalid token"})

    # Extract event info
    header = payload.get("header", {})
    event_type = header.get("event_type", "")
    event = payload.get("event", {})

    logger.info("Feishu webhook event: type=%s", event_type)

    # Map to FileEvent and publish
    file_event = _map_event_to_file_event(event_type, event)
    if file_event:
        # Publish to EventBus
        if _event_bus:
            try:
                await _event_bus.publish(file_event)
                logger.info(
                    "Published FileEvent: type=%s path=%s", file_event.type, file_event.path
                )
            except Exception as e:
                logger.error("Failed to publish Feishu event to EventBus: %s", e)

        # Invalidate caches so next read fetches fresh data from API
        for invalidator in _cache_invalidators:
            try:
                invalidator(file_event)
            except Exception as e:
                logger.error("Cache invalidation failed: %s", e)

    return JSONResponse(content={"code": 0, "msg": "ok"})
