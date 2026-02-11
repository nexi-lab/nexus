"""Version recorder: syncs Metastore writes to RecordStore version history.

Follows the same pattern as OperationLogger — takes a SQLAlchemy session,
called by kernel after Metastore.put()/delete().

Populates FilePathModel and VersionHistoryModel so Services
(VersionGC, TimeTravelReader, SemanticSearch) can query version data.

Architecture:
    Metastore (sled) = SSOT for FileMetadata
    RecordStore (SQL) = supplemental for version history + search indexing
    If sync fails, the write still succeeds (sled is authoritative).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from nexus.storage.models import FilePathModel, VersionHistoryModel

if TYPE_CHECKING:
    from nexus.core._metadata_generated import FileMetadata


def _utcnow_naive() -> datetime:
    """Return current UTC time as naive datetime (for SQLite compat)."""
    return datetime.now(UTC).replace(tzinfo=None)


class VersionRecorder:
    """Records file version history to RecordStore (FilePathModel + VersionHistoryModel).

    Usage (from kernel _write_internal):
        with self.SessionLocal() as session:
            recorder = VersionRecorder(session)
            recorder.record_write(metadata, is_new=True)
            session.commit()
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def record_write(self, metadata: FileMetadata, *, is_new: bool) -> None:
        """Record a file write (create or update).

        Args:
            metadata: FileMetadata that was just written to Metastore.
            is_new: True if new file, False if updating existing.
        """
        if is_new:
            self._record_create(metadata)
        else:
            self._record_update(metadata)

    def record_delete(self, path: str) -> None:
        """Record a file deletion (soft-delete FilePathModel).

        Args:
            path: Virtual path that was deleted from Metastore.
        """
        existing = self.session.execute(
            select(FilePathModel).where(
                FilePathModel.virtual_path == path,
                FilePathModel.deleted_at.is_(None),
            )
        ).scalar_one_or_none()

        if existing:
            self.session.execute(
                update(FilePathModel)
                .where(FilePathModel.path_id == existing.path_id)
                .values(deleted_at=_utcnow_naive())
            )

    def _record_create(self, metadata: FileMetadata) -> None:
        """Insert new FilePathModel + initial VersionHistoryModel."""
        # Remove any soft-deleted entries at this path (single statement, no SELECT)
        self.session.execute(
            delete(FilePathModel).where(
                FilePathModel.virtual_path == metadata.path,
                FilePathModel.deleted_at.is_not(None),
            )
        )

        from nexus.storage.metadata_mapper import MetadataMapper

        values = MetadataMapper.to_file_path_values(metadata)
        file_path = FilePathModel(
            path_id=str(uuid.uuid4()),
            **values,
        )
        self.session.add(file_path)
        self.session.flush()

        if metadata.etag is not None:
            version_entry = VersionHistoryModel(
                version_id=str(uuid.uuid4()),
                resource_type="file",
                resource_id=file_path.path_id,
                version_number=1,
                content_hash=metadata.etag,
                size_bytes=metadata.size or 0,
                mime_type=metadata.mime_type,
                parent_version_id=None,
                source_type="original",
                created_at=file_path.created_at,
                created_by=metadata.created_by,
            )
            self.session.add(version_entry)

    def _record_update(self, metadata: FileMetadata) -> None:
        """Update existing FilePathModel + append VersionHistoryModel."""
        existing = self.session.execute(
            select(FilePathModel).where(
                FilePathModel.virtual_path == metadata.path,
                FilePathModel.deleted_at.is_(None),
            )
        ).scalar_one_or_none()

        if not existing:
            # File not in RecordStore yet — create it
            self._record_create(metadata)
            return

        from nexus.storage.metadata_mapper import MetadataMapper

        update_values = MetadataMapper.to_file_path_update_values(metadata)
        # Preserve existing values for fields the metadata doesn't override
        if not metadata.backend_name:
            update_values["backend_id"] = existing.backend_id
        if not metadata.physical_path:
            update_values["physical_path"] = existing.physical_path

        if metadata.etag is not None:
            # Get previous version for lineage
            prev_version = self.session.execute(
                select(VersionHistoryModel)
                .where(
                    VersionHistoryModel.resource_type == "file",
                    VersionHistoryModel.resource_id == existing.path_id,
                    VersionHistoryModel.version_number == existing.current_version,
                )
                .limit(1)
            ).scalar_one_or_none()

            # Atomically increment version
            update_result = self.session.execute(
                update(FilePathModel)
                .where(FilePathModel.path_id == existing.path_id)
                .values(
                    **update_values,
                    current_version=FilePathModel.current_version + 1,
                )
                .returning(FilePathModel.current_version)
            )
            new_version = update_result.scalar_one()

            version_entry = VersionHistoryModel(
                version_id=str(uuid.uuid4()),
                resource_type="file",
                resource_id=existing.path_id,
                version_number=new_version,
                content_hash=metadata.etag,
                size_bytes=metadata.size or 0,
                mime_type=metadata.mime_type,
                parent_version_id=prev_version.version_id if prev_version else None,
                source_type="original",
                created_at=_utcnow_naive(),
                created_by=metadata.created_by,
            )
            self.session.add(version_entry)
        else:
            # No content hash — update metadata only
            self.session.execute(
                update(FilePathModel)
                .where(FilePathModel.path_id == existing.path_id)
                .values(**update_values)
            )
