"""Tests for request correlation middleware.

Issue #1002: Structured JSON logging with request correlation.

Tests the ASGI middleware that generates/propagates correlation IDs,
binds request metadata to structlog contextvars, and logs request lifecycle.
"""

from __future__ import annotations

import pytest
import structlog

from nexus.server.middleware.correlation import (
    CorrelationMiddleware,
    correlation_id_var,
)
from tests.unit.server.conftest import SendCapture, make_http_scope, make_receive

# We test via a minimal ASGI app rather than FastAPI to keep tests focused.


async def _ok_app(scope: dict, receive: object, send: object) -> None:
    """Minimal ASGI app that returns 200 with JSON body."""
    if scope["type"] == "http":
        await send(  # type: ignore[operator]
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"application/json"]],
            }
        )
        await send(  # type: ignore[operator]
            {"type": "http.response.body", "body": b'{"ok":true}'}
        )


async def _error_app(scope: dict, receive: object, send: object) -> None:
    """ASGI app that raises an exception."""
    raise ValueError("boom")


async def _capture_context_app(scope: dict, receive: object, send: object) -> None:
    """ASGI app that captures the current correlation_id from ContextVar."""
    scope["_captured_correlation_id"] = correlation_id_var.get()
    await _ok_app(scope, receive, send)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCorrelationIdGeneration:
    """Middleware generates or reads correlation IDs."""

    @pytest.mark.asyncio
    async def test_generates_correlation_id_when_no_header(self) -> None:
        middleware = CorrelationMiddleware(_ok_app)
        scope = make_http_scope()
        send = SendCapture()

        await middleware(scope, make_receive(), send)

        # Response should have X-Request-ID header
        assert "x-request-id" in send.response_headers

    @pytest.mark.asyncio
    async def test_reads_x_request_id_header(self) -> None:
        middleware = CorrelationMiddleware(_ok_app)
        scope = make_http_scope(
            headers=[(b"x-request-id", b"custom-id-123")],
        )
        send = SendCapture()

        await middleware(scope, make_receive(), send)

        assert send.response_headers["x-request-id"] == "custom-id-123"

    @pytest.mark.asyncio
    async def test_generated_id_is_hex_uuid(self) -> None:
        middleware = CorrelationMiddleware(_ok_app)
        scope = make_http_scope()
        send = SendCapture()

        await middleware(scope, make_receive(), send)

        cid = send.response_headers["x-request-id"]
        # UUID hex is 32 chars
        assert len(cid) == 32
        assert all(c in "0123456789abcdef" for c in cid)


class TestContextVarPropagation:
    """Correlation ID is available via ContextVar during request."""

    @pytest.mark.asyncio
    async def test_correlation_id_in_contextvars(self) -> None:
        middleware = CorrelationMiddleware(_capture_context_app)
        scope = make_http_scope(
            headers=[(b"x-request-id", b"test-corr-id")],
        )
        send = SendCapture()

        await middleware(scope, make_receive(), send)

        assert scope["_captured_correlation_id"] == "test-corr-id"

    @pytest.mark.asyncio
    async def test_context_cleared_after_request(self) -> None:
        middleware = CorrelationMiddleware(_ok_app)
        scope = make_http_scope()
        send = SendCapture()

        await middleware(scope, make_receive(), send)

        # After request completes, ContextVar should be cleared
        assert correlation_id_var.get() is None

    @pytest.mark.asyncio
    async def test_context_cleared_on_error(self) -> None:
        middleware = CorrelationMiddleware(_error_app)
        scope = make_http_scope()
        send = SendCapture()

        with pytest.raises(ValueError, match="boom"):
            await middleware(scope, make_receive(), send)

        # ContextVar should still be cleared even on error
        assert correlation_id_var.get() is None


class TestRequestLogging:
    """Middleware logs request start/completion with structured metadata."""

    @pytest.mark.asyncio
    async def test_request_completed_event_logged(self) -> None:
        middleware = CorrelationMiddleware(_ok_app)
        scope = make_http_scope(method="POST", path="/api/files")
        send = SendCapture()

        with structlog.testing.capture_logs() as cap:
            await middleware(scope, make_receive(), send)

        events = [e["event"] for e in cap]
        assert "request_completed" in events

    @pytest.mark.asyncio
    async def test_does_not_log_body(self) -> None:
        middleware = CorrelationMiddleware(_ok_app)
        scope = make_http_scope()
        send = SendCapture()

        with structlog.testing.capture_logs() as cap:
            await middleware(scope, make_receive(), send)

        # No log entry should contain the response body
        for entry in cap:
            assert '{"ok":true}' not in str(entry)


class TestCorrelationIdValidation:
    """Malicious or invalid X-Request-ID values are rejected."""

    @pytest.mark.asyncio
    async def test_rejects_newline_injection(self) -> None:
        middleware = CorrelationMiddleware(_ok_app)
        scope = make_http_scope(
            headers=[(b"x-request-id", b'fake-id\n{"level":"admin"}')],
        )
        send = SendCapture()

        await middleware(scope, make_receive(), send)

        # Should NOT use the injected value — should generate a new one
        cid = send.response_headers["x-request-id"]
        assert "\n" not in cid
        assert len(cid) == 32  # Generated UUID hex

    @pytest.mark.asyncio
    async def test_rejects_oversized_id(self) -> None:
        middleware = CorrelationMiddleware(_ok_app)
        scope = make_http_scope(
            headers=[(b"x-request-id", b"a" * 200)],
        )
        send = SendCapture()

        await middleware(scope, make_receive(), send)

        cid = send.response_headers["x-request-id"]
        assert len(cid) == 32  # Rejected, generated new one

    @pytest.mark.asyncio
    async def test_accepts_valid_uuid_with_hyphens(self) -> None:
        middleware = CorrelationMiddleware(_ok_app)
        valid_id = "550e8400-e29b-41d4-a716-446655440000"
        scope = make_http_scope(
            headers=[(b"x-request-id", valid_id.encode())],
        )
        send = SendCapture()

        await middleware(scope, make_receive(), send)

        assert send.response_headers["x-request-id"] == valid_id


class TestNonHttpScope:
    """Middleware passes through non-HTTP scopes (e.g., websocket, lifespan)."""

    @pytest.mark.asyncio
    async def test_non_http_scope_passthrough(self) -> None:
        inner_called = False

        async def inner_app(scope: dict, receive: object, send: object) -> None:
            nonlocal inner_called
            inner_called = True

        middleware = CorrelationMiddleware(inner_app)
        scope = {"type": "lifespan"}

        await middleware(scope, make_receive(), SendCapture())

        assert inner_called
