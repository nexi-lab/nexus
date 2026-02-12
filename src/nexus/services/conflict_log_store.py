"""Conflict log store for audit trail of conflict resolution events (Issue #1130).

Persists ConflictRecord entries to the conflict_log table and provides
query APIs for the REST conflict management endpoints.

Extends SyncStoreBase for shared session management and dialect detection.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from nexus.services.conflict_resolution import (
    ConflictRecord,
    ConflictStatus,
    ConflictStrategy,
    ResolutionOutcome,
)
from nexus.storage.sync_store_base import SyncStoreBase

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
        from nexus.storage.models import ConflictLogModel

        with self._with_session() as session:
            query = session.query(ConflictLogModel)

            if status is not None:
                query = query.filter(ConflictLogModel.status == status)
            if backend_name is not None:
                query = query.filter(ConflictLogModel.backend_name == backend_name)
            if zone_id is not None:
                query = query.filter(ConflictLogModel.zone_id == zone_id)

            query = query.order_by(ConflictLogModel.created_at.desc())
            rows = query.offset(offset).limit(limit).all()
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
        from nexus.storage.models import ConflictLogModel

        with self._with_session() as session:
            query = session.query(ConflictLogModel)
            if status is not None:
                query = query.filter(ConflictLogModel.status == status)
            if backend_name is not None:
                query = query.filter(ConflictLogModel.backend_name == backend_name)
            if zone_id is not None:
                query = query.filter(ConflictLogModel.zone_id == zone_id)
            return int(query.count())

    def get_conflict(self, conflict_id: str) -> ConflictRecord | None:
        """Look up a conflict record by ID.

        Args:
            conflict_id: UUID of the conflict record

        Returns:
            ConflictRecord if found, None otherwise
        """
        from nexus.storage.models import ConflictLogModel

        with self._with_session() as session:
            row = session.query(ConflictLogModel).filter_by(id=conflict_id).first()
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
        from nexus.storage.models import ConflictLogModel

        with self._with_session() as session:
            updated = (
                session.query(ConflictLogModel)
                .filter(
                    ConflictLogModel.id == conflict_id,
                    ConflictLogModel.status == ConflictStatus.MANUAL_PENDING,
                )
                .update(
                    {
                        "status": ConflictStatus.MANUALLY_RESOLVED,
                        "outcome": str(outcome),
                        "resolved_at": datetime.now(UTC),
                    },
                    synchronize_session="fetch",
                )
            )
            return bool(updated > 0)

    def expire_stale(self, ttl_seconds: int = 2592000, max_entries: int = 10000) -> int:
        """Expire old conflict records by TTL (30 days) and cap (10K).

        Args:
            ttl_seconds: Max age in seconds (default 30 days)
            max_entries: Max total records before oldest get deleted

        Returns:
            Number of records deleted
        """
        from nexus.storage.models import ConflictLogModel

        with self._with_session() as session:
            now = datetime.now(UTC)
            cutoff = datetime.fromtimestamp(now.timestamp() - ttl_seconds, tz=UTC)

            # Phase 1: TTL — delete records older than cutoff
            ttl_deleted = (
                session.query(ConflictLogModel)
                .filter(ConflictLogModel.created_at < cutoff)
                .delete(synchronize_session="fetch")
            )

            # Phase 2: Cap — if still over limit, delete oldest
            total_count = session.query(ConflictLogModel).count()
            cap_deleted = 0
            if total_count > max_entries:
                overflow = total_count - max_entries
                oldest_ids = (
                    session.query(ConflictLogModel.id)
                    .order_by(ConflictLogModel.created_at)
                    .limit(overflow)
                    .all()
                )
                if oldest_ids:
                    ids = [row[0] for row in oldest_ids]
                    cap_deleted = (
                        session.query(ConflictLogModel)
                        .filter(ConflictLogModel.id.in_(ids))
                        .delete(synchronize_session="fetch")
                    )

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
        from sqlalchemy import func

        from nexus.storage.models import ConflictLogModel

        with self._with_session() as session:
            rows = (
                session.query(ConflictLogModel.status, func.count())
                .group_by(ConflictLogModel.status)
                .all()
            )
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
