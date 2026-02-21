"""Conflict log store for audit trail of conflict resolution events (Issue #1130).

Persists ConflictRecord entries to the conflict_log table and provides
query APIs for the REST conflict management endpoints.

Extends SyncStoreBase for shared session management and dialect detection.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from nexus.storage.sync_store_base import SyncStoreBase

from .conflict_resolution import (
    ConflictRecord,
    ConflictStatus,
    ConflictStrategy,
    ResolutionOutcome,
)

logger = logging.getLogger(__name__)


class ConflictLogStore(SyncStoreBase):
    """Audit log store for conflict resolution events.

    Methods:
    - log_conflict: INSERT a ConflictRecord
    - list_conflicts: Paginated query with optional filters
    - get_conflict: Lookup by ID
    - resolve_conflict_manually: Update status to manually_resolved
    - expire_stale: TTL + cap-based retention
    - get_stats: Counts by status
    """

    def log_conflict(self, record: ConflictRecord) -> str:
        """Persist a conflict record and return its ID.

        Args:
            record: Immutable ConflictRecord to store

        Returns:
            The conflict record ID
        """
        from nexus.storage.models import ConflictLogModel

        with self._with_session() as session:
            model = ConflictLogModel(
                id=record.id,
                path=record.path,
                backend_name=record.backend_name,
                zone_id=record.zone_id,
                strategy=str(record.strategy),
                outcome=str(record.outcome),
                nexus_content_hash=record.nexus_content_hash,
                nexus_mtime=record.nexus_mtime,
                nexus_size=record.nexus_size,
                backend_content_hash=record.backend_content_hash,
                backend_mtime=record.backend_mtime,
                backend_size=record.backend_size,
                conflict_copy_path=record.conflict_copy_path,
                status=record.status,
                resolved_at=record.resolved_at,
                created_at=record.resolved_at,
            )
            session.add(model)

        return record.id

    def list_conflicts(
        self,
        *,
        status: str | None = None,
        backend_name: str | None = None,
        zone_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ConflictRecord]:
        """Query conflict records with optional filters and pagination.

        Args:
            status: Filter by status (auto_resolved, manual_pending, manually_resolved)
            backend_name: Filter by backend name
            zone_id: Filter by zone ID
            limit: Max records to return
            offset: Number of records to skip

        Returns:
            List of ConflictRecord entries
        """
        from sqlalchemy import select

        from nexus.storage.models import ConflictLogModel

        with self._with_session() as session:
            stmt = select(ConflictLogModel)

            if status is not None:
                stmt = stmt.where(ConflictLogModel.status == status)
            if backend_name is not None:
                stmt = stmt.where(ConflictLogModel.backend_name == backend_name)
            if zone_id is not None:
                stmt = stmt.where(ConflictLogModel.zone_id == zone_id)

            stmt = stmt.order_by(ConflictLogModel.created_at.desc())
            rows = session.execute(stmt.offset(offset).limit(limit)).scalars().all()
            return [self._row_to_record(row) for row in rows]

    def count_conflicts(
        self,
        *,
        status: str | None = None,
        backend_name: str | None = None,
        zone_id: str | None = None,
    ) -> int:
        """Count conflict records matching the given filters.

        Args:
            status: Filter by status
            backend_name: Filter by backend name
            zone_id: Filter by zone ID

        Returns:
            Total count matching filters
        """
        from sqlalchemy import func, select

        from nexus.storage.models import ConflictLogModel

        with self._with_session() as session:
            stmt = select(func.count()).select_from(ConflictLogModel)
            if status is not None:
                stmt = stmt.where(ConflictLogModel.status == status)
            if backend_name is not None:
                stmt = stmt.where(ConflictLogModel.backend_name == backend_name)
            if zone_id is not None:
                stmt = stmt.where(ConflictLogModel.zone_id == zone_id)
            return int(session.execute(stmt).scalar() or 0)

    def get_conflict(self, conflict_id: str) -> ConflictRecord | None:
        """Look up a conflict record by ID.

        Args:
            conflict_id: UUID of the conflict record

        Returns:
            ConflictRecord if found, None otherwise
        """
        from sqlalchemy import select

        from nexus.storage.models import ConflictLogModel

        with self._with_session() as session:
            row = (
                session.execute(select(ConflictLogModel).filter_by(id=conflict_id))
                .scalars()
                .first()
            )
            if row is None:
                return None
            return self._row_to_record(row)

    def resolve_conflict_manually(self, conflict_id: str, outcome: ResolutionOutcome) -> bool:
        """Update a conflict record to manually_resolved status.

        Args:
            conflict_id: UUID of the conflict record
            outcome: The chosen resolution outcome

        Returns:
            True if updated, False if not found or already resolved
        """
        from sqlalchemy import update

        from nexus.storage.models import ConflictLogModel

        with self._with_session() as session:
            result: Any = session.execute(
                update(ConflictLogModel)
                .where(
                    ConflictLogModel.id == conflict_id,
                    ConflictLogModel.status == ConflictStatus.MANUAL_PENDING,
                )
                .values(
                    status=ConflictStatus.MANUALLY_RESOLVED,
                    outcome=str(outcome),
                    resolved_at=datetime.now(UTC),
                )
            )
            return bool(result.rowcount > 0)

    def expire_stale(self, ttl_seconds: int = 2592000, max_entries: int = 10000) -> int:
        """Expire old conflict records by TTL (30 days) and cap (10K).

        Args:
            ttl_seconds: Max age in seconds (default 30 days)
            max_entries: Max total records before oldest get deleted

        Returns:
            Number of records deleted
        """
        from sqlalchemy import delete, func, select

        from nexus.storage.models import ConflictLogModel

        with self._with_session() as session:
            now = datetime.now(UTC)
            cutoff = datetime.fromtimestamp(now.timestamp() - ttl_seconds, tz=UTC)

            # Phase 1: TTL — delete records older than cutoff
            result: Any = session.execute(
                delete(ConflictLogModel).where(ConflictLogModel.created_at < cutoff)
            )
            ttl_deleted = result.rowcount

            # Phase 2: Cap — if still over limit, delete oldest
            total_count = int(
                session.execute(select(func.count()).select_from(ConflictLogModel)).scalar() or 0
            )
            cap_deleted = 0
            if total_count > max_entries:
                overflow = total_count - max_entries
                oldest_ids = session.execute(
                    select(ConflictLogModel.id)
                    .order_by(ConflictLogModel.created_at)
                    .limit(overflow)
                ).all()
                if oldest_ids:
                    ids = [row[0] for row in oldest_ids]
                    result = session.execute(
                        delete(ConflictLogModel).where(ConflictLogModel.id.in_(ids))
                    )
                    cap_deleted = result.rowcount

            total: int = ttl_deleted + cap_deleted
            if total > 0:
                logger.info(
                    f"[CONFLICT_LOG] Expired {total} records (ttl={ttl_deleted}, cap={cap_deleted})"
                )
            return total

    def get_stats(self) -> dict[str, Any]:
        """Get conflict log stats grouped by status.

        Returns:
            Dict with status counts and total
        """
        from sqlalchemy import func, select

        from nexus.storage.models import ConflictLogModel

        with self._with_session() as session:
            rows = session.execute(
                select(ConflictLogModel.status, func.count()).group_by(ConflictLogModel.status)
            ).all()
            result: dict[str, Any] = dict(rows)
            result["total"] = sum(result.values())
            return result

    @staticmethod
    def make_record_id() -> str:
        """Generate a new UUID for a conflict record."""
        return str(uuid.uuid4())

    @staticmethod
    def _row_to_record(row: Any) -> ConflictRecord:
        """Convert a ConflictLogModel row to an immutable ConflictRecord."""
        return ConflictRecord(
            id=row.id,
            path=row.path,
            backend_name=row.backend_name,
            zone_id=row.zone_id,
            strategy=ConflictStrategy(row.strategy),
            outcome=ResolutionOutcome(row.outcome),
            nexus_content_hash=row.nexus_content_hash,
            nexus_mtime=row.nexus_mtime,
            nexus_size=row.nexus_size,
            backend_content_hash=row.backend_content_hash,
            backend_mtime=row.backend_mtime,
            backend_size=row.backend_size,
            conflict_copy_path=row.conflict_copy_path,
            status=ConflictStatus(row.status),
            resolved_at=row.resolved_at,
        )
