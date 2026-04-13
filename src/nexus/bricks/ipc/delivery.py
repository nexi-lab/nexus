"""Message sending and processing for IPC.

MessageSender: writes messages to recipient inboxes with backpressure,
    best-effort EventBus notification, and DT_PIPE wakeup (Issue #3197).

MessageProcessor: reads messages from an agent's inbox, invokes a handler,
    and manages the lifecycle (inbox -> processed on success, inbox ->
    dead_letter on failure). Supports DT_PIPE wakeup + EventBus push with
    poll fallback. Listeners auto-reconnect with exponential backoff.
"""

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from nexus.bricks.ipc.conventions import (
    inbox_path,
    message_path_in_dead_letter,
    message_path_in_inbox,
    message_path_in_outbox,
    message_path_in_processed,
    outbox_path,
)
from nexus.bricks.ipc.envelope import MessageEnvelope, MessageType
from nexus.bricks.ipc.exceptions import (
    DLQReason,
    EnvelopeValidationError,
    InboxFullError,
    InboxNotFoundError,
    NonRetryableError,
)
from nexus.bricks.ipc.lifecycle import dead_letter_message
from nexus.bricks.ipc.protocols import (
    EventPublisher,
    EventSubscriber,
)
from nexus.storage.zone_settings import SigningMode

if TYPE_CHECKING:
    from nexus.bricks.ipc.signing import MessageSigner, MessageVerifier
    from nexus.contracts.cache_store import CacheStoreABC

logger = logging.getLogger(__name__)

# Type alias for message handler callbacks
MessageHandler = Callable[[MessageEnvelope], Coroutine[Any, Any, None]]

# Default inbox size limit for backpressure
DEFAULT_MAX_INBOX_SIZE = 1000

# Default max payload size (1 MB)
DEFAULT_MAX_PAYLOAD_BYTES = 1_048_576

# Default concurrency bound for handler dispatch
DEFAULT_MAX_HANDLER_CONCURRENCY = 50

# Maximum consecutive listener failures before stopping reconnection
_MAX_LISTENER_RETRIES = 5

# Minutes before an orphaned .proc_ claim file is considered stale and recovered.
# MUST be set to at least 2× the maximum handler execution time for your deployment.
# The default of 1440 minutes (24 hours) is conservative — set it lower only when
# you are certain no handler runs longer than half this window.
# WARNING: Setting this too low allows concurrent replay of non-idempotent handlers.
PROC_CLAIM_STALE_MINUTES: int = 1440  # 24 hours


