"""Version history garbage collection (Issue #974).

Automatic cleanup of old version_history entries to prevent unbounded table growth.
Inspired by TiDB's MVCC garbage collection patterns.

Environment variables:
    NEXUS_VERSION_GC_ENABLED: Enable automatic GC (default: true)
    NEXUS_VERSION_GC_RETENTION_DAYS: Keep versions for N days (default: 30)
    NEXUS_VERSION_GC_MAX_VERSIONS: Max versions per resource (default: 100)
    NEXUS_VERSION_GC_INTERVAL_HOURS: Run GC every N hours (default: 24)
    NEXUS_VERSION_GC_BATCH_SIZE: Delete batch size (default: 1000)
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import bindparam, text

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class GCStats:
    """Statistics from a garbage collection run."""

    deleted_by_age: int = 0  # Versions deleted due to age
    deleted_by_count: int = 0  # Versions deleted due to exceeding max per resource
    bytes_reclaimed: int = 0  # Approximate bytes freed
    resources_processed: int = 0  # Number of unique resources checked
    duration_seconds: float = 0.0  # Time taken for GC run
    dry_run: bool = False  # Whether this was a dry run

    @property
    def total_deleted(self) -> int:
        """Total versions deleted."""
        return self.deleted_by_age + self.deleted_by_count

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            "deleted_by_age": self.deleted_by_age,
            "deleted_by_count": self.deleted_by_count,
            "total_deleted": self.total_deleted,
            "bytes_reclaimed": self.bytes_reclaimed,
            "resources_processed": self.resources_processed,
            "duration_seconds": round(self.duration_seconds, 2),
            "dry_run": self.dry_run,
        }


@dataclass
class VersionGCSettings:
    """Configuration for version history garbage collection."""

    # Enable/disable GC
    enabled: bool = field(
        default_factory=lambda: os.environ.get("NEXUS_VERSION_GC_ENABLED", "true").lower()
        == "true"
    )

    # Keep versions for N days (default: 30 days)
    retention_days: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_VERSION_GC_RETENTION_DAYS", "30"))
    )

    # Maximum versions to keep per resource (default: 100)
    max_versions_per_resource: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_VERSION_GC_MAX_VERSIONS", "100"))
    )

    # Run GC every N hours (default: 24 hours)
    run_interval_hours: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_VERSION_GC_INTERVAL_HOURS", "24"))
    )

    # Batch size for deletions (default: 1000)
    batch_size: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_VERSION_GC_BATCH_SIZE", "1000"))
    )

    def validate(self) -> None:
        """Validate configuration values."""
        if self.retention_days < 1:
            raise ValueError("NEXUS_VERSION_GC_RETENTION_DAYS must be >= 1")
        if self.max_versions_per_resource < 1:
            raise ValueError("NEXUS_VERSION_GC_MAX_VERSIONS must be >= 1")
        if self.run_interval_hours < 1:
            raise ValueError("NEXUS_VERSION_GC_INTERVAL_HOURS must be >= 1")
        if self.batch_size < 1:
            raise ValueError("NEXUS_VERSION_GC_BATCH_SIZE must be >= 1")

    @classmethod
    def from_env(cls) -> VersionGCSettings:
        """Create settings from environment variables."""
        settings = cls()
        settings.validate()
        return settings

    def __repr__(self) -> str:
        return (
            f"VersionGCSettings("
            f"enabled={self.enabled}, "
            f"retention_days={self.retention_days}, "
            f"max_versions={self.max_versions_per_resource}, "
            f"interval_hours={self.run_interval_hours}, "
            f"batch_size={self.batch_size})"
        )


class VersionHistoryGC:
    """Garbage collector for version_history table.

    Implements two cleanup strategies:
    1. Age-based: Delete versions older than retention_days
    2. Count-based: Keep only max_versions_per_resource versions per resource

    Always preserves the latest version for each resource.

    Example:
        >>> gc = VersionHistoryGC(session_factory)
        >>> stats = gc.run_gc(VersionGCSettings())
        >>> print(f"Deleted {stats.total_deleted} versions")
    """

    def __init__(self, session_factory: Any) -> None:
        """Initialize garbage collector.

        Args:
            session_factory: SQLAlchemy session factory
        """
        self._session_factory = session_factory

    def run_gc(
        self,
        config: VersionGCSettings | None = None,
        dry_run: bool = False,
        retention_days: int | None = None,
        max_versions: int | None = None,
    ) -> GCStats:
        """Run garbage collection synchronously.

        Args:
            config: GC configuration (uses defaults if None)
            dry_run: If True, only count deletions without executing
            retention_days: Override config retention_days
            max_versions: Override config max_versions_per_resource

        Returns:
            GCStats with deletion statistics
        """
        start_time = datetime.now(UTC)
        config = config or VersionGCSettings.from_env()

        # Allow parameter overrides
        effective_retention = retention_days if retention_days is not None else config.retention_days
        effective_max_versions = (
            max_versions if max_versions is not None else config.max_versions_per_resource
        )

        stats = GCStats(dry_run=dry_run)

        with self._session_factory() as session:
            # Phase 1: Delete old versions (by age)
            stats.deleted_by_age, bytes_age = self._delete_old_versions(
                session,
                effective_retention,
                config.batch_size,
                dry_run,
            )

            # Phase 2: Trim excess versions (by count)
            stats.deleted_by_count, bytes_count = self._trim_excess_versions(
                session,
                effective_max_versions,
                config.batch_size,
                dry_run,
            )

            # Get resource count
            stats.resources_processed = self._count_resources(session)
            stats.bytes_reclaimed = bytes_age + bytes_count

            if not dry_run:
                session.commit()

        stats.duration_seconds = (datetime.now(UTC) - start_time).total_seconds()
        return stats

    async def run_gc_async(
        self,
        config: VersionGCSettings | None = None,
        dry_run: bool = False,
    ) -> GCStats:
        """Run garbage collection asynchronously.

        Yields to event loop between batches to avoid blocking.
        """
        start_time = datetime.now(UTC)
        config = config or VersionGCSettings.from_env()
        stats = GCStats(dry_run=dry_run)

        with self._session_factory() as session:
            # Phase 1: Delete old versions (by age) with async yields
            stats.deleted_by_age, bytes_age = await self._delete_old_versions_async(
                session,
                config.retention_days,
                config.batch_size,
                dry_run,
            )

            # Phase 2: Trim excess versions (by count) with async yields
            stats.deleted_by_count, bytes_count = await self._trim_excess_versions_async(
                session,
                config.max_versions_per_resource,
                config.batch_size,
                dry_run,
            )

            stats.resources_processed = self._count_resources(session)
            stats.bytes_reclaimed = bytes_age + bytes_count

            if not dry_run:
                session.commit()

        stats.duration_seconds = (datetime.now(UTC) - start_time).total_seconds()
        return stats

    def _is_sqlite(self, session: Session) -> bool:
        """Check if the database is SQLite."""
        return "sqlite" in str(session.bind.url) if session.bind else False

    def _delete_old_versions(
        self,
        session: Session,
        retention_days: int,
        batch_size: int,
        dry_run: bool,
    ) -> tuple[int, int]:
        """Delete versions older than retention period.

        Always preserves the latest version for each resource.

        Returns:
            Tuple of (deleted_count, bytes_reclaimed)
        """
        cutoff_date = datetime.now(UTC) - timedelta(days=retention_days)
        is_sqlite = self._is_sqlite(session)

        # SQLite-compatible query to find latest version per resource
        # Uses a subquery with GROUP BY instead of DISTINCT ON
        if is_sqlite:
            latest_versions_sql = """
                SELECT vh.version_id
                FROM version_history vh
                INNER JOIN (
                    SELECT resource_type, resource_id, MAX(version_number) as max_version
                    FROM version_history
                    GROUP BY resource_type, resource_id
                ) latest ON vh.resource_type = latest.resource_type
                        AND vh.resource_id = latest.resource_id
                        AND vh.version_number = latest.max_version
            """
        else:
            # PostgreSQL - use DISTINCT ON for efficiency
            latest_versions_sql = """
                SELECT version_id FROM (
                    SELECT DISTINCT ON (resource_type, resource_id) version_id
                    FROM version_history
                    ORDER BY resource_type, resource_id, version_number DESC
                ) AS latest
            """

        if dry_run:
            # Count what would be deleted
            count_query = text(f"""
                SELECT COUNT(*), COALESCE(SUM(size_bytes), 0)
                FROM version_history vh
                WHERE vh.created_at < :cutoff
                  AND vh.version_id NOT IN ({latest_versions_sql})
            """)
            result = session.execute(count_query, {"cutoff": cutoff_date}).fetchone()
            return (result[0] or 0, result[1] or 0) if result else (0, 0)

        # Batch delete - use database-appropriate query
        if is_sqlite:
            # SQLite: Simple batch delete without FOR UPDATE
            select_query = text(f"""
                SELECT vh.version_id, vh.size_bytes
                FROM version_history vh
                WHERE vh.created_at < :cutoff
                  AND vh.version_id NOT IN ({latest_versions_sql})
                LIMIT :batch_size
            """)
        else:
            # PostgreSQL: Use FOR UPDATE SKIP LOCKED for concurrency
            select_query = text(f"""
                SELECT vh.version_id, vh.size_bytes
                FROM version_history vh
                WHERE vh.created_at < :cutoff
                  AND vh.version_id NOT IN ({latest_versions_sql})
                LIMIT :batch_size
                FOR UPDATE SKIP LOCKED
            """)

        total_deleted = 0
        total_bytes = 0

        while True:
            # Get batch of IDs to delete
            rows = session.execute(
                select_query, {"cutoff": cutoff_date, "batch_size": batch_size}
            ).fetchall()

            if not rows:
                break

            ids_to_delete = [row[0] for row in rows]
            batch_bytes = sum(row[1] or 0 for row in rows)

            # Delete the batch using expanding bindparam for SQLite compatibility
            delete_query = text(
                "DELETE FROM version_history WHERE version_id IN :ids"
            ).bindparams(bindparam("ids", expanding=True))
            session.execute(delete_query, {"ids": ids_to_delete})

            total_deleted += len(ids_to_delete)
            total_bytes += batch_bytes

            logger.debug(f"Version GC: deleted batch of {len(ids_to_delete)} old versions")

        return total_deleted, total_bytes

    async def _delete_old_versions_async(
        self,
        session: Session,
        retention_days: int,
        batch_size: int,
        dry_run: bool,
    ) -> tuple[int, int]:
        """Async version that yields between batches."""
        cutoff_date = datetime.now(UTC) - timedelta(days=retention_days)
        is_sqlite = self._is_sqlite(session)

        # SQLite-compatible query to find latest version per resource
        if is_sqlite:
            latest_versions_sql = """
                SELECT vh.version_id
                FROM version_history vh
                INNER JOIN (
                    SELECT resource_type, resource_id, MAX(version_number) as max_version
                    FROM version_history
                    GROUP BY resource_type, resource_id
                ) latest ON vh.resource_type = latest.resource_type
                        AND vh.resource_id = latest.resource_id
                        AND vh.version_number = latest.max_version
            """
        else:
            latest_versions_sql = """
                SELECT version_id FROM (
                    SELECT DISTINCT ON (resource_type, resource_id) version_id
                    FROM version_history
                    ORDER BY resource_type, resource_id, version_number DESC
                ) AS latest
            """

        if dry_run:
            count_query = text(f"""
                SELECT COUNT(*), COALESCE(SUM(size_bytes), 0)
                FROM version_history vh
                WHERE vh.created_at < :cutoff
                  AND vh.version_id NOT IN ({latest_versions_sql})
            """)
            result = session.execute(count_query, {"cutoff": cutoff_date}).fetchone()
            return (result[0] or 0, result[1] or 0) if result else (0, 0)

        # Batch delete - use database-appropriate query
        if is_sqlite:
            select_query = text(f"""
                SELECT vh.version_id, vh.size_bytes
                FROM version_history vh
                WHERE vh.created_at < :cutoff
                  AND vh.version_id NOT IN ({latest_versions_sql})
                LIMIT :batch_size
            """)
        else:
            select_query = text(f"""
                SELECT vh.version_id, vh.size_bytes
                FROM version_history vh
                WHERE vh.created_at < :cutoff
                  AND vh.version_id NOT IN ({latest_versions_sql})
                LIMIT :batch_size
                FOR UPDATE SKIP LOCKED
            """)

        total_deleted = 0
        total_bytes = 0

        while True:
            rows = session.execute(
                select_query, {"cutoff": cutoff_date, "batch_size": batch_size}
            ).fetchall()

            if not rows:
                break

            ids_to_delete = [row[0] for row in rows]
            batch_bytes = sum(row[1] or 0 for row in rows)

            delete_query = text(
                "DELETE FROM version_history WHERE version_id IN :ids"
            ).bindparams(bindparam("ids", expanding=True))
            session.execute(delete_query, {"ids": ids_to_delete})

            total_deleted += len(ids_to_delete)
            total_bytes += batch_bytes

            # Yield to event loop between batches
            await asyncio.sleep(0.1)

        return total_deleted, total_bytes

    def _trim_excess_versions(
        self,
        session: Session,
        max_versions: int,
        batch_size: int,
        dry_run: bool,
    ) -> tuple[int, int]:
        """Delete versions exceeding max_versions per resource.

        Keeps the most recent versions by version_number.

        Returns:
            Tuple of (deleted_count, bytes_reclaimed)
        """
        _is_sqlite = self._is_sqlite(session)  # noqa: F841

        # SQLite doesn't support window functions in all contexts the same way,
        # but ROW_NUMBER() is supported in modern SQLite (3.25+)
        if dry_run:
            # Count what would be deleted - works on both SQLite and PostgreSQL
            count_query = text("""
                WITH ranked AS (
                    SELECT version_id, size_bytes,
                           ROW_NUMBER() OVER (
                               PARTITION BY resource_type, resource_id
                               ORDER BY version_number DESC
                           ) AS rn
                    FROM version_history
                )
                SELECT COUNT(*), COALESCE(SUM(size_bytes), 0)
                FROM ranked WHERE rn > :max_versions
            """)
            result = session.execute(count_query, {"max_versions": max_versions}).fetchone()
            return (result[0] or 0, result[1] or 0) if result else (0, 0)

        # Select excess versions to delete
        select_query = text("""
            WITH ranked AS (
                SELECT version_id, size_bytes,
                       ROW_NUMBER() OVER (
                           PARTITION BY resource_type, resource_id
                           ORDER BY version_number DESC
                       ) AS rn
                FROM version_history
            )
            SELECT version_id, size_bytes
            FROM ranked
            WHERE rn > :max_versions
            LIMIT :batch_size
        """)

        total_deleted = 0
        total_bytes = 0

        while True:
            rows = session.execute(
                select_query, {"max_versions": max_versions, "batch_size": batch_size}
            ).fetchall()

            if not rows:
                break

            ids_to_delete = [row[0] for row in rows]
            batch_bytes = sum(row[1] or 0 for row in rows)

            delete_query = text(
                "DELETE FROM version_history WHERE version_id IN :ids"
            ).bindparams(bindparam("ids", expanding=True))
            session.execute(delete_query, {"ids": ids_to_delete})

            total_deleted += len(ids_to_delete)
            total_bytes += batch_bytes

            logger.debug(f"Version GC: trimmed batch of {len(ids_to_delete)} excess versions")

        return total_deleted, total_bytes

    async def _trim_excess_versions_async(
        self,
        session: Session,
        max_versions: int,
        batch_size: int,
        dry_run: bool,
    ) -> tuple[int, int]:
        """Async version that yields between batches."""
        if dry_run:
            count_query = text("""
                WITH ranked AS (
                    SELECT version_id, size_bytes,
                           ROW_NUMBER() OVER (
                               PARTITION BY resource_type, resource_id
                               ORDER BY version_number DESC
                           ) AS rn
                    FROM version_history
                )
                SELECT COUNT(*), COALESCE(SUM(size_bytes), 0)
                FROM ranked WHERE rn > :max_versions
            """)
            result = session.execute(count_query, {"max_versions": max_versions}).fetchone()
            return (result[0] or 0, result[1] or 0) if result else (0, 0)

        select_query = text("""
            WITH ranked AS (
                SELECT version_id, size_bytes,
                       ROW_NUMBER() OVER (
                           PARTITION BY resource_type, resource_id
                           ORDER BY version_number DESC
                       ) AS rn
                FROM version_history
            )
            SELECT version_id, size_bytes
            FROM ranked
            WHERE rn > :max_versions
            LIMIT :batch_size
        """)

        total_deleted = 0
        total_bytes = 0

        while True:
            rows = session.execute(
                select_query, {"max_versions": max_versions, "batch_size": batch_size}
            ).fetchall()

            if not rows:
                break

            ids_to_delete = [row[0] for row in rows]
            batch_bytes = sum(row[1] or 0 for row in rows)

            delete_query = text(
                "DELETE FROM version_history WHERE version_id IN :ids"
            ).bindparams(bindparam("ids", expanding=True))
            session.execute(delete_query, {"ids": ids_to_delete})

            total_deleted += len(ids_to_delete)
            total_bytes += batch_bytes

            # Yield to event loop between batches
            await asyncio.sleep(0.1)

        return total_deleted, total_bytes

    def _count_resources(self, session: Session) -> int:
        """Count unique resources in version_history."""
        # Use subquery for SQLite compatibility (doesn't support COUNT(DISTINCT tuple))
        query = text("""
            SELECT COUNT(*) FROM (
                SELECT DISTINCT resource_type, resource_id
                FROM version_history
            ) AS unique_resources
        """)
        result = session.execute(query).fetchone()
        return result[0] if result else 0

    def get_stats(self) -> dict[str, Any]:
        """Get current version_history table statistics."""
        with self._session_factory() as session:
            # Get basic stats
            basic_query = text("""
                SELECT
                    COUNT(*) as total_versions,
                    COALESCE(SUM(size_bytes), 0) as total_bytes,
                    MIN(created_at) as oldest_version,
                    MAX(created_at) as newest_version
                FROM version_history
            """)
            basic_result = session.execute(basic_query).fetchone()

            # Get unique resource count separately for SQLite compatibility
            unique_query = text("""
                SELECT COUNT(*) FROM (
                    SELECT DISTINCT resource_type, resource_id
                    FROM version_history
                ) AS unique_resources
            """)
            unique_result = session.execute(unique_query).fetchone()

            if not basic_result:
                return {
                    "total_versions": 0,
                    "unique_resources": 0,
                    "total_bytes": 0,
                    "oldest_version": None,
                    "newest_version": None,
                }

            # Handle datetime - SQLite returns string, PostgreSQL returns datetime
            oldest = basic_result[2]
            newest = basic_result[3]

            if oldest and hasattr(oldest, "isoformat"):
                oldest = oldest.isoformat()
            elif oldest:
                oldest = str(oldest)

            if newest and hasattr(newest, "isoformat"):
                newest = newest.isoformat()
            elif newest:
                newest = str(newest)

            return {
                "total_versions": basic_result[0] or 0,
                "unique_resources": unique_result[0] if unique_result else 0,
                "total_bytes": basic_result[1] or 0,
                "oldest_version": oldest,
                "newest_version": newest,
            }
