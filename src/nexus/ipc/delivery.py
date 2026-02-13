"""Message sending and processing for filesystem-as-IPC.

MessageSender: writes messages to recipient inboxes with backpressure,
    permission checks (via VFS), and best-effort EventBus notification.

MessageProcessor: reads messages from an agent's inbox, invokes a handler,
    and manages the lifecycle (inbox -> processed on success, inbox ->
    dead_letter on failure). Supports EventBus push with poll fallback.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from typing import Any

from nexus.ipc.conventions import (
    dead_letter_path,
    inbox_path,
    message_path_in_dead_letter,
    message_path_in_inbox,
    message_path_in_outbox,
    message_path_in_processed,
    outbox_path,
)
from nexus.ipc.envelope import MessageEnvelope
from nexus.ipc.exceptions import (
    EnvelopeValidationError,
    InboxFullError,
    InboxNotFoundError,
)
from nexus.ipc.protocols import EventPublisher, VFSOperations

logger = logging.getLogger(__name__)

# Type alias for message handler callbacks
MessageHandler = Callable[[MessageEnvelope], Coroutine[Any, Any, None]]

# Default inbox size limit for backpressure
DEFAULT_MAX_INBOX_SIZE = 1000


class MessageSender:
    """Sends messages to agent inboxes via VFS writes.

    Each send operation:
    1. Validates the envelope
    2. Checks inbox exists (agent is provisioned)
    3. Checks backpressure (inbox size limit)
    4. Writes message to recipient's inbox
    5. Copies message to sender's outbox (audit trail)
    6. Publishes EventBus notification (best-effort)

    Args:
        vfs: VFS operations for file read/write.
        event_publisher: EventBus publisher for notifications. Optional.
        zone_id: Zone ID for multi-zone isolation.
        max_inbox_size: Maximum messages per inbox before backpressure.
    """

    def __init__(
        self,
        vfs: VFSOperations,
        event_publisher: EventPublisher | None = None,
        zone_id: str = "default",
        max_inbox_size: int = DEFAULT_MAX_INBOX_SIZE,
    ) -> None:
        self._vfs = vfs
        self._publisher = event_publisher
        self._zone_id = zone_id
        self._max_inbox_size = max_inbox_size

    async def send(self, envelope: MessageEnvelope) -> str:
        """Send a message to the recipient's inbox.

        Args:
            envelope: The message envelope to send.

        Returns:
            The full path where the message was written.

        Raises:
            InboxNotFoundError: If recipient's inbox doesn't exist.
            InboxFullError: If recipient's inbox exceeds size limit.
            EnvelopeValidationError: If envelope is invalid.
        """
        # 1. Validate envelope (Pydantic already validates on construction,
        #    but we check additional constraints here)
        self._validate_envelope(envelope)

        # 2. Check inbox exists
        recipient_inbox = inbox_path(envelope.recipient)
        if not await self._vfs.exists(recipient_inbox, self._zone_id):
            raise InboxNotFoundError(envelope.recipient)

        # 3. Check backpressure
        inbox_contents = await self._vfs.list_dir(recipient_inbox, self._zone_id)
        if len(inbox_contents) >= self._max_inbox_size:
            raise InboxFullError(envelope.recipient, len(inbox_contents), self._max_inbox_size)

        # 4. Write to recipient's inbox
        msg_path = message_path_in_inbox(envelope.recipient, envelope.id, envelope.timestamp)
        data = envelope.to_bytes()
        await self._vfs.write(msg_path, data, self._zone_id)

        # 5. Copy to sender's outbox (audit trail, best-effort)
        try:
            outbox_dir = outbox_path(envelope.sender)
            if await self._vfs.exists(outbox_dir, self._zone_id):
                outbox_msg_path = message_path_in_outbox(
                    envelope.sender, envelope.id, envelope.timestamp
                )
                await self._vfs.write(outbox_msg_path, data, self._zone_id)
        except Exception:
            logger.warning(
                "Failed to write outbox copy for message %s from %s",
                envelope.id,
                envelope.sender,
                exc_info=True,
            )

        # 6. Publish EventBus notification (best-effort)
        if self._publisher is not None:
            try:
                await self._publisher.publish(
                    channel=f"ipc.inbox.{envelope.recipient}",
                    data={
                        "event": "message_delivered",
                        "message_id": envelope.id,
                        "sender": envelope.sender,
                        "recipient": envelope.recipient,
                        "type": envelope.type.value,
                        "path": msg_path,
                    },
                )
            except Exception:
                logger.warning(
                    "EventBus notification failed for message %s "
                    "(message IS written, delivery will be picked up by poll)",
                    envelope.id,
                    exc_info=True,
                )

        logger.info(
            "Message %s sent: %s -> %s (%s)",
            envelope.id,
            envelope.sender,
            envelope.recipient,
            envelope.type.value,
        )
        return msg_path

    def _validate_envelope(self, envelope: MessageEnvelope) -> None:
        """Additional validation beyond Pydantic field validators."""
        if envelope.sender == envelope.recipient:
            raise EnvelopeValidationError("Sender and recipient must be different")


class MessageProcessor:
    """Processes messages from an agent's inbox.

    Reads messages, invokes the handler, and manages lifecycle:
    - Success: move to processed/
    - Failure: move to dead_letter/
    - Expired TTL: move to dead_letter/ without invoking handler
    - Duplicate: skip (dedup via in-memory ID set)

    Args:
        vfs: VFS operations for file read/write/rename.
        agent_id: The agent whose inbox to process.
        handler: Async callback invoked for each valid message.
        zone_id: Zone ID for multi-zone isolation.
        max_dedup_size: Maximum size of the in-memory dedup set.
    """

    def __init__(
        self,
        vfs: VFSOperations,
        agent_id: str,
        handler: MessageHandler,
        zone_id: str = "default",
        max_dedup_size: int = 10_000,
    ) -> None:
        self._vfs = vfs
        self._agent_id = agent_id
        self._handler = handler
        self._zone_id = zone_id
        self._max_dedup_size = max_dedup_size
        self._processed_ids: OrderedDict[str, None] = OrderedDict()

    async def process_inbox(self) -> int:
        """Process all messages currently in the inbox.

        Returns:
            Number of messages processed (including expired/deduped).
        """
        agent_inbox = inbox_path(self._agent_id)
        try:
            filenames = await self._vfs.list_dir(agent_inbox, self._zone_id)
        except Exception:
            logger.warning(
                "Failed to list inbox for agent %s",
                self._agent_id,
                exc_info=True,
            )
            return 0

        # Sort by filename (timestamp prefix gives chronological order)
        filenames = sorted(f for f in filenames if f.endswith(".json"))

        processed_count = 0
        for filename in filenames:
            msg_path = f"{agent_inbox}/{filename}"
            await self._process_one(msg_path)
            processed_count += 1

        return processed_count

    async def _process_one(self, msg_path: str) -> None:
        """Process a single message file.

        Reads the envelope, checks dedup and TTL, invokes handler,
        and moves to processed/ or dead_letter/.
        """
        # Read and parse envelope
        try:
            data = await self._vfs.read(msg_path, self._zone_id)
            envelope = MessageEnvelope.from_bytes(data)
        except Exception as exc:
            logger.error(
                "Failed to read/parse message at %s: %s",
                msg_path,
                exc,
            )
            # Move malformed message to dead letter
            await self._move_to_dead_letter_raw(msg_path, reason=str(exc))
            return

        # Dedup check (OrderedDict preserves insertion order for LRU eviction)
        if envelope.id in self._processed_ids:
            logger.debug(
                "Skipping duplicate message %s for agent %s",
                envelope.id,
                self._agent_id,
            )
            # Remove duplicate from inbox
            try:
                dl_path = message_path_in_dead_letter(
                    self._agent_id, envelope.id, envelope.timestamp
                )
                await self._vfs.rename(msg_path, dl_path, self._zone_id)
            except Exception:
                pass  # best-effort cleanup of duplicate
            return

        # TTL check
        if envelope.is_expired():
            logger.info(
                "Message %s expired (TTL: %ss) for agent %s",
                envelope.id,
                envelope.ttl_seconds,
                self._agent_id,
            )
            await self._move_to_dead_letter(msg_path, envelope, reason="ttl_expired")
            return

        # Invoke handler
        try:
            await self._handler(envelope)
        except Exception as exc:
            logger.error(
                "Handler failed for message %s: %s",
                envelope.id,
                exc,
                exc_info=True,
            )
            await self._move_to_dead_letter(msg_path, envelope, reason=f"handler_error: {exc}")
            return

        # Success: move to processed
        try:
            dest = message_path_in_processed(self._agent_id, envelope.id, envelope.timestamp)
            await self._vfs.rename(msg_path, dest, self._zone_id)
        except Exception:
            logger.warning(
                "Failed to move processed message %s (handler already succeeded)",
                envelope.id,
                exc_info=True,
            )

        # Track in dedup set (bounded, FIFO eviction via OrderedDict)
        self._processed_ids[envelope.id] = None
        while len(self._processed_ids) > self._max_dedup_size:
            self._processed_ids.popitem(last=False)  # evict oldest

    async def _move_to_dead_letter(
        self,
        msg_path: str,
        envelope: MessageEnvelope,
        reason: str,
    ) -> None:
        """Move a message to the dead letter directory."""
        try:
            dest = message_path_in_dead_letter(self._agent_id, envelope.id, envelope.timestamp)
            await self._vfs.rename(msg_path, dest, self._zone_id)
            logger.info(
                "Message %s moved to dead_letter for agent %s (reason: %s)",
                envelope.id,
                self._agent_id,
                reason,
            )
        except Exception:
            logger.error(
                "Failed to move message %s to dead_letter",
                envelope.id,
                exc_info=True,
            )

    async def _move_to_dead_letter_raw(self, msg_path: str, reason: str) -> None:
        """Move an unparseable message to dead letter using raw filename."""
        try:
            filename = msg_path.rsplit("/", 1)[-1]
            dest = f"{dead_letter_path(self._agent_id)}/{filename}"
            await self._vfs.rename(msg_path, dest, self._zone_id)
            logger.info(
                "Malformed message moved to dead_letter for agent %s: %s (reason: %s)",
                self._agent_id,
                filename,
                reason,
            )
        except Exception:
            logger.error(
                "Failed to move malformed message %s to dead_letter",
                msg_path,
                exc_info=True,
            )
