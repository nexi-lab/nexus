"""Tiger Cache background updater.

Processes ReBAC changelog entries and updates affected Tiger Cache
entries incrementally via a queue-based approach.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import OperationalError

from nexus.storage.models.permissions import (
    ReBACChangelogModel as RCL,
)
from nexus.storage.models.permissions import (
    TigerCacheQueueModel as TCQ,
)
from nexus.storage.models.permissions import (
    TigerResourceMapModel as TRM,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

    from nexus.bricks.rebac.cache.tiger.bitmap_cache import TigerCache
    from nexus.bricks.rebac.manager import ReBACManager

logger = logging.getLogger(__name__)


class TigerCacheUpdater:
    """Background worker for updating Tiger Cache from changelog.

    Processes ReBAC changelog entries and updates affected cache entries
    incrementally.
    """

    def __init__(
        self,
        engine: "Engine",
        tiger_cache: "TigerCache",
        rebac_manager: "ReBACManager | None" = None,
        *,
        is_postgresql: bool = False,
    ):
        """Initialize the updater.

        Args:
            engine: SQLAlchemy database engine
            tiger_cache: Tiger Cache instance to update
            rebac_manager: ReBAC manager for permission computation
        """
        self._engine = engine
        self._tiger_cache = tiger_cache
        self._rebac_manager = rebac_manager
        self._is_postgresql = is_postgresql
        self._last_processed_revision = 0

    def set_rebac_manager(self, manager: "ReBACManager") -> None:
        """Set the ReBAC manager for permission computation."""
        self._rebac_manager = manager
        self._tiger_cache.set_rebac_manager(manager)

    def queue_update(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,
        priority: int = 100,
        conn: "Connection | None" = None,
    ) -> int:
        """Queue a cache update for background processing.

        Args:
            subject_type: Type of subject
            subject_id: ID of subject
            permission: Permission to recompute
            resource_type: Type of resource
            zone_id: Zone ID
            priority: Priority (lower = higher priority)
            conn: Optional database connection

        Returns:
            Queue entry ID
        """

        stmt = insert(TCQ).values(
            subject_type=subject_type,
            subject_id=subject_id,
            permission=permission,
            resource_type=resource_type,
            zone_id=zone_id,
            priority=priority,
            status="pending",
            created_at=datetime.now(UTC),
        )

        def execute(connection: "Connection") -> int:
            result = connection.execute(stmt)
            return result.lastrowid or 0

        if conn:
            return execute(conn)
        else:
            with self._engine.begin() as new_conn:
                return execute(new_conn)

    def reset_stuck_entries(
        self, stuck_timeout_minutes: int = 5, conn: "Connection | None" = None
    ) -> int:
        """Reset entries stuck in 'processing' state.

        If a worker crashes while processing, entries can get stuck in
        'processing' state. This method resets them to 'pending' so they
        can be retried.

        Args:
            stuck_timeout_minutes: Reset entries stuck longer than this
            conn: Optional database connection

        Returns:
            Number of entries reset
        """

        cutoff = datetime.now(UTC) - timedelta(minutes=stuck_timeout_minutes)
        stmt = (
            update(TCQ)
            .where(TCQ.status == "processing", TCQ.created_at < cutoff)
            .values(status="pending")
        )

        def execute(connection: "Connection") -> int:
            result = connection.execute(stmt)
            count = result.rowcount
            if count > 0:
                logger.info("[TIGER] Reset %d stuck queue entries to pending", count)
            return count

        if conn:
            return execute(conn)
        else:
            with self._engine.begin() as new_conn:
                return execute(new_conn)

    def process_queue(self, batch_size: int = 100, conn: "Connection | None" = None) -> int:
        """Process pending queue entries.

        Args:
            batch_size: Maximum entries to process
            conn: Optional database connection

        Returns:
            Number of entries processed
        """

        if self._rebac_manager is None:
            logger.warning("[TIGER] Cannot process queue - no ReBAC manager set")
            return 0

        # Reset any stuck entries before processing
        try:
            self.reset_stuck_entries(stuck_timeout_minutes=5)
        except (OperationalError, RuntimeError) as e:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("[TIGER] Could not reset stuck entries: %s", e)

        # Helper to check if error is a database lock/deadlock error
        def is_lock_error(e: Exception) -> bool:
            err_str = str(e).lower()
            return (
                "database is locked" in err_str
                or "deadlock" in err_str
                or (isinstance(e, OperationalError) and "lock" in err_str)
            )

        # Get pending entries
        # Use FOR UPDATE SKIP LOCKED on PostgreSQL to avoid deadlocks
        select_query = (
            select(
                TCQ.queue_id,
                TCQ.subject_type,
                TCQ.subject_id,
                TCQ.permission,
                TCQ.resource_type,
                TCQ.zone_id,
            )
            .where(TCQ.status == "pending")
            .order_by(TCQ.priority, TCQ.created_at)
            .limit(batch_size)
        )
        if self._is_postgresql:
            select_query = select_query.with_for_update(skip_locked=True)

        def do_process(connection: "Connection") -> int:
            processed = 0
            result = connection.execute(select_query)
            entries = list(result)
            logger.info("[TIGER] do_process: fetched %d entries from queue", len(entries))

            for i, entry in enumerate(entries):
                logger.info(
                    "[TIGER] Processing entry %d/%d: %s", i + 1, len(entries), entry.subject_id
                )
                try:
                    # Mark as processing
                    connection.execute(
                        update(TCQ)
                        .where(TCQ.queue_id == entry.queue_id)
                        .values(status="processing"),
                    )

                    # Compute accessible resources
                    accessible = self._compute_accessible_resources(
                        entry.subject_type,
                        entry.subject_id,
                        entry.permission,
                        entry.resource_type,
                        entry.zone_id,
                        connection,
                    )

                    # Get current revision
                    revision = self._get_current_revision(entry.zone_id, connection)

                    # Update cache
                    self._tiger_cache.update_cache(
                        entry.subject_type,
                        entry.subject_id,
                        entry.permission,
                        entry.resource_type,
                        entry.zone_id,
                        accessible,
                        revision,
                        connection,
                    )

                    # Mark as completed
                    connection.execute(
                        update(TCQ)
                        .where(TCQ.queue_id == entry.queue_id)
                        .values(status="completed", processed_at=datetime.now(UTC)),
                    )
                    processed += 1

                except (OperationalError, RuntimeError, ValueError, KeyError) as e:
                    # For database lock errors, don't try to update (it would also fail)
                    # Leave entry in 'processing' state - it will be cleaned up later
                    if is_lock_error(e):
                        logger.debug(
                            "[TIGER] Database lock during queue processing for entry %s, will retry later",
                            entry.queue_id,
                        )
                    else:
                        logger.error(
                            "[TIGER] Failed to process queue entry %s: %s", entry.queue_id, e
                        )
                        try:
                            connection.execute(
                                update(TCQ)
                                .where(TCQ.queue_id == entry.queue_id)
                                .values(
                                    status="failed",
                                    error_message=str(e)[:1000],
                                    processed_at=datetime.now(UTC),
                                ),
                            )
                        except (OperationalError, RuntimeError) as update_err:
                            # fail-safe: if status update fails, just log and continue
                            logger.debug(
                                "[TIGER] Could not update queue entry status: %s",
                                update_err,
                            )

            return processed

        try:
            if conn:
                result = do_process(conn)
                logger.info("[TIGER] Queue processing complete (external conn): %d entries", result)
                return result
            else:
                with self._engine.begin() as new_conn:
                    result = do_process(new_conn)
                # Commit happens here when 'with' block exits
                logger.info("[TIGER] Queue processing COMMITTED: %d entries processed", result)
                return result
        except (OperationalError, RuntimeError) as e:
            # Handle lock errors at the top level (e.g., during SELECT)
            if is_lock_error(e):
                logger.debug(
                    "[TIGER] Database lock during queue processing, will retry later: %s",
                    e,
                )
                return 0
            logger.error("[TIGER] Queue processing FAILED: %s", e)
            raise

    def _compute_accessible_resources(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,
        conn: "Connection",
    ) -> set[int]:
        """Compute all resources accessible by subject.

        This is the expensive operation that Tiger Cache amortizes.

        Args:
            subject_type: Type of subject
            subject_id: ID of subject
            permission: Permission to check
            resource_type: Type of resource
            zone_id: Zone ID
            conn: Database connection

        Returns:
            Set of accessible resource integer IDs
        """

        if self._rebac_manager is None:
            return set()

        # Get all resources of this type
        # Note: tiger_resource_map has no zone_id column — resource paths are globally unique.
        # Zone scoping is handled by the rebac_check call below.
        stmt = select(TRM.resource_int_id, TRM.resource_id).where(
            TRM.resource_type == resource_type,
        )

        result = conn.execute(stmt)

        accessible: set[int] = set()
        for row in result:
            # Check permission
            has_access = self._rebac_manager.rebac_check(
                subject=(subject_type, subject_id),
                permission=permission,
                object=(resource_type, row.resource_id),
                zone_id=zone_id,
            )
            if has_access:
                accessible.add(row.resource_int_id)

        return accessible

    def _get_current_revision(self, zone_id: str, conn: "Connection") -> int:
        """Get current revision from changelog."""
        stmt = select(func.coalesce(func.max(RCL.change_id), 0)).where(
            RCL.zone_id == zone_id,
        )
        result = conn.execute(stmt)
        value = result.scalar()
        return int(value) if value is not None else 0

    def cleanup_completed(
        self, older_than_hours: int = 24, conn: "Connection | None" = None
    ) -> int:
        """Clean up completed queue entries.

        Args:
            older_than_hours: Delete entries older than this
            conn: Optional database connection

        Returns:
            Number of entries deleted
        """

        cutoff = datetime.now(UTC) - timedelta(hours=older_than_hours)
        stmt = delete(TCQ).where(
            TCQ.status.in_(["completed", "failed"]),
            TCQ.processed_at < cutoff,
        )

        def execute(connection: "Connection") -> int:
            result = connection.execute(stmt)
            return result.rowcount

        if conn:
            return execute(conn)
        else:
            with self._engine.begin() as new_conn:
                return execute(new_conn)
