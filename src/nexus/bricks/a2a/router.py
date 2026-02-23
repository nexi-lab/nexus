"""A2A protocol FastAPI router.

Implements:
- ``GET /.well-known/agent.json`` — public Agent Card discovery
- ``POST /a2a`` — JSON-RPC 2.0 dispatch for all A2A methods
- SSE streaming via ``StreamingResponse`` for ``sendStreamingMessage``
  and ``subscribeToTask``

This module handles only HTTP concerns (auth, body parsing, error
wrapping).  Business logic lives in ``handlers.py`` and
``streaming.py``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from nexus.bricks.a2a.agent_card import AgentCardCache
from nexus.bricks.a2a.exceptions import A2AError
from nexus.bricks.a2a.handlers import dispatch
from nexus.bricks.a2a.models import A2AErrorData, A2ARequest, A2AResponse
from nexus.bricks.a2a.streaming import handle_streaming
from nexus.bricks.a2a.task_manager import TaskManager
from nexus.contracts.constants import DEFAULT_NEXUS_URL

logger = logging.getLogger(__name__)

# Type alias for the injected auth callback
AuthFn = Callable[[Request], Awaitable[dict[str, Any] | None]]


def _error_response(
    request_id: str | int | None,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build a JSON-RPC error JSONResponse."""
    error = A2AErrorData(code=code, message=message, data=data)
    resp = A2AResponse.from_error(request_id, error)
    return JSONResponse(
        content=resp.model_dump(mode="json", exclude_none=True),
        status_code=200,
    )


