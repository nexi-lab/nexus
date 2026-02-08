"""Async SQLAlchemy-based metadata store implementation for Nexus (Issue #940).

Native async metadata store using SQLAlchemy async support for:
- Non-blocking database operations (10-100x throughput under concurrent load)
- No thread pool exhaustion under high concurrency
- Seamless integration with FastAPI's async endpoints

Performance benefits:
- Memory per connection: ~10KB (coroutine) vs ~1MB (thread stack)
- Max concurrent ops: 10,000+ (async) vs 40-200 (thread limit)
- No cold cache thundering herd (all requests run in parallel)

Reference implementation patterns from:
- AsyncReBACManager (src/nexus/core/async_rebac_manager.py)
- AsyncSemanticSearch (src/nexus/search/async_search.py)

Example:
    from sqlalchemy.ext.asyncio import create_async_engine
    from nexus.storage.async_metadata_store import AsyncSQLAlchemyMetadataStore

    # Create async engine
    engine = create_async_engine("postgresql+asyncpg://user:pass@host/db")

    # Initialize store
    store = AsyncSQLAlchemyMetadataStore(engine)

    # Use async methods
    metadata = await store.aget("/path/to/file.txt")
    await store.aput(file_metadata)
    files = await store.alist("/workspace/")
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from nexus.core._metadata_generated import FileMetadata, PaginatedResult
from nexus.core.exceptions import MetadataError
from nexus.storage.cache import _CACHE_MISS, MetadataCache
from nexus.storage.models import (
    FilePathModel,
    VersionHistoryModel,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# =============================================================================
# Prepared Statement Queries (Module-level for statement cache reuse)
# =============================================================================
# These queries are defined at module level so the same text() objects are
# reused across all calls. This enables asyncpg's prepared statement cache
# to efficiently cache the parsed query plans.
#
# Performance impact: 2-10x faster for hot path queries by avoiding:
# - SQL parsing overhead on each call
# - Query plan generation
# - Parameter type inference
# =============================================================================

# Hot path: Get file by path (excluding soft-deleted)
_QUERY_GET_BY_PATH = text("""
    SELECT path_id, zone_id, virtual_path, backend_id, physical_path,
           file_type, size_bytes, content_hash, created_at, updated_at,
           current_version, posix_uid
    FROM file_paths
    WHERE virtual_path = :path AND deleted_at IS NULL
""")

# Hot path: Check existence
_QUERY_EXISTS = text("""
    SELECT 1 FROM file_paths
    WHERE virtual_path = :path AND deleted_at IS NULL
    LIMIT 1
""")

# Soft delete
_QUERY_DELETE = text("""
    UPDATE file_paths
    SET deleted_at = :deleted_at
    WHERE virtual_path = :path AND deleted_at IS NULL
    RETURNING path_id, content_hash, size_bytes, current_version,
              backend_id, physical_path, zone_id
""")

# Insert new directory entry
_QUERY_INSERT_DIR_ENTRY_SQLITE = text("""
    INSERT OR IGNORE INTO directory_entries
        (zone_id, parent_path, entry_name, entry_type, created_at, updated_at)
    VALUES
        (:zone_id, :parent_path, :entry_name, :entry_type, :created_at, :updated_at)
""")

_QUERY_INSERT_DIR_ENTRY_POSTGRES = text("""
    INSERT INTO directory_entries
        (zone_id, parent_path, entry_name, entry_type, created_at, updated_at)
    VALUES
        (:zone_id, :parent_path, :entry_name, :entry_type, :created_at, :updated_at)
    ON CONFLICT (zone_id, parent_path, entry_name) DO NOTHING
""")

# Delete directory entry
_QUERY_DELETE_DIR_ENTRY = text("""
    DELETE FROM directory_entries
    WHERE zone_id = :zone_id AND parent_path = :parent_path AND entry_name = :entry_name