class MessageSender:
    """Sends messages to agent inboxes via NexusFS.

    Writes messages to the recipient's inbox directory, copies to the
    sender's outbox, and fires notifications (best-effort):
      1. EventBus notification (ms, cross-node capable)
      2. TTL schedule event via CacheStore pub/sub (for event-driven sweeping)

    Args:
        vfs: NexusFS instance for IPC read/write operations.
        event_publisher: EventBus publisher for cross-node notifications. Optional.
        zone_id: Zone ID for multi-tenant isolation.
        max_inbox_size: Maximum messages per inbox before backpressure.
        max_payload_bytes: Maximum serialized message size.
        signer: MessageSigner for envelope signing. Optional.
        cache_store: CacheStore for TTL schedule pub/sub. Optional.
    """

    def __init__(
        self,
        vfs: Any,
        event_publisher: EventPublisher | None = None,
        *,
        zone_id: str,
        max_inbox_size: int = DEFAULT_MAX_INBOX_SIZE,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
        signer: "MessageSigner | None" = None,
        cache_store: "CacheStoreABC | None" = None,
    ) -> None:
        self._vfs = vfs
        self._publisher = event_publisher
        self._zone_id = zone_id
        self._max_inbox_size = max_inbox_size
        self._max_payload_bytes = max_payload_bytes
        self._signer = signer
        self._cache_store = cache_store

    def _ctx(self) -> Any:
        from nexus.contracts.types import OperationContext

        return OperationContext(user_id="system", groups=[], zone_id=self._zone_id, is_system=True)

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
        # Sign envelope before serialization (if signer is configured)
        if self._signer is not None:
            envelope = self._signer.sign(envelope)

        data = envelope.to_bytes()
        self._validate_envelope(envelope, serialized_size=len(data))

        msg_path = await self._send_to_inbox(envelope, data)

        logger.info(
            "Message %s sent: %s -> %s (%s)",
            envelope.id,
            envelope.sender,
            envelope.recipient,
            envelope.type.value,
        )
        return msg_path

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _send_to_inbox(self, envelope: MessageEnvelope, data: bytes) -> str:
        """Write message to inbox, copy to outbox, and notify via DT_PIPE + EventBus."""
        recipient_inbox = inbox_path(envelope.recipient)
        if not self._vfs.access(recipient_inbox, context=self._ctx()):
            raise InboxNotFoundError(envelope.recipient)

        # Check backpressure
        inbox_count = len(
            self._vfs.sys_readdir(recipient_inbox, recursive=False, context=self._ctx())
        )
        if inbox_count >= self._max_inbox_size:
            raise InboxFullError(envelope.recipient, inbox_count, self._max_inbox_size)

        msg_path = message_path_in_inbox(envelope.recipient, envelope.id, envelope.timestamp)
        self._vfs.write(msg_path, data, context=self._ctx())

        # Outbox copy (best-effort)
        outbox_dir = outbox_path(envelope.sender)
        try:
            if self._vfs.access(outbox_dir, context=self._ctx()):
                outbox_msg_path = message_path_in_outbox(
                    envelope.sender, envelope.id, envelope.timestamp
                )
                self._vfs.write(outbox_msg_path, data, context=self._ctx())
        except Exception as exc:
            logger.warning(
                "Failed to write outbox copy",
                extra={
                    "message_id": envelope.id,
                    "sender": envelope.sender,
                    "recipient": envelope.recipient,
                    "zone_id": self._zone_id,
                    "outbox_dir": outbox_dir,
                    "storage_backend": type(self._vfs).__name__,
                    "error_type": type(exc).__name__,
                    "error_detail": str(exc),
                },
                exc_info=True,
            )

        # EventBus notification (best-effort, cross-node capable)
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
            except Exception as exc:
                logger.warning(
                    "EventBus notification failed (message IS written, delivery will be picked up by poll)",
                    extra={
                        "message_id": envelope.id,
                        "sender": envelope.sender,
                        "recipient": envelope.recipient,
                        "zone_id": self._zone_id,
                        "channel": f"ipc.inbox.{envelope.recipient}",
                        "message_path": msg_path,
                        "publisher_type": type(self._publisher).__name__
                        if self._publisher
                        else None,
                        "error_type": type(exc).__name__,
                        "error_detail": str(exc),
                    },
                    exc_info=True,
                )

        # TTL schedule event via CacheStore pub/sub (best-effort)
        if envelope.ttl_seconds is not None and self._cache_store is not None:
            try:
                expires_at = envelope.timestamp.timestamp() + envelope.ttl_seconds
                await self._cache_store.publish(
                    f"ipc:ttl:schedule:{self._zone_id}",
                    json.dumps(
                        {
                            "agent_id": envelope.recipient,
                            "msg_id": envelope.id,
                            "expires_at": expires_at,
                        }
                    ).encode(),
                )
            except Exception:
                logger.debug(
                    "TTL schedule publish failed for message %s (best-effort)",
                    envelope.id,
                )

        return msg_path

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

        # Note: Agent ID validation (path separators, format) is done in
        # MessageEnvelope field validators via validate_agent_id()

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

    Uses CacheStoreABC for TTL-based dedup tracking per KERNEL-ARCHITECTURE.md S2
    (CacheStore pillar: ephemeral KV with TTL).  When no cache_store is provided,
    a NullCacheStore is used and dedup is effectively disabled.

    Supports EventBus push notifications via EventSubscriber for ms-latency
    cross-node delivery (poll fallback always works).  The listener
    auto-reconnects with exponential backoff (up to ``_MAX_LISTENER_RETRIES``
    consecutive failures before stopping).

    Args:
        vfs: NexusFS instance for IPC read/write/rename.
        agent_id: The agent whose inbox to process.
        handler: Async callback invoked for each valid message.
        zone_id: Zone ID for multi-tenant isolation.
        cache_store: CacheStoreABC for dedup tracking (optional, degrades gracefully).
        dedup_ttl_seconds: TTL for dedup cache entries.
        verifier: MessageVerifier for signature verification. Optional.
        signing_mode: Signing enforcement mode.
        max_retries: Maximum handler retry attempts.
        retry_delays: Backoff delays between retries.
        event_subscriber: EventBus subscriber for push notifications. Optional.
    """

    def __init__(
        self,
        vfs: Any,
        agent_id: str,
        handler: MessageHandler,
        *,
        zone_id: str,
        cache_store: "CacheStoreABC | None" = None,
        dedup_ttl_seconds: int = 3600,
        verifier: "MessageVerifier | None" = None,
        signing_mode: SigningMode = SigningMode.OFF,
        max_retries: int = 3,
        retry_delays: tuple[float, ...] = (1.0, 2.0, 4.0),
        event_subscriber: EventSubscriber | None = None,
    ) -> None:
        self._vfs = vfs
        self._agent_id = agent_id
        self._handler = handler
        self._zone_id = zone_id
        self._cache_store = cache_store
        self._dedup_ttl = dedup_ttl_seconds
        self._event_subscriber = event_subscriber
        self._event_task: asyncio.Task[None] | None = None
        self._verifier = verifier
        self._signing_mode = signing_mode
        self._max_retries = max_retries
        self._retry_delays = retry_delays

    def _ctx(self) -> Any:
        from nexus.contracts.types import OperationContext

        return OperationContext(user_id="system", groups=[], zone_id=self._zone_id, is_system=True)

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
        """Start notification listeners (EventBus push notifications)."""
        if self._event_subscriber is not None and self._event_task is None:
            self._event_task = asyncio.create_task(self._event_listen_loop())
            logger.info("EventBus listener started for agent %s", self._agent_id)

    async def stop(self) -> None:
        """Stop all notification listeners."""
        if self._event_task is not None:
            self._event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._event_task
            self._event_task = None

        logger.info("Listeners stopped for agent %s", self._agent_id)

    async def _event_listen_loop(self) -> None:
        """Subscribe to EventBus for push notifications.

        Listens for "message_delivered" events on the agent's inbox channel
        and triggers process_inbox() when messages arrive.

        Auto-reconnects with exponential backoff on failure.
        """
        if self._event_subscriber is None:
            return
        channel = f"ipc.inbox.{self._agent_id}"
        consecutive_failures = 0
        while True:
            try:
                async for _event in self._event_subscriber.subscribe(channel):
                    consecutive_failures = 0
                    try:
                        await self.process_inbox()
                    except Exception:
                        logger.warning(
                            "EventBus-triggered inbox processing failed for agent %s",
                            self._agent_id,
                            exc_info=True,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive_failures += 1
                if consecutive_failures >= _MAX_LISTENER_RETRIES:
                    logger.error(
                        "EventBus listener failed %d times for agent %s, stopping",
                        _MAX_LISTENER_RETRIES,
                        self._agent_id,
                    )
                    break
                delay = min(2**consecutive_failures, 30)
                logger.warning(
                    "EventBus listener error for agent %s (attempt %d/%d), retrying in %ds",
                    self._agent_id,
                    consecutive_failures,
                    _MAX_LISTENER_RETRIES,
                    delay,
                    exc_info=True,
                )
                await asyncio.sleep(delay)

    async def process_inbox(self) -> int:
        """Process all messages currently in the inbox.

        May be triggered by multiple sources (polling, DT_PIPE, EventBus, POST_WRITE hook).
        Concurrent calls are safe: per-message dedup via CacheStore prevents
        double-processing, and asyncio single-thread model prevents true data races.

        Returns:
            Number of messages processed (including expired/deduped).
        """
        agent_inbox = inbox_path(self._agent_id)
        try:
            filenames = self._vfs.sys_readdir(agent_inbox, recursive=False, context=self._ctx())
        except Exception:
            logger.warning(
                "Failed to list inbox for agent %s",
                self._agent_id,
                exc_info=True,
            )
            return 0

        # Recover orphaned .proc_{claim_ts}_{id} files from previously crashed processors.
        # A .proc_ file means the processor claimed the message before handler execution
        # but crashed before completing. We only recover files whose embedded claim_ts is
        # older than _PROC_CLAIM_STALE_MINUTES — active handlers are left alone.
        # Dedup cache prevents double-execution when the handler already succeeded.
        stale_cutoff = datetime.now(UTC) - timedelta(minutes=PROC_CLAIM_STALE_MINUTES)
        all_filenames = sorted(filenames)
        for fn in all_filenames:
            if ".json.proc_" not in fn:
                continue
            proc_path = f"{agent_inbox}/{fn}"
            # Filename: "{orig_prefix}.json.proc_{claim_ts}_{proc_id}"
            # Extract orig filename and claim_ts
            try:
                pre, proc_suffix = fn.split(".json.proc_", 1)
                orig_fn = pre + ".json"
                claim_ts_str = proc_suffix.split("_", 1)[0]
                claim_dt = datetime.strptime(claim_ts_str, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
            except (ValueError, IndexError):
                continue  # unparseable — leave it alone
            if claim_dt >= stale_cutoff:
                continue  # recently claimed — active handler, do not disturb
            orig_path = f"{agent_inbox}/{orig_fn}"
            if not self._vfs.access(orig_path, context=self._ctx()):
                try:
                    self._vfs.sys_rename(proc_path, orig_path, context=self._ctx())
                    logger.info("Recovered stale .proc claim: %s → %s", fn, orig_fn)
                except Exception:
                    pass

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
            data = self._vfs.sys_read(msg_path, context=self._ctx())
            envelope = MessageEnvelope.from_bytes(data)
        except FileNotFoundError:
            # File was already moved/processed by another processor (race condition).
            # This is expected with at-least-once semantics -- skip silently.
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
                self._vfs.sys_rename(msg_path, dl_path, context=self._ctx())
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

        # Claim the message before handler execution.
        # Rename to "{msg_path}.proc_{claim_ts}_{proc_id}" — the embedded claim_ts
        # lets process_inbox() distinguish active handlers from stale orphans
        # (only recover files older than _PROC_CLAIM_STALE_MINUTES).
        # The drain filters for .json files; .proc_ files are invisible to it.
        claim_ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        proc_id = uuid.uuid4().hex[:4]
        proc_path = f"{msg_path}.proc_{claim_ts}_{proc_id}"
        try:
            self._vfs.sys_rename(msg_path, proc_path, context=self._ctx())
            msg_path = proc_path  # use claimed path for all subsequent ops
        except FileNotFoundError:
            # Drain or concurrent processor already claimed/moved it — skip.
            logger.debug(
                "Message at %s already claimed (drain or concurrent processor), skipping",
                msg_path,
            )
            return
        except Exception as claim_exc:
            # Non-fatal: proceed with original path, accepting the unlikely race.
            logger.debug("Could not claim %s for processing, continuing: %s", msg_path, claim_exc)

        # Invoke handler with exponential backoff retry.
        # NonRetryableError skips retry and dead-letters immediately.
        for attempt in range(self._max_retries + 1):
            try:
                await self._handler(envelope)
                break  # Success - exit retry loop
            except NonRetryableError as exc:
                # Handler explicitly signals no retry (e.g. invalid payload,
                # permission denied). Dead-letter immediately.
                logger.error(
                    "Handler raised NonRetryableError for message %s: %s",
                    envelope.id,
                    exc,
                    exc_info=True,
                )
                await self._dead_letter(
                    msg_path,
                    DLQReason.HANDLER_ERROR,
                    msg_id=envelope.id,
                    timestamp=envelope.timestamp,
                    detail=f"Non-retryable: {exc}",
                )
                return
            except Exception as exc:
                if attempt < self._max_retries:
                    # Retry with exponential backoff
                    delay = self._retry_delays[min(attempt, len(self._retry_delays) - 1)]
                    logger.warning(
                        "Handler failed for message %s (attempt %d/%d), retrying in %.1fs: %s",
                        envelope.id,
                        attempt + 1,
                        self._max_retries + 1,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                else:
                    # Final attempt failed - dead letter
                    logger.error(
                        "Handler failed for message %s after %d attempts: %s",
                        envelope.id,
                        self._max_retries + 1,
                        exc,
                        exc_info=True,
                    )
                    await self._dead_letter(
                        msg_path,
                        DLQReason.HANDLER_ERROR,
                        msg_id=envelope.id,
                        timestamp=envelope.timestamp,
                        detail=f"Failed after {self._max_retries + 1} attempts: {exc}",
                    )
                    return

        # Success: move to processed
        try:
            dest = message_path_in_processed(self._agent_id, envelope.id, envelope.timestamp)
            self._vfs.sys_rename(msg_path, dest, context=self._ctx())
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

        Returns:
            (proceed, reason, detail) -- proceed=True means handler should run.
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

    async def _verify_signature(self, msg_path: str, envelope: MessageEnvelope) -> bool:
        """Verify message signature (dead-letters on failure).

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
        """Move a message to dead_letter/ with a structured reason sidecar.

        Delegates to the shared ``lifecycle.dead_letter_message()`` helper
        for consistent behavior with TTLSweeper (Issue #3197, DRY).
        """
        await dead_letter_message(
            self._vfs,
            msg_path,
            self._agent_id,
            self._zone_id,
            reason,
            msg_id=msg_id,
            timestamp=timestamp,
            detail=detail,
        )
