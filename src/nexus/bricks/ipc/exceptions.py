"""IPC brick exceptions.

All exceptions inherit from IPCError so callers can catch the
entire family with a single except clause.
"""

from enum import StrEnum


class DLQReason(StrEnum):
    """Structured reason codes for dead-lettered messages."""

    TTL_EXPIRED = "ttl_expired"
    HANDLER_ERROR = "handler_error"
    ZONE_UNREACHABLE = "zone_unreachable"
    PERMISSION_DENIED = "permission_denied"
    MOUNT_NOT_FOUND = "mount_not_found"
    MAX_HOPS_EXCEEDED = "max_hops_exceeded"
    PARSE_ERROR = "parse_error"
    BACKPRESSURE = "backpressure"
    INVALID_SIGNATURE = "invalid_signature"
    UNSIGNED_MESSAGE = "unsigned_message"
    STALE_INBOX = "stale_inbox"


class IPCError(Exception):
    """Base exception for all IPC brick errors."""


class NonRetryableError(IPCError):
    """Raised by handlers to indicate the failure should not be retried.

    When a handler raises this (or a subclass), the message is immediately
    dead-lettered without exponential backoff retry. Use for errors where
    retry would produce the same failure (e.g. invalid payload, permission
    denied, business logic rejection).
    """


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


class CrossZoneDeliveryError(IPCError):
    """Raised when a cross-zone message delivery fails.

    Carries a structured :class:`DLQReason` and human-readable detail
    so dead-letter consumers can triage failures programmatically.
    """

    def __init__(
        self,
        reason: DLQReason,
        detail: str,
        *,
        source_zone: str | None = None,
        target_zone: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.source_zone = source_zone
        self.target_zone = target_zone
        self.agent_id = agent_id
        super().__init__(f"Cross-zone delivery failed [{reason.value}]: {detail}")
