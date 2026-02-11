"""SQL-backed metadata store — PostgreSQL as SSOT for file metadata.

Consolidates the dual-write pattern (Issues #1246, #1330):
- Before: Metastore (sled/redb) = SSOT, RecordStoreSyncer = write-through observer
- After: SQL (PostgreSQL) = SSOT, version history + audit log integrated

Architecture:
    SQL write path: put() → FilePathModel + VersionHistoryModel + OperationLog
    SQL read path:  get() → FilePathModel → FileMetadata
    Locks/revisions: delegated to raft_store (redb via PyO3)

Usage::

    from nexus.storage.sql_metadata_store import SqlMetadataStore

    store = SqlMetadataStore(
        session_factory=record_store.session_factory,
        raft_store=raft_metadata_store,  # for locks + extended metadata
    )
    store.put(metadata)
    result = store.get("/path/to/file.txt")
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from nexus.core._metadata_generated import FileMetadata, FileMetadataProtocol, PaginatedResult
from nexus.storage.models import FilePathModel, OperationLogModel, VersionHistoryModel

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Sentinel file_type for directory entries (distinct from MIME types).
_DIR_FILE_TYPE = "directory"


def _utcnow_naive() -> datetime:
    """Return current UTC time as naive datetime (for SQLite compat)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _to_naive(dt: datetime | None) -> datetime | None:
    """Strip timezone from datetime (SQLite stores naive UTC)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _row_to_metadata(row: FilePathModel) -> FileMetadata:
    """Convert a FilePathModel row to a FileMetadata dataclass."""
    is_dir = row.file_type == _DIR_FILE_TYPE
    return FileMetadata(
        path=row.virtual_path,
        backend_name=row.backend_id,
        physical_path=row.physical_path,
        size=row.size_bytes,
        etag=row.content_hash,
        mime_type=None if is_dir else row.file_type,
        created_at=row.created_at,
        modified_at=row.updated_at,
        version=row.current_version,
        zone_id=row.zone_id,
        created_by=None,  # Not stored in FilePathModel; available via VersionHistory
        is_directory=is_dir,
        owner_id=row.posix_uid,
    )


class SqlMetadataStore(FileMetadataProtocol):
    """SQL-backed metadata store — PostgreSQL as SSOT for file metadata.

    Consolidates the dual-write pattern (Issues #1246, #1330):
    - Before: Metastore (sled/redb) = SSOT, RecordStoreSyncer = observer
    - After: SQL (PostgreSQL) = SSOT, version history + audit log integrated

    Write path: SQL → FilePathModel + VersionHistoryModel + OperationLog
    Read path: SQL → FilePathModel → FileMetadata

    Optional raft_store for lock/extended metadata delegation:
    - Lock operations (acquire_lock, release_lock, etc.) → redb via Raft
    - Extended metadata (set_file_metadata, get_file_metadata) → redb
    - Revision counters → redb REVISIONS_TABLE
    """

    def __init__(
        self,
        session_factory: Callable[..., Any],
        *,
        raft_store: Any | None = None,
    ) -> None:
        """Initialize SQL metadata store.

        Args:
            session_factory: SQLAlchemy session factory (context manager).
            raft_store: Optional RaftMetadataStore for lock/extended metadata
                delegation. When None, lock and extended metadata operations
                will raise NotImplementedError.
        """
        self._session_factory = session_factory
        self._raft_store = raft_store

    # =========================================================================
    # FileMetadataProtocol — Core CRUD
    # =========================================================================

    def get(self, path: str) -> FileMetadata | None:
        """Get metadata for a file from SQL.

        Args:
            path: Virtual path

        Returns:
            FileMetadata if found, None otherwise
        """
        with self._session_factory() as session:
            row = session.execute(
                select(FilePathModel).where(
                    FilePathModel.virtual_path == path,
                    FilePathModel.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return _row_to_metadata(row)

    def put(self, metadata: FileMetadata) -> None:
        """Store or update file metadata in SQL (SSOT).

        Creates/updates FilePathModel, records version history, and logs
        the operation — all in a single SQL transaction.

        Args:
            metadata: File metadata to store
        """
        with self._session_factory() as session:
            existing = session.execute(
                select(FilePathModel).where(
                    FilePathModel.virtual_path == metadata.path,
                    FilePathModel.deleted_at.is_(None),
                )
            ).scalar_one_or_none()

            file_type = _DIR_FILE_TYPE if metadata.is_directory else metadata.mime_type

            if existing is None:
                self._create_file(session, metadata, file_type)
            else:
                self._update_file(session, existing, metadata, file_type)

            session.commit()

    def delete(self, path: str) -> dict[str, Any] | None:
        """Soft-delete file metadata in SQL.

        Args:
            path: Virtual path

        Returns:
            Dictionary with deleted file info or None if not found
        """
        with self._session_factory() as session:
            existing = session.execute(
                select(FilePathModel).where(
                    FilePathModel.virtual_path == path,
                    FilePathModel.deleted_at.is_(None),
                )
            ).scalar_one_or_none()

            if existing is None:
                return None

            result: dict[str, Any] = {
                "path": existing.virtual_path,
                "size": existing.size_bytes,
                "etag": existing.content_hash,
            }

            # Soft-delete
            session.execute(
                update(FilePathModel)
                .where(FilePathModel.path_id == existing.path_id)
                .values(deleted_at=_utcnow_naive())
            )

            # Log operation
            self._log_operation(
                session,
                "delete",
                path,
                zone_id=existing.zone_id,
                snapshot_hash=existing.content_hash,
            )

            session.commit()
            return result

    def exists(self, path: str) -> bool:
        """Check if metadata exists for a path in SQL.

        Args:
            path: Virtual path

        Returns:
            True if metadata exists, False otherwise
        """
        with self._session_factory() as session:
            row = session.execute(
                select(FilePathModel.path_id).where(
                    FilePathModel.virtual_path == path,
                    FilePathModel.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            return row is not None

    def list(
        self,
        prefix: str = "",
        recursive: bool = True,
        zone_id: str | None = None,
        accessible_int_ids: set[int] | None = None,
        **kwargs: Any,  # noqa: ARG002
    ) -> list[FileMetadata]:
        """List all files with given path prefix from SQL.

        Args:
            prefix: Path prefix to filter by
            recursive: If True, include all nested files
            zone_id: Filter by zone ID (optional)
            accessible_int_ids: Optional set of accessible file int_ids

        Returns:
            List of file metadata
        """
        with self._session_factory() as session:
            stmt = select(FilePathModel).where(
                FilePathModel.deleted_at.is_(None),
            )

            if prefix:
                stmt = stmt.where(FilePathModel.virtual_path.startswith(prefix))

            if zone_id:
                stmt = stmt.where(FilePathModel.zone_id == zone_id)

            stmt = stmt.order_by(FilePathModel.virtual_path)
            rows = session.execute(stmt).scalars().all()

            result = []
            for row in rows:
                if not recursive:
                    # Filter to direct children only
                    rel_path = row.virtual_path[len(prefix) :].lstrip("/")
                    if "/" in rel_path:
                        continue

                meta = _row_to_metadata(row)

                if accessible_int_ids is not None:
                    int_id = getattr(meta, "int_id", None)
                    if int_id is None or int_id not in accessible_int_ids:
                        continue

                result.append(meta)

            return result

    def list_paginated(
        self,
        prefix: str = "",
        recursive: bool = True,
        limit: int = 1000,
        cursor: str | None = None,
        zone_id: str | None = None,
    ) -> PaginatedResult:
        """List files with cursor-based pagination using SQL.

        Uses keyset pagination on virtual_path for O(log n) performance.
        """
        with self._session_factory() as session:
            stmt = select(FilePathModel).where(
                FilePathModel.deleted_at.is_(None),
            )

            if prefix:
                stmt = stmt.where(FilePathModel.virtual_path.startswith(prefix))

            if zone_id:
                stmt = stmt.where(FilePathModel.zone_id == zone_id)

            # Cursor-based pagination (keyset on virtual_path)
            if cursor:
                cursor_path = cursor
                try:
                    from nexus.core.pagination import decode_cursor

                    filters = {
                        "prefix": prefix,
                        "recursive": recursive,
                        "zone_id": zone_id,
                    }
                    decoded = decode_cursor(cursor, filters)
                    cursor_path = decoded.path
                except Exception:
                    cursor_path = cursor

                stmt = stmt.where(FilePathModel.virtual_path > cursor_path)

            stmt = stmt.order_by(FilePathModel.virtual_path).limit(limit + 1)

            rows = list(session.execute(stmt).scalars())

            # Apply non-recursive filter in Python
            if not recursive:
                rows = [
                    r
                    for r in rows
                    if "/" not in r.virtual_path[len(prefix) :].lstrip("/")
                ]

            has_more = len(rows) > limit
            if has_more:
                rows = rows[:limit]

            items = [_row_to_metadata(r) for r in rows]
            next_cursor = items[-1].path if has_more and items else None

            return PaginatedResult(
                items=items,
                next_cursor=next_cursor,
                has_more=has_more,
                total_count=None,  # Avoid COUNT(*) for performance
            )

    def close(self) -> None:
        """Close the metadata store and release resources."""
        if self._raft_store is not None:
            self._raft_store.close()

    # =========================================================================
    # Batch Operations (optimized SQL)
    # =========================================================================

    def get_batch(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        """Get metadata for multiple files in a single SQL query."""
        if not paths:
            return {}
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(FilePathModel).where(
                        FilePathModel.virtual_path.in_(paths),
                        FilePathModel.deleted_at.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            found = {row.virtual_path: _row_to_metadata(row) for row in rows}
            return {p: found.get(p) for p in paths}

    def put_batch(self, metadata_list: Sequence[FileMetadata]) -> None:
        """Store or update multiple file metadata entries in a single transaction."""
        if not metadata_list:
            return
        with self._session_factory() as session:
            # Fetch all existing paths in one query
            paths = [m.path for m in metadata_list]
            existing_rows = (
                session.execute(
                    select(FilePathModel).where(
                        FilePathModel.virtual_path.in_(paths),
                        FilePathModel.deleted_at.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            existing_map = {r.virtual_path: r for r in existing_rows}

            for metadata in metadata_list:
                file_type = _DIR_FILE_TYPE if metadata.is_directory else metadata.mime_type
                existing = existing_map.get(metadata.path)
                if existing is None:
                    self._create_file(session, metadata, file_type)
                else:
                    self._update_file(session, existing, metadata, file_type)

            session.commit()

    def delete_batch(self, paths: Sequence[str]) -> None:
        """Delete multiple files in a single transaction."""
        if not paths:
            return
        with self._session_factory() as session:
            now = _utcnow_naive()
            session.execute(
                update(FilePathModel)
                .where(
                    FilePathModel.virtual_path.in_(paths),
                    FilePathModel.deleted_at.is_(None),
                )
                .values(deleted_at=now)
            )
            session.commit()

    def batch_get_content_ids(self, paths: Sequence[str]) -> dict[str, str | None]:
        """Get content IDs (hashes) for multiple paths in a single query."""
        if not paths:
            return {}
        with self._session_factory() as session:
            rows = session.execute(
                select(FilePathModel.virtual_path, FilePathModel.content_hash).where(
                    FilePathModel.virtual_path.in_(paths),
                    FilePathModel.deleted_at.is_(None),
                )
            ).all()
            found = {r[0]: r[1] for r in rows}
            return {p: found.get(p) for p in paths}

    # =========================================================================
    # Revision Counter (delegate to raft_store — redb REVISIONS_TABLE)
    # =========================================================================

    def increment_revision(self, zone_id: str) -> int:
        """Atomically increment and return the new revision for a zone.

        Delegates to raft_store's redb REVISIONS_TABLE.
        No Python lock needed — redb's single-writer transaction provides atomicity.

        Args:
            zone_id: The zone to increment revision for

        Returns:
            The new revision number after incrementing
        """
        if self._raft_store is None:
            raise NotImplementedError("Revision counter requires raft_store")
        return self._raft_store.increment_revision(zone_id)

    def get_revision(self, zone_id: str) -> int:
        """Get the current revision for a zone without incrementing.

        Args:
            zone_id: The zone to get revision for

        Returns:
            The current revision number (0 if not found)
        """
        if self._raft_store is None:
            return 0
        return self._raft_store.get_revision(zone_id)

    # =========================================================================
    # Extended Operations — rename, implicit directories
    # =========================================================================

    def rename_path(self, old_path: str, new_path: str) -> None:
        """Rename a file by updating its path in SQL.

        Args:
            old_path: Current path
            new_path: New path

        Raises:
            FileNotFoundError: If old_path doesn't exist
        """
        with self._session_factory() as session:
            existing = session.execute(
                select(FilePathModel).where(
                    FilePathModel.virtual_path == old_path,
                    FilePathModel.deleted_at.is_(None),
                )
            ).scalar_one_or_none()

            if existing is None:
                raise FileNotFoundError(f"No metadata found for {old_path}")

            session.execute(
                update(FilePathModel)
                .where(FilePathModel.path_id == existing.path_id)
                .values(
                    virtual_path=new_path,
                    updated_at=_utcnow_naive(),
                )
            )

            # Log operation
            self._log_operation(
                session,
                "rename",
                old_path,
                new_path=new_path,
                zone_id=existing.zone_id,
            )

            session.commit()

    def is_implicit_directory(self, path: str) -> bool:
        """Check if a path is an implicit directory.

        An implicit directory exists because files exist underneath it,
        even though the directory itself has no explicit metadata.

        Args:
            path: Virtual path to check

        Returns:
            True if path is an implicit directory, False otherwise
        """
        prefix = path if path.endswith("/") else path + "/"
        with self._session_factory() as session:
            row = session.execute(
                select(FilePathModel.path_id)
                .where(
                    FilePathModel.virtual_path.startswith(prefix),
                    FilePathModel.deleted_at.is_(None),
                )
                .limit(1)
            ).scalar_one_or_none()
            return row is not None

    # =========================================================================
    # Extended Metadata (delegate to raft_store — stored in redb)
    # =========================================================================

    def set_file_metadata(self, path: str, key: str, value: Any) -> None:
        """Store custom metadata key-value pair for a file."""
        if self._raft_store is None:
            raise NotImplementedError("Extended metadata requires raft_store")
        self._raft_store.set_file_metadata(path, key, value)

    def get_file_metadata(self, path: str, key: str) -> Any:
        """Get custom metadata value for a file."""
        if self._raft_store is None:
            return None
        return self._raft_store.get_file_metadata(path, key)

    def get_file_metadata_bulk(self, paths: Sequence[str], key: str) -> dict[str, Any]:
        """Get custom metadata value for multiple files."""
        if self._raft_store is None:
            return dict.fromkeys(paths)
        return self._raft_store.get_file_metadata_bulk(paths, key)

    def get_searchable_text(self, path: str) -> str | None:
        """Get cached searchable text for a file."""
        return self.get_file_metadata(path, "parsed_text")

    def get_searchable_text_bulk(self, paths: Sequence[str]) -> dict[str, str]:
        """Get cached searchable text for multiple files."""
        if self._raft_store is None:
            return {}
        return self._raft_store.get_searchable_text_bulk(paths)

    # =========================================================================
    # Lock Operations (delegate to raft_store — redb provides atomicity)
    # =========================================================================

    def acquire_lock(
        self,
        path: str,
        holder_id: str,
        max_holders: int = 1,
        ttl_secs: int = 30,
    ) -> bool:
        """Acquire a distributed lock on a path."""
        if self._raft_store is None:
            raise NotImplementedError("Lock operations require raft_store")
        return self._raft_store.acquire_lock(path, holder_id, max_holders, ttl_secs)

    def release_lock(self, path: str, holder_id: str) -> bool:
        """Release a distributed lock."""
        if self._raft_store is None:
            raise NotImplementedError("Lock operations require raft_store")
        return self._raft_store.release_lock(path, holder_id)

    def extend_lock(self, path: str, holder_id: str, ttl_secs: int = 30) -> bool:
        """Extend a lock's TTL (heartbeat)."""
        if self._raft_store is None:
            raise NotImplementedError("Lock operations require raft_store")
        return self._raft_store.extend_lock(path, holder_id, ttl_secs)

    def get_lock_info(self, path: str) -> dict[str, Any] | None:
        """Get lock information for a path."""
        if self._raft_store is None:
            return None
        return self._raft_store.get_lock_info(path)

    def list_locks(self, prefix: str = "", limit: int = 1000) -> list[dict[str, Any]]:
        """List all active locks matching a prefix."""
        if self._raft_store is None:
            return []
        return self._raft_store.list_locks(prefix, limit)

    def force_release_lock(self, path: str) -> bool:
        """Force-release all holders of a lock (admin operation)."""
        if self._raft_store is None:
            raise NotImplementedError("Lock operations require raft_store")
        return self._raft_store.force_release_lock(path)

    # =========================================================================
    # Async Methods (for RemoteNexusFS compatibility)
    # =========================================================================

    async def get_async(self, path: str) -> FileMetadata | None:
        """Get metadata (async — wraps sync for local SQL)."""
        return self.get(path)

    async def put_async(self, metadata: FileMetadata) -> None:
        """Store metadata (async — wraps sync for local SQL)."""
        self.put(metadata)

    async def delete_async(self, path: str) -> dict[str, Any] | None:
        """Delete metadata (async — wraps sync for local SQL)."""
        return self.delete(path)

    async def exists_async(self, path: str) -> bool:
        """Check existence (async — wraps sync for local SQL)."""
        return self.exists(path)

    async def list_async(
        self,
        prefix: str = "",
        recursive: bool = True,
        zone_id: str | None = None,
    ) -> list[FileMetadata]:
        """List metadata (async — wraps sync for local SQL)."""
        return self.list(prefix, recursive, zone_id=zone_id)

    async def acquire_lock_async(
        self,
        path: str,
        holder_id: str,
        max_holders: int = 1,
        ttl_secs: int = 30,
    ) -> bool:
        """Acquire lock (async — delegates to raft_store)."""
        if self._raft_store is None:
            raise NotImplementedError("Lock operations require raft_store")
        return await self._raft_store.acquire_lock_async(
            path, holder_id, max_holders, ttl_secs
        )

    async def release_lock_async(self, path: str, holder_id: str) -> bool:
        """Release lock (async — delegates to raft_store)."""
        if self._raft_store is None:
            raise NotImplementedError("Lock operations require raft_store")
        return await self._raft_store.release_lock_async(path, holder_id)

    async def extend_lock_async(
        self, path: str, holder_id: str, ttl_secs: int = 30
    ) -> bool:
        """Extend lock TTL (async — delegates to raft_store)."""
        if self._raft_store is None:
            raise NotImplementedError("Lock operations require raft_store")
        return await self._raft_store.extend_lock_async(path, holder_id, ttl_secs)

    async def close_async(self) -> None:
        """Close the metadata store (async)."""
        if self._raft_store is not None:
            await self._raft_store.close_async()

    # =========================================================================
    # Private Helpers
    # =========================================================================

    def _create_file(
        self,
        session: Any,
        metadata: FileMetadata,
        file_type: str | None,
    ) -> None:
        """Insert new FilePathModel + initial VersionHistoryModel + OperationLog."""
        # Remove any soft-deleted entries at this path
        deleted_entry = session.execute(
            select(FilePathModel).where(
                FilePathModel.virtual_path == metadata.path,
                FilePathModel.deleted_at.is_not(None),
            )
        ).scalar_one_or_none()
        if deleted_entry:
            session.delete(deleted_entry)
            session.flush()

        path_id = str(uuid.uuid4())
        now = _utcnow_naive()
        created = _to_naive(metadata.created_at) or now
        modified = _to_naive(metadata.modified_at) or now

        file_path = FilePathModel(
            path_id=path_id,
            virtual_path=metadata.path,
            backend_id=metadata.backend_name or "local",
            physical_path=metadata.physical_path or metadata.path,
            size_bytes=metadata.size or 0,
            content_hash=metadata.etag,
            file_type=file_type,
            created_at=created,
            updated_at=modified,
            current_version=1,
            zone_id=metadata.zone_id or "default",
            posix_uid=metadata.owner_id,
        )
        session.add(file_path)
        session.flush()

        # Create initial version history entry
        if metadata.etag is not None:
            version_entry = VersionHistoryModel(
                version_id=str(uuid.uuid4()),
                resource_type="file",
                resource_id=path_id,
                version_number=1,
                content_hash=metadata.etag,
                size_bytes=metadata.size or 0,
                mime_type=file_type if file_type != _DIR_FILE_TYPE else None,
                parent_version_id=None,
                source_type="original",
                created_at=created,
                created_by=metadata.created_by,
            )
            session.add(version_entry)

        # Log operation
        self._log_operation(
            session,
            "write",
            metadata.path,
            zone_id=metadata.zone_id,
            snapshot_hash=metadata.etag,
        )

    def _update_file(
        self,
        session: Any,
        existing: FilePathModel,
        metadata: FileMetadata,
        file_type: str | None,
    ) -> None:
        """Update existing FilePathModel + append VersionHistoryModel + OperationLog."""
        now = _utcnow_naive()
        modified = _to_naive(metadata.modified_at) or now

        if metadata.etag is not None:
            # Get previous version for lineage
            prev_version = session.execute(
                select(VersionHistoryModel)
                .where(
                    VersionHistoryModel.resource_type == "file",
                    VersionHistoryModel.resource_id == existing.path_id,
                    VersionHistoryModel.version_number == existing.current_version,
                )
                .limit(1)
            ).scalar_one_or_none()

            # Atomically increment version
            update_result = session.execute(
                update(FilePathModel)
                .where(FilePathModel.path_id == existing.path_id)
                .values(
                    backend_id=metadata.backend_name or existing.backend_id,
                    physical_path=metadata.physical_path or existing.physical_path,
                    size_bytes=metadata.size or 0,
                    content_hash=metadata.etag,
                    file_type=file_type,
                    updated_at=modified,
                    current_version=FilePathModel.current_version + 1,
                    posix_uid=metadata.owner_id or existing.posix_uid,
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
                mime_type=file_type if file_type != _DIR_FILE_TYPE else None,
                parent_version_id=prev_version.version_id if prev_version else None,
                source_type="original",
                created_at=now,
                created_by=metadata.created_by,
            )
            session.add(version_entry)
        else:
            # No content hash — update metadata only
            session.execute(
                update(FilePathModel)
                .where(FilePathModel.path_id == existing.path_id)
                .values(
                    backend_id=metadata.backend_name or existing.backend_id,
                    physical_path=metadata.physical_path or existing.physical_path,
                    size_bytes=metadata.size or 0,
                    content_hash=metadata.etag,
                    file_type=file_type,
                    updated_at=modified,
                    posix_uid=metadata.owner_id or existing.posix_uid,
                )
            )

        # Log operation
        self._log_operation(
            session,
            "write",
            metadata.path,
            zone_id=metadata.zone_id,
            snapshot_hash=metadata.etag,
        )

    def _log_operation(
        self,
        session: Any,
        operation_type: str,
        path: str,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        new_path: str | None = None,
        snapshot_hash: str | None = None,
    ) -> None:
        """Log an operation to the OperationLog table."""
        operation = OperationLogModel(
            operation_type=operation_type,
            path=path,
            zone_id=zone_id or "default",
            agent_id=agent_id,
            new_path=new_path,
            snapshot_hash=snapshot_hash,
            status="success",
            created_at=datetime.now(UTC),
        )
        operation.validate()
        session.add(operation)