def build_router(
    *,
    _nexus_fs: Any = None,
    config: Any = None,
    base_url: str | None = None,
    task_manager: TaskManager | None = None,
    auth_required: bool = False,
    auth_fn: AuthFn | None = None,
) -> APIRouter:
    """Build and return the A2A FastAPI router.

    Parameters
    ----------
    task_manager:
        Optional pre-built TaskManager (useful for testing).
    auth_required:
        When *True*, all A2A operational endpoints require a valid
        ``Authorization`` header.
    auth_fn:
        Optional async callback ``(Request) -> dict | None`` for
        authentication.
    """
    effective_base_url = base_url or DEFAULT_NEXUS_URL

    router = APIRouter(tags=["a2a"])
    if task_manager is None:
        task_manager = TaskManager()
    tm: TaskManager = task_manager
    stream_registry = tm.stream_registry

    # Per-router Agent Card cache
    card_cache = AgentCardCache()

    # ------------------------------------------------------------------
    # Closure-local auth helper
    # ------------------------------------------------------------------

    async def _get_auth_result(request: Request) -> dict[str, Any] | None:
        if auth_fn is None:
            return None
        try:
            return await auth_fn(request)
        except (OSError, ConnectionError, TimeoutError, RuntimeError, ValueError, KeyError) as exc:
            # Expected auth failures: network issues, service unavailability,
            # invalid tokens, missing claims.  Treat as unauthenticated.
            # Programming errors (TypeError, AttributeError, etc.) propagate.
            logger.warning(
                "auth_fn raised %s; treating as unauthenticated",
                type(exc).__name__,
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Closure-local extended card handler
    # ------------------------------------------------------------------

    async def _handle_extended_card() -> dict[str, Any]:
        card = card_cache.get_card()
        if card is None:
            card_bytes = card_cache.get_card_bytes(config=config, base_url=effective_base_url)
            result: dict[str, Any] = json.loads(card_bytes)
            return result
        return card.model_dump(mode="json", exclude_none=True)

    # ------------------------------------------------------------------
    # Agent Card discovery (public — no auth)
    # ------------------------------------------------------------------

    @router.get("/.well-known/agent.json")
    async def get_agent_card() -> Response:
        card_bytes = card_cache.get_card_bytes(
            config=config,
            base_url=effective_base_url,
        )
        return Response(content=card_bytes, media_type="application/json")

    # ------------------------------------------------------------------
    # JSON-RPC dispatch
    # ------------------------------------------------------------------

    @router.post("/a2a")
    async def a2a_dispatch(request: Request) -> Response:
        # Enforce authentication
        if auth_required:
            auth_header = request.headers.get("Authorization")
            if not auth_header:
                return JSONResponse(
                    content={
                        "error": "Unauthorized",
                        "message": "Authentication required. Include Authorization header.",
                    },
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="A2A"'},
                )

        auth_result = await _get_auth_result(request)

        if auth_required and auth_result is None:
            return JSONResponse(
                content={
                    "error": "Unauthorized",
                    "message": "Invalid or expired credentials.",
                },
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="A2A"'},
            )

        # Parse JSON body — Starlette raises ValueError (json.JSONDecodeError)
        # or UnicodeDecodeError on malformed input.
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return _error_response(None, -32700, "Parse error")

        # Validate JSON-RPC request
        try:
            rpc_request = A2ARequest.model_validate(body)
        except ValidationError as e:
            return _error_response(body.get("id"), -32600, "Invalid request", {"detail": str(e)})

        method = rpc_request.method
        params = rpc_request.params or {}
        request_id = rpc_request.id
        zone_id = _extract_zone_id(auth_result)
        agent_id = _extract_agent_id(auth_result)

        # Streaming methods return SSE
        if method in ("a2a.tasks.sendStreamingMessage", "a2a.tasks.subscribeToTask"):
            try:
                return await handle_streaming(
                    method=method,
                    params=params,
                    request_id=request_id,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    task_manager=tm,
                    stream_registry=stream_registry,
                )
            except A2AError as e:
                return _error_response(request_id, e.code, e.message, e.data)
            except ValidationError as e:
                logger.warning("Bad request in A2A streaming dispatch: %s", e)
                return _error_response(request_id, -32602, "Invalid params")
            except Exception:
                # Last-resort boundary handler: log and return JSON-RPC internal
                # error.  Keeps the server running; prevents unhandled exceptions
                # from producing raw 500 responses.
                logger.exception("Unexpected error in A2A streaming dispatch")
                return _error_response(request_id, -32603, "Internal error")

        # Non-streaming methods return JSON-RPC
        try:
            result = await dispatch(
                method=method,
                params=params,
                zone_id=zone_id,
                agent_id=agent_id,
                task_manager=tm,
                handle_extended_card=_handle_extended_card,
            )
            resp = A2AResponse.success(request_id, result)
        except A2AError as e:
            resp = A2AResponse.from_error(
                request_id,
                A2AErrorData(code=e.code, message=e.message, data=e.data),
            )
        except ValidationError as e:
            logger.warning("Bad request in A2A dispatch: %s", e)
            resp = A2AResponse.from_error(
                request_id,
                A2AErrorData(code=-32602, message="Invalid params"),
            )
        except Exception:
            # Last-resort boundary handler: log and return JSON-RPC internal
            # error.  Keeps the server running; prevents unhandled exceptions
            # from producing raw 500 responses.
            logger.exception("Unexpected error in A2A dispatch")
            resp = A2AResponse.from_error(
                request_id,
                A2AErrorData(code=-32603, message="Internal error"),
            )

        return JSONResponse(
            content=resp.model_dump(mode="json", exclude_none=True),
            status_code=200,
        )

    return router


# ======================================================================
# Utilities
# ======================================================================


def _extract_zone_id(auth_result: dict[str, Any] | None) -> str:
    """Extract zone_id from auth result."""
    if auth_result and auth_result.get("zone_id"):
        zone_id: str = auth_result["zone_id"]
        return zone_id
    return "root"


def _extract_agent_id(auth_result: dict[str, Any] | None) -> str | None:
    """Extract agent_id from auth result."""
    if auth_result:
        return auth_result.get("x_agent_id") or auth_result.get("subject_id")
    return None
