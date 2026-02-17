"""TigerResourceMap - Bidirectional UUID-to-Integer Mapping for Roaring Bitmaps.

Maps resource UUIDs to int64 IDs for Roaring Bitmap compatibility in the
Tiger Cache system. Maintains both in-memory and database-backed mappings
with thread-safe access.

Resource paths are globally unique (zone-independent), so zone_id is
intentionally excluded from the mapping key.

Related: Issue #682, Issue #979
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import insert, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from nexus.storage.models.permissions import TigerResourceMapModel as TRM

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)


class TigerResourceMap:
    """Maps resource UUIDs to int64 IDs for Roaring Bitmap compatibility.

    Maintains a bidirectional mapping between string resource IDs and
    integer IDs suitable for Roaring Bitmaps.

    Note: zone_id is intentionally excluded from resource mapping.
    Resource paths are globally unique (e.g., /skills/system/docs is the same
    file regardless of who queries it). Zone isolation is enforced at the
    bitmap/permission level, not the resource ID mapping.

    See: Issue #979 - Cross-zone resource map optimization
    """

    def __init__(self, engine: Engine):
        self._engine = engine
        self._is_postgresql = "postgresql" in str(engine.url)

        # In-memory cache for frequently accessed mappings
        # Key is (type, id) - zone excluded for cross-zone compatibility
        self._uuid_to_int: dict[tuple[str, str], int] = {}  # (type, id) -> int
        self._int_to_uuid: dict[int, tuple[str, str]] = {}  # int -> (type, id)
        self._lock = threading.RLock()

    def get_or_create_int_id(
        self,
        resource_type: str,
        resource_id: str,
        conn: Connection | None = None,
    ) -> int:
        """Get or create an integer ID for a resource.

        Args:
            resource_type: Type of resource (e.g., "file")
            resource_id: String ID of resource (e.g., UUID or path)
            conn: Optional database connection

        Returns:
            Integer ID for use in bitmaps
        """
        # Key excludes zone - resource paths are globally unique
        key = (resource_type, resource_id)

        # Check memory cache first
        with self._lock:
            if key in self._uuid_to_int:
                return self._uuid_to_int[key]

        # Query/insert in database
        select_stmt = select(TRM.resource_int_id).where(
            TRM.resource_type == resource_type,
            TRM.resource_id == resource_id,
        )

        def do_get_or_create(connection: Connection) -> int:
            # Try to get existing (no zone filter)
            row = connection.execute(select_stmt).first()
            if row:
                return int(row.resource_int_id)

            # Insert new with conflict handling
            if self._is_postgresql:
                insert_stmt = (
                    pg_insert(TRM)
                    .values(
                        resource_type=resource_type,
                        resource_id=resource_id,
                        created_at=datetime.now(UTC),
                    )
                    .on_conflict_do_nothing(
                        index_elements=["resource_type", "resource_id"],
                    )
                    .returning(TRM.resource_int_id)
                )
                result = connection.execute(insert_stmt)
                # Commit so the data persists (Issue #934 fix)
                connection.commit()
                row = result.first()
                if row:
                    return int(row.resource_int_id)
                # Conflict occurred, fetch again
                row = connection.execute(select_stmt).first()
                return int(row.resource_int_id) if row else -1
            else:
                # SQLite - use INSERT OR IGNORE then SELECT
                connection.execute(
                    insert(TRM)
                    .prefix_with("OR IGNORE")
                    .values(
                        resource_type=resource_type,
                        resource_id=resource_id,
                        created_at=datetime.now(UTC),
                    )
                )
                # Commit so the SELECT can see the inserted row
                connection.commit()
                # Get the ID (either newly inserted or existing)
                row = connection.execute(select_stmt).first()
                return int(row.resource_int_id) if row else -1

        if conn:
            int_id = do_get_or_create(conn)
        else:
            with self._engine.connect() as new_conn:
                int_id = do_get_or_create(new_conn)

        # Cache the mapping
        with self._lock:
            self._uuid_to_int[key] = int_id
            self._int_to_uuid[int_id] = key

        return int_id

    def get_resource_id(
        self, int_id: int, conn: Connection | None = None
    ) -> tuple[str, str] | None:
        """Get resource info from integer ID.

        Args:
            int_id: Integer ID from bitmap
            conn: Optional database connection

        Returns:
            Tuple of (resource_type, resource_id) or None if not found
        """
        # Check memory cache first
        with self._lock:
            if int_id in self._int_to_uuid:
                return self._int_to_uuid[int_id]

        # Query database
        stmt = select(TRM.resource_type, TRM.resource_id).where(
            TRM.resource_int_id == int_id,
        )

        def execute(connection: Connection) -> tuple[str, str] | None:
            row = connection.execute(stmt).first()
            if row:
                return (row.resource_type, row.resource_id)
            return None

        if conn:
            info = execute(conn)
        else:
            with self._engine.connect() as new_conn:
                info = execute(new_conn)

        # Cache if found
        if info:
            with self._lock:
                self._int_to_uuid[int_id] = info
                self._uuid_to_int[info] = int_id

        return info

    def bulk_get_int_ids(
        self,
        resources: list[tuple[str, str]],  # List of (resource_type, resource_id)
        conn: Connection,
    ) -> dict[tuple[str, str], int | None]:
        """Bulk get integer IDs for multiple resources in a single query.

        Args:
            resources: List of (resource_type, resource_id) tuples
            conn: Database connection

        Returns:
            Dict mapping resource tuples to their int IDs (None if not found)
        """
        if not resources:
            return {}

        results: dict[tuple[str, str], int | None] = {}
        to_fetch: list[tuple[str, str]] = []

        # Check memory cache first
        with self._lock:
            for resource in resources:
                if resource in self._uuid_to_int:
                    results[resource] = self._uuid_to_int[resource]
                else:
                    to_fetch.append(resource)
                    results[resource] = None

        if not to_fetch:
            return results

        # Bulk fetch from database using OR conditions (dialect-agnostic)
        # Batch in groups to avoid too-large queries
        batch_size = 500
        for i in range(0, len(to_fetch), batch_size):
            batch = to_fetch[i : i + batch_size]
            conditions = [(TRM.resource_type == rt) & (TRM.resource_id == rid) for rt, rid in batch]
            stmt = select(TRM.resource_type, TRM.resource_id, TRM.resource_int_id).where(
                or_(*conditions)
            )
            result = conn.execute(stmt)

            # Process results and update cache
            with self._lock:
                for row in result:
                    key = (row.resource_type, row.resource_id)
                    int_id = int(row.resource_int_id)
                    results[key] = int_id
                    self._uuid_to_int[key] = int_id
                    self._int_to_uuid[int_id] = key

        return results

    def get_int_ids_batch(
        self,
        resources: list[tuple[str, str]],
        conn: Connection | None = None,
    ) -> dict[tuple[str, str], int]:
        """Get integer IDs for multiple resources in batch.

        Args:
            resources: List of (resource_type, resource_id) tuples
            conn: Optional database connection

        Returns:
            Dict mapping resource tuples to integer IDs
        """
        result: dict[tuple[str, str], int] = {}
        missing: list[tuple[str, str]] = []

        # Check memory cache first
        with self._lock:
            for key in resources:
                if key in self._uuid_to_int:
                    result[key] = self._uuid_to_int[key]
                else:
                    missing.append(key)

        if not missing:
            return result

        # Query database for missing using OR conditions (dialect-agnostic)
        batch_size = 500
        stmt_batches: list[list[tuple[str, str]]] = [
            missing[i : i + batch_size] for i in range(0, len(missing), batch_size)
        ]

        def execute(connection: Connection) -> None:
            for batch in stmt_batches:
                conditions = [
                    (TRM.resource_type == rt) & (TRM.resource_id == rid) for rt, rid in batch
                ]
                stmt = select(TRM.resource_type, TRM.resource_id, TRM.resource_int_id).where(
                    or_(*conditions)
                )
                db_result = connection.execute(stmt)
                for row in db_result:
                    key = (row.resource_type, row.resource_id)
                    result[key] = row.resource_int_id
                    with self._lock:
                        self._uuid_to_int[key] = row.resource_int_id
                        self._int_to_uuid[row.resource_int_id] = key

        if conn:
            execute(conn)
        else:
            with self._engine.connect() as new_conn:
                execute(new_conn)

        return result

    def clear_cache(self) -> None:
        """Clear in-memory cache."""
        with self._lock:
            self._uuid_to_int.clear()
            self._int_to_uuid.clear()
