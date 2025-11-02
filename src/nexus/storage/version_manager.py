"""Version management for Nexus metadata store.

Provides version tracking and history management:
- Get specific versions of files
- List version history
- Rollback to previous versions
- Compare versions (diff)
"""

from __future__ import annotations

import builtins
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from nexus.core.exceptions import MetadataError
from nexus.core.metadata import FileMetadata
from nexus.storage.models import FilePathModel, VersionHistoryModel


class VersionManager:
    """
    Version management for file metadata.

    Handles version tracking, history, rollbacks, and diffs using
    the version_history table and maintaining version lineage.
    """

    @staticmethod
    def get_version(session: Session, path: str) -> FileMetadata | None:
        """Get a specific version of a file.

        Retrieves file metadata for a specific version from version history.
        The content_hash in the returned metadata can be used to fetch the
        actual content from CAS storage.

        Args:
            session: SQLAlchemy session
            path: Virtual path
            version: Version number to retrieve

        Returns:
            FileMetadata for the specified version, or None if not found

        Raises:
            MetadataError: If query fails

        Example:
            >>> # Get version 2 of a file
            >>> with store.SessionLocal() as session:
            ...     metadata = VersionManager.get_version(session, "/workspace/data.txt", version=2)
            ...     if metadata:
            ...         content_hash = metadata.etag
            ...         # Use content_hash to fetch from CAS
        """
        try:
            # Extract version from path (format: path@version)
            if "@" not in path:
                return None

            virtual_path, version_str = path.rsplit("@", 1)
            try:
                version = int(version_str)
            except ValueError:
                return None

            # Get the file's path_id
            path_stmt = select(FilePathModel.path_id).where(
                FilePathModel.virtual_path == virtual_path,
                FilePathModel.deleted_at.is_(None),
            )
            path_id = session.scalar(path_stmt)

            if not path_id:
                return None

            # Get the version from history
            version_stmt = select(VersionHistoryModel).where(
                VersionHistoryModel.resource_type == "file",
                VersionHistoryModel.resource_id == path_id,
                VersionHistoryModel.version_number == version,
            )
            version_entry = session.scalar(version_stmt)

            if not version_entry:
                return None

            # Build FileMetadata from version entry
            # Note: We don't have backend info in version history, so use current file's backend
            file_stmt = select(FilePathModel).where(FilePathModel.path_id == path_id)
            file_path = session.scalar(file_stmt)

            if not file_path:
                return None

            return FileMetadata(
                path=file_path.virtual_path,
                backend_name=file_path.backend_id,
                physical_path=version_entry.content_hash,  # CAS: hash is the physical path
                size=version_entry.size_bytes,
                etag=version_entry.content_hash,
                mime_type=version_entry.mime_type,
                created_at=version_entry.created_at,
                modified_at=version_entry.created_at,
                version=version_entry.version_number,
                # v0.5.0: owner/group/mode removed - use ReBAC for permissions
            )
        except Exception as e:
            raise MetadataError(f"Failed to get version: {e}", path=path) from e

    @staticmethod
    def list_versions(session: Session, path: str) -> builtins.list[dict[str, Any]]:
        """List all versions of a file.

        Returns version history with metadata for each version.

        Args:
            session: SQLAlchemy session
            path: Virtual path

        Returns:
            List of version info dicts ordered by version number (newest first)

        Raises:
            MetadataError: If query fails

        Example:
            >>> with store.SessionLocal() as session:
            ...     versions = VersionManager.list_versions(session, "/workspace/SKILL.md")
            ...     for v in versions:
            ...         print(f"v{v['version']}: {v['size']} bytes, {v['created_at']}")
        """
        try:
            # Get the file's path_id
            path_stmt = select(FilePathModel.path_id).where(
                FilePathModel.virtual_path == path,
                FilePathModel.deleted_at.is_(None),
            )
            path_id = session.scalar(path_stmt)

            if not path_id:
                return []

            # Get all versions
            versions_stmt = (
                select(VersionHistoryModel)
                .where(
                    VersionHistoryModel.resource_type == "file",
                    VersionHistoryModel.resource_id == path_id,
                )
                .order_by(VersionHistoryModel.version_number.desc())
            )

            versions = []
            for v in session.scalars(versions_stmt):
                versions.append(
                    {
                        "version": v.version_number,
                        "content_hash": v.content_hash,
                        "size": v.size_bytes,
                        "mime_type": v.mime_type,
                        "created_at": v.created_at,
                        "created_by": v.created_by,
                        "change_reason": v.change_reason,
                        "source_type": v.source_type,
                        "parent_version_id": v.parent_version_id,
                    }
                )

            return versions
        except Exception as e:
            raise MetadataError(f"Failed to list versions: {e}", path=path) from e

    @staticmethod
    def rollback(session: Session, path: str, version: int, created_by: str | None = None) -> None:
        """Rollback file to a previous version.

        Updates the file to point to an older version's content.
        Creates a new version entry marking this as a rollback.

        Args:
            session: SQLAlchemy session
            path: Virtual path
            version: Version number to rollback to
            created_by: User or agent ID who performed the rollback (optional)

        Raises:
            MetadataError: If file or version not found

        Example:
            >>> # Rollback to version 2
            >>> with store.SessionLocal() as session:
            ...     VersionManager.rollback(session, "/workspace/data.txt", version=2, created_by="alice")
            ...     session.commit()
        """
        import logging

        logger = logging.getLogger(__name__)

        logger.info(
            f"[VERSION_MANAGER.rollback] Starting rollback for path={path}, version={version}"
        )
        try:
            # Get current file with row-level locking to prevent concurrent version conflicts
            logger.info(f"[VERSION_MANAGER.rollback] Querying file_paths table for path={path}")
            file_stmt = (
                select(FilePathModel)
                .where(
                    FilePathModel.virtual_path == path,
                    FilePathModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            file_path = session.scalar(file_stmt)

            if not file_path:
                logger.error(f"[VERSION_MANAGER.rollback] File not found in database: {path}")
                raise MetadataError(f"File not found: {path}", path=path)

            logger.info(
                f"[VERSION_MANAGER.rollback] Found file: path_id={file_path.path_id}, current_version={file_path.current_version}, content_hash={file_path.content_hash[:16] if file_path.content_hash else 'None'}"
            )

            # Get target version
            logger.info(
                f"[VERSION_MANAGER.rollback] Querying version_history for target version={version}"
            )
            version_stmt = select(VersionHistoryModel).where(
                VersionHistoryModel.resource_type == "file",
                VersionHistoryModel.resource_id == file_path.path_id,
                VersionHistoryModel.version_number == version,
            )
            target_version = session.scalar(version_stmt)

            if not target_version:
                logger.error(
                    f"[VERSION_MANAGER.rollback] Target version {version} not found for {path}"
                )
                raise MetadataError(f"Version {version} not found for {path}", path=path)

            logger.info(
                f"[VERSION_MANAGER.rollback] Found target version: version={version}, content_hash={target_version.content_hash[:16]}, size={target_version.size_bytes}"
            )

            # Get current version entry for lineage
            logger.info(
                f"[VERSION_MANAGER.rollback] Getting current version entry for version={file_path.current_version}"
            )
            current_version_stmt = select(VersionHistoryModel).where(
                VersionHistoryModel.resource_type == "file",
                VersionHistoryModel.resource_id == file_path.path_id,
                VersionHistoryModel.version_number == file_path.current_version,
            )
            current_version_entry = session.scalar(current_version_stmt)
            logger.info(
                f"[VERSION_MANAGER.rollback] Current version entry found: {current_version_entry is not None}"
            )

            # Update file to target version's content
            logger.info(
                "[VERSION_MANAGER.rollback] Updating file_paths record to target version's content"
            )
            logger.info(
                f"[VERSION_MANAGER.rollback] Before: content_hash={file_path.content_hash[:16] if file_path.content_hash else 'None'}, size={file_path.size_bytes}"
            )
            file_path.content_hash = target_version.content_hash
            file_path.size_bytes = target_version.size_bytes
            file_path.file_type = target_version.mime_type
            file_path.updated_at = datetime.now(UTC)
            logger.info(
                f"[VERSION_MANAGER.rollback] After: content_hash={file_path.content_hash[:16]}, size={file_path.size_bytes}"
            )

            # Atomically increment version at database level to prevent race conditions
            logger.info("[VERSION_MANAGER.rollback] Incrementing current_version in database")
            session.execute(
                update(FilePathModel)
                .where(FilePathModel.path_id == file_path.path_id)
                .values(current_version=FilePathModel.current_version + 1)
            )
            # Refresh to get the new version number
            session.refresh(file_path)
            logger.info(
                f"[VERSION_MANAGER.rollback] New version number: {file_path.current_version}"
            )

            # Create version history entry for the NEW version (rollback)
            logger.info(
                "[VERSION_MANAGER.rollback] Creating new version_history entry for rollback"
            )
            rollback_version_entry = VersionHistoryModel(
                version_id=str(uuid.uuid4()),
                resource_type="file",
                resource_id=file_path.path_id,
                version_number=file_path.current_version,  # NEW version number
                content_hash=target_version.content_hash,  # Points to old content
                size_bytes=target_version.size_bytes,
                mime_type=target_version.mime_type,
                parent_version_id=current_version_entry.version_id
                if current_version_entry
                else None,
                source_type="rollback",
                change_reason=f"Rollback to version {version}",
                created_at=datetime.now(UTC),
                created_by=created_by,  # Track who performed the rollback
            )
            rollback_version_entry.validate()
            session.add(rollback_version_entry)
            logger.info("[VERSION_MANAGER.rollback] Added rollback version entry to session")
            logger.info("[VERSION_MANAGER.rollback] Rollback completed successfully")

        except MetadataError:
            raise
        except Exception as e:
            raise MetadataError(f"Failed to rollback to version {version}: {e}", path=path) from e

    @staticmethod
    def get_version_diff(session: Session, path: str, v1: int, v2: int) -> dict[str, Any]:
        """Get diff information between two versions.

        Returns metadata differences between versions.
        For content diff, retrieve both versions and compare.

        Args:
            session: SQLAlchemy session
            path: Virtual path
            v1: First version number
            v2: Second version number

        Returns:
            Dict with diff information

        Raises:
            MetadataError: If file or versions not found

        Example:
            >>> with store.SessionLocal() as session:
            ...     diff = VersionManager.get_version_diff(session, "/workspace/file.txt", v1=1, v2=3)
            ...     print(f"Size changed: {diff['size_v1']} -> {diff['size_v2']}")
            ...     print(f"Content changed: {diff['content_changed']}")
        """
        try:
            # Get path_id
            path_stmt = select(FilePathModel.path_id).where(
                FilePathModel.virtual_path == path,
                FilePathModel.deleted_at.is_(None),
            )
            path_id = session.scalar(path_stmt)

            if not path_id:
                raise MetadataError(f"File not found: {path}", path=path)

            # Get both versions
            versions_stmt = select(VersionHistoryModel).where(
                VersionHistoryModel.resource_type == "file",
                VersionHistoryModel.resource_id == path_id,
                VersionHistoryModel.version_number.in_([v1, v2]),
            )

            versions_dict = {v.version_number: v for v in session.scalars(versions_stmt)}

            if v1 not in versions_dict:
                raise MetadataError(f"Version {v1} not found", path=path)
            if v2 not in versions_dict:
                raise MetadataError(f"Version {v2} not found", path=path)

            version1 = versions_dict[v1]
            version2 = versions_dict[v2]

            return {
                "path": path,
                "v1": v1,
                "v2": v2,
                "content_hash_v1": version1.content_hash,
                "content_hash_v2": version2.content_hash,
                "content_changed": version1.content_hash != version2.content_hash,
                "size_v1": version1.size_bytes,
                "size_v2": version2.size_bytes,
                "size_delta": version2.size_bytes - version1.size_bytes,
                "created_at_v1": version1.created_at,
                "created_at_v2": version2.created_at,
                "mime_type_v1": version1.mime_type,
                "mime_type_v2": version2.mime_type,
            }
        except MetadataError:
            raise
        except Exception as e:
            raise MetadataError(f"Failed to diff versions: {e}", path=path) from e
