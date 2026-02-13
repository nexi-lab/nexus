"""A2A protocol FastAPI router.

Implements:
- ``GET /.well-known/agent.json`` — public Agent Card discovery
- ``POST /a2a`` — JSON-RPC 2.0 dispatch for all A2A methods
- SSE streaming via ``StreamingResponse`` for ``sendStreamingMessage``
  and ``subscribeToTask``
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from nexus.a2a.agent_card import get_cached_card_bytes
from nexus.a2a.exceptions import (
    A2AError,
    InvalidParamsError,
    MethodNotFoundError,
    PushNotificationNotSupportedError,
)
from nexus.a2a.models import (
    A2AErrorData,
    A2ARequest,
    A2AResponse,
    SendParams,
    TaskIdParams,
    TaskQueryParams,
    TaskState,
)
from nexus.a2a.task_manager import TaskManager

logger = logging.getLogger(__name__)

# SSE configuration
SSE_KEEPALIVE_INTERVAL = 15  # seconds
SSE_MAX_LIFETIME = 1800  # 30 minutes


def build_router(
    *,
    _nexus_fs: Any = None,
    config: Any = None,
    base_url: str | None = None,
    task_manager: TaskManager | None = None,
) -> APIRouter:
    """Build and return the A2A FastAPI router.

    This function is called once during server startup.

    Parameters
    ----------
    task_manager:
        Optional pre-built TaskManager (useful for testing).
        When *None* a new instance is created.
    """
    effective_base_url = base_url or "http://localhost:2026"

    router = APIRouter(tags=["a2a"])
    if task_manager is None:
        task_manager = TaskManager()
    tm: TaskManager = task_manager  # Final binding for closure narrowing

    # ------------------------------------------------------------------
    # Agent Card discovery (public — no auth)
    # ------------------------------------------------------------------

    @router.get("/.well-known/agent.json")
    async def get_agent_card() -> Response:
        """Serve the A2A Agent Card.

        This is a public endpoint — no authentication required.
        Other agents use it to discover capabilities before initiating
        communication.
        """
        card_bytes = get_cached_card_bytes(
            config=config,
            base_url=effective_base_url,
        )
        return Response(content=card_bytes, media_type="application/json")

    # ------------------------------------------------------------------
    # JSON-RPC dispatch
    # ------------------------------------------------------------------

    @router.post("/a2a")
    async def a2a_dispatch(request: Request) -> Response:
        """A2A JSON-RPC 2.0 endpoint.

        Dispatches to the appropriate handler based on the ``method`` field.
        Streaming methods return SSE responses.

        **Authentication**: When the server has authentication enabled, all
        A2A requests require a valid ``Authorization`` header. This follows
        real-world A2A implementations (ServiceNow, LangSmith, etc.) which
        require auth for all operational endpoints.
        """
        auth_result = await _get_auth_result_safe(request)

        # Enforce authentication when server has auth configured
        # Per A2A spec: "All A2A requests must include a valid Authorization header"
        # Reference: https://a2a-protocol.org/latest/topics/enterprise-ready/
        try:
            from nexus.server.fastapi_server import _app_state

            has_auth = bool(_app_state.api_key or _app_state.auth_provider)
            if has_auth and not auth_result:
                # Return 401 Unauthorized per OAuth 2.0 / A2A best practices
                return JSONResponse(
                    content={
                        "error": "Unauthorized",
                        "message": "Authentication required. Include Authorization header.",
                    },
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="A2A"'},
                )
        except ImportError:
            # If fastapi_server not available, allow request (testing scenario)
            pass

        try:
            body = await request.json()
        except Exception:
            error = A2AErrorData(code=-32700, message="Parse error")
            resp = A2AResponse.from_error(None, error)
            return JSONResponse(
                content=resp.model_dump(mode="json", exclude_none=True),
                status_code=200,
            )

        try:
            rpc_request = A2ARequest.model_validate(body)
        except Exception as e:
            error = A2AErrorData(
                code=-32600,
                message="Invalid request",
                data={"detail": str(e)},
            )
            resp = A2AResponse.from_error(body.get("id"), error)
            return JSONResponse(
                content=resp.model_dump(mode="json", exclude_none=True),
                status_code=200,
            )

        method = rpc_request.method
        params = rpc_request.params or {}
        request_id = rpc_request.id
        zone_id = _extract_zone_id(auth_result)
        agent_id = _extract_agent_id(auth_result)

        # Streaming methods return SSE
        if method in ("a2a.tasks.sendStreamingMessage", "a2a.tasks.subscribeToTask"):
            try:
                return await _handle_streaming(
                    method=method,
                    params=params,
                    request_id=request_id,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    task_manager=tm,
                )
            except A2AError as e:
                resp = A2AResponse.from_error(
                    request_id,
                    A2AErrorData(code=e.code, message=e.message, data=e.data),
                )
                return JSONResponse(
                    content=resp.model_dump(mode="json", exclude_none=True),
                    status_code=200,
                )
            except Exception:
                logger.exception("Unexpected error in A2A streaming dispatch")
                resp = A2AResponse.from_error(
                    request_id,
                    A2AErrorData(code=-32603, message="Internal error"),
                )
                return JSONResponse(
                    content=resp.model_dump(mode="json", exclude_none=True),
                    status_code=200,
                )

        # Non-streaming methods return JSON-RPC
        try:
            result = await _dispatch(
                method=method,
                params=params,
                zone_id=zone_id,
                agent_id=agent_id,
                task_manager=tm,
            )
            resp = A2AResponse.success(request_id, result)
        except A2AError as e:
            resp = A2AResponse.from_error(
                request_id,
                A2AErrorData(code=e.code, message=e.message, data=e.data),
            )
        except Exception:
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
# Dispatch table
# ======================================================================


async def _dispatch(
    *,
    method: str,
    params: dict[str, Any],
    zone_id: str,
    agent_id: str | None,
    task_manager: TaskManager,
) -> Any:
    """Route a JSON-RPC method to its handler."""

    if method == "a2a.tasks.send":
        return await _handle_send(params, zone_id, agent_id, task_manager)
    elif method == "a2a.tasks.get":
        return await _handle_get(params, zone_id, task_manager)
    elif method == "a2a.tasks.cancel":
        return await _handle_cancel(params, zone_id, task_manager)
    elif method == "a2a.agent.getExtendedAgentCard":
        return await _handle_extended_card()
    elif method in (
        "a2a.tasks.createPushNotificationConfig",
        "a2a.tasks.getPushNotificationConfig",
        "a2a.tasks.deletePushNotificationConfig",
        "a2a.tasks.listPushNotificationConfigs",
    ):
        raise PushNotificationNotSupportedError()
    else:
        raise MethodNotFoundError(data={"method": method})


# ======================================================================
# Method handlers
# ======================================================================


async def _handle_send(
    params: dict[str, Any],
    zone_id: str,
    agent_id: str | None,
    task_manager: TaskManager,
) -> dict[str, Any]:
    """Handle a2a.tasks.send — create or continue a task."""
    try:
        send_params = SendParams.model_validate(params)
    except Exception as e:
        raise InvalidParamsError(data={"detail": str(e)}) from e

    # If params include an existing taskId (continuation), update it
    task_id = params.get("taskId")
    if task_id:
        task = await task_manager.get_task(task_id, zone_id=zone_id)
        # Add message to history and transition to working
        task = await task_manager.update_task_state(
            task_id,
            TaskState.WORKING,
            zone_id=zone_id,
            message=send_params.message,
        )
        return task.model_dump(mode="json")

    # New task
    task = await task_manager.create_task(
        send_params.message,
        zone_id=zone_id,
        agent_id=agent_id,
        metadata=send_params.metadata,
    )
    return task.model_dump(mode="json")


async def _handle_get(
    params: dict[str, Any],
    zone_id: str,
    task_manager: TaskManager,
) -> dict[str, Any]:
    """Handle a2a.tasks.get — retrieve a task by ID."""
    try:
        query_params = TaskQueryParams.model_validate(params)
    except Exception as e:
        raise InvalidParamsError(data={"detail": str(e)}) from e

    task = await task_manager.get_task(
        query_params.taskId,
        zone_id=zone_id,
        history_length=query_params.historyLength,
    )
    return task.model_dump(mode="json")


async def _handle_cancel(
    params: dict[str, Any],
    zone_id: str,
    task_manager: TaskManager,
) -> dict[str, Any]:
    """Handle a2a.tasks.cancel — cancel a running task."""
    try:
        cancel_params = TaskIdParams.model_validate(params)
    except Exception as e:
        raise InvalidParamsError(data={"detail": str(e)}) from e

    task = await task_manager.cancel_task(cancel_params.taskId, zone_id=zone_id)
    return task.model_dump(mode="json")


async def _handle_extended_card() -> dict[str, Any]:
    """Handle a2a.agent.getExtendedAgentCard — return full card (with auth)."""
    from nexus.a2a.agent_card import get_cached_card

    card = get_cached_card()
    if card is None:
        card_bytes = get_cached_card_bytes()
        result: dict[str, Any] = json.loads(card_bytes)
        return result
    return card.model_dump(mode="json", exclude_none=True)


# ======================================================================
# SSE streaming
# ======================================================================


async def _handle_streaming(
    *,
    method: str,
    params: dict[str, Any],
    request_id: str | int,
    zone_id: str,
    agent_id: str | None,
    task_manager: TaskManager,
) -> Response:
    """Handle streaming methods — returns an SSE StreamingResponse."""

    if method == "a2a.tasks.sendStreamingMessage":
        return await _handle_send_streaming(params, request_id, zone_id, agent_id, task_manager)
    elif method == "a2a.tasks.subscribeToTask":
        return await _handle_subscribe(params, request_id, zone_id, task_manager)
    else:
        # Should not reach here
        error = A2AErrorData(code=-32601, message="Method not found")
        resp = A2AResponse.from_error(request_id, error)
        return JSONResponse(
            content=resp.model_dump(mode="json", exclude_none=True),
            status_code=200,
        )


async def _handle_send_streaming(
    params: dict[str, Any],
    _request_id: str | int,
    zone_id: str,
    agent_id: str | None,
    task_manager: TaskManager,
) -> StreamingResponse:
    """Handle a2a.tasks.sendStreamingMessage — SSE response."""
    try:
        send_params = SendParams.model_validate(params)
    except Exception as e:
        raise InvalidParamsError(data={"detail": str(e)}) from e

    # Create task
    task = await task_manager.create_task(
        send_params.message,
        zone_id=zone_id,
        agent_id=agent_id,
        metadata=send_params.metadata,
    )

    # Register stream
    queue = task_manager.register_stream(task.id)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            # First event: the task itself
            yield _format_sse_event({"task": task.model_dump(mode="json")})

            start_time = time.monotonic()
            while True:
                elapsed = time.monotonic() - start_time
                if elapsed > SSE_MAX_LIFETIME:
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_INTERVAL)
                except TimeoutError:
                    # Send keepalive comment
                    yield ": keepalive\n\n"
                    continue

                if event is None:
                    # Sentinel: stream ended
                    break

                yield _format_sse_event(event)

                # If this was a final status update, close stream
                status_update = event.get("statusUpdate")
                if status_update and status_update.get("final"):
                    break
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            task_manager.unregister_stream(task.id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _handle_subscribe(
    params: dict[str, Any],
    _request_id: str | int,
    zone_id: str,
    task_manager: TaskManager,
) -> StreamingResponse:
    """Handle a2a.tasks.subscribeToTask — SSE response for existing task."""
    try:
        subscribe_params = TaskIdParams.model_validate(params)
    except Exception as e:
        raise InvalidParamsError(data={"detail": str(e)}) from e

    # Verify task exists
    task = await task_manager.get_task(subscribe_params.taskId, zone_id=zone_id)

    # Register stream
    queue = task_manager.register_stream(task.id)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            # First event: current task state
            yield _format_sse_event({"task": task.model_dump(mode="json")})

            # If already terminal, close immediately
            if task.status.state in (
                TaskState.COMPLETED,
                TaskState.FAILED,
                TaskState.CANCELED,
                TaskState.REJECTED,
            ):
                return

            start_time = time.monotonic()
            while True:
                elapsed = time.monotonic() - start_time
                if elapsed > SSE_MAX_LIFETIME:
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_INTERVAL)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue

                if event is None:
                    break

                yield _format_sse_event(event)

                status_update = event.get("statusUpdate")
                if status_update and status_update.get("final"):
                    break
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            task_manager.unregister_stream(task.id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ======================================================================
# Utilities
# ======================================================================


def _format_sse_event(data: dict[str, Any]) -> str:
    """Format a dict as an SSE event string."""
    payload = json.dumps(data, default=str)
    return f"data: {payload}\n\n"


async def _get_auth_result_safe(request: Request) -> dict[str, Any] | None:
    """Get auth result, returning None on failure (lazy import)."""
    try:
        from nexus.server.fastapi_server import get_auth_result

        return await get_auth_result(
            authorization=request.headers.get("Authorization"),
            x_agent_id=request.headers.get("X-Agent-ID"),
            x_nexus_subject=request.headers.get("X-Nexus-Subject"),
            x_nexus_zone_id=request.headers.get("X-Nexus-Zone-ID"),
        )
    except Exception:
        return None


def _extract_zone_id(auth_result: dict[str, Any] | None) -> str:
    """Extract zone_id from auth result."""
    if auth_result and auth_result.get("zone_id"):
        zone_id: str = auth_result["zone_id"]
        return zone_id
    return "default"


def _extract_agent_id(auth_result: dict[str, Any] | None) -> str | None:
    """Extract agent_id from auth result."""
    if auth_result:
        return auth_result.get("x_agent_id") or auth_result.get("subject_id")
    return None
