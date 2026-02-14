"""Tests for async context propagation in correlation middleware.

Issue #1002: Structured JSON logging with request correlation.

Verifies that correlation IDs are correctly isolated between concurrent
async requests and propagated through nested async call chains.
"""

from __future__ import annotations

import asyncio

import pytest
import structlog

from nexus.server.middleware.correlation import (
    CorrelationMiddleware,
    correlation_id_var,
)
from tests.unit.server.conftest import SendCapture, make_http_scope, make_receive

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConcurrentRequestIsolation:
    """Verify that concurrent requests have isolated correlation contexts."""

    @pytest.mark.asyncio
    async def test_concurrent_requests_have_different_correlation_ids(self) -> None:
        """Two parallel requests should each see their own correlation_id."""
        captured_ids: dict[str, str | None] = {}
        captured_ctxs: dict[str, dict] = {}

        async def capture_app(scope: dict, receive, send) -> None:
            # Simulate some async work
            await asyncio.sleep(0.01)
            captured_ids[scope["path"]] = correlation_id_var.get()
            # Also capture structlog contextvars for isolation verification
            captured_ctxs[scope["path"]] = structlog.contextvars.get_contextvars()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        middleware = CorrelationMiddleware(capture_app)

        scope_a = make_http_scope(
            path="/a",
            headers=[(b"x-request-id", b"id-aaa")],
        )
        scope_b = make_http_scope(
            path="/b",
            headers=[(b"x-request-id", b"id-bbb")],
        )

        await asyncio.gather(
            middleware(scope_a, make_receive(), SendCapture()),
            middleware(scope_b, make_receive(), SendCapture()),
        )

        # ContextVar isolation
        assert captured_ids["/a"] == "id-aaa"
        assert captured_ids["/b"] == "id-bbb"

        # structlog.contextvars isolation (Issue 11)
        assert captured_ctxs["/a"]["correlation_id"] == "id-aaa"
        assert captured_ctxs["/a"]["http_path"] == "/a"
        assert captured_ctxs["/b"]["correlation_id"] == "id-bbb"
        assert captured_ctxs["/b"]["http_path"] == "/b"

    @pytest.mark.asyncio
    async def test_context_not_leaked_between_sequential_requests(self) -> None:
        """After request A completes, request B should not see A's context."""
        captured_ids: list[str | None] = []

        async def capture_app(scope: dict, receive, send) -> None:
            captured_ids.append(correlation_id_var.get())
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        middleware = CorrelationMiddleware(capture_app)

        # Request A
        await middleware(
            make_http_scope(headers=[(b"x-request-id", b"first")]),
            make_receive(),
            SendCapture(),
        )

        # Request B (no header — gets auto-generated ID)
        await middleware(
            make_http_scope(),
            make_receive(),
            SendCapture(),
        )

        assert captured_ids[0] == "first"
        assert captured_ids[1] is not None
        assert captured_ids[1] != "first"

    @pytest.mark.asyncio
    async def test_nested_async_calls_preserve_correlation_id(self) -> None:
        """Nested async operations within a request see the same correlation_id."""
        inner_id: str | None = None

        async def nested_service_call() -> None:
            nonlocal inner_id
            await asyncio.sleep(0.001)
            inner_id = correlation_id_var.get()

        async def app_with_nested(scope: dict, receive, send) -> None:
            await nested_service_call()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        middleware = CorrelationMiddleware(app_with_nested)
        await middleware(
            make_http_scope(headers=[(b"x-request-id", b"nested-test")]),
            make_receive(),
            SendCapture(),
        )

        assert inner_id == "nested-test"

    @pytest.mark.asyncio
    async def test_context_cleared_on_request_exception(self) -> None:
        """Exception in request handler should still clear context."""

        async def failing_app(scope: dict, receive, send) -> None:
            assert correlation_id_var.get() == "will-fail"
            raise RuntimeError("handler error")

        middleware = CorrelationMiddleware(failing_app)

        with pytest.raises(RuntimeError, match="handler error"):
            await middleware(
                make_http_scope(headers=[(b"x-request-id", b"will-fail")]),
                make_receive(),
                SendCapture(),
            )

        # Context should be cleaned up
        assert correlation_id_var.get() is None

    @pytest.mark.asyncio
    async def test_structlog_contextvars_bound_during_request(self) -> None:
        """structlog.contextvars should have correlation_id during the request."""
        captured_ctx: dict = {}

        async def capture_ctx_app(scope: dict, receive, send) -> None:
            # Get the current structlog context
            ctx = structlog.contextvars.get_contextvars()
            captured_ctx.update(ctx)
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        middleware = CorrelationMiddleware(capture_ctx_app)
        await middleware(
            make_http_scope(
                method="POST",
                path="/api/upload",
                headers=[(b"x-request-id", b"ctx-test")],
            ),
            make_receive(),
            SendCapture(),
        )

        assert captured_ctx["correlation_id"] == "ctx-test"
        assert captured_ctx["http_method"] == "POST"
        assert captured_ctx["http_path"] == "/api/upload"
