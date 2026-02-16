"""Tiger Cache background updater.

Processes ReBAC changelog entries and updates affected Tiger Cache
entries incrementally via a queue-based approach.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

    from nexus.rebac.cache.tiger.bitmap_cache import TigerCache
    from nexus.rebac.rebac_manager_enhanced import EnhancedReBACManager

logger = logging.getLogger(__name__)


class TigerCacheUpdater:
    """Background worker for updating Tiger Cache from changelog.

    Processes ReBAC changelog entries and updates affected cache entries
    incrementally.
    """

    def __init__(
        self,
        engine: Engine,
        tiger_cache: TigerCache,
        rebac_manager: EnhancedReBACManager | None = None,
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
        self._is_postgresql = "postgresql" in str(engine.url)
        self._last_processed_revision = 0

    def set_rebac_manager(self, manager: EnhancedReBACManager) -> None:
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
        conn: Connection | None = None,
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

        now_sql = "NOW()" if self._is_postgresql else "datetime('now')"
        query = text(f"""
            INSERT INTO tiger_cache_queue
                (subject_type, subject_id, permission, resource_type, zone_id, priority, status, created_at)
            VALUES
                (:subject_type, :subject_id, :permission, :resource_type, :zone_id, :priority, 'pending', {now_sql})
        """)

        params = {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "permission": permission,
            "resource_type": resource_type,
            "zone_id": zone_id,
            "priority": priority,
        }

        def execute(connection: Connection) -> int:
            result = connection.execute(query, params)
            return result.lastrowid or 0

        if conn:
            return execute(conn)
        else:
            with self._engine.begin() as new_conn:
                # Set short timeout for Tiger Cache ops - fail fast instead of blocking
                if not self._is_postgresql:
                    new_conn.execute(text("PRAGMA busy_timeout=100"))
                return execute(new_conn)

    def reset_stuck_entries(
        self, stuck_timeout_minutes: int = 5, conn: Connection | None = None
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

        if self._is_postgresql:
            query = text("""
                UPDATE tiger_cache_queue
                SET status = 'pending'
                WHERE status = 'processing'
                  AND created_at < NOW() - INTERVAL ':minutes minutes'
            """)
        else:
            query = text("""
                UPDATE tiger_cache_queue
                SET status = 'pending'
                WHERE status = 'processing'
                  AND created_at < datetime('now', '-' || :minutes || ' minutes')
            """)

        def execute(connection: Connection) -> int:
            result = connection.execute(query, {"minutes": stuck_timeout_minutes})
            count = result.rowcount
            if count > 0:
                logger.info(f"[TIGER] Reset {count} stuck queue entries to pending")
            return count

        if conn:
            return execute(conn)
        else:
            with self._engine.begin() as new_conn:
                # Set short timeout for Tiger Cache ops - fail fast instead of blocking
                if not self._is_postgresql:
                    new_conn.execute(text("PRAGMA busy_timeout=100"))
                return execute(new_conn)

    def process_queue(self, batch_size: int = 100, conn: Connection | None = None) -> int:
        """Process pending queue entries.

        Args:
            batch_size: Maximum entries to process
            conn: Optional database connection

        Returns:
            Number of entries processed
        """

        from sqlalchemy import text

        if self._rebac_manager is None:
            logger.warning("[TIGER] Cannot process queue - no ReBAC manager set")
            return 0

        # Reset any stuck entries before processing
        try:
            self.reset_stuck_entries(stuck_timeout_minutes=5)
        except Exception as e:
            logger.debug(f"[TIGER] Could not reset stuck entries: {e}")

        now_sql = "NOW()" if self._is_postgresql else "datetime('now')"

        # Helper to check if error is a database lock/deadlock error
        def is_lock_error(e: Exception) -> bool:
            err_str = str(e).lower()
            return (
                "database is locked" in err_str
                or "deadlock" in err_str
                or isinstance(e, sqlite3.OperationalError)
                or (isinstance(e, OperationalError) and "lock" in err_str)
            )

        # Get pending entries
        # Use FOR UPDATE SKIP LOCKED on PostgreSQL to avoid deadlocks
        if self._is_postgresql:
            select_query = text(f"""
                SELECT queue_id, subject_type, subject_id, permission, resource_type, zone_id
                FROM tiger_cache_queue
                WHERE status = 'pending'
                ORDER BY priority, created_at
                LIMIT {batch_size}
                FOR UPDATE SKIP LOCKED
            """)
        else:
            select_query = text(f"""
                SELECT queue_id, subject_type, subject_id, permission, resource_type, zone_id
                FROM tiger_cache_queue
                WHERE status = 'pending'
                ORDER BY priority, created_at
                LIMIT {batch_size}
            """)

        def do_process(connection: Connection) -> int:
            processed = 0
            result = connection.execute(select_query)
            entries = list(result)
            logger.info(f"[TIGER] do_process: fetched {len(entries)} entries from queue")

            for i, entry in enumerate(entries):
                logger.info(f"[TIGER] Processing entry {i + 1}/{len(entries)}: {entry.subject_id}")
                try:
                    # Mark as processing
                    connection.execute(
                        text(
                            "UPDATE tiger_cache_queue SET status = 'processing' WHERE queue_id = :qid"
                        ),
                        {"qid": entry.queue_id},
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
                        text(
                            f"UPDATE tiger_cache_queue SET status = 'completed', processed_at = {now_sql} WHERE queue_id = :qid"
                        ),
                        {"qid": entry.queue_id},
                    )
                    processed += 1

                except Exception as e:
                    # For database lock errors, don't try to update (it would also fail)
                    # Leave entry in 'processing' state - it will be cleaned up later
                    if is_lock_error(e):
                        logger.debug(
                            f"[TIGER] Database lock during queue processing for entry {entry.queue_id}, will retry later"
                        )
                    else:
                        logger.error(f"[TIGER] Failed to process queue entry {entry.queue_id}: {e}")
                        try:
                            connection.execute(
                                text(
                                    f"UPDATE tiger_cache_queue SET status = 'failed', error_message = :err, processed_at = {now_sql} WHERE queue_id = :qid"
                                ),
                                {"qid": entry.queue_id, "err": str(e)[:1000]},
                            )
                        except Exception as update_err:
                            # If we can't update the status, just log and continue
                            logger.debug(
                                f"[TIGER] Could not update queue entry status: {update_err}"
                            )

            return processed

        try:
            if conn:
                result = do_process(conn)
                logger.info(f"[TIGER] Queue processing complete (external conn): {result} entries")
                return result
            else:
                with self._engine.begin() as new_conn:
                    # Set short timeout for Tiger Cache ops - fail fast instead of blocking
                    if not self._is_postgresql:
                        new_conn.execute(text("PRAGMA busy_timeout=100"))
                    result = do_process(new_conn)
                # Commit happens here when 'with' block exits
                logger.info(f"[TIGER] Queue processing COMMITTED: {result} entries processed")
                return result
        except Exception as e:
            # Handle lock errors at the top level (e.g., during SELECT)
            if is_lock_error(e):
                logger.debug(
                    f"[TIGER] Database lock during queue processing, will retry later: {e}"
                )
                return 0
            logger.error(f"[TIGER] Queue processing FAILED: {e}")
            raise

    def _compute_accessible_resources(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,
        conn: Connection,
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

        # Get all resources of this type in zone
        # (In practice, you might want to limit this or paginate)
        resources_query = text("""
            SELECT resource_int_id, resource_id
            FROM tiger_resource_map
            WHERE resource_type = :resource_type
              AND zone_id = :zone_id
        """)

        result = conn.execute(
            resources_query,
            {"resource_type": resource_type, "zone_id": zone_id},
        )

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

    def _get_current_revision(self, zone_id: str, conn: Connection) -> int:
        """Get current revision from changelog."""

        query = text("""
            SELECT COALESCE(MAX(change_id), 0) as revision
            FROM rebac_changelog
            WHERE zone_id = :zone_id
        """)
        result = conn.execute(query, {"zone_id": zone_id})
        row = result.fetchone()
        return int(row.revision) if row else 0

    def cleanup_completed(self, older_than_hours: int = 24, conn: Connection | None = None) -> int:
        """Clean up completed queue entries.

        Args:
            older_than_hours: Delete entries older than this
            conn: Optional database connection

        Returns:
            Number of entries deleted
        """

        if self._is_postgresql:
            query = text("""
                DELETE FROM tiger_cache_queue
                WHERE status IN ('completed', 'failed')
                  AND processed_at < NOW() - INTERVAL ':hours hours'
            """)
        else:
            query = text("""
                DELETE FROM tiger_cache_queue
                WHERE status IN ('completed', 'failed')
                  AND processed_at < datetime('now', '-' || :hours || ' hours')
            """)

        def execute(connection: Connection) -> int:
            result = connection.execute(query, {"hours": older_than_hours})
            return result.rowcount

        if conn:
            return execute(conn)
        else:
            with self._engine.begin() as new_conn:
                # Set short timeout for Tiger Cache ops - fail fast instead of blocking
                if not self._is_postgresql:
                    new_conn.execute(text("PRAGMA busy_timeout=100"))
                return execute(new_conn)
