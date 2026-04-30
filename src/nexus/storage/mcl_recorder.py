"""MCL recorder — writes metadata change log entries from write observer hooks (Issue #2929).

Integrates with RecordStoreWriteObserver to record MCL entries for file
operations (write, delete, rename). Uses strict_mode=False since MCL is
non-critical (reindex catches gaps).

Architecture:
    RecordStoreWriteObserver.on_write() → MCLRecorder.record_file_write()
    RecordStoreWriteObserver.on_delete() → MCLRecorder.record_file_delete()
    RecordStoreWriteObserver.on_rename() → MCLRecorder.record_file_rename()
"""

import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nexus.storage.models.metadata_change_log import MCLChangeType, MetadataChangeLogModel

logger = logging.getLogger(__name__)

# Maximum retries for sequence collision (SQLite fallback only)
_MAX_SEQUENCE_RETRIES = 3


class MCLRecorder:
    """Records metadata change log entries for file operations.

    Designed to be called from write observer hooks (fire-and-forget).
    Failures are logged but never raised (MCL is non-critical).

    Sequence allocation (Issue #3062):
        PostgreSQL — uses a database SEQUENCE (mcl_sequence_number_seq),
        allocated server-side with no Python-level race window.
        SQLite/other — falls back to time-based microsecond allocation
        with retry on uniqueness violation.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def _is_postgres(self) -> bool:
        return self._session.bind is not None and self._session.bind.dialect.name == "postgresql"

    def _next_sequence_fallback(self) -> int:
        """Fallback sequence allocation for non-PostgreSQL dialects.

        Uses time-based microsecond timestamps with MAX+1 floor.
        """
        time_based = int(time.time() * 1_000_000)
        result = self._session.execute(
            select(func.coalesce(func.max(MetadataChangeLogModel.sequence_number), 0))
        ).scalar()
        current_max = int(result) if result is not None else 0
        return max(time_based, current_max + 1)

    def _next_sequence_postgres(self) -> int:
        """Allocate from the PostgreSQL sequence before ORM insert."""
        result = self._session.execute(text("SELECT nextval('mcl_sequence_number_seq')"))
        return int(result.scalar_one())

    def _allocate_sequence(self) -> int:
        """Allocate a sequence number.

        PostgreSQL has a database sequence, but assigning the value explicitly
        avoids ORM inserts sending NULL when the mapped column has no server_default.
        """
        if self._is_postgres():
            return self._next_sequence_postgres()
        return self._next_sequence_fallback()

    def _record(self, mcl_kwargs: dict[str, Any], label: str) -> None:
        """Create and persist an MCL entry with retry on sequence collision.

        On PostgreSQL the DB sequence prevents collisions.  On SQLite the
        fallback allocator can race under concurrency; retrying with a
        fresh value resolves it (Issue #3062).
        """
        for attempt in range(_MAX_SEQUENCE_RETRIES):
            try:
                mcl_kwargs["sequence_number"] = self._allocate_sequence()
                mcl = MetadataChangeLogModel(**mcl_kwargs)
                self._session.add(mcl)
                self._session.flush()
                return
            except IntegrityError:
                self._session.rollback()
                if attempt == _MAX_SEQUENCE_RETRIES - 1:
                    logger.warning("MCL %s failed after %d retries", label, _MAX_SEQUENCE_RETRIES)
            except Exception:
                logger.warning("MCL %s failed", label, exc_info=True)
                return

    def record_file_write(
        self,
        entity_urn: str,
        metadata_dict: dict[str, Any] | None = None,
        *,
        zone_id: str | None = None,
        changed_by: str = "system",
        previous_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record an MCL entry for a file write operation."""
        self._record(
            {
                "entity_urn": entity_urn,
                "aspect_name": "file_metadata",
                "change_type": MCLChangeType.UPSERT.value,
                "aspect_value": json.dumps(metadata_dict, default=str) if metadata_dict else None,
                "previous_value": (
                    json.dumps(previous_metadata, default=str) if previous_metadata else None
                ),
                "zone_id": zone_id,
                "changed_by": changed_by,
                "created_at": datetime.now(UTC),
            },
            "record_file_write",
        )

    def record_file_delete(
        self,
        entity_urn: str,
        *,
        zone_id: str | None = None,
        changed_by: str = "system",
        previous_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record an MCL entry for a file delete operation."""
        self._record(
            {
                "entity_urn": entity_urn,
                "aspect_name": "file_metadata",
                "change_type": MCLChangeType.DELETE.value,
                "previous_value": (
                    json.dumps(previous_metadata, default=str) if previous_metadata else None
                ),
                "zone_id": zone_id,
                "changed_by": changed_by,
                "created_at": datetime.now(UTC),
            },
            "record_file_delete",
        )

    def record_file_rename(
        self,
        entity_urn: str,
        old_path: str,
        new_path: str,
        *,
        zone_id: str | None = None,
        changed_by: str = "system",
    ) -> None:
        """Record an MCL entry for a file rename (path aspect change).

        With UUID-based URN, rename doesn't change the URN — only the
        path aspect updates. We record a PATH_CHANGED event.
        """
        self._record(
            {
                "entity_urn": entity_urn,
                "aspect_name": "path",
                "change_type": MCLChangeType.PATH_CHANGED.value,
                "aspect_value": json.dumps({"virtual_path": new_path}, default=str),
                "previous_value": json.dumps({"virtual_path": old_path}, default=str),
                "zone_id": zone_id,
                "changed_by": changed_by,
                "created_at": datetime.now(UTC),
            },
            "record_file_rename",
        )

    # ------------------------------------------------------------------
    # Replay API (Issue #2929)
    # ------------------------------------------------------------------

    def replay_changes(
        self,
        *,
        from_sequence: int = 0,
        zone_id: str | None = None,
        aspect_name: str | None = None,
        batch_size: int = 500,
    ) -> Iterator[MetadataChangeLogModel]:
        """Yield MCL records for replay/reindexing.

        Returns an iterator of MCL records ordered by sequence_number.
        Supports filtering by zone_id, aspect_name, and sequence cursor.

        Args:
            from_sequence: Start from this sequence number (inclusive).
            zone_id: Filter by zone.
            aspect_name: Filter by aspect type.
            batch_size: Number of records to fetch per batch.

        Yields:
            MetadataChangeLogModel instances in sequence order.
        """
        stmt = select(MetadataChangeLogModel).order_by(MetadataChangeLogModel.sequence_number)

        if from_sequence > 0:
            stmt = stmt.where(MetadataChangeLogModel.sequence_number >= from_sequence)
        if zone_id is not None:
            stmt = stmt.where(MetadataChangeLogModel.zone_id == zone_id)
        if aspect_name is not None:
            stmt = stmt.where(MetadataChangeLogModel.aspect_name == aspect_name)

        offset = 0
        while True:
            batch = list(self._session.execute(stmt.limit(batch_size).offset(offset)).scalars())
            if not batch:
                break
            yield from batch
            offset += batch_size
