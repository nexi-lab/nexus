"""Shared MockWebSocket for testing WebSocket-related functionality."""

from __future__ import annotations

import asyncio
from typing import Any


class MockWebSocket:
    """Mock WebSocket for testing.

    Reusable across unit and integration tests for WebSocket manager
    and reactive subscription integration tests.
    """

    def __init__(self) -> None:
        self.accepted = False
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self.sent_messages: list[dict[str, Any]] = []
        self.receive_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = reason

    async def send_json(self, data: dict[str, Any]) -> None:
        if self.closed:
            raise RuntimeError("WebSocket closed")
        self.sent_messages.append(data)

    async def receive_json(self) -> dict[str, Any]:
        return await self.receive_queue.get()

    def add_message(self, data: dict[str, Any]) -> None:
        """Add a message to the receive queue."""
        self.receive_queue.put_nowait(data)
