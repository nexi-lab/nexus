"""Request correlation middleware for ASGI applications.

Issue #1002: Structured JSON logging with request correlation.

Generates or propagates a correlation ID for each HTTP request, binds request
metadata to structlog's contextvars, and logs request lifecycle (start/completion).

The correlation ID is:
- Read from the ``X-Request-ID`` header if present, or
- Generated as a new UUID hex string.

It is then:
- Stored in a ``ContextVar`` (accessible anywhere in the async call chain)
- Bound to structlog contextvars (auto-included in all log entries)
- Set as the ``X-Request-ID`` response header
"""

import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from contextvars import ContextVar
from typing import Any

import structlog

# #4777: requests slower than this log at WARNING with ``slow_request=True``
# even when they succeed, so a 300 s ``200 OK`` is not indistinguishable from
# a 20 ms one except by reading ``duration_ms``.  ``0`` disables the check.
SLOW_REQUEST_MS_ENV = "NEXUS_SLOW_REQUEST_MS"
DEFAULT_SLOW_REQUEST_MS = 10_000.0


def slow_request_threshold_ms() -> float:
    """Resolve the slow-request threshold from the environment (ms)."""
    raw = os.environ.get(SLOW_REQUEST_MS_ENV, "").strip()
    if not raw:
        return DEFAULT_SLOW_REQUEST_MS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_SLOW_REQUEST_MS


# ASGI type aliases for mypy compatibility with Starlette's _MiddlewareFactory
ASGIApp = Callable[
    [
        MutableMapping[str, Any],
        Callable[[], Awaitable[MutableMapping[str, Any]]],
        Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]

# Strict validation: alphanumeric + hyphens, max 128 chars.
# Prevents log injection (newlines, control chars, fake JSON fields).
_VALID_CORRELATION_ID = re.compile(r"^[a-zA-Z0-9\-]{1,128}$")

_log = structlog.get_logger(__name__)

# Public ContextVars for use by other modules (e.g., audit logging)
correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)


class CorrelationMiddleware:
    """ASGI middleware that generates/propagates correlation IDs.

    Binds ``correlation_id``, ``http_method``, and ``http_path`` to structlog
    contextvars for automatic inclusion in all log entries during the request.

    Logs request completion with ``status_code`` and ``duration_ms``.

    Non-HTTP scopes (websocket, lifespan) are passed through without modification.
    """

    def __init__(self, app: ASGIApp, slow_request_ms: float | None = None) -> None:
        self._app = app
        self._slow_request_ms = (
            slow_request_ms if slow_request_ms is not None else slow_request_threshold_ms()
        )

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        # Extract or generate correlation ID
        correlation_id = self._extract_correlation_id(scope) or uuid.uuid4().hex

        # Store in ContextVar
        token = correlation_id_var.set(correlation_id)

        # Bind request context to structlog.
        # NOTE: We intentionally log only path (no query_string, no headers,
        # no body) to avoid leaking PII or credentials into log aggregators.
        # Path parameters may still contain sensitive data in some routes.
        structlog.contextvars.bind_contextvars(
            correlation_id=correlation_id,
            http_method=scope.get("method", ""),
            http_path=scope.get("path", ""),
        )

        start_time = time.perf_counter()
        status_code = 0

        # Intercept response start to capture status code and inject header
        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            nonlocal status_code

            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
                # Inject X-Request-ID response header
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", correlation_id.encode()))
                message = {**message, "headers": headers}

            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - start_time) * 1000, 1)

            log_kw: dict[str, Any] = {
                "status_code": status_code,
                "duration_ms": duration_ms,
            }
            is_slow = self._slow_request_ms > 0 and duration_ms >= self._slow_request_ms
            if is_slow:
                log_kw["slow_request"] = True
                log_kw["slow_threshold_ms"] = self._slow_request_ms

            if status_code >= 500 or is_slow:
                _log.warning("request_completed", **log_kw)
            else:
                _log.info("request_completed", **log_kw)

            # Clear context to prevent leaking to next request
            structlog.contextvars.clear_contextvars()
            correlation_id_var.reset(token)

    @staticmethod
    def _extract_correlation_id(scope: dict) -> str | None:
        """Extract X-Request-ID from ASGI scope headers, with validation.

        Returns None (triggering auto-generation) if the header value
        fails validation. This prevents log injection attacks via crafted
        correlation IDs containing newlines, control characters, or fake
        JSON fields.
        """
        for key, value in scope.get("headers", []):
            if key == b"x-request-id":
                decoded: str = value.decode("utf-8", errors="replace")
                if _VALID_CORRELATION_ID.match(decoded):
                    return decoded
                # Invalid format — fall through to generate a new one
                return None
        return None