""")


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Ensure datetime is UTC-aware.

    SQLite stores datetimes without timezone info. When retrieving,
    we assume they're UTC and make them timezone-aware for consistent
    comparisons.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _to_naive_utc(dt: datetime | None) -> datetime | None:
    """Convert datetime to naive UTC (for asyncpg compatibility).

    asyncpg is strict about timezone-aware vs naive datetimes when
    the PostgreSQL column type is TIMESTAMP (without time zone).
    This converts aware datetimes to naive UTC.

    Args:
        dt: Datetime that may be timezone-aware or naive

    Returns:
        Naive datetime in UTC, or None if input is None
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        # Convert to UTC then strip timezone
        utc_dt = dt.astimezone(UTC)
        return utc_dt.replace(tzinfo=None)
    return dt


def _utcnow_naive() -> datetime:
    """Get current UTC time as naive datetime (for asyncpg compatibility)."""
    return datetime.now(UTC).replace(tzinfo=None)


class AsyncSQLAlchemyMetadataStore:
    """Async SQLAlchemy-based metadata store for file metadata operations.

    Provides non-blocking database operations using async SQLAlchemy with
    asyncpg (PostgreSQL) or aiosqlite (SQLite).

    Key methods (all async):
    - aget(): Get file metadata
    - aput(): Store file metadata
    - adelete(): Soft-delete file metadata
    - alist(): List files by prefix
    - aexists(): Check file existence
    - alist_paginated(): Paginated file listing
    """

    def __init__(
        self,
        engine: AsyncEngine,
        enable_cache: bool = True,
        cache_path_size: int = 512,
        cache_list_size: int = 1024,
        cache_kv_size: int = 256,
        cache_exists_size: int = 1024,
        cache_ttl_seconds: int | None = 300,
    ):
        """Initialize async metadata store.

        Args:
            engine: SQLAlchemy AsyncEngine (created with create_async_engine)
            enable_cache: Enable in-memory L1 cache (default: True)
            cache_path_size: Max entries for path metadata cache (default: 512)
            cache_list_size: Max entries for directory listing cache (default: 1024)
            cache_kv_size: Max entries for file metadata KV cache (default: 256)
            cache_exists_size: Max entries for existence check cache (default: 1024)
            cache_ttl_seconds: Cache TTL in seconds, None = no expiry (default: 300)
        """
        self.engine = engine
        self.db_type = self._detect_db_type()

        # Create async session factory with expire_on_commit=False
        # (recommended for async to avoid lazy loading issues)
        self.async_session = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        # Initialize L1 in-memory cache (thread-safe dict, used from async context)
        self._cache_enabled = enable_cache
        self._cache: MetadataCache | None = None
        if enable_cache:
            self._cache = MetadataCache(
                path_cache_size=cache_path_size,
                list_cache_size=cache_list_size,
                kv_cache_size=cache_kv_size,
                exists_cache_size=cache_exists_size,
                ttl_seconds=cache_ttl_seconds,
            )
            logger.info(
                f"Async metadata store L1 cache enabled: "
                f"path={cache_path_size}, list={cache_list_size}, ttl={cache_ttl_seconds}s"
            )

    def _detect_db_type(self) -> str:
        """Detect database type from engine URL."""
        url_str = str(self.engine.url)
        if "sqlite" in url_str:
            return "sqlite"
        elif "postgres" in url_str:
            return "postgresql"
        return "unknown"

    @asynccontextmanager
    async def _session(self) -> Any:
        """Get async database session with cancellation-safe cleanup.

        Uses asyncio.shield() to protect cleanup from task cancellation,
        preventing connection leaks when queries are interrupted by timeouts.

        Usage:
            async with self._session() as session:
                result = await session.execute(...)

        Reference: https://medium.com/@har.avetisyan2002/how-we-discovered-and-fixed-a-connection-leak-in-async-sqlalchemy-during-chaos-testing-bf45acf65559
        """
        async with self.async_session() as session:
            try:
                yield session
            finally:
                # Shield cleanup from cancellation to prevent connection leaks
                await asyncio.shield(session.close())

    async def aget(self, path: str) -> FileMetadata | None:
        """Get file metadata asynchronously.

        Args:
            path: Virtual path

        Returns:
            FileMetadata if found, None otherwise
        """
        # Check cache first
        if self._cache_enabled and self._cache:
            cached = self._cache.get_path(path)
            if cached is not _CACHE_MISS:
                return cached if isinstance(cached, FileMetadata) or cached is None else None

        try:
            async with self._session() as session:
                # Use ORM select for proper type conversion (especially datetime)
                stmt = select(FilePathModel).where(
                    FilePathModel.virtual_path == path,
                    FilePathModel.deleted_at.is_(None),
                )
                result = await session.execute(stmt)
                file_path = result.scalar_one_or_none()

                if file_path is None:
                    # Cache negative result
                    if self._cache_enabled and self._cache:
                        self._cache.set_path(path, None)
                    return None

                metadata = FileMetadata(
                    path=file_path.virtual_path,
                    backend_name=file_path.backend_id,
                    physical_path=file_path.physical_path,
                    size=file_path.size_bytes,
                    etag=file_path.content_hash,
                    mime_type=file_path.file_type,
                    created_at=_ensure_utc(file_path.created_at),
                    modified_at=_ensure_utc(file_path.updated_at),
                    version=file_path.current_version,
                    zone_id=file_path.zone_id,
                    owner_id=file_path.posix_uid,
                )

                # Cache result
                if self._cache_enabled and self._cache:
                    self._cache.set_path(path, metadata)

                return metadata

        except Exception as e:
            raise MetadataError(f"Failed to get metadata: {e}", path=path) from e

    async def aput(self, metadata: FileMetadata) -> None:
        """Store or update file metadata asynchronously with version tracking.

        When updating an existing file, creates a version history entry
        preserving the old content hash before updating to new content.

        Args:
            metadata: File metadata to store
        """
        # Validate BEFORE database operation
        metadata.validate()

        try:
            async with self._session() as session:
                # Check if file already exists
                result = await session.execute(
                    select(FilePathModel).where(
                        FilePathModel.virtual_path == metadata.path,
                        FilePathModel.deleted_at.is_(None),
                    )
                )
                existing = result.scalar_one_or_none()

                # Handle soft-deleted entries at this path
                if not existing:
                    result = await session.execute(
                        select(FilePathModel).where(
                            FilePathModel.virtual_path == metadata.path,
                            FilePathModel.deleted_at.is_not(None),
                        )
                    )
                    deleted_entry = result.scalar_one_or_none()
                    if deleted_entry:
                        await session.delete(deleted_entry)
                        await session.flush()

                if existing:
                    # UPDATE existing file - use explicit UPDATE to avoid ORM datetime issues
                    new_updated_at = _to_naive_utc(metadata.modified_at) or _utcnow_naive()

                    # Only create version history if we have content
                    if metadata.etag is not None:
                        # Get previous version for lineage
                        prev_result = await session.execute(
                            select(VersionHistoryModel)
                            .where(
                                VersionHistoryModel.resource_type == "file",
                                VersionHistoryModel.resource_id == existing.path_id,
                                VersionHistoryModel.version_number == existing.current_version,
                            )
                            .limit(1)
                        )
                        prev_version = prev_result.scalar_one_or_none()

                        # Atomically update file and increment version using explicit UPDATE
                        # This avoids ORM dirty-tracking issues with datetime timezone handling
                        update_result = await session.execute(
                            update(FilePathModel)
                            .where(FilePathModel.path_id == existing.path_id)
                            .values(
                                backend_id=metadata.backend_name,
                                physical_path=metadata.physical_path,
                                size_bytes=metadata.size,
                                content_hash=metadata.etag,
                                file_type=metadata.mime_type,
                                updated_at=new_updated_at,
                                current_version=FilePathModel.current_version + 1,
                            )
                            .returning(FilePathModel.current_version)
                        )
                        new_version = update_result.scalar_one()

                        # Create version history entry
                        version_entry = VersionHistoryModel(
                            version_id=str(uuid.uuid4()),
                            resource_type="file",
                            resource_id=existing.path_id,
                            version_number=new_version,
                            content_hash=metadata.etag,
                            size_bytes=metadata.size,
                            mime_type=metadata.mime_type,
                            parent_version_id=prev_version.version_id if prev_version else None,
                            source_type="original",
                            created_at=_utcnow_naive(),
                            created_by=metadata.created_by,
                        )
                        version_entry.validate()
                        session.add(version_entry)
                    else:
                        # No content hash - just update metadata without version increment
                        await session.execute(
                            update(FilePathModel)
                            .where(FilePathModel.path_id == existing.path_id)
                            .values(
                                backend_id=metadata.backend_name,
                                physical_path=metadata.physical_path,
                                size_bytes=metadata.size,
                                content_hash=metadata.etag,
                                file_type=metadata.mime_type,
                                updated_at=new_updated_at,
                            )
                        )

                    # Expire the ORM object to avoid stale data issues
                    await session.refresh(existing)
                else:
                    # CREATE new file
                    file_path = FilePathModel(
                        path_id=str(uuid.uuid4()),
                        virtual_path=metadata.path,
                        backend_id=metadata.backend_name,
                        physical_path=metadata.physical_path,
                        size_bytes=metadata.size,
                        content_hash=metadata.etag,
                        file_type=metadata.mime_type,
                        created_at=_to_naive_utc(metadata.created_at) or _utcnow_naive(),
                        updated_at=_to_naive_utc(metadata.modified_at) or _utcnow_naive(),
                        current_version=1,
                        zone_id=metadata.zone_id or "default",
                        posix_uid=metadata.owner_id,
                    )
                    file_path.validate()
                    session.add(file_path)
                    await session.flush()

                    # Create initial version history
                    if metadata.etag is not None:
                        version_entry = VersionHistoryModel(
                            version_id=str(uuid.uuid4()),
                            resource_type="file",
                            resource_id=file_path.path_id,
                            version_number=1,
                            content_hash=metadata.etag,
                            size_bytes=metadata.size,
                            mime_type=metadata.mime_type,
                            parent_version_id=None,
                            source_type="original",
                            created_at=file_path.created_at,
                            created_by=metadata.created_by,
                        )
                        version_entry.validate()
                        session.add(version_entry)

                # Update directory index (Issue #924)
                await self._aupdate_directory_index(
                    session, metadata.path, metadata.zone_id or "default", is_directory=False
                )

                await session.commit()

            # Invalidate cache
            if self._cache_enabled and self._cache:
                self._cache.invalidate_path(metadata.path)
                # Parent directory caches are invalidated by invalidate_path

        except MetadataError:
            raise
        except Exception as e:
            raise MetadataError(f"Failed to store metadata: {e}", path=metadata.path) from e

    async def adelete(self, path: str) -> dict[str, Any] | None:
        """Soft-delete file metadata asynchronously.

        Args:
            path: Virtual path

        Returns:
            Dictionary with deleted file info or None if not found.
        """
        deleted_info: dict[str, Any] | None = None

        try:
            async with self._session() as session:
                deleted_at = _utcnow_naive()

                result = await session.execute(
                    _QUERY_DELETE,
                    {"path": path, "deleted_at": deleted_at},
                )
                row = result.first()

                if row:
                    deleted_info = {
                        "path_id": row.path_id,
                        "content_hash": row.content_hash,
                        "size_bytes": row.size_bytes,
                        "version": row.current_version,
                        "backend_id": row.backend_id,
                        "physical_path": row.physical_path,
                        "deleted_at": deleted_at,
                        "zone_id": row.zone_id,
                    }

                    # Remove from directory index
                    await self._aremove_from_directory_index(session, path, row.zone_id)

                await session.commit()

            # Invalidate cache
            if self._cache_enabled and self._cache:
                self._cache.invalidate_path(path)

            return deleted_info

        except Exception as e:
            raise MetadataError(f"Failed to delete metadata: {e}", path=path) from e

    async def aexists(self, path: str) -> bool:
        """Check if file metadata exists asynchronously.

        Args:
            path: Virtual path

        Returns:
            True if exists, False otherwise
        """
        # Check cache first
        if self._cache_enabled and self._cache:
            cached = self._cache.get_exists(path)
            if cached is not None:
                return cached

        try:
            async with self._session() as session:
                result = await session.execute(
                    _QUERY_EXISTS,
                    {"path": path},
                )
                exists = result.first() is not None

                # Cache result
                if self._cache_enabled and self._cache:
                    self._cache.set_exists(path, exists)

                return exists

        except Exception as e:
            raise MetadataError(f"Failed to check existence: {e}", path=path) from e

    async def alist(
        self,
        prefix: str = "",
        recursive: bool = True,
        zone_id: str | None = None,
        accessible_paths: set[str] | None = None,
    ) -> list[FileMetadata]:
        """List all files with given path prefix asynchronously.

        Args:
            prefix: Path prefix to filter by
            recursive: If True, include nested files. If False, only direct children.
            zone_id: Optional tenant ID filter (PREWHERE optimization)
            accessible_paths: Optional set of paths for predicate pushdown

        Returns:
            List of file metadata
        """
        # Skip caching when accessible_paths is provided (results vary per user)
        tenant_key = zone_id or "all"
        cache_key = f"{prefix}:{'r' if recursive else 'nr'}:t={tenant_key}"
        use_cache = self._cache_enabled and self._cache and accessible_paths is None

        if use_cache and self._cache:
            cached = self._cache.get_list(cache_key)
            if cached is not None:
                return cached

        try:
            async with self._session() as session:
                # Build query with ORM for better maintainability
                from sqlalchemy import or_

                conditions: list[Any] = [FilePathModel.deleted_at.is_(None)]

                # Tenant filtering (Issue #904)
                if zone_id is not None:
                    conditions.append(
                        or_(
                            FilePathModel.zone_id == zone_id,
                            FilePathModel.zone_id == "default",
                            FilePathModel.zone_id.is_(None),
                        )
                    )

                # Predicate pushdown (Issue #1030)
                if accessible_paths is not None:
                    if len(accessible_paths) == 0:
                        return []
                    conditions.append(FilePathModel.virtual_path.in_(accessible_paths))

                # Build SELECT statement
                if prefix:
                    if recursive:
                        stmt = (
                            select(FilePathModel)
                            .where(
                                FilePathModel.virtual_path.like(f"{prefix}%"),
                                *conditions,
                            )
                            .order_by(FilePathModel.virtual_path)
                        )
                    else:
                        # Non-recursive: only direct children
                        stmt = (
                            select(FilePathModel)
                            .where(
                                FilePathModel.virtual_path.like(f"{prefix}%"),
                                ~FilePathModel.virtual_path.like(f"{prefix}%/%"),
                                *conditions,
                            )
                            .order_by(FilePathModel.virtual_path)
                        )
                else:
                    if recursive:
                        stmt = (
                            select(FilePathModel)
                            .where(*conditions)
                            .order_by(FilePathModel.virtual_path)
                        )
                    else:
                        stmt = (
                            select(FilePathModel)
                            .where(
                                FilePathModel.virtual_path.like("/%"),
                                ~FilePathModel.virtual_path.like("/%/%"),
                                *conditions,
                            )
                            .order_by(FilePathModel.virtual_path)
                        )

                result = await session.execute(stmt)
                results = [
                    FileMetadata(
                        path=fp.virtual_path,
                        backend_name=fp.backend_id,
                        physical_path=fp.physical_path,
                        size=fp.size_bytes,
                        etag=fp.content_hash,
                        mime_type=fp.file_type,
                        created_at=_ensure_utc(fp.created_at),
                        modified_at=_ensure_utc(fp.updated_at),
                        version=fp.current_version,
                        zone_id=fp.zone_id,
                    )
                    for fp in result.scalars()
                ]

                # Cache results
                if use_cache and self._cache:
                    self._cache.set_list(cache_key, results)

                return results

        except Exception as e:
            raise MetadataError(f"Failed to list metadata: {e}") from e

    async def alist_paginated(
        self,
        prefix: str = "",
        limit: int = 100,
        cursor: str | None = None,
        recursive: bool = True,
        zone_id: str | None = None,
    ) -> PaginatedResult:
        """List files with pagination support asynchronously.

        Args:
            prefix: Path prefix to filter by
            limit: Maximum items per page
            cursor: Cursor from previous page (path to start after)
            recursive: If True, include nested files
            zone_id: Optional tenant ID filter

        Returns:
            PaginatedResult with items, cursor, and has_more flag
        """
        try:
            async with self._session() as session:
                from sqlalchemy import or_

                conditions: list[Any] = [FilePathModel.deleted_at.is_(None)]

                if zone_id is not None:
                    conditions.append(
                        or_(
                            FilePathModel.zone_id == zone_id,
                            FilePathModel.zone_id == "default",
                            FilePathModel.zone_id.is_(None),
                        )
                    )

                # Build base query
                if prefix:
                    if recursive:
                        stmt = select(FilePathModel).where(
                            FilePathModel.virtual_path.like(f"{prefix}%"),
                            *conditions,
                        )
                    else:
                        stmt = select(FilePathModel).where(
                            FilePathModel.virtual_path.like(f"{prefix}%"),
                            ~FilePathModel.virtual_path.like(f"{prefix}%/%"),
                            *conditions,
                        )
                else:
                    if recursive:
                        stmt = select(FilePathModel).where(*conditions)
                    else:
                        stmt = select(FilePathModel).where(
                            FilePathModel.virtual_path.like("/%"),
                            ~FilePathModel.virtual_path.like("/%/%"),
                            *conditions,
                        )

                # Apply cursor (pagination)
                if cursor:
                    stmt = stmt.where(FilePathModel.virtual_path > cursor)

                # Order and limit (+1 to check for more)
                stmt = stmt.order_by(FilePathModel.virtual_path).limit(limit + 1)

                result = await session.execute(stmt)
                rows = list(result.scalars())

                # Check if there are more results
                has_more = len(rows) > limit
                if has_more:
                    rows = rows[:limit]

                items = [
                    FileMetadata(
                        path=fp.virtual_path,
                        backend_name=fp.backend_id,
                        physical_path=fp.physical_path,
                        size=fp.size_bytes,
                        etag=fp.content_hash,
                        mime_type=fp.file_type,
                        created_at=_ensure_utc(fp.created_at),
                        modified_at=_ensure_utc(fp.updated_at),
                        version=fp.current_version,
                        zone_id=fp.zone_id,
                    )
                    for fp in rows
                ]

                # Next cursor is the last item's path
                next_cursor = items[-1].path if items and has_more else None

                return PaginatedResult(
                    items=items,
                    next_cursor=next_cursor,
                    has_more=has_more,
                )

        except Exception as e:
            raise MetadataError(f"Failed to list paginated metadata: {e}") from e

    async def _aupdate_directory_index(
        self,
        session: AsyncSession,
        path: str,
        zone_id: str,
        is_directory: bool = False,
    ) -> None:
        """Update directory index entries for a path (Issue #924).

        Creates entries for the file and all parent directories.
        """
        if not path or path == "/":
            return

        # Parse path to get parent and entry name
        path = path.rstrip("/")
        parts = path.split("/")
        entry_name = parts[-1]
        parent_path = "/".join(parts[:-1]) + "/" if len(parts) > 1 else "/"

        now = _utcnow_naive()
        query = (
            _QUERY_INSERT_DIR_ENTRY_POSTGRES
            if self.db_type == "postgresql"
            else _QUERY_INSERT_DIR_ENTRY_SQLITE
        )

        # Insert entry for this file/directory
        await session.execute(
            query,
            {
                "zone_id": zone_id,
                "parent_path": parent_path,
                "entry_name": entry_name,
                "entry_type": "directory" if is_directory else "file",
                "created_at": now,
                "updated_at": now,
            },
        )

        # Recursively ensure parent directories exist in index
        if parent_path != "/":
            await self._aupdate_directory_index(
                session, parent_path.rstrip("/"), zone_id, is_directory=True
            )

    async def _aremove_from_directory_index(
        self,
        session: AsyncSession,
        path: str,
        zone_id: str,
    ) -> None:
        """Remove a file from the directory index."""
        if not path or path == "/":
            return

        path = path.rstrip("/")
        parts = path.split("/")
        entry_name = parts[-1]
        parent_path = "/".join(parts[:-1]) + "/" if len(parts) > 1 else "/"

        await session.execute(
            _QUERY_DELETE_DIR_ENTRY,
            {
                "zone_id": zone_id,
                "parent_path": parent_path,
                "entry_name": entry_name,
            },
        )
