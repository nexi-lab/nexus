"""Discord channel adapter.

Bridges Discord to the message gateway using Discord's Gateway WebSocket
and REST APIs.

Requirements:
    - discord.py library (pip install discord.py)
    - Discord bot token with MESSAGE_CONTENT intent
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from nexus.message_gateway.session_router import derive_session_key, parse_session_key
from nexus.message_gateway.types import Message

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


class DiscordAdapter:
    """Discord channel adapter.

    Implements the ChannelAdapter protocol for Discord integration.

    Usage:
        adapter = DiscordAdapter(
            token="your-bot-token",
            nexus_fs=nexus_fs,
            context=operation_context,
        )
        await adapter.start()
    """

    def __init__(
        self,
        token: str,
        nexus_fs: NexusFS,
        context: OperationContext,
        *,
        intents: Any | None = None,
    ) -> None:
        """Initialize the Discord adapter.

        Args:
            token: Discord bot token
            nexus_fs: NexusFS instance for message storage
            context: Operation context for permissions
            intents: Optional discord.py Intents object
        """
        self._token = token
        self._nexus_fs = nexus_fs
        self._context = context
        self._client: Any = None
        self._running = False
        self._intents = intents

    @property
    def channel(self) -> str:
        """Platform identifier."""
        return "discord"

    @property
    def is_running(self) -> bool:
        """Check if adapter is running."""
        return self._running

    async def start(self) -> None:
        """Start the Discord adapter.

        Connects to Discord Gateway and begins receiving messages.
        """
        try:
            import discord
        except ImportError as e:
            raise ImportError(
                "discord.py is required for Discord adapter. Install with: pip install discord.py"
            ) from e

        # Set up intents
        if self._intents is None:
            intents = discord.Intents.default()
            intents.message_content = True
            intents.guilds = True
        else:
            intents = self._intents

        # Create client
        self._client = discord.Client(intents=intents)

        # Register event handlers
        @self._client.event
        async def on_ready() -> None:
            logger.info(f"Discord adapter connected as {self._client.user}")

        @self._client.event
        async def on_message(message: Any) -> None:
            await self._handle_message(message)

        # Start client
        self._running = True
        try:
            await self._client.start(self._token)
        except asyncio.CancelledError:
            logger.info("Discord adapter cancelled")
        finally:
            self._running = False

    async def stop(self) -> None:
        """Stop the Discord adapter."""
        if self._client:
            await self._client.close()
        self._running = False
        logger.info("Discord adapter stopped")

    async def send_message(
        self,
        session_id: str,
        text: str,
        *,
        parent_id: str | None = None,
    ) -> None:
        """Send a message to Discord.

        Args:
            session_id: Boardroom key (discord:guild_id:channel_id)
            text: Message content
            parent_id: Optional message ID to reply to
        """
        if not self._client or not self._running:
            raise RuntimeError("Discord adapter is not running")

        # Parse session key
        parsed = parse_session_key(session_id)
        if parsed["channel"] != "discord":
            raise ValueError(f"Invalid channel for Discord adapter: {parsed['channel']}")

        channel_id = int(parsed["chat_id"])
        channel = self._client.get_channel(channel_id)

        if not channel:
            logger.error(f"Channel {channel_id} not found")
            return

        try:
            if parent_id:
                # Try to fetch parent message for threading
                try:
                    parent_msg = await channel.fetch_message(int(parent_id))
                    await parent_msg.reply(text)
                except Exception:
                    # Fallback to regular message if reply fails
                    await channel.send(text)
            else:
                await channel.send(text)
        except Exception as e:
            logger.error(f"Failed to send Discord message: {e}")
            raise

    async def fetch_history(
        self,
        session_id: str,
        *,
        limit: int = 100,
        before_id: str | None = None,
        after_id: str | None = None,
    ) -> list[Message]:
        """Fetch message history from Discord.

        Args:
            session_id: Boardroom key (discord:guild_id:channel_id)
            limit: Maximum number of messages to fetch
            before_id: Fetch messages before this message ID
            after_id: Fetch messages after this message ID

        Returns:
            List of messages, oldest first
        """
        if not self._client or not self._running:
            raise RuntimeError("Discord adapter is not running")

        try:
            import discord
        except ImportError as e:
            raise ImportError(
                "discord.py is required for Discord adapter. Install with: pip install discord.py"
            ) from e

        # Parse session key
        parsed = parse_session_key(session_id)
        if parsed["channel"] != "discord":
            raise ValueError(f"Invalid channel for Discord adapter: {parsed['channel']}")

        channel_id = int(parsed["chat_id"])
        channel = self._client.get_channel(channel_id)

        if not channel:
            raise ValueError(f"Channel {channel_id} not found")

        # Build fetch parameters
        fetch_kwargs: dict[str, Any] = {"limit": limit}

        if before_id:
            # Create a partial object for the before parameter
            fetch_kwargs["before"] = discord.Object(id=int(before_id))
        if after_id:
            fetch_kwargs["after"] = discord.Object(id=int(after_id))

        # Fetch messages from Discord
        messages: list[Message] = []
        try:
            async for discord_msg in channel.history(**fetch_kwargs):
                # Convert to Message
                msg = self._discord_message_to_message(discord_msg, session_id)
                messages.append(msg)
        except Exception as e:
            logger.error(f"Failed to fetch Discord history: {e}")
            raise

        # Discord returns newest first, we want oldest first
        messages.reverse()

        logger.info(f"Fetched {len(messages)} messages from Discord channel {channel_id}")
        return messages

    def _discord_message_to_message(self, discord_msg: Any, session_id: str) -> Message:
        """Convert a Discord message to our Message format.

        Args:
            discord_msg: discord.py Message object
            session_id: Session key

        Returns:
            Message object
        """
        # Determine role - bot messages are "agent", others are "human"
        role = "agent" if discord_msg.author.bot else "human"

        return Message(
            id=str(discord_msg.id),
            text=discord_msg.content,
            user=str(discord_msg.author.id),
            role=role,
            session_id=session_id,
            channel="discord",
            ts=discord_msg.created_at.isoformat(),
            parent_id=str(discord_msg.reference.message_id) if discord_msg.reference else None,
            target=None,
            metadata={
                "author_name": str(discord_msg.author),
                "guild_name": discord_msg.guild.name if discord_msg.guild else None,
                "channel_name": discord_msg.channel.name if hasattr(discord_msg.channel, "name") else None,
            },
        )

    async def _handle_message(self, message: Any) -> None:
        """Handle incoming Discord message.

        Args:
            message: discord.py Message object
        """
        # Ignore bot messages
        if message.author.bot:
            return

        # Ignore DMs for now (only handle guild messages)
        if not message.guild:
            return

        from nexus.message_gateway.conversation import append_message, ensure_session_metadata

        try:
            # Derive session key
            session_id = derive_session_key(
                channel="discord",
                account_id=str(message.guild.id),
                chat_id=str(message.channel.id),
            )

            # Ensure session metadata exists (human-readable info)
            ensure_session_metadata(
                nx=self._nexus_fs,
                session_id=session_id,
                metadata={
                    "channel": "discord",
                    "guild_id": str(message.guild.id),
                    "guild_name": message.guild.name,
                    "channel_id": str(message.channel.id),
                    "channel_name": message.channel.name,
                },
                context=self._context,
            )

            # Create Message using shared conversion
            msg = self._discord_message_to_message(message, session_id)

            # Append to conversation
            append_message(
                nx=self._nexus_fs,
                session_id=session_id,
                message=msg,
                context=self._context,
            )

            logger.debug(f"Discord message {message.id} stored in session {session_id}")

        except Exception as e:
            logger.error(f"Failed to handle Discord message: {e}", exc_info=True)
