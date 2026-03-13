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
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nexus.storage.models.metadata_change_log import MCLChangeType, MetadataChangeLogModel

logger = logging.getLogger(__name__)


class MCLRecorder:
    """Records metadata change log entries for file operations.

    Designed to be called from write observer hooks (fire-and-forget).
    Failures are logged but never raised (MCL is non-critical).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def _next_sequence(self) -> int:
        """Get the next MCL sequence number."""
        result = self._session.execute(
            select(func.coalesce(func.max(MetadataChangeLogModel.sequence_number), 0) + 1)
        ).scalar()
        return int(result) if result is not None else 1

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
        try:
            mcl = MetadataChangeLogModel(
                sequence_number=self._next_sequence(),
                entity_urn=entity_urn,
                aspect_name="file_metadata",
                change_type=MCLChangeType.UPSERT.value,
                aspect_value=json.dumps(metadata_dict, default=str) if metadata_dict else None,
                previous_value=(
                    json.dumps(previous_metadata, default=str) if previous_metadata else None
                ),
                zone_id=zone_id,
                changed_by=changed_by,
                created_at=datetime.now(UTC),
            )
            self._session.add(mcl)
        except Exception:
            logger.warning("MCL record_file_write failed", exc_info=True)

    def record_file_delete(
        self,
        entity_urn: str,
        *,
        zone_id: str | None = None,
        changed_by: str = "system",
        previous_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record an MCL entry for a file delete operation."""
        try:
            mcl = MetadataChangeLogModel(
                sequence_number=self._next_sequence(),
                entity_urn=entity_urn,
                aspect_name="file_metadata",
                change_type=MCLChangeType.DELETE.value,
                previous_value=(
                    json.dumps(previous_metadata, default=str) if previous_metadata else None
                ),
                zone_id=zone_id,
                changed_by=changed_by,
                created_at=datetime.now(UTC),
            )
            self._session.add(mcl)
        except Exception:
            logger.warning("MCL record_file_delete failed", exc_info=True)

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
        try:
            mcl = MetadataChangeLogModel(
                sequence_number=self._next_sequence(),
                entity_urn=entity_urn,
                aspect_name="path",
                change_type=MCLChangeType.PATH_CHANGED.value,
                aspect_value=json.dumps({"virtual_path": new_path}, default=str),
                previous_value=json.dumps({"virtual_path": old_path}, default=str),
                zone_id=zone_id,
                changed_by=changed_by,
                created_at=datetime.now(UTC),
            )
            self._session.add(mcl)
        except Exception:
            logger.warning("MCL record_file_rename failed", exc_info=True)
