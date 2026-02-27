"""EventBusBase — shared ABC for all event bus backends."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from nexus.core.file_events import FileEvent, FileEventType
from nexus.services.event_bus.protocol import AckableEvent

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
        session_factory: Any | None = None,
        node_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._node_id = node_id or self._generate_node_id()

    @staticmethod
    def _generate_node_id() -> str:
        """Generate a unique node ID based on hostname and process."""
        import os
        import socket

        hostname = socket.gethostname()
        pid = os.getpid()
        return f"{hostname}-{pid}"

    @abstractmethod
    async def start(self) -> None:
        """Start the event bus."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the event bus."""
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
        since_revision: int | None = None,
    ) -> FileEvent | None:
        """Wait for an event matching the path pattern.

        Args:
            zone_id: Zone ID to subscribe to
            path_pattern: Path pattern to match
            timeout: Maximum time to wait in seconds
            since_revision: Only return events with revision > this value (Issue #1187)

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
        """Get the last sync checkpoint from the database.

        Runs synchronous SQLAlchemy in a thread executor to avoid blocking
        the event loop. Handles missing system_settings table gracefully
        (e.g. SQLite embedded mode).
        """
        if not self._session_factory:
            return None

        session_factory = self._session_factory  # bind for mypy narrowing

        def _query() -> datetime | None:
            from sqlalchemy import select
            from sqlalchemy.exc import OperationalError, ProgrammingError

            from nexus.storage.models import SystemSettingsModel

            try:
                with session_factory() as session:
                    stmt = select(SystemSettingsModel).where(
                        SystemSettingsModel.key == self._get_checkpoint_key()
                    )
                    setting = session.execute(stmt).scalar_one_or_none()

                    if setting:
                        return datetime.fromisoformat(setting.value)
                    return None
            except (OperationalError, ProgrammingError):
                # Table may not exist in SQLite embedded mode
                return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _query)

    async def _update_checkpoint(self, timestamp: datetime) -> None:
        """Update the sync checkpoint in the database.

        Runs synchronous SQLAlchemy in a thread executor to avoid blocking
        the event loop.
        """
        if not self._session_factory:
            return

        session_factory = self._session_factory  # bind for mypy narrowing

        def _update() -> None:
            from sqlalchemy import select

            from nexus.storage.models import SystemSettingsModel

            with session_factory() as session:
                key = self._get_checkpoint_key()
                stmt = select(SystemSettingsModel).where(SystemSettingsModel.key == key)
                setting = session.execute(stmt).scalar_one_or_none()

                if setting:
                    setting.value = timestamp.isoformat()
                else:
                    setting = SystemSettingsModel(
                        key=key,
                        value=timestamp.isoformat(),
                        description=f"Event sync checkpoint for node {self._node_id}",
                    )
                    session.add(setting)

                session.commit()

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _update)

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
            logger.info(f"No checkpoint found, using default lookback: {default_lookback_hours}h")
        elif checkpoint.tzinfo is not None:
            # Convert to naive if checkpoint has timezone info
            checkpoint = checkpoint.replace(tzinfo=None)

        logger.info(f"Starting sync from checkpoint: {checkpoint.isoformat()}")

        # Query operations since checkpoint
        with self._session_factory() as session:
            stmt = (
                select(OperationLogModel)
                .where(OperationLogModel.created_at > checkpoint)
                .where(OperationLogModel.status == "success")
                .order_by(OperationLogModel.created_at)
            )
            operations = session.execute(stmt).scalars().all()

            if not operations:
                logger.info("No missed events to sync")
                await self._update_checkpoint(_utcnow_naive())
                return 0

            logger.info(f"Found {len(operations)} missed events to sync")

            # Process each operation
            synced_count = 0
            latest_timestamp = checkpoint

            for op in operations:
                # Convert OperationLogModel to FileEvent
                event = FileEvent(
                    type=self._operation_type_to_event_type(op.operation_type),
                    path=op.path,
                    zone_id=op.zone_id,
                    timestamp=op.created_at.isoformat(),
                    old_path=op.new_path,  # new_path in operation_log is old_path for rename
                )

                if event_handler:
                    try:
                        await event_handler(event)
                        synced_count += 1
                    except Exception as e:
                        logger.error(f"Failed to handle event {event.event_id}: {e}")
                else:
                    logger.debug(f"Synced event: {event.type} on {event.path}")
                    synced_count += 1

                if op.created_at > latest_timestamp:
                    latest_timestamp = op.created_at

            # Update checkpoint
            await self._update_checkpoint(latest_timestamp)
            logger.info(f"Startup sync complete: {synced_count} events processed")

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
