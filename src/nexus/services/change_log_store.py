"""Change Log Store for Delta Sync (Issue #1127).

Extracted from sync_service.py during Phase 0 refactoring for
Issue #1129 (Bidirectional Sync).

Provides CRUD operations for BackendChangeLogModel to support delta sync.
Uses SyncStoreBase for shared session management and dialect detection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from nexus.services.sync_store_base import SyncStoreBase

if TYPE_CHECKING:
    from nexus.services.gateway import NexusFSGateway

logger = logging.getLogger(__name__)


@dataclass
class ChangeLogEntry:
    """Cached change log entry for delta sync comparison."""

    path: str
    backend_name: str
    size_bytes: int | None = None
    mtime: datetime | None = None
    backend_version: str | None = None
    content_hash: str | None = None
    synced_at: datetime | None = None


class ChangeLogStore(SyncStoreBase):
    """Lightweight store for change log operations (Issue #1127).

    Provides CRUD operations for BackendChangeLogModel to support delta sync.
    Uses the gateway's session factory for database access.

    Inherits from SyncStoreBase for session management and dialect detection.
    """

    def __init__(self, gateway: NexusFSGateway) -> None:
        """Initialize change log store.

        Args:
            gateway: NexusFSGateway for database session access
        """
        super().__init__(gateway)

    def get_change_log(
        self, path: str, backend_name: str, zone_id: str = "default"
    ) -> ChangeLogEntry | None:
        """Get change log entry for a path.

        Args:
            path: Virtual file path
            backend_name: Backend identifier
            zone_id: Zone ID

        Returns:
            ChangeLogEntry if found, None otherwise
        """
        from nexus.storage.models import BackendChangeLogModel

        session = self._get_session()
        if session is None:
            return None

        try:
            entry = (
                session.query(BackendChangeLogModel)
                .filter(
                    BackendChangeLogModel.path == path,
                    BackendChangeLogModel.backend_name == backend_name,
                    BackendChangeLogModel.zone_id == zone_id,
                )
                .first()
            )

            if entry:
                return ChangeLogEntry(
                    path=entry.path,
                    backend_name=entry.backend_name,
                    size_bytes=entry.size_bytes,
                    mtime=entry.mtime,
                    backend_version=entry.backend_version,
                    content_hash=entry.content_hash,
                    synced_at=entry.synced_at,
                )
            return None
        except Exception as e:
            logger.warning(f"Failed to get change log for {path}: {e}")
            return None
        finally:
            session.close()

    def upsert_change_log(
        self,
        path: str,
        backend_name: str,
        zone_id: str = "default",
        size_bytes: int | None = None,
        mtime: datetime | None = None,
        backend_version: str | None = None,
        content_hash: str | None = None,
    ) -> bool:
        """Insert or update change log entry.

        Args:
            path: Virtual file path
            backend_name: Backend identifier
            zone_id: Zone ID
            size_bytes: File size in bytes
            mtime: Last modification time
            backend_version: Backend-specific version (GCS generation, S3 version ID)
            content_hash: Content hash if computed

        Returns:
            True if successful, False otherwise
        """
        from nexus.storage.models import BackendChangeLogModel

        session = self._get_session()
        if session is None:
            return False

        try:
            now = datetime.now(UTC)
            values = {
                "path": path,
                "backend_name": backend_name,
                "zone_id": zone_id,
                "size_bytes": size_bytes,
                "mtime": mtime,
                "backend_version": backend_version,
                "content_hash": content_hash,
                "synced_at": now,
            }
            update_set = {
                "size_bytes": size_bytes,
                "mtime": mtime,
                "backend_version": backend_version,
                "content_hash": content_hash,
                "synced_at": now,
            }

            self._dialect_upsert(
                session,
                BackendChangeLogModel,
                values,
                pg_constraint="uq_backend_change_log",
                sqlite_index_elements=["path", "backend_name", "zone_id"],
                update_set=update_set,
            )
            session.commit()
            return True
        except Exception as e:
            logger.warning(f"Failed to upsert change log for {path}: {e}")
            session.rollback()
            return False
        finally:
            session.close()

    def get_last_sync_time(self, backend_name: str, zone_id: str = "default") -> datetime | None:
        """Get the most recent sync time for a backend.

        Args:
            backend_name: Backend identifier
            zone_id: Zone ID

        Returns:
            Most recent synced_at timestamp, or None if no entries
        """
        from sqlalchemy import func

        from nexus.storage.models import BackendChangeLogModel

        session = self._get_session()
        if session is None:
            return None

        try:
            result = (
                session.query(func.max(BackendChangeLogModel.synced_at))
                .filter(
                    BackendChangeLogModel.backend_name == backend_name,
                    BackendChangeLogModel.zone_id == zone_id,
                )
                .scalar()
            )
            return result  # type: ignore[no-any-return]
        except Exception as e:
            logger.warning(f"Failed to get last sync time for {backend_name}: {e}")
            return None
        finally:
            session.close()

    def get_change_logs_batch(
        self, backend_name: str, zone_id: str, path_prefix: str
    ) -> dict[str, ChangeLogEntry]:
        """Fetch all change logs for a mount prefix in one query.

        Used before BFS traversal to pre-load all cached entries, eliminating
        per-file database round-trips (~100x speedup for large mounts).

        Args:
            backend_name: Backend identifier
            zone_id: Zone ID
            path_prefix: Mount point prefix (e.g. "/mnt/gcs")

        Returns:
            Dict mapping path to ChangeLogEntry
        """
        from nexus.storage.models import BackendChangeLogModel

        session = self._get_session()
        if session is None:
            return {}

        try:
            # Escape SQL LIKE wildcards in prefix to prevent unintended matching
            escaped = path_prefix.replace("%", r"\%").replace("_", r"\_")
            entries = (
                session.query(BackendChangeLogModel)
                .filter(
                    BackendChangeLogModel.backend_name == backend_name,
                    BackendChangeLogModel.zone_id == zone_id,
                    BackendChangeLogModel.path.like(f"{escaped}%", escape="\\"),
                )
                .all()
            )

            return {
                entry.path: ChangeLogEntry(
                    path=entry.path,
                    backend_name=entry.backend_name,
                    size_bytes=entry.size_bytes,
                    mtime=entry.mtime,
                    backend_version=entry.backend_version,
                    content_hash=entry.content_hash,
                    synced_at=entry.synced_at,
                )
                for entry in entries
            }
        except Exception as e:
            logger.warning(f"Failed to batch-fetch change logs for {path_prefix}: {e}")
            return {}
        finally:
            session.close()

    def upsert_change_logs_batch(self, entries: list[ChangeLogEntry]) -> bool:
        """Bulk upsert change log entries in a single transaction.

        Used after BFS traversal to flush all change log updates at once,
        eliminating per-file commit overhead.

        Args:
            entries: List of ChangeLogEntry objects

        Returns:
            True if successful, False otherwise
        """
        if not entries:
            return True

        from nexus.storage.models import BackendChangeLogModel

        session = self._get_session()
        if session is None:
            return False

        try:
            now = datetime.now(UTC)
            is_pg = self._detect_dialect()

            # Build all value dicts upfront
            all_values = [
                {
                    "path": entry.path,
                    "backend_name": entry.backend_name,
                    "zone_id": getattr(entry, "zone_id", "default") or "default",
                    "size_bytes": entry.size_bytes,
                    "mtime": entry.mtime,
                    "backend_version": entry.backend_version,
                    "content_hash": entry.content_hash,
                    "synced_at": now,
                }
                for entry in entries
            ]

            # Use multi-row insert for true batch performance
            # SQLite has a variable limit (~999), so chunk to be safe
            chunk_size = 500
            for i in range(0, len(all_values), chunk_size):
                chunk = all_values[i : i + chunk_size]

                if is_pg:
                    # PG supports multi-row insert with stmt.excluded references
                    stmt = self._dialect_insert(BackendChangeLogModel).values(chunk)
                    stmt = stmt.on_conflict_do_update(
                        constraint="uq_backend_change_log",
                        set_={
                            "size_bytes": stmt.excluded.size_bytes,
                            "mtime": stmt.excluded.mtime,
                            "backend_version": stmt.excluded.backend_version,
                            "content_hash": stmt.excluded.content_hash,
                            "synced_at": stmt.excluded.synced_at,
                        },
                    )
                    session.execute(stmt)
                else:
                    # SQLite doesn't support multi-row on_conflict_do_update
                    # with excluded references, so use per-row upserts
                    for row in chunk:
                        self._dialect_upsert(
                            session,
                            BackendChangeLogModel,
                            row,
                            pg_constraint="uq_backend_change_log",
                            sqlite_index_elements=["path", "backend_name", "zone_id"],
                            update_set={
                                "size_bytes": row["size_bytes"],
                                "mtime": row["mtime"],
                                "backend_version": row["backend_version"],
                                "content_hash": row["content_hash"],
                                "synced_at": now,
                            },
                        )

            session.commit()
            logger.debug(f"[DELTA_SYNC] Batch upserted {len(entries)} change log entries")
            return True
        except Exception as e:
            logger.warning(f"Failed to batch-upsert {len(entries)} change logs: {e}")
            session.rollback()
            return False
        finally:
            session.close()

    def delete_change_log(self, path: str, backend_name: str, zone_id: str = "default") -> bool:
        """Delete change log entry for a path.

        Used during file deletion to prevent stale entries that could
        cause false skips when files are re-created with the same path.

        Args:
            path: Virtual file path
            backend_name: Backend identifier
            zone_id: Zone ID

        Returns:
            True if successful, False otherwise
        """
        from nexus.storage.models import BackendChangeLogModel

        session = self._get_session()
        if session is None:
            return False

        try:
            session.query(BackendChangeLogModel).filter(
                BackendChangeLogModel.path == path,
                BackendChangeLogModel.backend_name == backend_name,
                BackendChangeLogModel.zone_id == zone_id,
            ).delete()
            session.commit()
            return True
        except Exception as e:
            logger.warning(f"Failed to delete change log for {path}: {e}")
            session.rollback()
            return False
        finally:
            session.close()
