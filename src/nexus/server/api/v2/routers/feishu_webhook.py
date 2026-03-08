"""Feishu webhook event ingestion router.

Handles inbound Feishu webhook events and publishes them to the EventBus
as FileEvents for downstream processing.

Endpoint: POST /api/v2/webhooks/feishu

This is the HTTP-push ingress. For the recommended WebSocket (long connection)
ingress, see ``nexus.backends.connectors.feishu.ws_worker``.

Both ingress methods use the shared ``FeishuEventTranslator`` in
``nexus.backends.connectors.feishu.events`` for identical mapping.
"""

import hashlib
import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from nexus.backends.connectors.feishu.events import translate_feishu_event

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

    # Map to FileEvent via shared translator and publish
    file_event = translate_feishu_event(event_type, event)
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
