"""Sync Backlog Store for bidirectional sync write-back (Issue #1129).

Manages the sync_backlog table: enqueue, fetch, status transitions, and expiry.
Inherits shared session/dialect logic from SyncStoreBase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from nexus.storage.sync_store_base import SyncStoreBase

if TYPE_CHECKING:
    from nexus.services.gateway import NexusFSGateway
    from nexus.storage.models import SyncBacklogModel

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncBacklogEntry:
    """Immutable representation of a sync backlog row."""

    id: str
    path: str
    backend_name: str
    zone_id: str
    operation_type: str  # write, delete, mkdir, rename
    content_hash: str | None
    new_path: str | None
    status: str
    retry_count: int
    max_retries: int
    created_at: datetime
    updated_at: datetime
    last_attempted_at: datetime | None
    error_message: str | None


class SyncBacklogStore(SyncStoreBase):
    """Store for sync backlog CRUD operations (Issue #1129).

    Manages pending write-back operations from Nexus to source backends.
    Supports upsert coalescing, FIFO fetch, status transitions, and TTL expiry.
    """

    def __init__(self, gateway: NexusFSGateway) -> None:
        super().__init__(gateway)

    def enqueue(
        self,
        path: str,
        backend_name: str,
        zone_id: str = "default",
        operation_type: str = "write",
        content_hash: str | None = None,
        new_path: str | None = None,
    ) -> bool:
        """Enqueue or coalesce a write-back operation.

        If a pending entry for the same (path, backend, zone) exists,
        updates it (coalescing). Otherwise creates a new entry.

        Args:
            path: Virtual file path
            backend_name: Backend identifier
            zone_id: Zone ID
            operation_type: Operation type (write, delete, mkdir, rename)
            content_hash: CAS hash for write operations
            new_path: Target path for rename operations

        Returns:
            True if successful, False otherwise
        """
        from nexus.storage.models import SyncBacklogModel

        session = self._get_session()
        if session is None:
            return False

        try:
            now = datetime.now(UTC)
            values = {
                "path": path,
                "backend_name": backend_name,
                "zone_id": zone_id,
                "operation_type": operation_type,
                "content_hash": content_hash,
                "new_path": new_path,
                "status": "pending",
                "retry_count": 0,
                "max_retries": 5,
                "created_at": now,
                "updated_at": now,
            }
            update_set = {
                "operation_type": operation_type,
                "content_hash": content_hash,
                "new_path": new_path,
                "updated_at": now,
            }

            self._dialect_upsert(
                session,
                SyncBacklogModel,
                values,
                pg_constraint="uq_sync_backlog_pending",
                sqlite_index_elements=["path", "backend_name", "zone_id", "status"],
                update_set=update_set,
            )
            session.commit()
            return True
        except Exception as e:
            logger.warning(f"Failed to enqueue backlog for {path}: {e}")
            session.rollback()
            return False
        finally:
            session.close()

    def fetch_pending(
        self,
        backend_name: str,
        zone_id: str = "default",
        limit: int = 100,
    ) -> list[SyncBacklogEntry]:
        """Fetch pending entries for a backend, FIFO ordered.

        Args:
            backend_name: Backend identifier
            zone_id: Zone ID
            limit: Maximum entries to return

        Returns:
            List of pending SyncBacklogEntry objects
        """
        from nexus.storage.models import SyncBacklogModel

        session = self._get_session()
        if session is None:
            return []

        try:
            rows = (
                session.query(SyncBacklogModel)
                .filter(
                    SyncBacklogModel.backend_name == backend_name,
                    SyncBacklogModel.zone_id == zone_id,
                    SyncBacklogModel.status == "pending",
                )
                .order_by(SyncBacklogModel.created_at)
                .limit(limit)
                .all()
            )
            return [self._to_entry(row) for row in rows]
        except Exception as e:
            logger.warning(f"Failed to fetch pending backlog for {backend_name}: {e}")
            return []
        finally:
            session.close()

    def mark_in_progress(self, entry_id: str) -> bool:
        """Transition entry from pending to in_progress.

        Args:
            entry_id: Backlog entry ID

        Returns:
            True if transition succeeded
        """
        return self._update_status(entry_id, "in_progress", from_status="pending")

    def mark_completed(self, entry_id: str) -> bool:
        """Transition entry from in_progress to completed.

        Args:
            entry_id: Backlog entry ID

        Returns:
            True if transition succeeded
        """
        return self._update_status(entry_id, "completed", from_status="in_progress")

    def mark_failed(self, entry_id: str, error_message: str) -> bool:
        """Increment retry count and mark as failed if max retries exceeded.

        Args:
            entry_id: Backlog entry ID
            error_message: Error description

        Returns:
            True if update succeeded
        """
        from nexus.storage.models import SyncBacklogModel

        session = self._get_session()
        if session is None:
            return False

        try:
            row = session.query(SyncBacklogModel).filter_by(id=entry_id).first()
            if row is None:
                return False

            now = datetime.now(UTC)
            new_retry = row.retry_count + 1
            new_status = "failed" if new_retry >= row.max_retries else "pending"

            row.retry_count = new_retry
            row.status = new_status
            row.error_message = error_message
            row.last_attempted_at = now
            row.updated_at = now
            session.commit()
            return True
        except Exception as e:
            logger.warning(f"Failed to mark backlog {entry_id} as failed: {e}")
            session.rollback()
            return False
        finally:
            session.close()

    def expire_stale(self, ttl_seconds: int = 86400, max_entries: int = 10000) -> int:
        """Expire stale entries by TTL and cap total pending count.

        Args:
            ttl_seconds: Max age in seconds for pending entries
            max_entries: Max pending entries before oldest get expired

        Returns:
            Number of entries expired
        """
        from nexus.storage.models import SyncBacklogModel

        session = self._get_session()
        if session is None:
            return 0

        try:
            now = datetime.now(UTC)
            cutoff = datetime.fromtimestamp(now.timestamp() - ttl_seconds, tz=UTC)

            # Phase 1: TTL expiry
            ttl_expired = (
                session.query(SyncBacklogModel)
                .filter(
                    SyncBacklogModel.status == "pending",
                    SyncBacklogModel.created_at < cutoff,
                )
                .update(
                    {"status": "expired", "updated_at": now},
                    synchronize_session="fetch",
                )
            )

            # Phase 2: Cap-based expiry (oldest first)
            pending_count = (
                session.query(SyncBacklogModel).filter(SyncBacklogModel.status == "pending").count()
            )
            cap_expired = 0
            if pending_count > max_entries:
                overflow = pending_count - max_entries
                oldest_ids = (
                    session.query(SyncBacklogModel.id)
                    .filter(SyncBacklogModel.status == "pending")
                    .order_by(SyncBacklogModel.created_at)
                    .limit(overflow)
                    .all()
                )
                if oldest_ids:
                    ids = [row[0] for row in oldest_ids]
                    cap_expired = (
                        session.query(SyncBacklogModel)
                        .filter(SyncBacklogModel.id.in_(ids))
                        .update(
                            {"status": "expired", "updated_at": now},
                            synchronize_session="fetch",
                        )
                    )

            session.commit()
            total: int = ttl_expired + cap_expired
            if total > 0:
                logger.info(
                    f"[SYNC_BACKLOG] Expired {total} entries (ttl={ttl_expired}, cap={cap_expired})"
                )
            return total
        except Exception as e:
            logger.warning(f"Failed to expire stale backlog entries: {e}")
            session.rollback()
            return 0
        finally:
            session.close()

    def get_stats(self, backend_name: str | None = None) -> dict[str, int]:
        """Get backlog stats grouped by status.

        Args:
            backend_name: Optional filter by backend

        Returns:
            Dict mapping status -> count
        """
        from sqlalchemy import func

        from nexus.storage.models import SyncBacklogModel

        session = self._get_session()
        if session is None:
            return {}

        try:
            query = session.query(
                SyncBacklogModel.status,
                func.count(SyncBacklogModel.id),
            ).group_by(SyncBacklogModel.status)

            if backend_name:
                query = query.filter(SyncBacklogModel.backend_name == backend_name)

            return dict(query.all())
        except Exception as e:
            logger.warning(f"Failed to get backlog stats: {e}")
            return {}
        finally:
            session.close()

    def _update_status(self, entry_id: str, new_status: str, from_status: str) -> bool:
        """Generic status transition with guard.

        Args:
            entry_id: Backlog entry ID
            new_status: Target status
            from_status: Expected current status

        Returns:
            True if transition succeeded
        """
        from nexus.storage.models import SyncBacklogModel

        session = self._get_session()
        if session is None:
            return False

        try:
            now = datetime.now(UTC)
            updated = {
                "status": new_status,
                "updated_at": now,
            }
            if new_status == "in_progress":
                updated["last_attempted_at"] = now

            count = (
                session.query(SyncBacklogModel)
                .filter(
                    SyncBacklogModel.id == entry_id,
                    SyncBacklogModel.status == from_status,
                )
                .update(updated, synchronize_session="fetch")
            )
            session.commit()
            return int(count) > 0
        except Exception as e:
            logger.warning(
                f"Failed to transition backlog {entry_id} from {from_status} to {new_status}: {e}"
            )
            session.rollback()
            return False
        finally:
            session.close()

    @staticmethod
    def _to_entry(row: SyncBacklogModel) -> SyncBacklogEntry:
        """Convert SQLAlchemy model to frozen dataclass."""
        return SyncBacklogEntry(
            id=row.id,
            path=row.path,
            backend_name=row.backend_name,
            zone_id=row.zone_id,
            operation_type=row.operation_type,
            content_hash=row.content_hash,
            new_path=row.new_path,
            status=row.status,
            retry_count=row.retry_count,
            max_retries=row.max_retries,
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_attempted_at=row.last_attempted_at,
            error_message=row.error_message,
        )
