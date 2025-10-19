"""SQLAlchemy-based metadata store implementation for Nexus.

Production-ready metadata store using SQLAlchemy ORM with support for:
- File path mapping (virtual path → physical backend path)
- File metadata (arbitrary key-value pairs)
- Content chunks (for deduplication)
"""

from __future__ import annotations

import builtins
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from nexus.core.exceptions import MetadataError
from nexus.core.metadata import FileMetadata, MetadataStore
from nexus.storage.cache import _CACHE_MISS, MetadataCache
from nexus.storage.models import Base, FileMetadataModel, FilePathModel


class SQLAlchemyMetadataStore(MetadataStore):
    """
    SQLAlchemy-based metadata store for embedded mode.

    Uses SQLAlchemy ORM for database operations with support for:
    - File path mapping (virtual path -> physical backend path)
    - File metadata (arbitrary key-value pairs)
    - Content chunks (for deduplication)
    """

    def __init__(
        self,
        db_path: str | Path,
        run_migrations: bool = False,
        enable_cache: bool = True,
        cache_path_size: int = 512,
        cache_list_size: int = 128,
        cache_kv_size: int = 256,
        cache_exists_size: int = 1024,
        cache_ttl_seconds: int | None = 300,
    ):
        """
        Initialize SQLAlchemy metadata store.

        Args:
            db_path: Path to SQLite database file
            run_migrations: If True, run Alembic migrations on startup (default: False)
            enable_cache: If True, enable in-memory caching (default: True)
            cache_path_size: Max entries for path metadata cache (default: 512)
            cache_list_size: Max entries for directory listing cache (default: 128)
            cache_kv_size: Max entries for file metadata KV cache (default: 256)
            cache_exists_size: Max entries for existence check cache (default: 1024)
            cache_ttl_seconds: Cache TTL in seconds, None = no expiry (default: 300)
        """
        self.db_path = Path(db_path)
        self._ensure_parent_exists()

        # Create engine and session factory
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            # Enable connection pooling for better concurrency
            pool_pre_ping=True,
            # Use NullPool for SQLite to avoid concurrency issues
            poolclass=None,
        )

        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

        # Initialize cache
        self._cache_enabled = enable_cache
        self._cache: MetadataCache | None
        if enable_cache:
            self._cache = MetadataCache(
                path_cache_size=cache_path_size,
                list_cache_size=cache_list_size,
                kv_cache_size=cache_kv_size,
                exists_cache_size=cache_exists_size,
                ttl_seconds=cache_ttl_seconds,
            )
        else:
            self._cache = None

        # Initialize schema
        if run_migrations:
            self._run_migrations()
        else:
            # Create tables if they don't exist
            Base.metadata.create_all(self.engine)
            # Create SQL views for work detection
            self._create_views()

        # Enable WAL mode for better concurrency and to avoid journal files
        try:
            with self.engine.connect() as conn:
                conn.execute(text("PRAGMA journal_mode=WAL"))
                conn.commit()
        except Exception:
            # Ignore if WAL mode cannot be enabled
            pass

    def _ensure_parent_exists(self) -> None:
        """Create parent directory for database if it doesn't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _run_migrations(self) -> None:
        """Run Alembic migrations to create/update schema."""
        try:
            from alembic.config import Config

            from alembic import command

            # Configure Alembic
            alembic_cfg = Config("alembic.ini")
            alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{self.db_path}")

            # Run migrations
            command.upgrade(alembic_cfg, "head")
        except Exception as e:
            raise MetadataError(f"Failed to run migrations: {e}") from e

    def _create_views(self) -> None:
        """Create SQL views for work detection if they don't exist."""
        try:
            from nexus.storage import views

            with self.engine.connect() as conn:
                for _name, view_sql in views.ALL_VIEWS:
                    conn.execute(view_sql)
                    conn.commit()
        except Exception:
            # Views might already exist, which is fine
            # Log or ignore the error
            pass

    def get(self, path: str) -> FileMetadata | None:
        """
        Get metadata for a file.

        Args:
            path: Virtual path

        Returns:
            FileMetadata if found, None otherwise
        """
        # Check cache first
        if self._cache_enabled and self._cache:
            cached = self._cache.get_path(path)
            if cached is not _CACHE_MISS:
                # Type narrowing: we know it's FileMetadata | None here
                return cached if isinstance(cached, FileMetadata) or cached is None else None

        try:
            with self.SessionLocal() as session:
                stmt = select(FilePathModel).where(
                    FilePathModel.virtual_path == path, FilePathModel.deleted_at.is_(None)
                )
                file_path = session.scalar(stmt)

                if file_path is None:
                    # Cache the negative result
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
                    created_at=file_path.created_at,
                    modified_at=file_path.updated_at,
                    version=1,  # Not tracking versions yet in simplified schema
                    # UNIX-style permissions (v0.3.0)
                    owner=file_path.owner,
                    group=file_path.group,
                    mode=file_path.mode,
                )

                # Cache the result
                if self._cache_enabled and self._cache:
                    self._cache.set_path(path, metadata)

                return metadata
        except Exception as e:
            raise MetadataError(f"Failed to get metadata: {e}", path=path) from e

    def put(self, metadata: FileMetadata) -> None:
        """
        Store or update file metadata.

        Args:
            metadata: File metadata to store
        """
        # Validate BEFORE database operation
        metadata.validate()

        try:
            with self.SessionLocal() as session:
                # Check if file path already exists
                stmt = select(FilePathModel).where(
                    FilePathModel.virtual_path == metadata.path,
                    FilePathModel.deleted_at.is_(None),
                )
                existing = session.scalar(stmt)

                if existing:
                    # Update existing record
                    existing.backend_id = metadata.backend_name
                    existing.physical_path = metadata.physical_path
                    existing.size_bytes = metadata.size
                    existing.content_hash = metadata.etag
                    existing.file_type = metadata.mime_type
                    existing.updated_at = metadata.modified_at or datetime.now(UTC)
                    # Update permissions (v0.3.0)
                    existing.owner = metadata.owner
                    existing.group = metadata.group
                    existing.mode = metadata.mode
                else:
                    # Create new record
                    file_path = FilePathModel(
                        path_id=str(uuid.uuid4()),
                        tenant_id=str(uuid.uuid4()),  # Default tenant for embedded mode
                        virtual_path=metadata.path,
                        backend_id=metadata.backend_name,
                        physical_path=metadata.physical_path,
                        size_bytes=metadata.size,
                        content_hash=metadata.etag,
                        file_type=metadata.mime_type,
                        created_at=metadata.created_at or datetime.now(UTC),
                        updated_at=metadata.modified_at or datetime.now(UTC),
                        # UNIX-style permissions (v0.3.0)
                        owner=metadata.owner,
                        group=metadata.group,
                        mode=metadata.mode,
                    )
                    # Validate model before inserting
                    file_path.validate()
                    session.add(file_path)

                session.commit()

            # Invalidate cache for this path
            if self._cache_enabled and self._cache:
                self._cache.invalidate_path(metadata.path)
        except Exception as e:
            raise MetadataError(f"Failed to store metadata: {e}", path=metadata.path) from e

    def delete(self, path: str) -> None:
        """
        Delete file metadata (soft delete).

        Args:
            path: Virtual path
        """
        try:
            with self.SessionLocal() as session:
                stmt = select(FilePathModel).where(
                    FilePathModel.virtual_path == path, FilePathModel.deleted_at.is_(None)
                )
                file_path = session.scalar(stmt)

                if file_path:
                    # Soft delete
                    file_path.deleted_at = datetime.now(UTC)
                    session.commit()

            # Invalidate cache for this path
            if self._cache_enabled and self._cache:
                self._cache.invalidate_path(path)
        except Exception as e:
            raise MetadataError(f"Failed to delete metadata: {e}", path=path) from e

    def exists(self, path: str) -> bool:
        """
        Check if metadata exists for a path.

        Args:
            path: Virtual path

        Returns:
            True if metadata exists, False otherwise
        """
        # Check cache first
        if self._cache_enabled and self._cache:
            cached = self._cache.get_exists(path)
            if cached is not None:
                return cached

        try:
            with self.SessionLocal() as session:
                stmt = select(FilePathModel.path_id).where(
                    FilePathModel.virtual_path == path, FilePathModel.deleted_at.is_(None)
                )
                exists = session.scalar(stmt) is not None

                # Cache the result
                if self._cache_enabled and self._cache:
                    self._cache.set_exists(path, exists)

                return exists
        except Exception as e:
            raise MetadataError(f"Failed to check existence: {e}", path=path) from e

    def list(self, prefix: str = "") -> list[FileMetadata]:
        """
        List all files with given path prefix.

        Args:
            prefix: Path prefix to filter by

        Returns:
            List of file metadata
        """
        # Check cache first
        if self._cache_enabled and self._cache:
            cached = self._cache.get_list(prefix)
            if cached is not None:
                return cached

        try:
            with self.SessionLocal() as session:
                if prefix:
                    stmt = (
                        select(FilePathModel)
                        .where(
                            FilePathModel.virtual_path.like(f"{prefix}%"),
                            FilePathModel.deleted_at.is_(None),
                        )
                        .order_by(FilePathModel.virtual_path)
                    )
                else:
                    stmt = (
                        select(FilePathModel)
                        .where(FilePathModel.deleted_at.is_(None))
                        .order_by(FilePathModel.virtual_path)
                    )

                results = []
                for file_path in session.scalars(stmt):
                    results.append(
                        FileMetadata(
                            path=file_path.virtual_path,
                            backend_name=file_path.backend_id,
                            physical_path=file_path.physical_path,
                            size=file_path.size_bytes,
                            etag=file_path.content_hash,
                            mime_type=file_path.file_type,
                            created_at=file_path.created_at,
                            modified_at=file_path.updated_at,
                            version=1,
                            # UNIX-style permissions (v0.3.0)
                            owner=file_path.owner,
                            group=file_path.group,
                            mode=file_path.mode,
                        )
                    )

                # Cache the results
                if self._cache_enabled and self._cache:
                    self._cache.set_list(prefix, results)

                return results
        except Exception as e:
            raise MetadataError(f"Failed to list metadata: {e}") from e

    def close(self) -> None:
        """Close database connection and dispose of engine."""
        if hasattr(self, "engine"):
            # For SQLite, checkpoint WAL/journal files before disposing
            try:
                # Create a new connection to ensure we have exclusive access
                with self.engine.connect() as conn:
                    # Checkpoint WAL file to merge changes back to main database
                    conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
                    conn.commit()

                    # Switch to DELETE mode to remove WAL files
                    conn.execute(text("PRAGMA journal_mode=DELETE"))
                    conn.commit()

                    # Close the connection explicitly
                    conn.close()
            except Exception:
                # Ignore errors during checkpoint (e.g., database already closed)
                pass

            # Dispose of the connection pool - this closes all connections
            self.engine.dispose()

            # Give the OS time to release file handles (especially on Windows)
            import time

            time.sleep(0.01)  # 10ms should be enough

            # Additional cleanup: Try to remove any lingering SQLite temp files
            # This helps with test cleanup when using tempfile.TemporaryDirectory()
            try:
                import os

                for suffix in ["-wal", "-shm", "-journal"]:
                    temp_file = Path(str(self.db_path) + suffix)
                    if temp_file.exists():
                        # On Windows, retry a few times if file is locked
                        for _ in range(3):
                            try:
                                os.remove(temp_file)
                                break
                            except (OSError, PermissionError):
                                time.sleep(0.01)
            except Exception:
                # Ignore errors - these files may not exist or may be locked
                pass

    def get_cache_stats(self) -> dict[str, Any] | None:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache statistics, or None if caching is disabled
        """
        if self._cache_enabled and self._cache:
            return self._cache.get_stats()
        return None

    def clear_cache(self) -> None:
        """Clear all cache entries."""
        if self._cache_enabled and self._cache:
            self._cache.clear()

    # Batch operations for performance

    def get_batch(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        """
        Get metadata for multiple files in a single query.

        This is more efficient than calling get() multiple times as it uses
        a single SQL query with IN clause instead of N queries.

        Args:
            paths: List of virtual paths

        Returns:
            Dictionary mapping path to FileMetadata (or None if not found)
        """
        if not paths:
            return {}

        # Check cache first for all paths
        result: dict[str, FileMetadata | None] = {}
        uncached_paths: list[str] = []

        if self._cache_enabled and self._cache:
            for path in paths:
                cached = self._cache.get_path(path)
                if cached is not _CACHE_MISS:
                    # Type narrowing: we know it's FileMetadata | None here
                    result[path] = (
                        cached if isinstance(cached, FileMetadata) or cached is None else None
                    )
                else:
                    uncached_paths.append(path)
        else:
            uncached_paths = list(paths)

        # If all paths were cached, return early
        if not uncached_paths:
            return result

        # Batch query for uncached paths
        try:
            with self.SessionLocal() as session:
                stmt = select(FilePathModel).where(
                    FilePathModel.virtual_path.in_(uncached_paths),
                    FilePathModel.deleted_at.is_(None),
                )

                # Build result dict
                found_paths = set()
                for file_path in session.scalars(stmt):
                    metadata = FileMetadata(
                        path=file_path.virtual_path,
                        backend_name=file_path.backend_id,
                        physical_path=file_path.physical_path,
                        size=file_path.size_bytes,
                        etag=file_path.content_hash,
                        mime_type=file_path.file_type,
                        created_at=file_path.created_at,
                        modified_at=file_path.updated_at,
                        version=1,
                        # UNIX-style permissions (v0.3.0)
                        owner=file_path.owner,
                        group=file_path.group,
                        mode=file_path.mode,
                    )
                    result[file_path.virtual_path] = metadata
                    found_paths.add(file_path.virtual_path)

                    # Cache the result
                    if self._cache_enabled and self._cache:
                        self._cache.set_path(file_path.virtual_path, metadata)

                # Add None for paths not found
                for path in uncached_paths:
                    if path not in found_paths:
                        result[path] = None
                        # Cache the negative result
                        if self._cache_enabled and self._cache:
                            self._cache.set_path(path, None)

                return result
        except Exception as e:
            raise MetadataError(f"Failed to get batch metadata: {e}") from e

    def delete_batch(self, paths: Sequence[str]) -> None:
        """
        Delete multiple files in a single transaction.

        This is more efficient than calling delete() multiple times as it uses
        a single SQL UPDATE with IN clause instead of N queries.

        Args:
            paths: List of virtual paths to delete
        """
        if not paths:
            return

        try:
            with self.SessionLocal() as session:
                # Soft delete all paths in a single query
                stmt = select(FilePathModel).where(
                    FilePathModel.virtual_path.in_(paths), FilePathModel.deleted_at.is_(None)
                )

                deleted_paths: list[str] = []
                for file_path in session.scalars(stmt):
                    file_path.deleted_at = datetime.now(UTC)
                    deleted_paths.append(file_path.virtual_path)

                session.commit()

            # Invalidate cache for all deleted paths
            if self._cache_enabled and self._cache:
                for path in deleted_paths:
                    self._cache.invalidate_path(path)
        except Exception as e:
            raise MetadataError(f"Failed to delete batch metadata: {e}") from e

    def put_batch(self, metadata_list: Sequence[FileMetadata]) -> None:
        """
        Store or update multiple file metadata entries in a single transaction.

        This is more efficient than calling put() multiple times as it uses
        a single transaction instead of N transactions.

        Args:
            metadata_list: List of file metadata to store
        """
        if not metadata_list:
            return

        # Validate all metadata BEFORE any database operations
        for metadata in metadata_list:
            metadata.validate()

        try:
            with self.SessionLocal() as session:
                # Get all paths to check for existing entries
                paths: list[str] = [m.path for m in metadata_list]
                stmt = select(FilePathModel).where(
                    FilePathModel.virtual_path.in_(paths), FilePathModel.deleted_at.is_(None)
                )

                # Build dict of existing entries
                existing = {fp.virtual_path: fp for fp in session.scalars(stmt)}

                # Update or create entries
                for metadata in metadata_list:
                    if metadata.path in existing:
                        # Update existing record
                        file_path = existing[metadata.path]
                        file_path.backend_id = metadata.backend_name
                        file_path.physical_path = metadata.physical_path
                        file_path.size_bytes = metadata.size
                        file_path.content_hash = metadata.etag
                        file_path.file_type = metadata.mime_type
                        file_path.updated_at = metadata.modified_at or datetime.now(UTC)
                        # Update permissions (v0.3.0)
                        file_path.owner = metadata.owner
                        file_path.group = metadata.group
                        file_path.mode = metadata.mode
                    else:
                        # Create new record
                        file_path = FilePathModel(
                            path_id=str(uuid.uuid4()),
                            tenant_id=str(uuid.uuid4()),  # Default tenant for embedded mode
                            virtual_path=metadata.path,
                            backend_id=metadata.backend_name,
                            physical_path=metadata.physical_path,
                            size_bytes=metadata.size,
                            content_hash=metadata.etag,
                            file_type=metadata.mime_type,
                            created_at=metadata.created_at or datetime.now(UTC),
                            updated_at=metadata.modified_at or datetime.now(UTC),
                            # UNIX-style permissions (v0.3.0)
                            owner=metadata.owner,
                            group=metadata.group,
                            mode=metadata.mode,
                        )
                        # Validate model before inserting
                        file_path.validate()
                        session.add(file_path)

                session.commit()

            # Invalidate cache for all affected paths
            if self._cache_enabled and self._cache:
                for metadata in metadata_list:
                    self._cache.invalidate_path(metadata.path)
        except Exception as e:
            raise MetadataError(f"Failed to store batch metadata: {e}") from e

    def batch_get_content_ids(self, paths: Sequence[str]) -> dict[str, str | None]:
        """
        Get content IDs (hashes) for multiple paths in a single query.

        This is optimized for CAS (Content-Addressable Storage) deduplication.
        Instead of fetching full metadata for each file, this only fetches the
        content_hash field, which is more efficient for deduplication checks.

        Performance: Single SQL query with IN clause instead of N queries.

        Args:
            paths: List of virtual paths

        Returns:
            Dictionary mapping path to content_hash (or None if file not found)

        Example:
            >>> hashes = store.batch_get_content_ids(["/a.txt", "/b.txt", "/c.txt"])
            >>> # Find duplicates
            >>> from collections import defaultdict
            >>> by_hash = defaultdict(list)
            >>> for path, hash in hashes.items():
            ...     if hash:
            ...         by_hash[hash].append(path)
            >>> duplicates = {h: paths for h, paths in by_hash.items() if len(paths) > 1}
        """
        if not paths:
            return {}

        try:
            with self.SessionLocal() as session:
                # Single query to fetch only virtual_path and content_hash
                stmt = select(FilePathModel.virtual_path, FilePathModel.content_hash).where(
                    FilePathModel.virtual_path.in_(paths),
                    FilePathModel.deleted_at.is_(None),
                )

                # Build result dict
                result: dict[str, str | None] = {}
                found_paths = set()

                for virtual_path, content_hash in session.execute(stmt):
                    result[virtual_path] = content_hash
                    found_paths.add(virtual_path)

                # Add None for paths not found
                for path in paths:
                    if path not in found_paths:
                        result[path] = None

                return result
        except Exception as e:
            raise MetadataError(f"Failed to get batch content IDs: {e}") from e

    # Additional methods for file metadata (key-value pairs)

    def get_file_metadata(self, path: str, key: str) -> Any | None:
        """
        Get a specific metadata value for a file.

        Args:
            path: Virtual path
            key: Metadata key

        Returns:
            Metadata value (deserialized from JSON) or None
        """
        # Check cache first
        if self._cache_enabled and self._cache:
            cached = self._cache.get_kv(path, key)
            if cached is not _CACHE_MISS:
                return cached

        try:
            with self.SessionLocal() as session:
                # Get file path ID
                path_stmt = select(FilePathModel.path_id).where(
                    FilePathModel.virtual_path == path, FilePathModel.deleted_at.is_(None)
                )
                path_id = session.scalar(path_stmt)

                if path_id is None:
                    # Cache the negative result
                    if self._cache_enabled and self._cache:
                        self._cache.set_kv(path, key, None)
                    return None

                # Get metadata
                metadata_stmt = select(FileMetadataModel).where(
                    FileMetadataModel.path_id == path_id, FileMetadataModel.key == key
                )
                metadata = session.scalar(metadata_stmt)

                if metadata is None:
                    # Cache the negative result
                    if self._cache_enabled and self._cache:
                        self._cache.set_kv(path, key, None)
                    return None

                value = json.loads(metadata.value) if metadata.value else None

                # Cache the result
                if self._cache_enabled and self._cache:
                    self._cache.set_kv(path, key, value)

                return value
        except Exception as e:
            raise MetadataError(f"Failed to get file metadata: {e}", path=path) from e

    def set_file_metadata(self, path: str, key: str, value: Any) -> None:
        """
        Set a metadata value for a file.

        Args:
            path: Virtual path
            key: Metadata key
            value: Metadata value (will be serialized to JSON)
        """
        try:
            with self.SessionLocal() as session:
                # Get file path ID
                path_stmt = select(FilePathModel.path_id).where(
                    FilePathModel.virtual_path == path, FilePathModel.deleted_at.is_(None)
                )
                path_id = session.scalar(path_stmt)

                if path_id is None:
                    raise MetadataError("File not found", path=path)

                # Check if metadata exists
                metadata_stmt = select(FileMetadataModel).where(
                    FileMetadataModel.path_id == path_id, FileMetadataModel.key == key
                )
                metadata = session.scalar(metadata_stmt)

                value_json = json.dumps(value) if value is not None else None

                if metadata:
                    # Update existing
                    metadata.value = value_json
                else:
                    # Create new
                    metadata = FileMetadataModel(
                        metadata_id=str(uuid.uuid4()),
                        path_id=path_id,
                        key=key,
                        value=value_json,
                        created_at=datetime.now(UTC),
                    )
                    # Validate model before inserting
                    metadata.validate()
                    session.add(metadata)

                session.commit()

            # Invalidate cache for this specific key
            if self._cache_enabled and self._cache:
                self._cache.invalidate_kv(path, key)
        except Exception as e:
            raise MetadataError(f"Failed to set file metadata: {e}", path=path) from e

    def get_path_id(self, path: str) -> str | None:
        """Get the UUID path_id for a virtual path.

        This is useful for setting up work dependencies (depends_on metadata).

        Args:
            path: Virtual path

        Returns:
            UUID path_id string or None if path doesn't exist
        """
        try:
            with self.SessionLocal() as session:
                stmt = select(FilePathModel.path_id).where(
                    FilePathModel.virtual_path == path, FilePathModel.deleted_at.is_(None)
                )
                path_id = session.scalar(stmt)
                return path_id
        except Exception as e:
            raise MetadataError(f"Failed to get path_id: {e}", path=path) from e

    # Work detection queries (using SQL views)

    def get_ready_work(self, limit: int | None = None) -> builtins.list[dict[str, Any]]:
        """Get files that are ready for processing.

        Uses the ready_work_items SQL view which efficiently finds files with:
        - status='ready'
        - No blocking dependencies

        Args:
            limit: Optional limit on number of results

        Returns:
            List of work item dicts with path, status, priority, etc.
        """
        try:
            with self.SessionLocal() as session:
                query = "SELECT * FROM ready_work_items"
                if limit:
                    query += f" LIMIT {limit}"

                result = session.execute(text(query))
                rows = result.fetchall()

                return [
                    {
                        "path_id": row[0],
                        "tenant_id": row[1],
                        "virtual_path": row[2],
                        "backend_id": row[3],
                        "physical_path": row[4],
                        "file_type": row[5],
                        "size_bytes": row[6],
                        "content_hash": row[7],
                        "created_at": row[8],
                        "updated_at": row[9],
                        "status": json.loads(row[10]) if row[10] else None,
                        "priority": json.loads(row[11]) if row[11] else None,
                    }
                    for row in rows
                ]
        except Exception as e:
            raise MetadataError(f"Failed to get ready work: {e}") from e

    def get_pending_work(self, limit: int | None = None) -> builtins.list[dict[str, Any]]:
        """Get files with status='pending' ordered by priority.

        Uses the pending_work_items SQL view.

        Args:
            limit: Optional limit on number of results

        Returns:
            List of work item dicts
        """
        try:
            with self.SessionLocal() as session:
                query = "SELECT * FROM pending_work_items"
                if limit:
                    query += f" LIMIT {limit}"

                result = session.execute(text(query))
                rows = result.fetchall()

                return [
                    {
                        "path_id": row[0],
                        "tenant_id": row[1],
                        "virtual_path": row[2],
                        "backend_id": row[3],
                        "physical_path": row[4],
                        "file_type": row[5],
                        "size_bytes": row[6],
                        "content_hash": row[7],
                        "created_at": row[8],
                        "updated_at": row[9],
                        "status": json.loads(row[10]) if row[10] else None,
                        "priority": json.loads(row[11]) if row[11] else None,
                    }
                    for row in rows
                ]
        except Exception as e:
            raise MetadataError(f"Failed to get pending work: {e}") from e

    def get_blocked_work(self, limit: int | None = None) -> builtins.list[dict[str, Any]]:
        """Get files that are blocked by dependencies.

        Uses the blocked_work_items SQL view.

        Args:
            limit: Optional limit on number of results

        Returns:
            List of work item dicts with blocker_count
        """
        try:
            with self.SessionLocal() as session:
                query = "SELECT * FROM blocked_work_items"
                if limit:
                    query += f" LIMIT {limit}"

                result = session.execute(text(query))
                rows = result.fetchall()

                return [
                    {
                        "path_id": row[0],
                        "tenant_id": row[1],
                        "virtual_path": row[2],
                        "backend_id": row[3],
                        "physical_path": row[4],
                        "file_type": row[5],
                        "size_bytes": row[6],
                        "content_hash": row[7],
                        "created_at": row[8],
                        "updated_at": row[9],
                        "status": json.loads(row[10]) if row[10] else None,
                        "priority": json.loads(row[11]) if row[11] else None,
                        "blocker_count": row[12],
                    }
                    for row in rows
                ]
        except Exception as e:
            raise MetadataError(f"Failed to get blocked work: {e}") from e

    def get_in_progress_work(self, limit: int | None = None) -> builtins.list[dict[str, Any]]:
        """Get files currently being processed.

        Uses the in_progress_work SQL view.

        Args:
            limit: Optional limit on number of results

        Returns:
            List of work item dicts with worker_id and started_at
        """
        try:
            with self.SessionLocal() as session:
                query = "SELECT * FROM in_progress_work"
                if limit:
                    query += f" LIMIT {limit}"

                result = session.execute(text(query))
                rows = result.fetchall()

                return [
                    {
                        "path_id": row[0],
                        "tenant_id": row[1],
                        "virtual_path": row[2],
                        "backend_id": row[3],
                        "file_type": row[4],
                        "size_bytes": row[5],
                        "created_at": row[6],
                        "updated_at": row[7],
                        "status": json.loads(row[8]) if row[8] else None,
                        "worker_id": json.loads(row[9]) if row[9] else None,
                        "started_at": json.loads(row[10]) if row[10] else None,
                    }
                    for row in rows
                ]
        except Exception as e:
            raise MetadataError(f"Failed to get in-progress work: {e}") from e

    def get_work_by_priority(self, limit: int | None = None) -> builtins.list[dict[str, Any]]:
        """Get all work items ordered by priority.

        Uses the work_by_priority SQL view.

        Args:
            limit: Optional limit on number of results

        Returns:
            List of work item dicts
        """
        try:
            with self.SessionLocal() as session:
                query = "SELECT * FROM work_by_priority"
                if limit:
                    query += f" LIMIT {limit}"

                result = session.execute(text(query))
                rows = result.fetchall()

                return [
                    {
                        "path_id": row[0],
                        "tenant_id": row[1],
                        "virtual_path": row[2],
                        "backend_id": row[3],
                        "file_type": row[4],
                        "size_bytes": row[5],
                        "created_at": row[6],
                        "updated_at": row[7],
                        "status": json.loads(row[8]) if row[8] else None,
                        "priority": json.loads(row[9]) if row[9] else None,
                        "tags": json.loads(row[10]) if row[10] else None,
                    }
                    for row in rows
                ]
        except Exception as e:
            raise MetadataError(f"Failed to get work by priority: {e}") from e

    def __enter__(self) -> SQLAlchemyMetadataStore:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()
