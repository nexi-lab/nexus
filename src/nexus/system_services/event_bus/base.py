"""EventBusBase — shared ABC for all event bus backends."""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from nexus.system_services.event_bus.protocol import AckableEvent
from nexus.system_services.event_bus.types import FileEvent, FileEventType

if TYPE_CHECKING:
    from nexus.contracts.auth_store_protocols import SystemSettingsStoreProtocol
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    """Get current UTC time as a naive datetime (no timezone info).

    PostgreSQL TIMESTAMP WITHOUT TIME ZONE columns don't store timezone info,
    so we need naive datetimes. This avoids the deprecated datetime.utcnow().
    """
    return datetime.now(UTC).replace(tzinfo=None)


class EventBusBase(ABC):
    """Abstract base class for event bus implementations.

    Provides common functionality and enforces the interface contract.
    Subclasses must implement all abstract methods.

    Shared logic (node ID, checkpoints, startup sync) lives here so
    every backend gets it for free.
    """

    CHECKPOINT_KEY_PREFIX = "node_sync_checkpoint"

    def __init__(
        self,
        record_store: "RecordStoreABC | None" = None,
        node_id: str | None = None,
        max_sync_events: int = 10_000,
        settings_store: "SystemSettingsStoreProtocol | None" = None,
    ) -> None:
        self._session_factory = record_store.session_factory if record_store else None
        self._settings_store = settings_store
        self._node_id = node_id or self._generate_node_id()
        self._max_sync_events = max_sync_events
        self._started = False
        self._lock = asyncio.Lock()

    @staticmethod
    def _generate_node_id() -> str:
        """Generate a unique node ID based on hostname and process."""
        import os
        import socket

        hostname = socket.gethostname()
        pid = os.getpid()
        return f"{hostname}-{pid}"

    async def start(self) -> None:
        """Start the event bus. Template method with double-checked locking."""
        if self._started:
            return
        async with self._lock:
            if self._started:
                return
            await self._do_start()
            self._started = True
            logger.info(f"{self.__class__.__name__} started")

    async def stop(self) -> None:
        """Stop the event bus. Template method with double-checked locking."""
        if not self._started:
            return
        async with self._lock:
            if not self._started:
                return
            await self._do_stop()
            self._started = False
            logger.info(f"{self.__class__.__name__} stopped")

    @abstractmethod
    async def _do_start(self) -> None:
        """Backend-specific startup logic. Subclasses implement this."""
        pass

    @abstractmethod
    async def _do_stop(self) -> None:
        """Backend-specific shutdown logic. Subclasses implement this."""
        pass

    @abstractmethod
    async def publish(self, event: FileEvent) -> int:
        """Publish an event."""
        pass

    @abstractmethod
    async def wait_for_event(
        self,
        zone_id: str,
        path_pattern: str,
        timeout: float = 30.0,
        since_version: int | None = None,
    ) -> FileEvent | None:
        """Wait for an event matching the path pattern.

        Args:
            zone_id: Zone ID to subscribe to
            path_pattern: Path pattern to match
            timeout: Maximum time to wait in seconds
            since_version: If set, skip events with version <= this value

        Returns:
            FileEvent if matched, None on timeout
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the event bus is healthy."""
        pass

    @abstractmethod
    def subscribe(self, zone_id: str) -> AsyncIterator[FileEvent]:
        """Subscribe to all events for a zone (async generator).

        Use this for background listeners like cache invalidation.

        Args:
            zone_id: Zone ID to subscribe to

        Yields:
            FileEvent objects as they are received
        """
        pass

    @abstractmethod
    def subscribe_durable(
        self,
        zone_id: str,
        consumer_name: str,
        deliver_policy: str = "all",
    ) -> AsyncIterator[AckableEvent]:
        """Subscribe with durable consumer semantics.

        Args:
            zone_id: Zone ID to subscribe to
            consumer_name: Durable consumer name (survives reconnects)
            deliver_policy: Delivery policy ("all", "last", "new")

        Yields:
            AckableEvent objects with ack/nack support
        """
        pass

    async def get_stats(self) -> dict[str, Any]:
        """Get event bus statistics. Override in subclasses for more details."""
        return {
            "backend": self.__class__.__name__,
            "status": "running" if await self.health_check() else "stopped",
        }

    # =========================================================================
    # SSOT: Checkpoint & Startup Sync (shared across backends)
    # =========================================================================

    def _get_checkpoint_key(self) -> str:
        """Get the SystemSettings key for this node's checkpoint."""
        return f"{self.CHECKPOINT_KEY_PREFIX}:{self._node_id}"

    async def _get_checkpoint(self) -> datetime | None:
        """Get the last sync checkpoint from the settings store."""
        if not self._settings_store:
            return None

        try:
            setting = self._settings_store.get_setting(self._get_checkpoint_key())
            if setting:
                return datetime.fromisoformat(setting.value)
            return None
        except Exception:
            logger.warning("Failed to read checkpoint", exc_info=True)
            return None

    async def _update_checkpoint(self, timestamp: datetime) -> None:
        """Update the sync checkpoint in the settings store."""
        if not self._settings_store:
            return

        try:
            self._settings_store.set_setting(
                self._get_checkpoint_key(),
                timestamp.isoformat(),
                description=f"Event sync checkpoint for node {self._node_id}",
            )
        except Exception:
            logger.warning("Failed to update checkpoint", exc_info=True)

    async def startup_sync(
        self,
        event_handler: Callable[[FileEvent], Awaitable[None]] | None = None,
        default_lookback_hours: int = 1,
    ) -> int:
        """Sync missed events from PG SSOT on startup.

        This method should be called after start() to reconcile any events
        that were missed while this node was down.

        Args:
            event_handler: Async callback to handle each missed event.
                          If None, events are logged but not processed.
            default_lookback_hours: If no checkpoint exists, look back this many hours.

        Returns:
            Number of events synced.
        """
        if not self._session_factory:
            logger.warning("startup_sync skipped: no session_factory configured")
            return 0

        from sqlalchemy import select

        from nexus.storage.models import OperationLogModel

        # Get last checkpoint
        checkpoint = await self._get_checkpoint()
        if checkpoint is None:
            # First run: use default lookback
            # Use naive datetime to match database (TIMESTAMP WITHOUT TIME ZONE)
            checkpoint = _utcnow_naive() - timedelta(hours=default_lookback_hours)
            logger.info("No checkpoint found, using default lookback: %dh", default_lookback_hours)
        elif checkpoint.tzinfo is not None:
            # Convert to naive if checkpoint has timezone info
            checkpoint = checkpoint.replace(tzinfo=None)

        logger.info("Starting sync from checkpoint: %s", checkpoint.isoformat())

        # Query operations since checkpoint
        with self._session_factory() as session:
            stmt = (
                select(OperationLogModel)
                .where(OperationLogModel.created_at > checkpoint)
                .where(OperationLogModel.status == "success")
                .order_by(OperationLogModel.created_at)
                .limit(self._max_sync_events)
            )
            operations = session.execute(stmt).scalars().all()

            if not operations:
                logger.info("No missed events to sync")
                await self._update_checkpoint(_utcnow_naive())
                return 0

            logger.info("Found %d missed events to sync", len(operations))

            # Convert all operations to events
            events = [
                FileEvent(
                    type=self._operation_type_to_event_type(op.operation_type),
                    path=op.path,
                    zone_id=op.zone_id,
                    timestamp=op.created_at.isoformat(),
                    old_path=op.new_path,  # new_path in operation_log is old_path for rename
                )
                for op in operations
            ]

            # Process events sequentially to preserve ordering guarantees.
            # On first failure we stop — remaining events will be retried on
            # next startup because the checkpoint only advances to the last
            # successfully processed event (Issue #2752).
            synced_count = 0
            failed_count = 0

            if event_handler:
                for i, event in enumerate(events):
                    try:
                        await event_handler(event)
                        synced_count += 1
                    except Exception as e:
                        logger.error("Failed to handle event %s: %s", event.event_id, e)
                        failed_count += 1
                        skipped = len(events) - i - 1
                        if skipped > 0:
                            logger.warning(
                                "Stopping startup sync: %d events skipped after failure",
                                skipped,
                            )
                        break
            else:
                synced_count = len(events)
                for event in events:
                    logger.debug("Synced event: %s on %s", event.type, event.path)

            # Only advance checkpoint to the last *successfully* processed
            # event.  If nothing succeeded, leave checkpoint unchanged so the
            # entire batch is retried on next startup (Issue #2752).
            if synced_count > 0:
                safe_timestamp = operations[synced_count - 1].created_at
                await self._update_checkpoint(safe_timestamp)

            if len(operations) == self._max_sync_events and failed_count == 0:
                logger.warning(
                    "Startup sync hit max_sync_events limit (%d). "
                    "More events may exist beyond this batch.",
                    self._max_sync_events,
                )

            logger.info(
                "Startup sync complete: synced=%d, failed=%d, total=%d",
                synced_count,
                failed_count,
                len(events),
            )

            return synced_count

    @staticmethod
    def _operation_type_to_event_type(op_type: str) -> FileEventType | str:
        """Convert operation_log type to FileEventType."""
        mapping = {
            "write": FileEventType.FILE_WRITE,
            "delete": FileEventType.FILE_DELETE,
            "rename": FileEventType.FILE_RENAME,
            "mkdir": FileEventType.DIR_CREATE,
            "rmdir": FileEventType.DIR_DELETE,
        }
        return mapping.get(op_type, op_type)
