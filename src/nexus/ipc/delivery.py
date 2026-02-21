"""Message sending and processing for IPC.

MessageSender: writes messages to recipient inboxes with backpressure
    and best-effort EventBus notification.

MessageProcessor: reads messages from an agent's inbox, invokes a handler,
    and manages the lifecycle (inbox -> processed on success, inbox ->
    dead_letter on failure). Supports EventBus push with poll fallback.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from nexus.ipc.conventions import (
    dead_letter_path,
    inbox_path,
    message_path_in_dead_letter,
    message_path_in_inbox,
    message_path_in_outbox,
    message_path_in_processed,
    outbox_path,
)
from nexus.ipc.envelope import MessageEnvelope, MessageType
from nexus.ipc.exceptions import (
    DLQReason,
    EnvelopeValidationError,
    InboxFullError,
    InboxNotFoundError,
)
from nexus.ipc.protocols import EventPublisher, HotPathPublisher, HotPathSubscriber
from nexus.ipc.storage.protocol import IPCStorageDriver
from nexus.storage.zone_settings import SigningMode

if TYPE_CHECKING:
    from nexus.core.cache_store import CacheStoreABC
    from nexus.ipc.signing import MessageSigner, MessageVerifier

logger = logging.getLogger(__name__)


class DeliveryMode(StrEnum):
    """How messages are delivered between agents."""

    COLD_ONLY = "cold_only"  # Current behavior: filesystem only
    HOT_COLD = "hot_cold"  # NATS instant + async filesystem persistence
    HOT_ONLY = "hot_only"  # NATS only, no persistence


# Type alias for message handler callbacks
MessageHandler = Callable[[MessageEnvelope], Coroutine[Any, Any, None]]

# Default inbox size limit for backpressure
DEFAULT_MAX_INBOX_SIZE = 1000

# Default max payload size (1 MB)
DEFAULT_MAX_PAYLOAD_BYTES = 1_048_576

# Default concurrency bounds for hot/cold delivery
DEFAULT_MAX_COLD_CONCURRENCY = 100
DEFAULT_MAX_HANDLER_CONCURRENCY = 50


class MessageSender:
    """Sends messages to agent inboxes via IPCStorageDriver.

    Supports tiered delivery modes:

    - **COLD_ONLY** (default): write to filesystem synchronously.
    - **HOT_COLD**: publish via NATS for instant delivery, then persist
      to filesystem asynchronously in the background.
    - **HOT_ONLY**: publish via NATS only — no persistence.

    When the hot path is unavailable (NATS down), HOT_COLD silently
    degrades to synchronous cold-only delivery.

    Args:
        storage: Storage driver for IPC read/write operations.
        event_publisher: EventBus publisher for notifications. Optional.
        zone_id: Zone ID for multi-tenant isolation.
        max_inbox_size: Maximum messages per inbox before backpressure.
        max_payload_bytes: Maximum serialized message size.
        hot_publisher: NATS hot-path publisher. Optional.
        delivery_mode: Delivery tier. Auto-overridden to COLD_ONLY when
            hot_publisher is None.
        max_cold_concurrency: Semaphore bound for background cold writes.
    """

    def __init__(
        self,
        storage: IPCStorageDriver,
        event_publisher: EventPublisher | None = None,
        *,
        zone_id: str,
        max_inbox_size: int = DEFAULT_MAX_INBOX_SIZE,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
        hot_publisher: HotPathPublisher | None = None,
        delivery_mode: DeliveryMode = DeliveryMode.COLD_ONLY,
        max_cold_concurrency: int = DEFAULT_MAX_COLD_CONCURRENCY,
        signer: "MessageSigner | None" = None,
    ) -> None:
        self._storage = storage
        self._publisher = event_publisher
        self._zone_id = zone_id
        self._max_inbox_size = max_inbox_size
        self._max_payload_bytes = max_payload_bytes
        self._hot_publisher = hot_publisher
        # Safety: force COLD_ONLY when no hot publisher is provided
        self._mode = delivery_mode if hot_publisher is not None else DeliveryMode.COLD_ONLY
        self._cold_semaphore = asyncio.Semaphore(max_cold_concurrency)
        self._pending_tasks: set[asyncio.Task[None]] = set()
        self._signer = signer

    async def send(self, envelope: MessageEnvelope) -> str:
        """Send a message to the recipient's inbox.

        Args:
            envelope: The message envelope to send.

        Returns:
            The full path where the message was written (or a synthetic
            ``hot://`` path for HOT_ONLY mode).

        Raises:
            InboxNotFoundError: If recipient's inbox doesn't exist (cold modes).
            InboxFullError: If recipient's inbox exceeds size limit (cold modes).
            EnvelopeValidationError: If envelope is invalid.
        """
        # Sign envelope before serialization (if signer is configured)
        if self._signer is not None:
            envelope = self._signer.sign(envelope)

        data = envelope.to_bytes()
        self._validate_envelope(envelope, serialized_size=len(data))

        # --- Hot path ---
        hot_ok = False
        if self._mode in (DeliveryMode.HOT_COLD, DeliveryMode.HOT_ONLY):
            hot_ok = await self._hot_send(envelope)

        # --- Cold path ---
        msg_path: str | None = None
        if self._mode == DeliveryMode.COLD_ONLY:
            msg_path = await self._cold_send(envelope, data)
        elif self._mode == DeliveryMode.HOT_COLD:
            if hot_ok:
                # Hot succeeded — persist asynchronously in background
                self._enqueue_cold_write(envelope, data)
            else:
                # Hot failed — fall back to synchronous cold write
                msg_path = await self._cold_send(envelope, data)

        # HOT_ONLY: no filesystem write; return synthetic path
        if msg_path is None:
            msg_path = f"hot://agents.{envelope.recipient}.inbox/{envelope.id}"

        logger.info(
            "Message %s sent: %s -> %s (%s, mode=%s)",
            envelope.id,
            envelope.sender,
            envelope.recipient,
            envelope.type.value,
            self._mode.value,
        )
        return msg_path

    async def drain(self) -> None:
        """Await all pending background cold-write tasks (graceful shutdown)."""
        if self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)
            self._pending_tasks.clear()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _hot_send(self, envelope: MessageEnvelope) -> bool:
        """Publish envelope via NATS hot path. Returns True on success."""
        assert self._hot_publisher is not None  # noqa: S101
        subject = f"agents.{envelope.recipient}.inbox"
        try:
            await self._hot_publisher.publish(subject, envelope.to_hot_bytes())
            return True
        except Exception:
            logger.warning(
                "Hot-path publish failed for message %s, degrading to cold path",
                envelope.id,
                exc_info=True,
            )
            return False

    async def _cold_send(self, envelope: MessageEnvelope, data: bytes) -> str:
        """Synchronous cold path: inbox write + outbox copy + EventBus notify."""
        recipient_inbox = inbox_path(envelope.recipient)
        if not await self._storage.exists(recipient_inbox, self._zone_id):
            raise InboxNotFoundError(envelope.recipient)

        # 4. Check backpressure (count_dir is more efficient than list_dir)
        inbox_count = await self._storage.count_dir(recipient_inbox, self._zone_id)
        if inbox_count >= self._max_inbox_size:
            raise InboxFullError(envelope.recipient, inbox_count, self._max_inbox_size)

        msg_path = message_path_in_inbox(envelope.recipient, envelope.id, envelope.timestamp)
        await self._storage.write(msg_path, data, self._zone_id)

        # Outbox copy (best-effort)
        try:
            outbox_dir = outbox_path(envelope.sender)
            if await self._storage.exists(outbox_dir, self._zone_id):
                outbox_msg_path = message_path_in_outbox(
                    envelope.sender, envelope.id, envelope.timestamp
                )
                await self._storage.write(outbox_msg_path, data, self._zone_id)
        except Exception:
            logger.warning(
                "Failed to write outbox copy for message %s from %s",
                envelope.id,
                envelope.sender,
                exc_info=True,
            )

        # EventBus notification (best-effort)
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

        return msg_path

    def _enqueue_cold_write(self, envelope: MessageEnvelope, data: bytes) -> None:
        """Enqueue an async cold write bounded by the semaphore."""

        async def _bounded_cold() -> None:
            async with self._cold_semaphore:
                try:
                    await self._cold_send(envelope, data)
                except Exception:
                    logger.warning(
                        "Background cold write failed for message %s",
                        envelope.id,
                        exc_info=True,
                    )

        task = asyncio.create_task(_bounded_cold())
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    def _validate_envelope(
        self, envelope: MessageEnvelope, *, serialized_size: int | None = None
    ) -> None:
        """Additional validation beyond Pydantic field validators.

        Checks:
        - Sender and recipient must be different
        - Payload size must not exceed max_payload_bytes
        - CANCEL and RESPONSE types require correlation_id
        - Sender/recipient must not contain path separators

        Args:
            envelope: The envelope to validate.
            serialized_size: Pre-computed serialized size (avoids double serialization).
        """
        if envelope.sender == envelope.recipient:
            raise EnvelopeValidationError("Sender and recipient must be different")

        # Sender/recipient must not contain path separators (prevents path traversal)
        for field_name, value in [("sender", envelope.sender), ("recipient", envelope.recipient)]:
            if "/" in value or "\\" in value:
                raise EnvelopeValidationError(
                    f"{field_name} must not contain path separators: '{value}'"
                )

        # Payload size check
        payload_size = serialized_size if serialized_size is not None else len(envelope.to_bytes())
        if payload_size > self._max_payload_bytes:
            raise EnvelopeValidationError(
                f"Message size {payload_size} bytes exceeds limit of {self._max_payload_bytes} bytes"
            )

        # CANCEL and RESPONSE must have correlation_id
        if (
            envelope.type in (MessageType.RESPONSE, MessageType.CANCEL)
            and not envelope.correlation_id
        ):
            raise EnvelopeValidationError(
                f"Messages of type '{envelope.type.value}' require a correlation_id"
            )


class MessageProcessor:
    """Processes messages from an agent's inbox.

    Reads messages, invokes the handler, and manages lifecycle:
    - Success: move to processed/
    - Failure: move to dead_letter/
    - Expired TTL: move to dead_letter/ without invoking handler
    - Duplicate: skip (dedup via CacheStoreABC)

    Uses CacheStoreABC for TTL-based dedup tracking per KERNEL-ARCHITECTURE.md §2
    (CacheStore pillar: ephemeral KV with TTL).  When no cache_store is provided,
    a NullCacheStore is used and dedup is effectively disabled.

    Optionally listens on a NATS hot-path subject for instant delivery.
    The dedup set is shared between cold (``process_inbox``) and hot
    (``_hot_listen_loop``) paths — both run in the same event loop so
    no lock is needed.

    Args:
        storage: Storage driver for IPC read/write/rename.
        agent_id: The agent whose inbox to process.
        handler: Async callback invoked for each valid message.
        zone_id: Zone ID for multi-tenant isolation.
        cache_store: CacheStoreABC for dedup tracking (optional, degrades gracefully).
        dedup_ttl_seconds: TTL for dedup cache entries.
        hot_subscriber: NATS hot-path subscriber. Optional.
        max_handler_concurrency: Semaphore bound for concurrent handler dispatch.
    """

    def __init__(
        self,
        storage: IPCStorageDriver,
        agent_id: str,
        handler: MessageHandler,
        *,
        zone_id: str,
        cache_store: "CacheStoreABC | None" = None,
        dedup_ttl_seconds: int = 3600,
        hot_subscriber: HotPathSubscriber | None = None,
        max_handler_concurrency: int = DEFAULT_MAX_HANDLER_CONCURRENCY,
        verifier: "MessageVerifier | None" = None,
        signing_mode: SigningMode = SigningMode.OFF,
    ) -> None:
        self._storage = storage
        self._agent_id = agent_id
        self._handler = handler
        self._zone_id = zone_id
        self._cache_store = cache_store
        self._dedup_ttl = dedup_ttl_seconds
        self._hot_subscriber = hot_subscriber
        self._hot_task: asyncio.Task[None] | None = None
        self._handler_semaphore = asyncio.Semaphore(max_handler_concurrency)
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._verifier = verifier
        self._signing_mode = signing_mode

    def _dedup_key(self, msg_id: str) -> str:
        """Zone-scoped cache key for message dedup."""
        return f"ipc:dedup:{self._zone_id}:{msg_id}"

    async def _is_duplicate(self, msg_id: str) -> bool:
        """Check if a message ID has already been processed (via CacheStoreABC)."""
        if self._cache_store is None:
            return False
        return await self._cache_store.exists(self._dedup_key(msg_id))

    async def _track_processed(self, message_id: str) -> None:
        """Record message ID in dedup cache (TTL-based eviction via CacheStoreABC)."""
        if self._cache_store is not None:
            await self._cache_store.set(self._dedup_key(message_id), b"1", ttl=self._dedup_ttl)

    async def start(self) -> None:
        """Start the hot-path listener (if a subscriber is configured)."""
        if self._hot_subscriber is not None and self._hot_task is None:
            self._hot_task = asyncio.create_task(self._hot_listen_loop())
            logger.info("Hot-path listener started for agent %s", self._agent_id)

    async def stop(self) -> None:
        """Stop the hot-path listener and await pending handler tasks."""
        if self._hot_task is not None:
            self._hot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._hot_task
            self._hot_task = None
        if self._handler_tasks:
            await asyncio.gather(*self._handler_tasks, return_exceptions=True)
            self._handler_tasks.clear()
        logger.info("Hot-path listener stopped for agent %s", self._agent_id)

    async def _hot_listen_loop(self) -> None:
        """Subscribe to hot-path subject and dispatch messages."""
        assert self._hot_subscriber is not None  # noqa: S101
        subject = f"agents.{self._agent_id}.inbox"
        try:
            async for raw in self._hot_subscriber.subscribe(subject):
                try:
                    envelope = MessageEnvelope.from_bytes(raw)
                except Exception:
                    logger.warning(
                        "Failed to parse hot-path message for agent %s",
                        self._agent_id,
                        exc_info=True,
                    )
                    continue

                # Dedup (shared with process_inbox)
                if await self._is_duplicate(envelope.id):
                    logger.debug("Hot-path dedup: skipping %s", envelope.id)
                    continue

                # TTL check
                if envelope.is_expired():
                    logger.info(
                        "Hot-path message %s expired (TTL: %ss)",
                        envelope.id,
                        envelope.ttl_seconds,
                    )
                    await self._track_processed(envelope.id)
                    continue

                # Signature verification for hot path
                if not self._verify_signature_hot(envelope):
                    await self._track_processed(envelope.id)
                    continue

                # Dispatch handler with semaphore
                await self._dispatch_hot_handler(envelope)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "Hot-path listen loop crashed for agent %s",
                self._agent_id,
                exc_info=True,
            )

    async def _dispatch_hot_handler(self, envelope: MessageEnvelope) -> None:
        """Dispatch the handler for a hot-path message with concurrency control."""

        async def _run() -> None:
            async with self._handler_semaphore:
                try:
                    await self._handler(envelope)
                except Exception:
                    logger.error(
                        "Handler failed for hot-path message %s",
                        envelope.id,
                        exc_info=True,
                    )
                await self._track_processed(envelope.id)

        task = asyncio.create_task(_run())
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    async def process_inbox(self) -> int:
        """Process all messages currently in the inbox.

        Returns:
            Number of messages processed (including expired/deduped).
        """
        agent_inbox = inbox_path(self._agent_id)
        try:
            filenames = await self._storage.list_dir(agent_inbox, self._zone_id)
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
            data = await self._storage.read(msg_path, self._zone_id)
            envelope = MessageEnvelope.from_bytes(data)
        except FileNotFoundError:
            # File was already moved/processed by another processor (race condition).
            # This is expected with at-least-once semantics — skip silently.
            logger.debug(
                "Message at %s already moved (concurrent processing), skipping",
                msg_path,
            )
            return
        except Exception as exc:
            logger.error(
                "Failed to read/parse message at %s: %s",
                msg_path,
                exc,
            )
            # Move malformed message to dead letter
            await self._dead_letter(msg_path, DLQReason.PARSE_ERROR, detail=str(exc))
            return

        # Dedup check via CacheStoreABC
        if await self._is_duplicate(envelope.id):
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
                await self._storage.rename(msg_path, dl_path, self._zone_id)
            except Exception as e:
                logger.debug(
                    "Best-effort cleanup of duplicate message %s failed: %s", envelope.id, e
                )
            return

        # TTL check
        if envelope.is_expired():
            logger.info(
                "Message %s expired (TTL: %ss) for agent %s",
                envelope.id,
                envelope.ttl_seconds,
                self._agent_id,
            )
            await self._dead_letter(
                msg_path,
                DLQReason.TTL_EXPIRED,
                msg_id=envelope.id,
                timestamp=envelope.timestamp,
                detail=f"TTL {envelope.ttl_seconds}s expired",
            )
            return

        # Signature verification (when signing_mode != OFF)
        if not await self._verify_signature(msg_path, envelope):
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
            await self._dead_letter(
                msg_path,
                DLQReason.HANDLER_ERROR,
                msg_id=envelope.id,
                timestamp=envelope.timestamp,
                detail=str(exc),
            )
            return

        # Success: move to processed
        try:
            dest = message_path_in_processed(self._agent_id, envelope.id, envelope.timestamp)
            await self._storage.rename(msg_path, dest, self._zone_id)
        except Exception:
            logger.warning(
                "Failed to move processed message %s (handler already succeeded)",
                envelope.id,
                exc_info=True,
            )

        # Track in dedup cache (TTL-based eviction via CacheStoreABC)
        await self._track_processed(envelope.id)

    def _check_signature_policy(
        self, envelope: MessageEnvelope
    ) -> tuple[bool, DLQReason | None, str]:
        """Evaluate signature policy for an envelope.

        Shared logic for both hot and cold paths.

        Returns:
            (proceed, reason, detail) — proceed=True means handler should run.
            When proceed=False, reason and detail describe the rejection.
        """
        if self._signing_mode == SigningMode.OFF:
            return True, None, ""

        has_signature = envelope.signature is not None

        if not has_signature:
            if self._signing_mode == SigningMode.ENFORCE:
                return False, DLQReason.UNSIGNED_MESSAGE, "Message has no signature (enforce mode)"
            # VERIFY_ONLY: warn but allow through
            logger.warning(
                "Unsigned message %s (verify_only mode) for agent %s",
                envelope.id,
                self._agent_id,
            )
            return True, None, ""

        if self._verifier is None:
            logger.warning(
                "No verifier configured but message %s has signature; allowing through",
                envelope.id,
            )
            return True, None, ""

        result = self._verifier.verify(envelope)
        if not result.valid:
            return False, DLQReason.INVALID_SIGNATURE, result.detail

        return True, None, ""

    def _verify_signature_hot(self, envelope: MessageEnvelope) -> bool:
        """Verify message signature on the hot path (no dead-letter, just drop).

        Returns True if the message should proceed to the handler.
        """
        proceed, reason, detail = self._check_signature_policy(envelope)
        if not proceed:
            logger.warning(
                "Hot-path: message %s rejected (%s): %s",
                envelope.id,
                reason.value if reason else "unknown",
                detail,
            )
        return proceed

    async def _verify_signature(self, msg_path: str, envelope: MessageEnvelope) -> bool:
        """Verify message signature on the cold path (dead-letters on failure).

        Returns True if the message should proceed to the handler,
        False if it was dead-lettered or rejected.
        """
        proceed, reason, detail = self._check_signature_policy(envelope)
        if not proceed and reason is not None:
            logger.warning(
                "Message %s rejected for agent %s (%s): %s",
                envelope.id,
                self._agent_id,
                reason.value,
                detail,
            )
            await self._dead_letter(
                msg_path,
                reason,
                msg_id=envelope.id,
                timestamp=envelope.timestamp,
                detail=detail,
            )
        return proceed

    async def _dead_letter(
        self,
        msg_path: str,
        reason: DLQReason,
        *,
        msg_id: str | None = None,
        timestamp: datetime | None = None,
        detail: str = "",
    ) -> None:
        """Move a message to the dead-letter directory with a structured reason.

        If *msg_id* and *timestamp* are provided (parsed envelope), the
        destination is built from envelope fields.  Otherwise falls back
        to extracting the filename from *msg_path* (raw/unparseable messages).

        A ``.reason.json`` sidecar is written alongside the dead-lettered
        message for programmatic triage.
        """
        try:
            if msg_id is not None and timestamp is not None:
                dest = message_path_in_dead_letter(self._agent_id, msg_id, timestamp)
            else:
                filename = msg_path.rsplit("/", 1)[-1]
                dest = f"{dead_letter_path(self._agent_id)}/{filename}"

            await self._storage.rename(msg_path, dest, self._zone_id)

            # Write structured .reason.json sidecar (best-effort)
            try:
                reason_data = json.dumps(
                    {
                        "reason": reason.value,
                        "detail": detail,
                        "agent_id": self._agent_id,
                        "zone_id": self._zone_id,
                        "msg_id": msg_id,
                    },
                    indent=2,
                ).encode("utf-8")
                reason_path = dest + ".reason.json"
                await self._storage.write(reason_path, reason_data, self._zone_id)
            except Exception:
                logger.debug(
                    "Failed to write .reason.json for dead-lettered message at %s",
                    dest,
                    exc_info=True,
                )

            logger.info(
                "Message %s moved to dead_letter for agent %s (reason: %s, detail: %s)",
                msg_id or msg_path,
                self._agent_id,
                reason.value,
                detail,
            )
        except Exception:
            logger.error(
                "Failed to move message %s to dead_letter",
                msg_id or msg_path,
                exc_info=True,
            )
