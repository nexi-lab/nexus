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
from typing import TYPE_CHECKING

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
        _zone_id: str | None = None,  # Deprecated: kept for API compatibility, ignored
        conn: Connection | None = None,
    ) -> int:
        """Get or create an integer ID for a resource.

        Args:
            resource_type: Type of resource (e.g., "file")
            resource_id: String ID of resource (e.g., UUID or path)
            zone_id: DEPRECATED - ignored, kept for API compatibility
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
        from sqlalchemy import text

        def do_get_or_create(connection: Connection) -> int:
            # Try to get existing (no zone filter)
            query = text("""
                SELECT resource_int_id FROM tiger_resource_map
                WHERE resource_type = :resource_type
                  AND resource_id = :resource_id
            """)
            result = connection.execute(
                query,
                {
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                },
            )
            row = result.fetchone()
            if row:
                return int(row.resource_int_id)

            # Insert new
            if self._is_postgresql:
                insert_query = text("""
                    INSERT INTO tiger_resource_map (resource_type, resource_id, created_at)
                    VALUES (:resource_type, :resource_id, NOW())
                    ON CONFLICT (resource_type, resource_id) DO NOTHING
                    RETURNING resource_int_id
                """)
                result = connection.execute(
                    insert_query,
                    {
                        "resource_type": resource_type,
                        "resource_id": resource_id,
                    },
                )
                row = result.fetchone()
                # Commit so the data persists (Issue #934 fix)
                connection.commit()
                if row:
                    return int(row.resource_int_id)
                # Conflict occurred, fetch again
                result = connection.execute(
                    query,
                    {
                        "resource_type": resource_type,
                        "resource_id": resource_id,
                    },
                )
                row = result.fetchone()
                return int(row.resource_int_id) if row else -1
            else:
                # SQLite - use INSERT OR IGNORE then SELECT
                # Need to commit after INSERT so SELECT can see the new row
                insert_query = text("""
                    INSERT OR IGNORE INTO tiger_resource_map (resource_type, resource_id, created_at)
                    VALUES (:resource_type, :resource_id, datetime('now'))
                """)
                connection.execute(
                    insert_query,
                    {
                        "resource_type": resource_type,
                        "resource_id": resource_id,
                    },
                )
                # Commit so the SELECT can see the inserted row
                connection.commit()
                # Get the ID (either newly inserted or existing)
                result = connection.execute(
                    query,
                    {
                        "resource_type": resource_type,
                        "resource_id": resource_id,
                    },
                )
                row = result.fetchone()
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
        from sqlalchemy import text

        query = text("""
            SELECT resource_type, resource_id
            FROM tiger_resource_map
            WHERE resource_int_id = :int_id
        """)

        def execute(connection: Connection) -> tuple[str, str] | None:
            result = connection.execute(query, {"int_id": int_id})
            row = result.fetchone()
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
        from sqlalchemy import text

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

        # Bulk fetch from database (no zone filter)
        if self._is_postgresql:
            # PostgreSQL: Use UNNEST for efficient bulk lookup
            query = text("""
                SELECT resource_type, resource_id, resource_int_id
                FROM tiger_resource_map
                WHERE (resource_type, resource_id) IN (
                    SELECT UNNEST(:types), UNNEST(:ids)
                )
            """)
            types = [r[0] for r in to_fetch]
            ids = [r[1] for r in to_fetch]
            result = conn.execute(query, {"types": types, "ids": ids})
        else:
            # SQLite: Use VALUES clause
            if len(to_fetch) > 500:
                # Batch for large sets
                for i in range(0, len(to_fetch), 500):
                    batch = to_fetch[i : i + 500]
                    batch_results = self.bulk_get_int_ids(batch, conn)
                    results.update(batch_results)
                return results

            values = ", ".join(f"('{r[0]}', '{r[1]}')" for r in to_fetch)
            query = text(f"""
                SELECT resource_type, resource_id, resource_int_id
                FROM tiger_resource_map
                WHERE (resource_type, resource_id) IN (VALUES {values})
            """)
            result = conn.execute(query)

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

        # Query database for missing (no zone filter)
        from sqlalchemy import text

        if self._is_postgresql:
            # Use UNNEST for efficient batch lookup
            query = text("""
                SELECT resource_type, resource_id, resource_int_id
                FROM tiger_resource_map
                WHERE (resource_type, resource_id) IN (
                    SELECT unnest(:types::text[]), unnest(:ids::text[])
                )
            """)
            types = [m[0] for m in missing]
            ids = [m[1] for m in missing]

            def execute(connection: Connection) -> None:
                db_result = connection.execute(query, {"types": types, "ids": ids})
                for row in db_result:
                    key = (row.resource_type, row.resource_id)
                    result[key] = row.resource_int_id
                    with self._lock:
                        self._uuid_to_int[key] = row.resource_int_id
                        self._int_to_uuid[row.resource_int_id] = key
        else:
            # SQLite: Use individual queries (less efficient)
            query = text("""
                SELECT resource_int_id FROM tiger_resource_map
                WHERE resource_type = :type AND resource_id = :id
            """)

            def execute(connection: Connection) -> None:
                for key in missing:
                    db_result = connection.execute(query, {"type": key[0], "id": key[1]})
                    row = db_result.fetchone()
                    if row:
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
