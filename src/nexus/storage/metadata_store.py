"""SQLAlchemy-based metadata store implementation for Nexus.

Production-ready metadata store using SQLAlchemy ORM with support for:
- File path mapping (virtual path → physical backend path)
- File metadata (arbitrary key-value pairs)
- Content chunks (for deduplication)
- Multiple database backends (SQLite, PostgreSQL)
"""

from __future__ import annotations

import builtins
import json
import logging
import os
import uuid
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, pool, select, text, update
from sqlalchemy.orm import sessionmaker

try:
    import psycopg2.extras
except ImportError:
    psycopg2 = None

from nexus.core.exceptions import MetadataError
from nexus.core.metadata import FileMetadata, MetadataStore, PaginatedResult
from nexus.storage.cache import _CACHE_MISS, MetadataCache
from nexus.storage.models import (
    Base,
    ContentCacheModel,
    DirectoryEntryModel,
    FileMetadataModel,
    FilePathModel,
    VersionHistoryModel,
)
from nexus.storage.query_builder import WorkQueryBuilder
from nexus.storage.version_manager import VersionManager

logger = logging.getLogger(__name__)


class SQLAlchemyMetadataStore(MetadataStore):
    """
    SQLAlchemy-based metadata store supporting multiple database backends.

    Uses SQLAlchemy ORM for database operations with support for:
    - File path mapping (virtual path -> physical backend path)
    - File metadata (arbitrary key-value pairs)
    - Content chunks (for deduplication)
    - Multiple database backends (SQLite, PostgreSQL)

    Environment Variables:
        NEXUS_DATABASE_URL: Database connection URL (e.g., postgresql://user:pass@host/db)
                           If not set, falls back to db_path parameter (SQLite)
        POSTGRES_URL: Alternative to NEXUS_DATABASE_URL for PostgreSQL
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        db_url: str | None = None,
        run_migrations: bool = False,  # Auto-run migrations on startup (Issue #979)
        enable_cache: bool = True,
        cache_path_size: int = 512,
        cache_list_size: int = 1024,  # Increased from 128 for better descendant caching
        cache_kv_size: int = 256,
        cache_exists_size: int = 1024,
        cache_ttl_seconds: int | None = 300,
    ):
        """
        Initialize SQLAlchemy metadata store.

        Args:
            db_path: Path to SQLite database file (deprecated, use db_url for flexibility)
            db_url: Database URL (e.g., 'postgresql://user:pass@host/db' or 'sqlite:///path/to/db')
                   If not provided, checks NEXUS_DATABASE_URL or POSTGRES_URL env vars,
                   then falls back to db_path parameter
            run_migrations: If True, run Alembic migrations on startup (default: True)
            enable_cache: If True, enable in-memory caching (default: True)
            cache_path_size: Max entries for path metadata cache (default: 512)
            cache_list_size: Max entries for directory listing cache (default: 1024)
            cache_kv_size: Max entries for file metadata KV cache (default: 256)
            cache_exists_size: Max entries for existence check cache (default: 1024)
            cache_ttl_seconds: Cache TTL in seconds, None = no expiry (default: 300)
        """
        # Determine database URL from multiple sources (priority order)
        self.database_url: str = (
            db_url
            or os.getenv("NEXUS_DATABASE_URL")
            or os.getenv("POSTGRES_URL")
            or (f"sqlite:///{db_path}" if db_path else None)
            or ""
        )

        if not self.database_url:
            raise MetadataError(
                "Database URL must be provided via db_url parameter, db_path parameter, "
                "NEXUS_DATABASE_URL, or POSTGRES_URL environment variable"
            )

        # Detect database type
        self.db_type = self._detect_db_type(self.database_url)

        # Log database configuration for debugging
        import logging

        logger = logging.getLogger(__name__)
        logger.info(
            f"MetadataStore database: {self.db_type} ({self.database_url[:50]}...)"
            if len(self.database_url) > 50
            else f"MetadataStore database: {self.db_type} ({self.database_url})"
        )

        # For SQLite, extract and ensure parent directory exists
        self.db_path: Path | None
        if self.db_type == "sqlite":
            self.db_path = self._extract_sqlite_path(self.database_url)
            self._ensure_parent_exists()
        else:
            self.db_path = None

        # Create engine with database-specific configuration
        engine_kwargs = self._get_engine_config()
        self.engine = create_engine(self.database_url, **engine_kwargs)

        # Set SQLite busy timeout on every connection to handle concurrent access
        if self.db_type == "sqlite":

            @event.listens_for(self.engine, "connect")
            def set_sqlite_pragma(dbapi_conn: Any, _connection_record: Any) -> None:
                cursor = dbapi_conn.cursor()
                # Wait up to 30 seconds for locks instead of failing immediately
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.close()

        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

        # Setup connection pool monitoring (enabled via environment variable)
        self._pool_checkout_count = 0
        self._pool_checkin_count = 0
        if os.getenv("NEXUS_POOL_LOGGING", "").lower() in ("1", "true", "yes"):
            self._setup_pool_logging()

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

        # Track if store has been closed
        self._closed = False

        # Initialize schema
        if run_migrations:
            self._run_migrations()
        else:
            # Create tables if they don't exist
            Base.metadata.create_all(self.engine)
            # Create SQL views for work detection
            self._create_views()

        # Apply database-specific optimizations
        self._apply_db_optimizations()

    def _detect_db_type(self, db_url: str) -> str:
        """Detect database type from connection URL.

        Args:
            db_url: Database connection URL

        Returns:
            Database type: 'sqlite', 'postgresql', etc.
        """
        if db_url.startswith("sqlite"):
            return "sqlite"
        elif db_url.startswith(("postgres", "postgresql")):
            return "postgresql"
        else:
            # Default to generic SQL database
            return "unknown"

    def _extract_sqlite_path(self, db_url: str) -> Path:
        """Extract file path from SQLite URL.

        Args:
            db_url: SQLite database URL (e.g., 'sqlite:///path/to/db')

        Returns:
            Path object to the database file
        """
        # Remove sqlite:/// prefix
        path_str = db_url.replace("sqlite:///", "")
        return Path(path_str)

    def _get_engine_config(self) -> dict[str, Any]:
        """Get database-specific engine configuration.

        Returns:
            Dictionary of engine kwargs for create_engine()
        """
        config: dict[str, Any] = {
            "pool_pre_ping": True,  # Check connections before using them
        }

        if self.db_type == "sqlite":
            # SQLite-specific configuration
            # Use NullPool to avoid concurrency issues with SQLite
            config["poolclass"] = pool.NullPool
            # Set a longer timeout for concurrent operations (30 seconds)
            # This allows threads to wait for locks instead of failing immediately
            config["connect_args"] = {"timeout": 30}
        elif self.db_type == "postgresql":
            # PostgreSQL-specific configuration
            # Use QueuePool with configurable settings for production scalability
            # Environment variables allow tuning without code changes
            config["poolclass"] = pool.QueuePool
            config["pool_size"] = int(os.getenv("NEXUS_DB_POOL_SIZE", "20"))
            config["max_overflow"] = int(os.getenv("NEXUS_DB_MAX_OVERFLOW", "30"))
            config["pool_timeout"] = int(os.getenv("NEXUS_DB_POOL_TIMEOUT", "10"))
            config["pool_recycle"] = 1800  # Recycle connections every 30 min
            config["pool_pre_ping"] = True  # Verify connections are alive before use

        return config

    def _setup_pool_logging(self) -> None:
        """Setup connection pool event listeners for debugging.

        Enable by setting NEXUS_POOL_LOGGING=1 environment variable.
        Logs checkout/checkin events and tracks connection counts.
        """
        pool_obj = self.engine.pool

        @event.listens_for(pool_obj, "checkout")
        def receive_checkout(
            _dbapi_conn: Any, _connection_record: Any, _connection_proxy: Any
        ) -> None:
            self._pool_checkout_count += 1
            # Use getattr for pool stats as they're QueuePool-specific
            pool_size = getattr(pool_obj, "size", lambda: "N/A")()
            checked_out = getattr(pool_obj, "checkedout", lambda: "N/A")()
            logger.debug(
                f"Connection checked out from pool "
                f"(total checkouts: {self._pool_checkout_count}, "
                f"pool size: {pool_size}, checked out: {checked_out})"
            )

        @event.listens_for(pool_obj, "checkin")
        def receive_checkin(_dbapi_conn: Any, _connection_record: Any) -> None:
            self._pool_checkin_count += 1
            pool_size = getattr(pool_obj, "size", lambda: "N/A")()
            checked_out = getattr(pool_obj, "checkedout", lambda: "N/A")()
            logger.debug(
                f"Connection returned to pool "
                f"(total checkins: {self._pool_checkin_count}, "
                f"pool size: {pool_size}, checked out: {checked_out})"
            )

    def get_pool_stats(self) -> dict[str, Any]:
        """Get connection pool statistics.

        Returns:
            Dictionary with pool statistics including:
            - pool_size: Current number of connections in pool
            - checked_out: Number of connections currently checked out
            - overflow: Current overflow connections
            - checkedout_total: Total checkout operations
            - checkedin_total: Total checkin operations
            - pool_class: Pool implementation class name
        """
        pool_obj = self.engine.pool
        stats: dict[str, Any] = {
            "pool_class": pool_obj.__class__.__name__,
            "checkedout_total": self._pool_checkout_count,
            "checkedin_total": self._pool_checkin_count,
        }

        # QueuePool-specific stats
        if hasattr(pool_obj, "size"):
            stats["pool_size"] = pool_obj.size()
        if hasattr(pool_obj, "checkedout"):
            stats["checked_out"] = pool_obj.checkedout()
        if hasattr(pool_obj, "overflow"):
            stats["overflow"] = pool_obj.overflow()
        if hasattr(pool_obj, "checkedin"):
            stats["checked_in"] = pool_obj.checkedin()

        return stats

    def get_pg_connection_stats(self) -> dict[str, Any] | None:
        """Get PostgreSQL connection statistics from pg_stat_activity.

        Queries pg_stat_activity to monitor active database connections,
        useful for diagnosing connection pool exhaustion issues.

        Returns:
            Dictionary with connection stats or None if not PostgreSQL:
            - total: Total connections to this database
            - active: Actively running queries
            - idle: Idle connections (not in transaction)
            - idle_in_transaction: Idle but holding transaction open (potential leak)
            - waiting: Waiting for lock
            - by_application: Breakdown by application_name
            - by_state: Breakdown by connection state

        Example:
            >>> store.get_pg_connection_stats()
            {
                "total": 45,
                "active": 3,
                "idle": 40,
                "idle_in_transaction": 2,
                "waiting": 0,
                "by_state": {"idle": 40, "active": 3, "idle in transaction": 2}
            }
        """
        if self.db_type != "postgresql":
            return None

        try:
            with self.engine.connect() as conn:
                # Get connection counts by state
                result = conn.execute(
                    text("""
                        SELECT
                            COUNT(*) as total,
                            COUNT(*) FILTER (WHERE state = 'active') as active,
                            COUNT(*) FILTER (WHERE state = 'idle') as idle,
                            COUNT(*) FILTER (WHERE state = 'idle in transaction') as idle_in_transaction,
                            COUNT(*) FILTER (WHERE wait_event_type IS NOT NULL) as waiting
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                    """)
                )
                row = result.fetchone()

                stats: dict[str, Any] = {
                    "total": row[0] if row else 0,
                    "active": row[1] if row else 0,
                    "idle": row[2] if row else 0,
                    "idle_in_transaction": row[3] if row else 0,
                    "waiting": row[4] if row else 0,
                }

                # Get breakdown by state
                result = conn.execute(
                    text("""
                        SELECT state, COUNT(*) as count
                        FROM pg_stat_activity
                        WHERE datname = current_database() AND state IS NOT NULL
                        GROUP BY state
                    """)
                )
                stats["by_state"] = {row[0]: row[1] for row in result.fetchall()}

                # Get breakdown by application
                result = conn.execute(
                    text("""
                        SELECT
                            COALESCE(application_name, 'unknown') as app,
                            COUNT(*) as count
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                        GROUP BY application_name
                        ORDER BY count DESC
                        LIMIT 10
                    """)
                )
                stats["by_application"] = {row[0]: row[1] for row in result.fetchall()}

                # Get long-running idle in transaction connections (potential leaks)
                result = conn.execute(
                    text("""
                        SELECT
                            pid,
                            application_name,
                            EXTRACT(EPOCH FROM (NOW() - state_change))::int as idle_seconds,
                            query
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND state = 'idle in transaction'
                          AND state_change < NOW() - INTERVAL '60 seconds'
                        ORDER BY state_change
                        LIMIT 5
                    """)
                )
                long_idle = []
                for row in result.fetchall():
                    long_idle.append(
                        {
                            "pid": row[0],
                            "application": row[1],
                            "idle_seconds": row[2],
                            "query": row[3][:200] if row[3] else None,  # Truncate long queries
                        }
                    )
                if long_idle:
                    stats["long_idle_in_transaction"] = long_idle

                return stats
        except Exception as e:
            logger.warning(f"Failed to get pg_stat_activity stats: {e}")
            return None

    def _apply_db_optimizations(self) -> None:
        """Apply database-specific performance optimizations."""
        if self.db_type == "sqlite":
            # Enable WAL mode for better concurrency and to avoid journal files
            # Note: busy_timeout is set via event listener on every connection
            try:
                with self.engine.connect() as conn:
                    conn.execute(text("PRAGMA journal_mode=WAL"))
                    # Use FULL synchronous mode to prevent race conditions
                    # This ensures commits are fully written to disk before returning
                    # Critical for preventing "File not found" errors in CAS operations
                    conn.execute(text("PRAGMA synchronous=FULL"))
                    conn.commit()
            except OSError:
                # Ignore if optimizations cannot be enabled (file system issues)
                pass
        elif self.db_type == "postgresql":
            # PostgreSQL optimizations can be set at connection level if needed
            # Most optimizations are better set in postgresql.conf
            pass

    @property
    def supports_old_new_returning(self) -> bool:
        """Check if database supports OLD/NEW in RETURNING clauses.

        PostgreSQL 18+ supports OLD/NEW aliases in RETURNING clauses for
        UPDATE, DELETE, and MERGE statements. This allows capturing both
        before and after values in a single query.

        Returns:
            True if PostgreSQL 18+, False otherwise (SQLite, older PostgreSQL)
        """
        if self.db_type != "postgresql":
            return False

        # Check PostgreSQL version (cached after first check)
        if not hasattr(self, "_pg_version"):
            try:
                with self.engine.connect() as conn:
                    result = conn.execute(text("SELECT version()"))
                    version_str = result.scalar()
                    # Extract major version number (e.g., "PostgreSQL 18.1" -> 18)
                    import re

                    match = re.search(r"PostgreSQL (\d+)", version_str or "")
                    self._pg_version = int(match.group(1)) if match else 0
            except Exception:
                self._pg_version = 0

        return self._pg_version >= 18

    def _ensure_parent_exists(self) -> None:
        """Create parent directory for database if it doesn't exist (SQLite only)."""
        if self.db_path:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _run_migrations(self) -> None:
        """Run Alembic migrations to create/update schema."""
        try:
            from alembic.config import Config

            from alembic import command

            # Configure Alembic
            alembic_cfg = Config("alembic.ini")
            alembic_cfg.set_main_option("sqlalchemy.url", self.database_url)

            # Run migrations
            command.upgrade(alembic_cfg, "head")
        except Exception as e:
            raise MetadataError(f"Failed to run migrations: {e}") from e

    def _create_views(self) -> None:
        """Create SQL views for work detection if they don't exist.

        For PostgreSQL: Uses CREATE OR REPLACE VIEW (always updates views)
        For SQLite: Uses CREATE VIEW IF NOT EXISTS (creates only if missing)
        """
        try:
            from nexus.storage import views

            # Get database-specific views
            all_views = views.get_all_views(self.db_type)

            with self.engine.connect() as conn:
                for _name, view_sql in all_views:
                    # Validate view name against allowlist to prevent SQL injection
                    if _name not in views.ALLOWED_VIEW_NAMES:
                        raise ValueError(f"Invalid view name: {_name}")

                    try:
                        conn.execute(view_sql)
                        conn.commit()
                    except OSError:
                        # For PostgreSQL, if CREATE OR REPLACE fails, rollback and try dropping first
                        if self.db_type == "postgresql":
                            try:
                                # CRITICAL: Rollback the failed transaction first
                                conn.rollback()

                                # SECURITY FIX: Use proper SQL identifier quoting to prevent injection
                                # Even with validation, avoid f-strings in SQL to prevent bypass vectors
                                from sqlalchemy.sql import quoted_name

                                view_identifier = quoted_name(_name, quote=True)

                                # Build parameterized SQL with properly quoted identifier
                                drop_sql = text(f"DROP VIEW IF EXISTS {view_identifier} CASCADE")

                                conn.execute(drop_sql)
                                conn.commit()
                                conn.execute(view_sql)
                                conn.commit()
                            except OSError:
                                # Still failed - skip this view
                                conn.rollback()  # Clean up after failure
                                pass
                        # For SQLite, IF NOT EXISTS should handle it - silently skip failures
                        pass

        except (OSError, ImportError):
            # Silently ignore view creation failures
            # Views are optional optimizations for work detection queries
            # Common failures: DB connection issues, missing views module
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
                    version=file_path.current_version,  # Version tracking (v0.3.5)
                    tenant_id=file_path.tenant_id,  # P0 SECURITY: Defense-in-depth (v0.7.0)
                    owner_id=file_path.posix_uid,  # Issue #920: O(1) owner permission checks
                )

                # Cache the result
                if self._cache_enabled and self._cache:
                    self._cache.set_path(path, metadata)

                return metadata
        except Exception as e:
            raise MetadataError(f"Failed to get metadata: {e}", path=path) from e

    def put(self, metadata: FileMetadata) -> None:
        """
        Store or update file metadata WITH VERSION TRACKING.

        When updating an existing file, creates a version history entry
        preserving the old content hash before updating to new content.

        Args:
            metadata: File metadata to store
        """
        import random
        import time

        from sqlalchemy.exc import IntegrityError, OperationalError

        # Validate BEFORE database operation
        metadata.validate()

        # Retry logic for handling concurrent version conflicts in SQLite
        max_retries = 10  # Increased for high concurrency scenarios
        retry_delay = 0.001  # Start with 1ms, faster initial retries
        max_retry_delay = 1.0  # Cap at 1 second to prevent exponential overflow

        for attempt in range(max_retries):
            try:
                with self.SessionLocal() as session:
                    # Check if file path already exists
                    # Use row-level locking to prevent concurrent version conflicts
                    # CRITICAL: Use NOWAIT or SKIP LOCKED to prevent indefinite blocking
                    # This prevents connection pool exhaustion under high concurrency
                    stmt = (
                        select(FilePathModel)
                        .where(
                            FilePathModel.virtual_path == metadata.path,
                            FilePathModel.deleted_at.is_(None),
                        )
                        .with_for_update(nowait=True)  # Fail fast instead of blocking indefinitely
                    )
                    existing = session.scalar(stmt)

                    # FIX: Permanently delete soft-deleted entries to avoid version conflicts
                    # When a file is soft-deleted, its version_history entries remain in the database.
                    # Reusing the same path_id would cause UniqueViolation on version_history inserts.
                    # Also, the unique constraint on virtual_path prevents creating a new entry
                    # at the same path while a soft-deleted entry exists.
                    #
                    # Solution: Permanently delete the soft-deleted entry before creating new one.
                    # The old version_history entries will remain (orphaned) for potential recovery.
                    if not existing:
                        # Check for soft-deleted entry at this path
                        deleted_stmt = (
                            select(FilePathModel)
                            .where(
                                FilePathModel.virtual_path == metadata.path,
                                FilePathModel.deleted_at.is_not(None),
                            )
                            .with_for_update(nowait=True)
                        )
                        deleted_entry = session.scalar(deleted_stmt)
                        if deleted_entry:
                            # Permanently delete the soft-deleted entry
                            # This allows us to create a new file at the same path
                            # Note: version_history entries for old path_id remain for recovery
                            session.delete(deleted_entry)
                            session.flush()  # Ensure deletion completes before insert

                    if existing:
                        # FILE UPDATE - Increment version and create history entry for NEW version

                        # Update existing record with new content
                        existing.backend_id = metadata.backend_name
                        existing.physical_path = metadata.physical_path
                        existing.size_bytes = metadata.size
                        existing.content_hash = metadata.etag  # NEW content hash
                        existing.file_type = metadata.mime_type
                        existing.updated_at = metadata.modified_at or datetime.now(UTC)

                        # Only create version history if we have actual content (etag is not None)
                        if metadata.etag is not None:
                            # Get the current version entry to link lineage
                            prev_version_stmt = (
                                select(VersionHistoryModel)
                                .where(
                                    VersionHistoryModel.resource_type == "file",
                                    VersionHistoryModel.resource_id == existing.path_id,
                                    VersionHistoryModel.version_number == existing.current_version,
                                )
                                .limit(1)
                            )
                            prev_version = session.scalar(prev_version_stmt)

                            # Atomically increment version at database level to prevent race conditions
                            # CRITICAL FIX: Use UPDATE ... RETURNING to get incremented value atomically
                            # This prevents race condition where concurrent transactions get same version
                            #
                            # PostgreSQL 18+: Use OLD/NEW RETURNING to capture both old and new values
                            # in a single query, eliminating the need for separate prev_version lookup
                            if self.supports_old_new_returning:
                                # PostgreSQL 18+: Capture old and new values in single query
                                result = session.execute(
                                    text("""
                                        UPDATE file_paths
                                        SET current_version = current_version + 1,
                                            updated_at = :updated_at
                                        WHERE path_id = :path_id
                                        RETURNING
                                            old.current_version AS old_version,
                                            new.current_version AS new_version,
                                            old.content_hash AS old_content_hash
                                    """),
                                    {
                                        "path_id": existing.path_id,
                                        "updated_at": metadata.modified_at or datetime.now(UTC),
                                    },
                                )
                                row = result.one()
                                new_version = row.new_version
                            else:
                                # SQLite / older PostgreSQL: Standard RETURNING
                                result = session.execute(
                                    update(FilePathModel)
                                    .where(FilePathModel.path_id == existing.path_id)
                                    .values(current_version=FilePathModel.current_version + 1)
                                    .returning(FilePathModel.current_version)
                                )
                                new_version = result.scalar_one()

                            # Update existing object with new version (avoiding race-prone refresh())
                            existing.current_version = new_version

                            # Create version history entry for NEW version
                            version_entry = VersionHistoryModel(
                                version_id=str(uuid.uuid4()),
                                resource_type="file",
                                resource_id=existing.path_id,
                                version_number=existing.current_version,  # NEW version number
                                content_hash=metadata.etag,  # NEW content hash
                                size_bytes=metadata.size,
                                mime_type=metadata.mime_type,
                                parent_version_id=prev_version.version_id if prev_version else None,
                                source_type="original",
                                created_at=datetime.now(UTC),
                                created_by=metadata.created_by,  # Track who created this version
                            )
                            version_entry.validate()
                            session.add(version_entry)
                    else:
                        # NEW FILE - Create record and initial version history
                        file_path = FilePathModel(
                            path_id=str(uuid.uuid4()),
                            virtual_path=metadata.path,
                            backend_id=metadata.backend_name,
                            physical_path=metadata.physical_path,
                            size_bytes=metadata.size,
                            content_hash=metadata.etag,
                            file_type=metadata.mime_type,
                            created_at=metadata.created_at or datetime.now(UTC),
                            updated_at=metadata.modified_at or datetime.now(UTC),
                            current_version=1,  # Initial version
                            tenant_id=metadata.tenant_id,  # P0 SECURITY: Defense-in-depth (v0.7.0)
                            posix_uid=metadata.owner_id,  # Issue #920: O(1) owner permission checks
                        )
                        # Validate model before inserting
                        file_path.validate()
                        session.add(file_path)
                        session.flush()  # Get path_id

                        # Only create version history if we have actual content (etag is not None)
                        if metadata.etag is not None:
                            # Create initial version history entry
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
                                created_by=metadata.created_by,  # Track who created this version
                            )
                            version_entry.validate()
                            session.add(version_entry)

                    # Update directory index (Issue #924)
                    self._update_directory_index(
                        session, metadata.path, metadata.tenant_id, is_directory=False
                    )

                    session.commit()

                # Invalidate cache for this path
                if self._cache_enabled and self._cache:
                    self._cache.invalidate_path(metadata.path)

                # Success - exit retry loop
                break

            except OperationalError as e:
                # Handle lock acquisition failures from nowait=True
                # This prevents indefinite blocking and connection pool exhaustion
                error_msg = str(e).lower()
                if "could not obtain lock" in error_msg or "lock" in error_msg:
                    if attempt < max_retries - 1:
                        # Retry with exponential backoff + jitter (capped to prevent overflow)
                        jitter = random.uniform(0, retry_delay * 0.5)
                        time.sleep(retry_delay + jitter)
                        retry_delay = min(
                            retry_delay * 2, max_retry_delay
                        )  # Cap to prevent overflow
                        continue
                    else:
                        raise MetadataError(
                            f"Failed to acquire lock after {max_retries} retries - high contention on {metadata.path}",
                            path=metadata.path,
                        ) from e
                else:
                    # Different operational error - don't retry
                    raise MetadataError(f"Failed to store metadata: {e}", path=metadata.path) from e
            except IntegrityError as e:
                # Handle concurrent version conflicts
                if "UNIQUE constraint failed: version_history" in str(e):
                    if attempt < max_retries - 1:
                        # Retry with exponential backoff + jitter to avoid thundering herd (capped)
                        jitter = random.uniform(0, retry_delay * 0.5)  # Add up to 50% jitter
                        time.sleep(retry_delay + jitter)
                        retry_delay = min(
                            retry_delay * 2, max_retry_delay
                        )  # Cap to prevent overflow
                        continue
                    else:
                        # Final attempt failed
                        raise MetadataError(
                            f"Failed to store metadata after {max_retries} retries due to concurrent version conflict: {e}",
                            path=metadata.path,
                        ) from e
                else:
                    # Different integrity error - don't retry
                    raise MetadataError(f"Failed to store metadata: {e}", path=metadata.path) from e
            except Exception as e:
                # Non-retryable error
                raise MetadataError(f"Failed to store metadata: {e}", path=metadata.path) from e

    def delete(self, path: str) -> dict[str, Any] | None:
        """
        Delete file metadata (soft delete).

        Args:
            path: Virtual path

        Returns:
            Dictionary with deleted file info (PostgreSQL 18+) or None.
            Contains: path_id, content_hash, size_bytes, current_version
            Useful for audit logging and undo operations.
        """
        deleted_info: dict[str, Any] | None = None
        try:
            with self.SessionLocal() as session:
                deleted_at = datetime.now(UTC)

                # PostgreSQL 18+: Use UPDATE...RETURNING OLD to capture deleted state
                # in a single query for audit logging
                if self.supports_old_new_returning:
                    result = session.execute(
                        text("""
                            UPDATE file_paths
                            SET deleted_at = :deleted_at
                            WHERE virtual_path = :path AND deleted_at IS NULL
                            RETURNING
                                old.path_id AS path_id,
                                old.content_hash AS content_hash,
                                old.size_bytes AS size_bytes,
                                old.current_version AS version,
                                old.backend_id AS backend_id,
                                old.physical_path AS physical_path,
                                old.tenant_id AS tenant_id
                        """),
                        {"path": path, "deleted_at": deleted_at},
                    )
                    row = result.first()
                    if row:
                        deleted_info = {
                            "path_id": row.path_id,
                            "content_hash": row.content_hash,
                            "size_bytes": row.size_bytes,
                            "version": row.version,
                            "backend_id": row.backend_id,
                            "physical_path": row.physical_path,
                            "deleted_at": deleted_at,
                            "tenant_id": row.tenant_id,
                        }
                        # Remove from directory index (Issue #924)
                        self._remove_from_directory_index(session, path, row.tenant_id)
                    session.commit()
                else:
                    # SQLite / older PostgreSQL: SELECT then UPDATE
                    stmt = select(FilePathModel).where(
                        FilePathModel.virtual_path == path, FilePathModel.deleted_at.is_(None)
                    )
                    file_path = session.scalar(stmt)

                    if file_path:
                        # Capture info before soft delete for audit logging
                        deleted_info = {
                            "path_id": file_path.path_id,
                            "content_hash": file_path.content_hash,
                            "size_bytes": file_path.size_bytes,
                            "version": file_path.current_version,
                            "backend_id": file_path.backend_id,
                            "physical_path": file_path.physical_path,
                            "deleted_at": deleted_at,
                            "tenant_id": file_path.tenant_id,
                        }
                        # Remove from directory index (Issue #924)
                        self._remove_from_directory_index(session, path, file_path.tenant_id)
                        # Soft delete
                        file_path.deleted_at = deleted_at
                        session.commit()

            # Invalidate cache for this path
            if self._cache_enabled and self._cache:
                self._cache.invalidate_path(path)

            return deleted_info
        except Exception as e:
            raise MetadataError(f"Failed to delete metadata: {e}", path=path) from e

    def rename_path(self, old_path: str, new_path: str) -> None:
        """
        Rename/move a file or directory by updating virtual paths in metadata.

        This is a metadata-only operation that does NOT touch the actual
        file content in CAS storage. Only the virtual_path is updated.

        For directories, this recursively updates all child file paths.

        Args:
            old_path: Current virtual path
            new_path: New virtual path

        Raises:
            MetadataError: If the old path doesn't exist or new path already exists
        """
        try:
            with self.SessionLocal() as session:
                # Check if source exists (as a file)
                stmt_old = select(FilePathModel).where(
                    FilePathModel.virtual_path == old_path, FilePathModel.deleted_at.is_(None)
                )
                file_path = session.scalar(stmt_old)

                # Check if this is a directory (has children)
                is_directory = self.is_implicit_directory(old_path)

                # If not a file and not a directory, error
                if not file_path and not is_directory:
                    raise MetadataError(f"Source path not found: {old_path}", path=old_path)

                # Check if destination already exists
                stmt_new = select(FilePathModel).where(
                    FilePathModel.virtual_path == new_path, FilePathModel.deleted_at.is_(None)
                )
                existing = session.scalar(stmt_new)

                if existing:
                    raise MetadataError(
                        f"Destination path already exists: {new_path}", path=new_path
                    )

                # If it's a file, update it
                if file_path:
                    file_path.virtual_path = new_path
                    file_path.updated_at = datetime.now(UTC)

                # If it's a directory, update all child paths
                children: list[Any] = []
                if is_directory:
                    # Find all children (files with paths starting with old_path/)
                    dir_prefix = old_path.rstrip("/") + "/"
                    stmt_children = select(FilePathModel).where(
                        FilePathModel.virtual_path.startswith(dir_prefix),
                        FilePathModel.deleted_at.is_(None),
                    )
                    children = list(session.scalars(stmt_children).all())

                    # Update each child's path
                    for child in children:
                        # Replace old_path prefix with new_path
                        relative_path = child.virtual_path[len(old_path) :]
                        child.virtual_path = new_path + relative_path
                        child.updated_at = datetime.now(UTC)

                # Update directory index (Issue #924)
                # Get tenant_id from file_path (file rename) or first child (directory rename)
                tenant_id: str | None = None
                if file_path:
                    tenant_id = file_path.tenant_id
                elif is_directory and children:
                    tenant_id = children[0].tenant_id

                self._rename_in_directory_index(
                    session, old_path, new_path, tenant_id, is_directory=is_directory
                )

                session.commit()

            # Invalidate cache for both paths
            if self._cache_enabled and self._cache:
                self._cache.invalidate_path(old_path)
                self._cache.invalidate_path(new_path)
                # Also invalidate parent directories (this invalidates their listings too)
                old_parent = old_path.rsplit("/", 1)[0] or "/"
                new_parent = new_path.rsplit("/", 1)[0] or "/"
                self._cache.invalidate_path(old_parent)
                if old_parent != new_parent:
                    self._cache.invalidate_path(new_parent)

                # If directory, invalidate all children too
                if is_directory:
                    # Invalidate the directory itself in cache
                    self._cache.invalidate_path(old_path + "/")
                    self._cache.invalidate_path(new_path + "/")

        except MetadataError:
            raise
        except Exception as e:
            raise MetadataError(f"Failed to rename path: {e}", path=old_path) from e

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

    def is_implicit_directory(self, path: str) -> bool:
        """
        Check if path is an implicit directory (has files beneath it).

        In NexusFS, directories are implicit - they exist if any files exist
        with the directory path as a prefix.

        Args:
            path: Virtual path to check

        Returns:
            True if path is an implicit directory, False otherwise
        """
        try:
            # Normalize path - ensure it ends with / for prefix matching
            dir_path = path.rstrip("/") + "/"

            with self.SessionLocal() as session:
                # Check if any files exist with this path as a prefix
                stmt = (
                    select(FilePathModel.path_id)
                    .where(
                        FilePathModel.virtual_path.like(f"{dir_path}%"),
                        FilePathModel.deleted_at.is_(None),
                    )
                    .limit(1)
                )
                has_children = session.scalar(stmt) is not None
                return has_children
        except OSError:
            # Database query failed - assume not a directory
            return False

    def list(
        self,
        prefix: str = "",
        recursive: bool = True,
        tenant_id: str | None = None,
    ) -> list[FileMetadata]:
        """
        List all files with given path prefix.

        PERFORMANCE OPTIMIZATION (v0.7.0): Non-recursive listings now filter at database level
        instead of loading all files into memory and filtering in Python.

        PERFORMANCE OPTIMIZATION (Issue #904): PREWHERE-style tenant filtering at DB level.
        When tenant_id is provided, filters by tenant at the database level (indexed),
        reducing rows loaded by 30-90% in multi-tenant deployments.

        Args:
            prefix: Path prefix to filter by
            recursive: If True, include all nested files. If False, only direct children.
            tenant_id: Optional tenant ID to filter by (PREWHERE optimization).
                      When provided, only files belonging to this tenant are returned.
                      When None, all files are returned (backward compatible).

        Returns:
            List of file metadata

        Examples:
            # Recursive (default) - all files under /workspace
            >>> store.list("/workspace/", recursive=True)
            ['/workspace/file.txt', '/workspace/sub/deep.txt']

            # Non-recursive - only direct children of /workspace
            >>> store.list("/workspace/", recursive=False)
            ['/workspace/file.txt']  # /workspace/sub/deep.txt excluded

            # Tenant-filtered (Issue #904) - only files for specific tenant
            >>> store.list("/workspace/", tenant_id="org_acme")
            ['/workspace/acme_file.txt']  # Only org_acme's files
        """
        # Check cache first (note: cache key includes recursive flag and tenant_id)
        tenant_key = tenant_id or "all"
        cache_key = f"{prefix}:{'r' if recursive else 'nr'}:t={tenant_key}"
        if self._cache_enabled and self._cache:
            cached = self._cache.get_list(cache_key)
            if cached is not None:
                logger.debug(
                    f"[METADATA-CACHE] HIT for list({prefix}, recursive={recursive}), {len(cached)} items"
                )
                return cached
            logger.debug(f"[METADATA-CACHE] MISS for list({prefix}, recursive={recursive})")

            # OPTIMIZATION: Check if a PARENT prefix is cached and filter locally
            # This avoids DB queries for /skills/system/ when /skills/ is already cached
            # Note: Parent cache lookup must also match tenant_id
            if recursive and prefix and prefix != "/":
                parent_prefix = prefix.rstrip("/")
                prev_prefix = None  # Track previous to detect infinite loop
                # Try progressively shorter parent prefixes
                while "/" in parent_prefix and parent_prefix != prev_prefix:
                    prev_prefix = parent_prefix
                    parent_prefix = parent_prefix.rsplit("/", 1)[0] + "/"
                    if parent_prefix == "/":
                        parent_prefix = "/"
                    parent_cache_key = f"{parent_prefix}:r:t={tenant_key}"
                    parent_cached = self._cache.get_list(parent_cache_key)
                    if parent_cached is not None:
                        # Filter parent cache for our prefix
                        filtered = [item for item in parent_cached if item.path.startswith(prefix)]
                        logger.debug(
                            f"[METADATA-CACHE] PARENT-HIT for list({prefix}), "
                            f"filtered {len(filtered)} from {len(parent_cached)} items in {parent_prefix}"
                        )
                        # Cache the filtered result for future use
                        self._cache.set_list(cache_key, filtered)
                        return filtered
                    if parent_prefix == "/":
                        break

            logger.debug(f"[METADATA-CACHE] MISS for list({prefix}, recursive={recursive})")

        try:
            with self.SessionLocal() as session:
                # Build base conditions list
                # Issue #904: PREWHERE-style tenant filtering at DB level
                # Include matching tenant, 'default' tenant, AND legacy NULL files
                base_conditions: list[Any] = [FilePathModel.deleted_at.is_(None)]
                if tenant_id is not None:
                    from sqlalchemy import or_

                    base_conditions.append(
                        or_(
                            FilePathModel.tenant_id == tenant_id,
                            FilePathModel.tenant_id == "default",  # Include default tenant files
                            FilePathModel.tenant_id.is_(None),
                        )
                    )

                if prefix:
                    if recursive:
                        # Recursive: All files under prefix
                        stmt = (
                            select(FilePathModel)
                            .where(
                                FilePathModel.virtual_path.like(f"{prefix}%"),
                                *base_conditions,
                            )
                            .order_by(FilePathModel.virtual_path)
                        )
                    else:
                        # Non-recursive: Only direct children (no nested subdirectories)
                        # PERFORMANCE FIX: Filter at database level using SQL NOT LIKE
                        # Pattern: path starts with prefix BUT doesn't have another / after the prefix
                        #
                        # Example for prefix="/workspace/":
                        #   MATCH:   /workspace/file.txt     (direct child)
                        #   NO MATCH: /workspace/sub/file.txt (in subdirectory)
                        #
                        # SQL: WHERE path LIKE '/workspace/%' AND path NOT LIKE '/workspace/%/%'
                        stmt = (
                            select(FilePathModel)
                            .where(
                                FilePathModel.virtual_path.like(f"{prefix}%"),
                                ~FilePathModel.virtual_path.like(
                                    f"{prefix}%/%"
                                ),  # ~ is SQLAlchemy NOT
                                *base_conditions,
                            )
                            .order_by(FilePathModel.virtual_path)
                        )
                else:
                    # No prefix - list from root
                    if recursive:
                        stmt = (
                            select(FilePathModel)
                            .where(*base_conditions)
                            .order_by(FilePathModel.virtual_path)
                        )
                    else:
                        # Non-recursive from root: only files directly in / (no subdirectories)
                        # Match paths like "/file.txt" but not "/subdir/file.txt"
                        # Pattern: starts with / and has no more / after the first one
                        stmt = (
                            select(FilePathModel)
                            .where(
                                FilePathModel.virtual_path.like("/%"),  # Must start with /
                                ~FilePathModel.virtual_path.like("/%/%"),  # But not have another /
                                *base_conditions,
                            )
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
                            version=file_path.current_version,  # Version tracking (v0.3.5)
                            tenant_id=file_path.tenant_id,  # P0 SECURITY: Defense-in-depth (v0.7.0)
                        )
                    )

                # Cache the results (use cache_key that includes recursive flag)
                if self._cache_enabled and self._cache:
                    self._cache.set_list(cache_key, results)
                    logger.debug(
                        f"[METADATA-CACHE] SET for list({prefix}, recursive={recursive}), {len(results)} items"
                    )

                return results
        except Exception as e:
            raise MetadataError(f"Failed to list metadata: {e}") from e

    def list_paginated(
        self,
        prefix: str = "",
        recursive: bool = True,
        limit: int = 1000,
        cursor: str | None = None,
        tenant_id: str | None = None,
    ) -> PaginatedResult:
        """List files with cursor-based (keyset) pagination (Issue #937).

        Uses keyset pagination for O(log n) performance at any page depth.
        Contrast with OFFSET which is O(n) for deep pages.

        SQL Pattern:
            SELECT * FROM file_paths
            WHERE (virtual_path, path_id) > (:cursor_path, :cursor_id)
              AND virtual_path LIKE :prefix || '%'
              AND deleted_at IS NULL
            ORDER BY virtual_path, path_id
            LIMIT :limit + 1

        Args:
            prefix: Path prefix to filter by
            recursive: If True, include nested files. If False, only direct children.
            limit: Maximum items per page (1-10000, default: 1000)
            cursor: Continuation token from previous page
            tenant_id: Optional tenant filter (PREWHERE optimization from Issue #904)

        Returns:
            PaginatedResult with items, next_cursor, and has_more flag

        Raises:
            CursorError: If cursor is invalid or filters changed
            MetadataError: If database query fails
        """
        from nexus.core.metadata import PaginatedResult
        from nexus.core.pagination import CursorData, CursorError, decode_cursor, encode_cursor

        # Validate and cap limit
        limit = min(max(1, limit), 10000)  # 1 <= limit <= 10000

        # Build filters dict for cursor validation
        filters = {
            "prefix": prefix,
            "recursive": recursive,
            "tenant_id": tenant_id,
        }

        # Decode cursor if provided
        cursor_data: CursorData | None = None
        if cursor:
            cursor_data = decode_cursor(cursor, filters)

        try:
            with self.SessionLocal() as session:
                # Build base conditions
                conditions: builtins.list[Any] = [FilePathModel.deleted_at.is_(None)]

                # Tenant filtering (Issue #904 PREWHERE optimization)
                # Include matching tenant, 'default' tenant, AND legacy NULL files
                if tenant_id is not None:
                    from sqlalchemy import or_

                    conditions.append(
                        or_(
                            FilePathModel.tenant_id == tenant_id,
                            FilePathModel.tenant_id == "default",  # Include default tenant files
                            FilePathModel.tenant_id.is_(None),
                        )
                    )

                # Prefix filtering
                if prefix:
                    conditions.append(FilePathModel.virtual_path.like(f"{prefix}%"))
                    if not recursive:
                        # Non-recursive: exclude nested paths
                        conditions.append(~FilePathModel.virtual_path.like(f"{prefix}%/%"))
                elif not recursive:
                    # Root non-recursive
                    conditions.append(FilePathModel.virtual_path.like("/%"))
                    conditions.append(~FilePathModel.virtual_path.like("/%/%"))

                # Keyset pagination condition
                if cursor_data:
                    from sqlalchemy import tuple_

                    conditions.append(
                        tuple_(FilePathModel.virtual_path, FilePathModel.path_id)
                        > (cursor_data.path, cursor_data.path_id)
                    )

                # Build query with ORDER BY for stable pagination
                stmt = (
                    select(FilePathModel)
                    .where(*conditions)
                    .order_by(FilePathModel.virtual_path, FilePathModel.path_id)
                    .limit(limit + 1)  # +1 to detect has_more
                )

                rows = builtins.list(session.scalars(stmt))

                # Check if more results exist
                has_more = len(rows) > limit
                if has_more:
                    rows = rows[:limit]  # Trim extra row

                # Convert to FileMetadata
                items = [
                    FileMetadata(
                        path=row.virtual_path,
                        backend_name=row.backend_id,
                        physical_path=row.physical_path,
                        size=row.size_bytes,
                        etag=row.content_hash,
                        mime_type=row.file_type,
                        created_at=row.created_at,
                        modified_at=row.updated_at,
                        version=row.current_version,
                        tenant_id=row.tenant_id,
                    )
                    for row in rows
                ]

                # Generate next cursor
                next_cursor = None
                if has_more and rows:
                    last_row = rows[-1]
                    next_cursor = encode_cursor(
                        last_path=last_row.virtual_path,
                        last_path_id=last_row.path_id,
                        filters=filters,
                    )

                return PaginatedResult(
                    items=items,
                    next_cursor=next_cursor,
                    has_more=has_more,
                    total_count=None,  # Skip expensive COUNT(*) by default
                )

        except CursorError:
            raise  # Re-raise cursor errors as-is
        except Exception as e:
            raise MetadataError(f"Failed to list paginated metadata: {e}") from e

    def list_with_pattern(self, pattern: str) -> builtins.list[FileMetadata]:
        """
        List all files matching a SQL LIKE pattern.

        This is a performance optimization for glob() - instead of loading
        all files and regex matching in Python, we use SQL LIKE queries
        which are much faster for simple patterns.

        Args:
            pattern: SQL LIKE pattern (use % for *, _ for ?)
                    Example: "/src/%.py" matches all .py files in /src/

        Returns:
            List of file metadata matching the pattern

        Performance:
            - Simple patterns (*.py): ~100x faster than loading all files
            - Uses indexed SQL query instead of full table scan with Python regex
        """
        try:
            with self.SessionLocal() as session:
                stmt = (
                    select(FilePathModel)
                    .where(
                        FilePathModel.virtual_path.like(pattern),
                        FilePathModel.deleted_at.is_(None),
                    )
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
                            version=file_path.current_version,
                        )
                    )

                return results
        except Exception as e:
            raise MetadataError(f"Failed to list metadata with pattern: {e}") from e

    def close(self) -> None:
        """Close database connection and dispose of engine."""
        import gc
        import sys
        import time

        if not hasattr(self, "engine"):
            return  # Already closed or never initialized

        # Prevent double-close
        if getattr(self, "_closed", False):
            return
        self._closed = True

        try:
            # CRITICAL: Force garbage collection BEFORE closing database
            # This ensures any lingering session references are cleaned up first
            # Especially important on Windows where sessions may hold file locks
            gc.collect()
            gc.collect(1)
            gc.collect(2)
            # Brief wait to let OS release file handles
            if sys.platform == "win32":
                time.sleep(0.05)  # 50ms on Windows
            else:
                time.sleep(0.01)  # 10ms elsewhere

            # SQLite-specific cleanup
            if self.db_type == "sqlite":
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
                    # Ignore errors during checkpoint (e.g., database already closed or locked)
                    pass

            # Dispose of the connection pool - this closes all connections
            # Note: All sessions should be closed via context managers (with statements)
            # before this point. The dispose() call will close any remaining connections.
            self.engine.dispose()

            # CRITICAL: On Windows, force GC after disposal to release lingering references
            gc.collect()
            gc.collect(1)
            gc.collect(2)
            # Minimal wait for OS to release handles
            if sys.platform == "win32":
                time.sleep(0.1)  # 100ms on Windows
            else:
                time.sleep(0.01)  # 10ms elsewhere

            # SQLite-specific file cleanup
            if self.db_type == "sqlite" and self.db_path:
                # Additional cleanup: Try to remove any lingering SQLite temp files
                # This helps with test cleanup when using tempfile.TemporaryDirectory()
                try:
                    import os

                    for suffix in ["-wal", "-shm", "-journal"]:
                        temp_file = Path(str(self.db_path) + suffix)
                        if temp_file.exists():
                            # Retry a few times with small delays
                            # Windows needs more retries due to file locking behavior
                            max_retries = 10 if sys.platform == "win32" else 3
                            for attempt in range(max_retries):
                                try:
                                    os.remove(temp_file)
                                    break
                                except (OSError, PermissionError):
                                    # Short exponential backoff: 10ms, 20ms, 40ms, 80ms, ...
                                    # Cap at 500ms to avoid long delays
                                    wait_time = 0.01 * (2**attempt)
                                    time.sleep(min(wait_time, 0.5))
                                    # On last attempt, do one final GC
                                    if attempt == max_retries - 1:
                                        gc.collect()
                                        gc.collect(1)
                                        gc.collect(2)
                                        time.sleep(0.1)
                except (OSError, PermissionError):
                    # Ignore errors - these files may not exist or may be locked
                    pass
        finally:
            # Mark as closed even if cleanup partially failed
            self._closed = True

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
                        version=file_path.current_version,  # Version tracking (v0.3.5)
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
                # Group paths by tenant_id for directory index update (Issue #924)
                paths_by_tenant: dict[str | None, list[str]] = {}
                for file_path in session.scalars(stmt):
                    file_path.deleted_at = datetime.now(UTC)
                    deleted_paths.append(file_path.virtual_path)
                    # Group by tenant_id
                    if file_path.tenant_id not in paths_by_tenant:
                        paths_by_tenant[file_path.tenant_id] = []
                    paths_by_tenant[file_path.tenant_id].append(file_path.virtual_path)

                # Remove from directory index (Issue #924)
                for tenant_id, tenant_paths in paths_by_tenant.items():
                    self._remove_from_directory_index_batch(session, tenant_paths, tenant_id)

                session.commit()

            # Invalidate cache for all deleted paths
            if self._cache_enabled and self._cache:
                for path in deleted_paths:
                    self._cache.invalidate_path(path)
        except Exception as e:
            raise MetadataError(f"Failed to delete batch metadata: {e}") from e

    def put_batch(self, metadata_list: Sequence[FileMetadata]) -> None:
        """
        Store or update multiple file metadata entries in a single transaction WITH VERSION TRACKING.

        PERFORMANCE OPTIMIZATION (v0.7.0): Uses bulk_insert_mappings() for new files,
        providing 10-20x speedup for large batches.

        When updating existing files, creates version history entries for each update
        preserving the old content hashes before updating to new content.

        Performance:
        - Old: 10,000 files in ~5-10 seconds (individual INSERTs)
        - New: 10,000 files in ~0.5 seconds (bulk INSERT)

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
                # Use row-level locking to prevent concurrent version conflicts
                # CRITICAL: Use nowait=True to prevent indefinite blocking
                paths: list[str] = [m.path for m in metadata_list]
                stmt = (
                    select(FilePathModel)
                    .where(
                        FilePathModel.virtual_path.in_(paths),
                        FilePathModel.deleted_at.is_(None),
                    )
                    .with_for_update(nowait=True)  # Fail fast instead of blocking indefinitely
                )

                # Build dict of existing entries
                existing = {fp.virtual_path: fp for fp in session.scalars(stmt)}

                # PERFORMANCE OPTIMIZATION: Separate updates from inserts
                # Updates need ORM for version tracking, but inserts can use bulk operations
                updates: list[FileMetadata] = []
                new_files: list[FileMetadata] = []

                for metadata in metadata_list:
                    if metadata.path in existing:
                        updates.append(metadata)
                    else:
                        new_files.append(metadata)

                # Process UPDATES using ORM (need version tracking)
                for metadata in updates:
                    # FILE UPDATE - Increment version and create history entry for NEW version
                    file_path = existing[metadata.path]

                    # Update existing record with new content
                    file_path.backend_id = metadata.backend_name
                    file_path.physical_path = metadata.physical_path
                    file_path.size_bytes = metadata.size
                    file_path.content_hash = metadata.etag  # NEW content hash
                    file_path.file_type = metadata.mime_type
                    file_path.updated_at = metadata.modified_at or datetime.now(UTC)

                    # Only create version history if we have actual content (etag is not None)
                    if metadata.etag is not None:
                        # Get the current version entry to link lineage
                        prev_version_stmt = (
                            select(VersionHistoryModel)
                            .where(
                                VersionHistoryModel.resource_type == "file",
                                VersionHistoryModel.resource_id == file_path.path_id,
                                VersionHistoryModel.version_number == file_path.current_version,
                            )
                            .limit(1)
                        )
                        prev_version = session.scalar(prev_version_stmt)

                        # Atomically increment version at database level to prevent race conditions
                        session.execute(
                            update(FilePathModel)
                            .where(FilePathModel.path_id == file_path.path_id)
                            .values(current_version=FilePathModel.current_version + 1)
                        )
                        # Refresh to get the new version number
                        session.refresh(file_path)

                        # Create version history entry for NEW version
                        version_entry = VersionHistoryModel(
                            version_id=str(uuid.uuid4()),
                            resource_type="file",
                            resource_id=file_path.path_id,
                            version_number=file_path.current_version,  # NEW version number
                            content_hash=metadata.etag,  # NEW content hash
                            size_bytes=metadata.size,
                            mime_type=metadata.mime_type,
                            parent_version_id=prev_version.version_id if prev_version else None,
                            source_type="original",
                            created_at=datetime.now(UTC),
                            created_by=metadata.created_by,  # Track who created this version
                        )
                        version_entry.validate()
                        session.add(version_entry)

                # PERFORMANCE OPTIMIZATION: Process NEW FILES using bulk_insert_mappings()
                # This bypasses ORM overhead and uses a single INSERT statement for all files
                # Performance: 10-20x faster than individual session.add() calls
                if new_files:
                    now = datetime.now(UTC)

                    # Generate path_ids upfront so we can reference them in version history
                    path_id_map = {m.path: str(uuid.uuid4()) for m in new_files}

                    # Prepare file_paths mappings for bulk insert
                    file_path_mappings = [
                        {
                            "path_id": path_id_map[m.path],
                            "virtual_path": m.path,
                            "backend_id": m.backend_name,
                            "physical_path": m.physical_path,
                            "size_bytes": m.size,
                            "content_hash": m.etag,
                            "file_type": m.mime_type,
                            "created_at": m.created_at or now,
                            "updated_at": m.modified_at or now,
                            "current_version": 1,
                            "tenant_id": m.tenant_id,
                            "deleted_at": None,
                            "locked_by": None,
                            "accessed_at": None,
                        }
                        for m in new_files
                    ]

                    # Bulk insert file paths - SINGLE INSERT statement
                    session.bulk_insert_mappings(FilePathModel, file_path_mappings)  # type: ignore[arg-type]

                    # Prepare version history mappings (only for files with content)
                    version_mappings = [
                        {
                            "version_id": str(uuid.uuid4()),
                            "resource_type": "file",
                            "resource_id": path_id_map[m.path],
                            "version_number": 1,
                            "content_hash": m.etag,
                            "size_bytes": m.size,
                            "mime_type": m.mime_type,
                            "parent_version_id": None,
                            "source_type": "original",
                            "created_at": m.created_at or now,
                            "is_snapshot": False,
                        }
                        for m in new_files
                        if m.etag is not None  # Only create version history if we have content
                    ]

                    # Bulk insert version history - SINGLE INSERT statement
                    if version_mappings:
                        session.bulk_insert_mappings(VersionHistoryModel, version_mappings)  # type: ignore[arg-type]

                # Update directory index for all files (Issue #924)
                self._update_directory_index_batch(session, metadata_list)

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
                # PERFORMANCE FIX: Single query with subquery instead of 2 separate queries
                # This reduces database round-trips by 50% for metadata lookups
                subquery = (
                    select(FilePathModel.path_id)
                    .where(
                        FilePathModel.virtual_path == path,
                        FilePathModel.deleted_at.is_(None),
                    )
                    .scalar_subquery()
                )
                metadata_stmt = select(FileMetadataModel).where(
                    FileMetadataModel.path_id == subquery,
                    FileMetadataModel.key == key,
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

    def get_file_metadata_bulk(self, paths: builtins.list[str], key: str) -> dict[str, Any]:
        """
        Get a specific metadata value for multiple files in bulk.

        Optimized for batch operations - uses single query instead of N queries.

        Args:
            paths: List of virtual paths
            key: Metadata key to retrieve

        Returns:
            Dict mapping path -> metadata value (only for paths with the key set)
        """
        if not paths:
            return {}

        results: dict[str, Any] = {}

        # Check cache first for all paths
        uncached_paths = []
        if self._cache_enabled and self._cache:
            for path in paths:
                cached = self._cache.get_kv(path, key)
                if cached is not _CACHE_MISS:
                    if cached is not None:
                        results[path] = cached
                else:
                    uncached_paths.append(path)
        else:
            uncached_paths = list(paths)

        if not uncached_paths:
            return results

        try:
            with self.SessionLocal() as session:
                # Single bulk query with join
                stmt = (
                    select(FilePathModel.virtual_path, FileMetadataModel.value)
                    .join(
                        FileMetadataModel,
                        FilePathModel.path_id == FileMetadataModel.path_id,
                    )
                    .where(
                        FilePathModel.virtual_path.in_(uncached_paths),
                        FilePathModel.deleted_at.is_(None),
                        FileMetadataModel.key == key,
                    )
                )
                rows = session.execute(stmt).fetchall()

                # Process results
                found_paths = set()
                for virtual_path, value_json in rows:
                    if value_json:
                        value = json.loads(value_json)
                        results[virtual_path] = value
                        found_paths.add(virtual_path)
                        # Cache the result
                        if self._cache_enabled and self._cache:
                            self._cache.set_kv(virtual_path, key, value)

                # Cache negative results for paths not found
                if self._cache_enabled and self._cache:
                    for path in uncached_paths:
                        if path not in found_paths:
                            self._cache.set_kv(path, key, None)

                return results
        except Exception as e:
            raise MetadataError(f"Failed to get bulk file metadata: {e}") from e

    def get_searchable_text(self, path: str) -> str | None:
        """
        Get searchable text for a file, checking both cache sources.

        This method provides a unified interface for getting searchable text,
        checking both connector cache (content_cache.content_text) and local
        file metadata (file_metadata with key='parsed_text').

        Priority:
        1. content_cache.content_text (for connector files)
        2. file_metadata.parsed_text (for local files)

        Args:
            path: Virtual path

        Returns:
            Searchable text content or None if not available
        """
        try:
            with self.SessionLocal() as session:
                # PERFORMANCE FIX: Use subquery instead of 2 separate queries
                path_subquery = (
                    select(FilePathModel.path_id)
                    .where(
                        FilePathModel.virtual_path == path,
                        FilePathModel.deleted_at.is_(None),
                    )
                    .scalar_subquery()
                )

                # 1. Check content_cache first (connector files)
                cache_stmt = select(ContentCacheModel.content_text).where(
                    ContentCacheModel.path_id == path_subquery,
                    ContentCacheModel.stale == False,  # noqa: E712
                )
                content_text = session.scalar(cache_stmt)

                if content_text:
                    return content_text

                # 2. Fall back to file_metadata (local files)
                metadata_stmt = select(FileMetadataModel.value).where(
                    FileMetadataModel.path_id == path_subquery,
                    FileMetadataModel.key == "parsed_text",
                )
                metadata_value = session.scalar(metadata_stmt)

                if metadata_value:
                    parsed: str | None = json.loads(metadata_value)
                    return parsed

                return None
        except Exception:
            return None

    def get_searchable_text_bulk(self, paths: builtins.list[str]) -> dict[str, str]:
        """
        Get searchable text for multiple files in bulk.

        Optimized for batch operations like grep. Checks both content_cache
        and file_metadata tables in single queries.

        Args:
            paths: List of virtual paths

        Returns:
            Dict mapping path -> searchable text (only for paths with text)
        """
        if not paths:
            return {}

        results: dict[str, str] = {}

        try:
            with self.SessionLocal() as session:
                # Get path_ids for all paths
                path_stmt = select(FilePathModel.virtual_path, FilePathModel.path_id).where(
                    FilePathModel.virtual_path.in_(paths),
                    FilePathModel.deleted_at.is_(None),
                )
                path_rows = session.execute(path_stmt).fetchall()
                path_to_id = {row[0]: row[1] for row in path_rows}

                if not path_to_id:
                    return {}

                path_ids = list(path_to_id.values())
                id_to_path = {v: k for k, v in path_to_id.items()}

                # 1. Bulk query content_cache (connector files)
                cache_stmt = select(
                    ContentCacheModel.path_id, ContentCacheModel.content_text
                ).where(
                    ContentCacheModel.path_id.in_(path_ids),
                    ContentCacheModel.stale == False,  # noqa: E712
                    ContentCacheModel.content_text.isnot(None),
                )
                cache_rows = session.execute(cache_stmt).fetchall()

                found_path_ids = set()
                for path_id, content_text in cache_rows:
                    if content_text and path_id in id_to_path:
                        results[id_to_path[path_id]] = content_text
                        found_path_ids.add(path_id)

                # 2. For remaining paths, check file_metadata (local files)
                remaining_path_ids = [pid for pid in path_ids if pid not in found_path_ids]

                if remaining_path_ids:
                    metadata_stmt = select(
                        FileMetadataModel.path_id, FileMetadataModel.value
                    ).where(
                        FileMetadataModel.path_id.in_(remaining_path_ids),
                        FileMetadataModel.key == "parsed_text",
                        FileMetadataModel.value.isnot(None),
                    )
                    metadata_rows = session.execute(metadata_stmt).fetchall()

                    for path_id, value in metadata_rows:
                        if value and path_id in id_to_path:
                            results[id_to_path[path_id]] = json.loads(value)

                return results
        except Exception:
            return {}

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

    # Work detection queries (using SQL views - delegated to WorkQueryBuilder)

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
        with self.SessionLocal() as session:
            return WorkQueryBuilder.get_ready_work(session, limit)

    def get_pending_work(self, limit: int | None = None) -> builtins.list[dict[str, Any]]:
        """Get files with status='pending' ordered by priority.

        Uses the pending_work_items SQL view.

        Args:
            limit: Optional limit on number of results

        Returns:
            List of work item dicts
        """
        with self.SessionLocal() as session:
            return WorkQueryBuilder.get_pending_work(session, limit)

    def get_blocked_work(self, limit: int | None = None) -> builtins.list[dict[str, Any]]:
        """Get files that are blocked by dependencies.

        Uses the blocked_work_items SQL view.

        Args:
            limit: Optional limit on number of results

        Returns:
            List of work item dicts with blocker_count
        """
        with self.SessionLocal() as session:
            return WorkQueryBuilder.get_blocked_work(session, limit)

    def get_in_progress_work(self, limit: int | None = None) -> builtins.list[dict[str, Any]]:
        """Get files currently being processed.

        Uses the in_progress_work SQL view.

        Args:
            limit: Optional limit on number of results

        Returns:
            List of work item dicts with worker_id and started_at
        """
        with self.SessionLocal() as session:
            return WorkQueryBuilder.get_in_progress_work(session, limit)

    def get_work_by_priority(self, limit: int | None = None) -> builtins.list[dict[str, Any]]:
        """Get all work items ordered by priority.

        Uses the work_by_priority SQL view.

        Args:
            limit: Optional limit on number of results

        Returns:
            List of work item dicts
        """
        with self.SessionLocal() as session:
            return WorkQueryBuilder.get_work_by_priority(session, limit)

    # Version tracking methods (v0.3.5 - delegated to VersionManager)

    def get_version(self, path: str, version: int) -> FileMetadata | None:
        """Get a specific version of a file.

        Retrieves file metadata for a specific version from version history.
        The content_hash in the returned metadata can be used to fetch the
        actual content from CAS storage.

        Args:
            path: Virtual path
            version: Version number to retrieve

        Returns:
            FileMetadata for the specified version, or None if not found

        Example:
            >>> # Get version 2 of a file
            >>> metadata = store.get_version("/workspace/data.txt", version=2)
            >>> if metadata:
            ...     content_hash = metadata.etag
            ...     # Use content_hash to fetch from CAS
        """
        # Use path@version format for VersionManager
        versioned_path = f"{path}@{version}"
        with self.SessionLocal() as session:
            return VersionManager.get_version(session, versioned_path)

    def list_versions(self, path: str) -> builtins.list[dict[str, Any]]:
        """List all versions of a file.

        Returns version history with metadata for each version.

        Args:
            path: Virtual path

        Returns:
            List of version info dicts ordered by version number (newest first)

        Example:
            >>> versions = store.list_versions("/workspace/SKILL.md")
            >>> for v in versions:
            ...     print(f"v{v['version']}: {v['size']} bytes, {v['created_at']}")
        """
        with self.SessionLocal() as session:
            return VersionManager.list_versions(session, path)

    def rollback(self, path: str, version: int, created_by: str | None = None) -> None:
        """Rollback file to a previous version.

        Updates the file to point to an older version's content.
        Creates a new version entry marking this as a rollback.

        Args:
            path: Virtual path
            version: Version number to rollback to
            created_by: User or agent ID who performed the rollback (optional)

        Example:
            >>> # Rollback to version 2
            >>> store.rollback("/workspace/data.txt", version=2, created_by="alice")
        """
        with self.SessionLocal() as session:
            VersionManager.rollback(session, path, version, created_by=created_by)
            session.commit()

        # Invalidate cache
        if self._cache_enabled and self._cache:
            self._cache.invalidate_path(path)

    def get_version_diff(self, path: str, v1: int, v2: int) -> dict[str, Any]:
        """Get diff information between two versions.

        Returns metadata differences between versions.
        For content diff, retrieve both versions and compare.

        Args:
            path: Virtual path
            v1: First version number
            v2: Second version number

        Returns:
            Dict with diff information

        Example:
            >>> diff = store.get_version_diff("/workspace/file.txt", v1=1, v2=3)
            >>> print(f"Size changed: {diff['size_v1']} -> {diff['size_v2']}")
            >>> print(f"Content changed: {diff['content_changed']}")
        """
        with self.SessionLocal() as session:
            return VersionManager.get_version_diff(session, path, v1, v2)

    # =========================================================================
    # Directory Index Methods (Issue #924)
    # =========================================================================

    def _update_directory_index(
        self,
        session: Any,
        path: str,
        tenant_id: str | None,
        is_directory: bool = False,
    ) -> None:
        """Add file/directory and all parent directories to the directory index.

        Called by put() to maintain the sparse directory index.

        Args:
            session: SQLAlchemy session
            path: Virtual path (e.g., "/workspace/src/file.txt")
            tenant_id: Tenant ID for multi-tenant isolation
            is_directory: True if path is a directory, False if file
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        # Use default tenant if not specified (required for DB constraint)
        effective_tenant_id = tenant_id or "default"

        # Normalize path - remove trailing slash for files
        path = path.rstrip("/") if not is_directory else path.rstrip("/") + "/"

        # Parse path into components
        # e.g., "/workspace/src/file.txt" -> ["workspace", "src", "file.txt"]
        parts = path.strip("/").split("/")
        if not parts or parts == [""]:
            return  # Root path, nothing to index

        entries_to_upsert: list[dict[str, Any]] = []

        # Build entries for each level of the path
        for depth in range(len(parts)):
            parent_path = "/" if depth == 0 else "/" + "/".join(parts[:depth]) + "/"
            entry_name = parts[depth]
            entry_type = "file" if depth == len(parts) - 1 and not is_directory else "directory"

            entries_to_upsert.append(
                {
                    "tenant_id": effective_tenant_id,
                    "parent_path": parent_path,
                    "entry_name": entry_name,
                    "entry_type": entry_type,
                }
            )

        # Upsert all entries (INSERT ... ON CONFLICT DO UPDATE)
        if self.db_type == "postgresql":
            for entry in entries_to_upsert:
                stmt = pg_insert(DirectoryEntryModel).values(**entry)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["tenant_id", "parent_path", "entry_name"],
                    set_={"entry_type": entry["entry_type"], "updated_at": datetime.now(UTC)},
                )
                session.execute(stmt)
        else:
            # SQLite uses INSERT OR REPLACE
            for entry in entries_to_upsert:
                sqlite_stmt = sqlite_insert(DirectoryEntryModel).values(**entry)
                sqlite_stmt = sqlite_stmt.on_conflict_do_update(
                    index_elements=["tenant_id", "parent_path", "entry_name"],
                    set_={"entry_type": entry["entry_type"], "updated_at": datetime.now(UTC)},
                )
                session.execute(sqlite_stmt)

    def _update_directory_index_batch(
        self,
        session: Any,
        metadata_list: Sequence[FileMetadata],
    ) -> None:
        """Bulk update directory index for multiple files.

        Optimized for put_batch() - collects all entries and performs bulk upsert.

        Args:
            session: SQLAlchemy session
            metadata_list: List of file metadata being written
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        # Collect all unique entries across all paths
        entries_map: dict[tuple[str, str, str], dict[str, Any]] = {}

        for metadata in metadata_list:
            path = metadata.path.rstrip("/")
            # Use default tenant if not specified (required for DB constraint)
            tenant_id = metadata.tenant_id or "default"
            parts = path.strip("/").split("/")

            if not parts or parts == [""]:
                continue

            for depth in range(len(parts)):
                parent_path = "/" if depth == 0 else "/" + "/".join(parts[:depth]) + "/"
                entry_name = parts[depth]
                entry_type = "file" if depth == len(parts) - 1 else "directory"

                key = (tenant_id, parent_path, entry_name)
                # Only update if not already set, or if this is a file (takes precedence)
                if key not in entries_map or entry_type == "file":
                    entries_map[key] = {
                        "tenant_id": tenant_id,
                        "parent_path": parent_path,
                        "entry_name": entry_name,
                        "entry_type": entry_type,
                    }

        if not entries_map:
            return

        # Bulk upsert all entries
        entries = list(entries_map.values())

        if self.db_type == "postgresql":
            for entry in entries:
                stmt = pg_insert(DirectoryEntryModel).values(**entry)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["tenant_id", "parent_path", "entry_name"],
                    set_={"entry_type": entry["entry_type"], "updated_at": datetime.now(UTC)},
                )
                session.execute(stmt)
        else:
            for entry in entries:
                sqlite_stmt = sqlite_insert(DirectoryEntryModel).values(**entry)
                sqlite_stmt = sqlite_stmt.on_conflict_do_update(
                    index_elements=["tenant_id", "parent_path", "entry_name"],
                    set_={"entry_type": entry["entry_type"], "updated_at": datetime.now(UTC)},
                )
                session.execute(sqlite_stmt)

    def _remove_from_directory_index(
        self,
        session: Any,
        path: str,
        tenant_id: str | None,
    ) -> None:
        """Remove a file entry from the directory index.

        Called by delete(). Only removes the file entry, not parent directories
        (they may still have other children).

        Args:
            session: SQLAlchemy session
            path: Virtual path of file being deleted
            tenant_id: Tenant ID for multi-tenant isolation
        """
        path = path.rstrip("/")
        parts = path.strip("/").split("/")

        if not parts or parts == [""]:
            return

        # Get parent path and entry name
        parent_path = "/" if len(parts) == 1 else "/" + "/".join(parts[:-1]) + "/"
        entry_name = parts[-1]

        # Delete the file entry
        session.execute(
            select(DirectoryEntryModel).where(
                DirectoryEntryModel.tenant_id == tenant_id,
                DirectoryEntryModel.parent_path == parent_path,
                DirectoryEntryModel.entry_name == entry_name,
                DirectoryEntryModel.entry_type == "file",
            )
        )
        session.query(DirectoryEntryModel).filter(
            DirectoryEntryModel.tenant_id == tenant_id,
            DirectoryEntryModel.parent_path == parent_path,
            DirectoryEntryModel.entry_name == entry_name,
            DirectoryEntryModel.entry_type == "file",
        ).delete(synchronize_session=False)

    def _remove_from_directory_index_batch(
        self,
        session: Any,
        paths: Sequence[str],
        tenant_id: str | None,
    ) -> None:
        """Bulk remove file entries from directory index.

        Called by delete_batch().

        Args:
            session: SQLAlchemy session
            paths: List of virtual paths being deleted
            tenant_id: Tenant ID for multi-tenant isolation
        """
        if not paths:
            return

        # Collect all (parent_path, entry_name) pairs to delete
        entries_to_delete: list[tuple[str, str]] = []

        for path in paths:
            path = path.rstrip("/")
            parts = path.strip("/").split("/")

            if not parts or parts == [""]:
                continue

            parent_path = "/" if len(parts) == 1 else "/" + "/".join(parts[:-1]) + "/"
            entry_name = parts[-1]
            entries_to_delete.append((parent_path, entry_name))

        if not entries_to_delete:
            return

        # Delete all file entries in a single query using OR conditions
        from sqlalchemy import and_, or_

        conditions = [
            and_(
                DirectoryEntryModel.parent_path == parent_path,
                DirectoryEntryModel.entry_name == entry_name,
            )
            for parent_path, entry_name in entries_to_delete
        ]

        session.query(DirectoryEntryModel).filter(
            DirectoryEntryModel.tenant_id == tenant_id,
            DirectoryEntryModel.entry_type == "file",
            or_(*conditions),
        ).delete(synchronize_session=False)

    def delete_directory_entries_recursive(
        self,
        path: str,
        _tenant_id: str | None = None,
    ) -> int:
        """Delete all directory entries under a path (recursive).

        Removes both file and directory entries under the given path,
        plus the entry for the path itself. Used for mount point cleanup.

        Args:
            path: Virtual path to clean up (includes tenant prefix)
            _tenant_id: Reserved for API compatibility (path contains tenant info)

        Returns:
            Number of entries deleted
        """
        path = path.rstrip("/")
        prefix = path + "/"

        with self.SessionLocal() as session:
            # Note: Path already contains tenant info (e.g., /tenant:multifi.ai/...)
            # so we can match by path pattern. Also match tenant_id if provided,
            # but fall back to path-only matching for entries with mismatched tenant_id.

            # Delete all entries with parent_path starting with prefix
            # (all descendants)
            deleted = (
                session.query(DirectoryEntryModel)
                .filter(
                    DirectoryEntryModel.parent_path.like(prefix + "%"),
                )
                .delete(synchronize_session=False)
            )

            # Delete all entries where parent_path equals the path
            # (direct children)
            deleted += (
                session.query(DirectoryEntryModel)
                .filter(
                    DirectoryEntryModel.parent_path == prefix,
                )
                .delete(synchronize_session=False)
            )

            # Delete the entry for the path itself
            parts = path.strip("/").split("/")
            if parts and parts != [""]:
                parent_path = "/" if len(parts) == 1 else "/" + "/".join(parts[:-1]) + "/"
                entry_name = parts[-1]
                deleted += (
                    session.query(DirectoryEntryModel)
                    .filter(
                        DirectoryEntryModel.parent_path == parent_path,
                        DirectoryEntryModel.entry_name == entry_name,
                    )
                    .delete(synchronize_session=False)
                )

            session.commit()
            return deleted

    def _rename_in_directory_index(
        self,
        session: Any,
        old_path: str,
        new_path: str,
        tenant_id: str | None,
        is_directory: bool = False,
    ) -> None:
        """Update directory index when a file or directory is renamed.

        Called by rename_path(). Removes old entries and adds new entries.

        Args:
            session: SQLAlchemy session
            old_path: Original virtual path
            new_path: New virtual path
            tenant_id: Tenant ID for multi-tenant isolation
            is_directory: True if renaming a directory
        """
        if is_directory:
            # For directories, we need to update all entries under the old path
            old_prefix = old_path.rstrip("/") + "/"
            new_prefix = new_path.rstrip("/") + "/"

            # Find all entries with parent_path starting with old_prefix
            entries = (
                session.query(DirectoryEntryModel)
                .filter(
                    DirectoryEntryModel.tenant_id == tenant_id,
                    DirectoryEntryModel.parent_path.like(f"{old_prefix}%"),
                )
                .all()
            )

            # Update parent_path for each entry
            for entry in entries:
                new_parent = new_prefix + entry.parent_path[len(old_prefix) :]
                entry.parent_path = new_parent

            # Also update the directory entry itself in its parent
            self._remove_from_directory_index(session, old_path, tenant_id)
            self._update_directory_index(session, new_path, tenant_id, is_directory=True)
        else:
            # For files, just remove old and add new
            self._remove_from_directory_index(session, old_path, tenant_id)
            self._update_directory_index(session, new_path, tenant_id, is_directory=False)

    def list_directory_entries(
        self,
        parent_path: str,
        tenant_id: str | None = None,
    ) -> builtins.list[dict[str, Any]] | None:
        """List directory entries using the sparse index (O(1) lookup).

        Returns None if no index entries exist for this path (triggers fallback).

        Args:
            parent_path: Directory path to list (must end with "/")
            tenant_id: Tenant ID for multi-tenant isolation

        Returns:
            List of entry dicts with name, type, or None if no index data
        """
        # Normalize parent_path
        if not parent_path.endswith("/"):
            parent_path = parent_path + "/"

        try:
            with self.SessionLocal() as session:
                # Check if we have any entries for this path
                # Include entries with matching tenant_id OR NULL/default tenant_id (shared/system paths)
                from sqlalchemy import or_

                tenant_condition = or_(
                    DirectoryEntryModel.tenant_id == tenant_id,
                    DirectoryEntryModel.tenant_id.is_(None),
                    DirectoryEntryModel.tenant_id == "default",  # Legacy backfill entries
                )
                stmt = (
                    select(DirectoryEntryModel)
                    .where(
                        tenant_condition,
                        DirectoryEntryModel.parent_path == parent_path,
                    )
                    .order_by(DirectoryEntryModel.entry_name)
                )

                entries = session.scalars(stmt).all()

                # Debug: log query details when MISS
                if not entries:
                    # Check what entries exist for this parent_path (any tenant)
                    debug_stmt = (
                        select(DirectoryEntryModel.tenant_id, DirectoryEntryModel.entry_name)
                        .where(DirectoryEntryModel.parent_path == parent_path)
                        .limit(5)
                    )
                    debug_entries = session.execute(debug_stmt).all()
                    if debug_entries:
                        sample = [(str(t), n) for t, n in debug_entries]
                        logger.info(
                            f"[SPARSE-DEBUG] MISS for parent={parent_path}, tenant={tenant_id}, but found entries with tenants: {sample}"
                        )
                    else:
                        logger.info(
                            f"[SPARSE-DEBUG] MISS for parent={parent_path}, tenant={tenant_id}, NO entries exist for this parent_path"
                        )
                    return None

                return [
                    {
                        "name": entry.entry_name,
                        "type": entry.entry_type,
                        "created_at": entry.created_at,
                        "updated_at": entry.updated_at,
                    }
                    for entry in entries
                ]
        except Exception as e:
            logger.warning(f"Failed to query directory index: {e}")
            return None  # Fallback to LIKE query

    def backfill_directory_index(self, prefix: str = "/", tenant_id: str | None = None) -> int:
        """Backfill sparse directory index from existing file_paths.

        Use this to populate the index for directories that existed before
        the sparse index feature was added.

        Args:
            prefix: Path prefix to backfill (default: "/" for all)
            tenant_id: Tenant ID to backfill (None for all tenants)

        Returns:
            Number of directory entries created
        """
        import logging

        logger = logging.getLogger(__name__)

        try:
            with self.SessionLocal() as session:
                # Get all file paths matching the prefix
                conditions: list[Any] = [FilePathModel.deleted_at.is_(None)]
                if prefix and prefix != "/":
                    conditions.append(FilePathModel.virtual_path.like(f"{prefix}%"))
                if tenant_id is not None:
                    conditions.append(FilePathModel.tenant_id == tenant_id)

                stmt = select(FilePathModel.virtual_path, FilePathModel.tenant_id).where(
                    *conditions
                )
                paths = session.execute(stmt).all()

                logger.info(f"[BACKFILL] Found {len(paths)} paths to process for prefix={prefix}")

                # Track unique directory entries to create
                entries_to_create: dict[
                    tuple[str | None, str, str], str
                ] = {}  # (tenant, parent, name) -> type

                for vpath, file_tenant_id in paths:
                    # Build directory entries for each path component
                    parts = vpath.strip("/").split("/")
                    current_path = "/"

                    for i, part in enumerate(parts):
                        parent_path = (
                            current_path if current_path.endswith("/") else current_path + "/"
                        )
                        is_last = i == len(parts) - 1
                        entry_type = "file" if is_last else "directory"

                        key = (file_tenant_id, parent_path, part)
                        # Directories take precedence over files with same name
                        if key not in entries_to_create or entry_type == "directory":
                            entries_to_create[key] = entry_type

                        current_path = parent_path + part

                # Log sample of entries being created
                sample_entries = list(entries_to_create.items())[:5]
                sample_str = [
                    (f"tenant={t}", f"parent={p}", f"name={n}", f"type={ty}")
                    for (t, p, n), ty in sample_entries
                ]
                logger.info(
                    f"[BACKFILL] Creating {len(entries_to_create)} directory entries, sample: {sample_str}"
                )

                # Batch insert using upsert
                from sqlalchemy.dialects.postgresql import insert as pg_insert

                created = 0
                batch = []
                for (t_id, parent, name), entry_type in entries_to_create.items():
                    batch.append(
                        {
                            "tenant_id": t_id,
                            "parent_path": parent,
                            "entry_name": name,
                            "entry_type": entry_type,
                        }
                    )

                    if len(batch) >= 1000:
                        insert_stmt = pg_insert(DirectoryEntryModel).values(batch)
                        insert_stmt = insert_stmt.on_conflict_do_nothing()
                        result = session.execute(insert_stmt)
                        created += getattr(result, "rowcount", None) or len(batch)
                        batch = []

                if batch:
                    insert_stmt = pg_insert(DirectoryEntryModel).values(batch)
                    insert_stmt = insert_stmt.on_conflict_do_nothing()
                    result = session.execute(insert_stmt)
                    created += getattr(result, "rowcount", None) or len(batch)

                session.commit()
                logger.info(f"[BACKFILL] Created {created} directory entries")
                return created

        except Exception as e:
            logger.error(f"[BACKFILL] Failed: {e}")
            raise

    def __enter__(self) -> SQLAlchemyMetadataStore:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()

    def __del__(self) -> None:
        """Destructor to ensure database is closed."""
        with suppress(Exception):
            self.close()
