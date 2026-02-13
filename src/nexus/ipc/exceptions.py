"""IPC brick exceptions.

All exceptions inherit from IPCError so callers can catch the
entire family with a single except clause.
"""

from __future__ import annotations


class IPCError(Exception):
    """Base exception for all IPC brick errors."""


class EnvelopeValidationError(IPCError):
    """Raised when a message envelope fails validation."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Invalid message envelope: {detail}")


class InboxNotFoundError(IPCError):
    """Raised when the target agent's inbox directory does not exist."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(
            f"Inbox not found for agent '{agent_id}'. Agent may not be registered or provisioned."
        )


class InboxFullError(IPCError):
    """Raised when the target agent's inbox exceeds the message limit."""

    def __init__(self, agent_id: str, current: int, limit: int) -> None:
        self.agent_id = agent_id
        self.current = current
        self.limit = limit
        super().__init__(
            f"Inbox full for agent '{agent_id}': {current}/{limit} messages. Retry later."
        )


class MessageExpiredError(IPCError):
    """Raised when a message has exceeded its TTL."""

    def __init__(self, message_id: str, ttl_seconds: int) -> None:
        self.message_id = message_id
        self.ttl_seconds = ttl_seconds
        super().__init__(f"Message '{message_id}' expired (TTL: {ttl_seconds}s)")
