"""Shared test fixtures for server unit tests.

Issue #1002: Structured JSON logging with request correlation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

# ---------------------------------------------------------------------------
# ASGI test helpers — shared between correlation middleware test files
# ---------------------------------------------------------------------------


def make_http_scope(
    method: str = "GET",
    path: str = "/test",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict:
    """Create a minimal ASGI HTTP scope for testing."""
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "query_string": b"",
    }


def make_receive() -> AsyncMock:
    """Create a mock ASGI receive callable."""
    return AsyncMock(return_value={"type": "http.request", "body": b""})


class SendCapture:
    """Captures ASGI send() calls for inspection."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)

    @property
    def response_headers(self) -> dict[str, str]:
        for msg in self.messages:
            if msg["type"] == "http.response.start":
                return {k.decode(): v.decode() for k, v in msg.get("headers", [])}
        return {}

    @property
    def status_code(self) -> int:
        for msg in self.messages:
            if msg["type"] == "http.response.start":
                return msg["status"]
        return 0
