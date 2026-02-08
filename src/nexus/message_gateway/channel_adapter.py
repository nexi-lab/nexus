"""Channel adapter protocol for platform integrations.

Defines the interface for channel adapters (Discord, Slack, Telegram, etc.)
that connect external messaging platforms to the message gateway.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ChannelAdapter(Protocol):
    """Protocol for channel adapters.

    Channel adapters bridge external messaging platforms to the gateway.
    They handle:
    - Receiving messages from the platform
    - Normalizing to Message format
    - Sending agent responses back to the platform

    Implementations should be async and handle reconnection gracefully.
    """

    @property
    def channel(self) -> str:
        """Platform identifier (e.g., "discord", "slack", "telegram")."""
        ...

    async def send_message(
        self,
        session_id: str,
        text: str,
        *,
        parent_id: str | None = None,
    ) -> None:
        """Send a message to the channel.

        Args:
            session_id: Boardroom key (channel:account_id:chat_id)
            text: Message content to send
            parent_id: Optional message ID to reply to (for threading)
        """
        ...

    async def start(self) -> None:
        """Start the adapter.

        Connects to the platform and begins receiving messages.
        Should handle reconnection on disconnect.
        """
        ...

    async def stop(self) -> None:
        """Stop the adapter.

        Gracefully disconnects from the platform.
        Should cancel any pending operations.
        """
        ...

    @property
    def is_running(self) -> bool:
        """Check if the adapter is currently running."""
        ...
