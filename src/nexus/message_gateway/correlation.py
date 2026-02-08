"""Correlation tracking for agent-to-agent communication.

Provides request/response pattern for agents communicating through
the message gateway. Agent A sends a message with a correlation_id,
then waits for a response with the same correlation_id.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from nexus.message_gateway.types import Message

logger = logging.getLogger(__name__)

# Constants
DEFAULT_TIMEOUT = 30.0  # seconds
CLEANUP_INTERVAL = 60.0  # seconds


@dataclass
class PendingRequest:
    """A pending request waiting for a response."""

    correlation_id: str
    session_id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    timeout: float = DEFAULT_TIMEOUT
    future: asyncio.Future = field(default_factory=asyncio.Future)


class CorrelationTracker:
    """Tracks correlation IDs for request/response patterns.

    Used by agents to wait for responses to their messages.

    Usage:
        tracker = CorrelationTracker()

        # Start the cleanup task
        await tracker.start()

        # Send a message and wait for response
        correlation_id = tracker.create_correlation_id()
        await gateway_client.send_message(..., correlation_id=correlation_id)
        response = await tracker.wait_for_response(
            correlation_id=correlation_id,
            session_id=session_id,
            timeout=30.0,
        )

        # Register this with the watcher to receive responses
        watcher.on_human_message(tracker.handle_message)
    """

    def __init__(self) -> None:
        """Initialize the correlation tracker."""
        self._pending: dict[str, PendingRequest] = {}
        self._cleanup_task: asyncio.Task | None = None
        self._running = False

    def create_correlation_id(self) -> str:
        """Generate a new unique correlation ID.

        Returns:
            Unique correlation ID string
        """
        return f"corr_{uuid.uuid4().hex[:16]}"

    async def start(self) -> None:
        """Start the cleanup background task."""
        if self._running:
            return
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("CorrelationTracker started")

    async def stop(self) -> None:
        """Stop the tracker and cancel pending requests."""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Cancel all pending requests
        for request in self._pending.values():
            if not request.future.done():
                request.future.cancel()
        self._pending.clear()

        logger.info("CorrelationTracker stopped")

    async def wait_for_response(
        self,
        correlation_id: str,
        session_id: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Message:
        """Wait for a response with the given correlation ID.

        Args:
            correlation_id: Correlation ID to wait for
            session_id: Session to watch for response
            timeout: Maximum time to wait in seconds

        Returns:
            The response message

        Raises:
            asyncio.TimeoutError: If no response within timeout
            asyncio.CancelledError: If the request was cancelled
        """
        if correlation_id in self._pending:
            raise ValueError(f"Correlation ID {correlation_id} already pending")

        request = PendingRequest(
            correlation_id=correlation_id,
            session_id=session_id,
            timeout=timeout,
        )
        self._pending[correlation_id] = request

        try:
            return await asyncio.wait_for(request.future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"Correlation {correlation_id} timed out after {timeout}s")
            raise
        finally:
            self._pending.pop(correlation_id, None)

    async def handle_message(self, message: Message, session_id: str) -> None:
        """Handle an incoming message (use as watcher callback).

        Checks if the message has a correlation_id that matches a
        pending request, and if so, completes the request.

        Args:
            message: Incoming message
            session_id: Session ID
        """
        # Check for correlation_id in metadata
        correlation_id = message.metadata.get("correlation_id")
        if not correlation_id:
            return

        request = self._pending.get(correlation_id)
        if not request:
            logger.debug(f"No pending request for correlation {correlation_id}")
            return

        # Verify session matches
        if request.session_id != session_id:
            logger.warning(
                f"Correlation {correlation_id} session mismatch: "
                f"expected {request.session_id}, got {session_id}"
            )
            return

        # Complete the request
        if not request.future.done():
            request.future.set_result(message)
            logger.debug(f"Correlation {correlation_id} completed")

    async def _cleanup_loop(self) -> None:
        """Background task to clean up expired pending requests."""
        while self._running:
            await asyncio.sleep(CLEANUP_INTERVAL)
            await self._cleanup_expired()

    async def _cleanup_expired(self) -> None:
        """Remove expired pending requests."""
        now = datetime.now(UTC)
        expired = []

        for corr_id, request in self._pending.items():
            deadline = request.created_at + timedelta(seconds=request.timeout)
            if now > deadline:
                expired.append(corr_id)

        for corr_id in expired:
            request = self._pending.pop(corr_id, None)
            if request and not request.future.done():
                request.future.set_exception(
                    asyncio.TimeoutError(f"Correlation {corr_id} expired")
                )
            logger.debug(f"Cleaned up expired correlation {corr_id}")


async def request_response(
    gateway_client: Any,
    watcher: Any,
    text: str,
    user: str,
    session_id: str,
    channel: str,
    *,
    target: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    tracker: CorrelationTracker | None = None,
) -> Message:
    """Send a message and wait for a response.

    Convenience function for the request/response pattern.

    Args:
        gateway_client: GatewayClient instance
        watcher: ConversationWatcher instance
        text: Message content
        user: Sender ID
        session_id: Session key
        channel: Platform
        target: Optional @mention hint
        timeout: Maximum wait time
        tracker: Optional CorrelationTracker (creates one if not provided)

    Returns:
        The response message

    Raises:
        asyncio.TimeoutError: If no response within timeout
    """
    from nexus.message_gateway.client import GatewayClient
    from nexus.message_gateway.watcher import ConversationWatcher

    # Create tracker if not provided
    own_tracker = tracker is None
    if own_tracker:
        tracker = CorrelationTracker()
        await tracker.start()
        watcher.on_human_message(tracker.handle_message)

    try:
        # Generate correlation ID
        correlation_id = tracker.create_correlation_id()

        # Send message
        await gateway_client.send_message(
            text=text,
            user=user,
            session_id=session_id,
            channel=channel,
            target=target,
            correlation_id=correlation_id,
        )

        # Wait for response
        return await tracker.wait_for_response(
            correlation_id=correlation_id,
            session_id=session_id,
            timeout=timeout,
        )

    finally:
        if own_tracker:
            await tracker.stop()
