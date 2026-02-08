"""Conversation watcher for LangGraph integration.

Watches for new messages in conversation files and triggers agent processing.
Uses SubscriptionManager webhooks for file change notifications.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.message_gateway.conversation import read_messages
from nexus.message_gateway.types import Message

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.permissions import OperationContext
    from nexus.server.subscriptions.manager import SubscriptionManager
    from nexus.server.subscriptions.models import SubscriptionCreate

logger = logging.getLogger(__name__)

# Type alias for message handlers
MessageHandler = Callable[[Message, str], Awaitable[None]]  # (message, session_id) -> None


class ConversationWatcher:
    """Watches conversations for new human messages.

    Integrates with SubscriptionManager to receive file change events,
    then notifies registered handlers when new human messages arrive.

    Usage:
        watcher = ConversationWatcher(
            nexus_fs=nx,
            context=context,
            subscription_manager=sub_mgr,
        )

        # Register a handler
        watcher.on_human_message(my_handler)

        # Start watching
        await watcher.start()

        # Or use with FastAPI
        app.include_router(watcher.get_router())
    """

    def __init__(
        self,
        nexus_fs: NexusFS,
        context: OperationContext,
        subscription_manager: SubscriptionManager | None = None,
        *,
        zone_id: str = "default",
        webhook_url: str | None = None,
    ) -> None:
        """Initialize the watcher.

        Args:
            nexus_fs: NexusFS instance for reading messages
            context: Operation context for permissions
            subscription_manager: Optional SubscriptionManager for auto-registration
            zone_id: Zone ID for subscription
            webhook_url: URL for webhook callbacks (required if using SubscriptionManager)
        """
        self._nexus_fs = nexus_fs
        self._context = context
        self._subscription_manager = subscription_manager
        self._zone_id = zone_id
        self._webhook_url = webhook_url

        self._handlers: list[MessageHandler] = []
        self._subscription_id: str | None = None
        self._last_message_ids: dict[str, set[str]] = {}  # session_id -> seen message IDs
        self._running = False

    def on_human_message(self, handler: MessageHandler) -> None:
        """Register a handler for new human messages.

        The handler is called with (message, session_id) for each new
        human message detected.

        Args:
            handler: Async callback function
        """
        self._handlers.append(handler)
        logger.info(f"Registered message handler: {handler.__name__}")

    async def start(self) -> None:
        """Start watching for new messages.

        If SubscriptionManager is configured, creates a subscription
        for conversation file changes.
        """
        if self._running:
            return

        self._running = True

        if self._subscription_manager and self._webhook_url:
            await self._create_subscription()

        logger.info("ConversationWatcher started")

    async def stop(self) -> None:
        """Stop watching for messages."""
        self._running = False

        if self._subscription_manager and self._subscription_id:
            try:
                self._subscription_manager.delete(self._subscription_id, self._zone_id)
                logger.info(f"Deleted subscription {self._subscription_id}")
            except Exception as e:
                logger.warning(f"Failed to delete subscription: {e}")

        logger.info("ConversationWatcher stopped")

    async def _create_subscription(self) -> None:
        """Create subscription for conversation file changes."""
        from nexus.server.subscriptions.models import SubscriptionCreate

        try:
            subscription = self._subscription_manager.create(
                zone_id=self._zone_id,
                data=SubscriptionCreate(
                    url=self._webhook_url,
                    event_types=["file_write", "file_append"],
                    patterns=["/sessions/*/conversation.jsonl"],
                    name="conversation-watcher",
                    description="Watches for new messages in conversations",
                ),
                created_by="system:conversation-watcher",
            )
            self._subscription_id = subscription.id
            logger.info(f"Created subscription {subscription.id} for conversation watching")
        except Exception as e:
            logger.error(f"Failed to create subscription: {e}")
            raise

    async def handle_webhook(self, payload: dict[str, Any]) -> dict[str, str]:
        """Handle incoming webhook from SubscriptionManager.

        Args:
            payload: Webhook payload with event data

        Returns:
            Response dict
        """
        if not self._running:
            return {"status": "not_running"}

        event_type = payload.get("event")
        data = payload.get("data", {})
        file_path = data.get("file_path", "")

        logger.debug(f"Webhook received: {event_type} for {file_path}")

        # Only process conversation files
        if not file_path.endswith("/conversation.jsonl"):
            return {"status": "ignored"}

        # Extract session_id from path
        # Path format: /sessions/{session_id}/conversation.jsonl
        parts = file_path.split("/")
        if len(parts) < 3:
            return {"status": "invalid_path"}

        session_id = parts[2]  # /sessions/{session_id}/conversation.jsonl

        # Process new messages
        await self._process_new_messages(session_id)

        return {"status": "processed"}

    async def _process_new_messages(self, session_id: str) -> None:
        """Process new messages in a session.

        Reads all messages, identifies new ones, and notifies handlers
        for new human messages.

        Args:
            session_id: Session ID to process
        """
        try:
            messages = read_messages(self._nexus_fs, session_id, self._context)
        except Exception as e:
            logger.error(f"Failed to read messages for {session_id}: {e}")
            return

        # Get previously seen message IDs
        seen_ids = self._last_message_ids.get(session_id, set())

        # Find new messages
        new_messages = [m for m in messages if m.id not in seen_ids]

        if not new_messages:
            return

        # Update seen IDs
        self._last_message_ids[session_id] = {m.id for m in messages}

        # Process new human messages
        for message in new_messages:
            if message.role == "human":
                await self._notify_handlers(message, session_id)

    async def _notify_handlers(self, message: Message, session_id: str) -> None:
        """Notify all handlers of a new human message.

        Args:
            message: The new message
            session_id: Session ID
        """
        for handler in self._handlers:
            try:
                await handler(message, session_id)
            except Exception as e:
                logger.error(f"Handler {handler.__name__} failed: {e}", exc_info=True)

    def get_router(self) -> Any:
        """Get FastAPI router for webhook endpoint.

        Returns:
            FastAPI APIRouter
        """
        from fastapi import APIRouter, Request

        router = APIRouter(prefix="/webhooks", tags=["webhooks"])

        @router.post("/conversations")
        async def handle_conversation_webhook(request: Request) -> dict[str, str]:
            """Receive conversation file change webhooks."""
            payload = await request.json()
            return await self.handle_webhook(payload)

        return router


class PollingWatcher:
    """Alternative watcher that polls for new messages.

    Use this when webhooks are not available or for simpler setups.

    Usage:
        watcher = PollingWatcher(
            nexus_fs=nx,
            context=context,
            session_ids=["discord:123:456"],
            poll_interval=1.0,
        )
        watcher.on_human_message(my_handler)
        await watcher.start()
    """

    def __init__(
        self,
        nexus_fs: NexusFS,
        context: OperationContext,
        session_ids: list[str],
        *,
        poll_interval: float = 1.0,
    ) -> None:
        """Initialize the polling watcher.

        Args:
            nexus_fs: NexusFS instance
            context: Operation context
            session_ids: Session IDs to watch
            poll_interval: Seconds between polls
        """
        self._nexus_fs = nexus_fs
        self._context = context
        self._session_ids = session_ids
        self._poll_interval = poll_interval

        self._handlers: list[MessageHandler] = []
        self._last_message_ids: dict[str, set[str]] = {}
        self._running = False
        self._task: asyncio.Task | None = None

    def on_human_message(self, handler: MessageHandler) -> None:
        """Register a handler for new human messages."""
        self._handlers.append(handler)

    async def start(self) -> None:
        """Start polling for new messages."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(f"PollingWatcher started for {len(self._session_ids)} sessions")

    async def stop(self) -> None:
        """Stop polling."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("PollingWatcher stopped")

    async def _poll_loop(self) -> None:
        """Main polling loop."""
        while self._running:
            for session_id in self._session_ids:
                await self._check_session(session_id)
            await asyncio.sleep(self._poll_interval)

    async def _check_session(self, session_id: str) -> None:
        """Check a session for new messages."""
        try:
            messages = read_messages(self._nexus_fs, session_id, self._context)
        except Exception as e:
            logger.debug(f"Failed to read {session_id}: {e}")
            return

        seen_ids = self._last_message_ids.get(session_id, set())
        new_messages = [m for m in messages if m.id not in seen_ids]

        if not new_messages:
            return

        self._last_message_ids[session_id] = {m.id for m in messages}

        for message in new_messages:
            if message.role == "human":
                for handler in self._handlers:
                    try:
                        await handler(message, session_id)
                    except Exception as e:
                        logger.error(f"Handler error: {e}", exc_info=True)
