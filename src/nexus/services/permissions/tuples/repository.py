"""Tuple Repository - Data access layer for ReBAC relationship tuples.

Extracted from rebac_manager.py (Issue #1459 Phase 7).

Provides:
- Connection management (pooled DBAPI connections via SQLAlchemy)
- SQL dialect abstraction (PostgreSQL/SQLite placeholder conversion)
- Zone revision tracking (revision-based cache quantization)
- Pure tuple query methods (subject sets, related objects, direct subjects)
- Cycle detection (recursive CTE)
- Cross-zone validation
- ABAC condition evaluation
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.core.rebac import WILDCARD_SUBJECT, Entity

if TYPE_CHECKING:
    from collections.abc import Generator

    from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


class TupleRepository:
    """Data access layer for ReBAC relationship tuples.

    Owns database connection management and provides pure query methods
    for reading tuple data. Write operations are orchestrated by the
    manager layer which handles cache invalidation and other cross-cutting
    concerns.

    Args:
        engine: SQLAlchemy database engine (SQLite or PostgreSQL)
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

        # Track DBAPI to SQLAlchemy connection mapping for proper cleanup
        # (sqlite3.Connection in Python 3.13+ doesn't allow setting arbitrary attributes)
        self._conn_map: dict[int, Any] = {}

        # PostgreSQL version cache for feature detection
        self._pg_version: int | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def get_connection(self) -> Any:
        """Get a DBAPI connection from the pool.

        Uses engine.connect() which properly goes through the connection pool
        and respects pool_pre_ping for automatic stale connection detection.

        Note: Caller is responsible for closing the connection.
        Prefer using connection() context manager when possible.

        Returns:
            DBAPI connection object
        """
        sa_conn = self.engine.connect()
        dbapi_conn = sa_conn.connection.dbapi_connection
        self._conn_map[id(dbapi_conn)] = sa_conn
        return dbapi_conn

    def close_connection(self, conn: Any) -> None:
        """Close a connection obtained from get_connection().

        Args:
            conn: DBAPI connection to close
        """
        import contextlib as _contextlib

        conn_id = id(conn)
        if conn_id in self._conn_map:
            with _contextlib.suppress(Exception):
                self._conn_map[conn_id].close()
            self._conn_map.pop(conn_id, None)
        else:
            with _contextlib.suppress(Exception):
                conn.close()

    @contextmanager
    def connection(self) -> Generator[Any, None, None]:
        """Context manager for database connections.

        Uses engine.connect() which properly goes through the connection pool
        and respects pool_pre_ping for automatic stale connection detection.

        Usage:
            with repo.connection() as conn:
                cursor = repo.create_cursor(conn)
                cursor.execute(...)
                conn.commit()
        """
        from sqlalchemy import text

        with self.engine.connect() as sa_conn:
            # Fix PostgreSQL prepared statement performance issue (#683)
            if self.engine.dialect.name == "postgresql":
                sa_conn.execute(text("SET plan_cache_mode = 'force_custom_plan'"))

            dbapi_conn = sa_conn.connection.dbapi_connection
            try:
                yield dbapi_conn
                sa_conn.commit()
            except Exception:
                sa_conn.rollback()
                raise

    def create_cursor(self, conn: Any) -> Any:
        """Create a cursor with appropriate cursor factory for the database type.

        For PostgreSQL: Uses RealDictCursor to return dict-like rows
        For SQLite: Ensures Row factory is set for dict-like access

        Args:
            conn: DB-API connection object

        Returns:
            Database cursor
        """
        actual_conn = conn.dbapi_connection if hasattr(conn, "dbapi_connection") else conn
        conn_module = type(actual_conn).__module__

        if "psycopg2" in conn_module:
            try:
                import psycopg2.extras

                return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            except (ImportError, AttributeError):
                return conn.cursor()
        elif "sqlite3" in conn_module:
            import sqlite3

            if not hasattr(actual_conn, "row_factory") or actual_conn.row_factory is None:
                actual_conn.row_factory = sqlite3.Row
            return conn.cursor()
        else:
            return conn.cursor()

    # ------------------------------------------------------------------
    # SQL dialect helpers
    # ------------------------------------------------------------------

    def fix_sql_placeholders(self, sql: str) -> str:
        """Convert SQLite ? placeholders to PostgreSQL %s if needed.

        Args:
            sql: SQL query with ? placeholders

        Returns:
            SQL query with appropriate placeholders for the database dialect
        """
        if self.engine.dialect.name == "postgresql":
            return sql.replace("?", "%s")
        return sql

    @property
    def supports_old_new_returning(self) -> bool:
        """Check if database supports OLD/NEW in RETURNING clauses.

        PostgreSQL 18+ supports OLD/NEW aliases in RETURNING clauses.

        Returns:
            True if PostgreSQL 18+, False otherwise
        """
        if self.engine.dialect.name != "postgresql":
            return False

        if self._pg_version is None:
            try:
                from sqlalchemy import text

                with self.engine.connect() as conn:
                    result = conn.execute(text("SELECT version()"))
                    version_str = result.scalar()
                    import re

                    match = re.search(r"PostgreSQL (\d+)", version_str or "")
                    self._pg_version = int(match.group(1)) if match else 0
            except Exception:
                self._pg_version = 0

        return self._pg_version >= 18

    # ------------------------------------------------------------------
    # Zone revision tracking (Issue #909)
    # ------------------------------------------------------------------

    def get_zone_revision(self, zone_id: str | None, conn: Any | None = None) -> int:
        """Get current revision for a zone (read-only, no increment).

        Used for revision-based cache key generation (Issue #909).

        Args:
            zone_id: Zone ID (defaults to "default")
            conn: Optional database connection to reuse

        Returns:
            Current revision number (0 if zone has no writes yet)
        """
        effective_zone = zone_id or "default"
        should_close = conn is None
        if conn is None:
            conn = self.get_connection()
        try:
            cursor = self.create_cursor(conn)
            cursor.execute(
                self.fix_sql_placeholders(
                    "SELECT current_version FROM rebac_version_sequences WHERE zone_id = ?"
                ),
                (effective_zone,),
            )
            row = cursor.fetchone()
            return int(row["current_version"]) if row else 0
        finally:
            if should_close:
                self.close_connection(conn)

    def increment_zone_revision(self, zone_id: str | None, conn: Any) -> int:
        """Increment and return the new revision for a zone.

        Called after successful write operations. Uses atomic DB operations
        for distributed consistency (Issue #909).

        Args:
            zone_id: Zone ID (defaults to "default")
            conn: Database connection (reuse existing transaction)

        Returns:
            New revision number after increment
        """
        effective_zone = zone_id or "default"
        cursor = self.create_cursor(conn)

        if self.engine.dialect.name == "postgresql":
            cursor.execute(
                """
                INSERT INTO rebac_version_sequences (zone_id, current_version, updated_at)
                VALUES (%s, 1, NOW())
                ON CONFLICT (zone_id)
                DO UPDATE SET current_version = rebac_version_sequences.current_version + 1,
                              updated_at = NOW()
                RETURNING current_version
                """,
                (effective_zone,),
            )
            row = cursor.fetchone()
            return int(row["current_version"]) if row else 1
        else:
            cursor.execute(
                self.fix_sql_placeholders(
                    "SELECT current_version FROM rebac_version_sequences WHERE zone_id = ?"
                ),
                (effective_zone,),
            )
            row = cursor.fetchone()

            if row:
                new_version = row["current_version"] + 1
                cursor.execute(
                    self.fix_sql_placeholders(
                        """
                        UPDATE rebac_version_sequences
                        SET current_version = ?, updated_at = ?
                        WHERE zone_id = ?
                        """
                    ),
                    (new_version, datetime.now(UTC).isoformat(), effective_zone),
                )
            else:
                new_version = 1
                cursor.execute(
                    self.fix_sql_placeholders(
                        """
                        INSERT INTO rebac_version_sequences (zone_id, current_version, updated_at)
                        VALUES (?, ?, ?)
                        """
                    ),
                    (effective_zone, new_version, datetime.now(UTC).isoformat()),
                )

            return int(new_version)

    # ------------------------------------------------------------------
    # Cycle detection
    # ------------------------------------------------------------------

    def would_create_cycle(
        self, conn: Any, subject: Entity, object_entity: Entity, zone_id: str | None
    ) -> bool:
        """Check if creating a parent relation would create a cycle.

        A cycle exists if object is already an ancestor of subject.
        Uses a recursive CTE for efficient single-query cycle detection
        (5-8x faster than iterative BFS for deep hierarchies).

        Args:
            conn: Database connection
            subject: The child node (e.g., file A)
            object_entity: The parent node (e.g., file B)
            zone_id: Optional zone ID for isolation

        Returns:
            True if adding this relation would create a cycle
        """
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "CYCLE CHECK: Want to create %s:%s -> parent -> %s:%s",
                subject.entity_type,
                subject.entity_id,
                object_entity.entity_type,
                object_entity.entity_id,
            )

        cursor = self.create_cursor(conn)
        max_depth = 50

        if self.engine.dialect.name == "postgresql":
            query = """
                WITH RECURSIVE ancestors AS (
                    SELECT
                        object_type as ancestor_type,
                        object_id as ancestor_id,
                        1 as depth
                    FROM rebac_tuples
                    WHERE subject_type = %s
                      AND subject_id = %s
                      AND relation = 'parent'
                      AND (zone_id = %s OR (zone_id IS NULL AND %s IS NULL))

                    UNION ALL

                    SELECT
                        t.object_type,
                        t.object_id,
                        a.depth + 1
                    FROM rebac_tuples t
                    INNER JOIN ancestors a
                        ON t.subject_type = a.ancestor_type
                        AND t.subject_id = a.ancestor_id
                    WHERE t.relation = 'parent'
                      AND (t.zone_id = %s OR (t.zone_id IS NULL AND %s IS NULL))
                      AND a.depth < %s
                )
                SELECT 1 FROM ancestors
                WHERE ancestor_type = %s AND ancestor_id = %s
                LIMIT 1
            """
        else:
            query = self.fix_sql_placeholders(
                """
                WITH RECURSIVE ancestors AS (
                    SELECT
                        object_type as ancestor_type,
                        object_id as ancestor_id,
                        1 as depth
                    FROM rebac_tuples
                    WHERE subject_type = ?
                      AND subject_id = ?
                      AND relation = 'parent'
                      AND (zone_id = ? OR (zone_id IS NULL AND ? IS NULL))

                    UNION ALL

                    SELECT
                        t.object_type,
                        t.object_id,
                        a.depth + 1
                    FROM rebac_tuples t
                    INNER JOIN ancestors a
                        ON t.subject_type = a.ancestor_type
                        AND t.subject_id = a.ancestor_id
                    WHERE t.relation = 'parent'
                      AND (t.zone_id = ? OR (t.zone_id IS NULL AND ? IS NULL))
                      AND a.depth < ?
                )
                SELECT 1 FROM ancestors
                WHERE ancestor_type = ? AND ancestor_id = ?
                LIMIT 1
            """
            )

        params = (
            object_entity.entity_type,
            object_entity.entity_id,
            zone_id,
            zone_id,
            zone_id,
            zone_id,
            max_depth,
            subject.entity_type,
            subject.entity_id,
        )

        cursor.execute(query, params)
        result = cursor.fetchone()

        if result:
            logger.warning(
                "Cycle detected: %s:%s is an ancestor of %s:%s. Cannot create parent relation.",
                subject.entity_type,
                subject.entity_id,
                object_entity.entity_type,
                object_entity.entity_id,
            )
            return True

        logger.debug("  No cycle detected")
        return False

    # ------------------------------------------------------------------
    # Cross-zone validation
    # ------------------------------------------------------------------

    @staticmethod
    def validate_cross_zone(
        zone_id: str | None,
        subject_zone_id: str | None,
        object_zone_id: str | None,
    ) -> None:
        """Validate cross-zone relationships (P0-4).

        Prevents cross-zone relationship tuples for security.

        Args:
            zone_id: Tuple zone ID
            subject_zone_id: Subject's zone ID
            object_zone_id: Object's zone ID

        Raises:
            ValueError: If cross-zone relationship is detected
        """
        if zone_id is not None and subject_zone_id is not None and subject_zone_id != zone_id:
            raise ValueError(
                f"Cross-zone relationship not allowed: subject zone '{subject_zone_id}' "
                f"!= tuple zone '{zone_id}'"
            )
        if zone_id is not None and object_zone_id is not None and object_zone_id != zone_id:
            raise ValueError(
                f"Cross-zone relationship not allowed: object zone '{object_zone_id}' "
                f"!= tuple zone '{zone_id}'"
            )

    # ------------------------------------------------------------------
    # Tuple query methods
    # ------------------------------------------------------------------

    def find_subject_sets(
        self, relation: str, obj: Entity, zone_id: str | None = None
    ) -> list[tuple[str, str, str]]:
        """Find all subject sets that have a relation to an object.

        Subject sets are tuples with subject_relation set, like:
        (group:eng#member, editor-of, file:readme)

        SECURITY FIX (P0): Enforces zone_id filtering to prevent cross-zone leaks.

        Args:
            relation: Relation type
            obj: Object entity
            zone_id: Optional zone ID for multi-zone isolation

        Returns:
            List of (subject_type, subject_id, subject_relation) tuples
        """
        with self.connection() as conn:
            cursor = self.create_cursor(conn)

            if zone_id is None:
                cursor.execute(
                    self.fix_sql_placeholders(
                        """
                        SELECT subject_type, subject_id, subject_relation
                        FROM rebac_tuples
                        WHERE zone_id IS NULL
                          AND relation = ?
                          AND object_type = ? AND object_id = ?
                          AND subject_relation IS NOT NULL
                          AND (expires_at IS NULL OR expires_at >= ?)
                        """
                    ),
                    (relation, obj.entity_type, obj.entity_id, datetime.now(UTC).isoformat()),
                )
            else:
                cursor.execute(
                    self.fix_sql_placeholders(
                        """
                        SELECT subject_type, subject_id, subject_relation
                        FROM rebac_tuples
                        WHERE zone_id = ?
                          AND relation = ?
                          AND object_type = ? AND object_id = ?
                          AND subject_relation IS NOT NULL
                          AND (expires_at IS NULL OR expires_at >= ?)
                        """
                    ),
                    (
                        zone_id,
                        relation,
                        obj.entity_type,
                        obj.entity_id,
                        datetime.now(UTC).isoformat(),
                    ),
                )

            return [
                (row["subject_type"], row["subject_id"], row["subject_relation"])
                for row in cursor.fetchall()
            ]

    def find_related_objects(self, obj: Entity, relation: str) -> list[Entity]:
        """Find all objects related to obj via relation.

        For tupleToUserset traversal: finds tuples where (obj, relation, object).
        Example: parent of file X = tuples where subject=X, relation='parent'.

        Args:
            obj: Object entity (the subject of the tuple)
            relation: Relation type (e.g., "parent")

        Returns:
            List of related object entities
        """
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "find_related_objects: Looking for tuples where subject=%s, relation='%s'",
                obj,
                relation,
            )

        with self.connection() as conn:
            cursor = self.create_cursor(conn)

            cursor.execute(
                self.fix_sql_placeholders(
                    """
                    SELECT object_type, object_id
                    FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND relation = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (obj.entity_type, obj.entity_id, relation, datetime.now(UTC).isoformat()),
            )

            results = []
            for row in cursor.fetchall():
                entity = Entity(row["object_type"], row["object_id"])
                results.append(entity)

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("find_related_objects: found %d results", len(results))

            return results

    def find_subjects_with_relation(self, obj: Entity, relation: str) -> list[Entity]:
        """Find all subjects that have a relation to obj.

        Reverse of find_related_objects: finds tuples where (subject, relation, obj).
        Used for group permission inheritance patterns.

        Args:
            obj: Object entity
            relation: Relation type (e.g., "direct_viewer")

        Returns:
            List of subject entities
        """
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "find_subjects_with_relation: Looking for tuples where (?, '%s', %s)",
                relation,
                obj,
            )

        with self.connection() as conn:
            cursor = self.create_cursor(conn)

            cursor.execute(
                self.fix_sql_placeholders(
                    """
                    SELECT subject_type, subject_id
                    FROM rebac_tuples
                    WHERE object_type = ? AND object_id = ?
                      AND relation = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (obj.entity_type, obj.entity_id, relation, datetime.now(UTC).isoformat()),
            )

            results = []
            for row in cursor.fetchall():
                entity = Entity(row["subject_type"], row["subject_id"])
                results.append(entity)

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("find_subjects_with_relation: found %d results", len(results))

            return results

    def get_direct_subjects(self, relation: str, obj: Entity) -> list[tuple[str, str]]:
        """Get all subjects with direct relation to object.

        Args:
            relation: Relation type
            obj: Object entity

        Returns:
            List of (subject_type, subject_id) tuples
        """
        with self.connection() as conn:
            cursor = self.create_cursor(conn)

            cursor.execute(
                self.fix_sql_placeholders(
                    """
                    SELECT subject_type, subject_id
                    FROM rebac_tuples
                    WHERE relation = ?
                      AND object_type = ? AND object_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (relation, obj.entity_type, obj.entity_id, datetime.now(UTC).isoformat()),
            )

            return [(row["subject_type"], row["subject_id"]) for row in cursor.fetchall()]

    def bulk_check_tuples_exist(
        self,
        cursor: Any,
        parsed_tuples: list[dict[str, Any]],
    ) -> set[tuple[Any, ...]]:
        """Check which tuples already exist (bulk query).

        Args:
            cursor: Database cursor
            parsed_tuples: List of parsed tuple dicts

        Returns:
            Set of (subject, relation, object, zone_id) tuples that exist
        """
        if not parsed_tuples:
            return set()

        CHUNK_SIZE = 100
        existing: set[tuple[Any, ...]] = set()

        for chunk_start in range(0, len(parsed_tuples), CHUNK_SIZE):
            chunk = parsed_tuples[chunk_start : chunk_start + CHUNK_SIZE]

            conditions = []
            params: list[Any] = []

            for pt in chunk:
                conditions.append(
                    "(subject_type = ? AND subject_id = ? AND "
                    "(subject_relation = ? OR (subject_relation IS NULL AND ? IS NULL)) AND "
                    "relation = ? AND object_type = ? AND object_id = ? AND "
                    "(zone_id = ? OR (zone_id IS NULL AND ? IS NULL)))"
                )
                params.extend([
                    pt["subject_type"],
                    pt["subject_id"],
                    pt["subject_relation"],
                    pt["subject_relation"],
                    pt["relation"],
                    pt["object_type"],
                    pt["object_id"],
                    pt["zone_id"],
                    pt["zone_id"],
                ])

            query = f"""
                SELECT subject_type, subject_id, subject_relation, relation,
                       object_type, object_id, zone_id
                FROM rebac_tuples
                WHERE {" OR ".join(conditions)}
            """

            cursor.execute(self.fix_sql_placeholders(query), params)
            results = cursor.fetchall()

            for row in results:
                try:
                    subject_relation = row["subject_relation"]
                except (KeyError, IndexError):
                    subject_relation = None
                try:
                    row_zone_id = row["zone_id"]
                except (KeyError, IndexError):
                    row_zone_id = None

                existing.add((
                    (row["subject_type"], row["subject_id"], subject_relation),
                    row["relation"],
                    (row["object_type"], row["object_id"]),
                    row_zone_id,
                ))

        return existing

    # ------------------------------------------------------------------
    # ABAC condition evaluation
    # ------------------------------------------------------------------

    @staticmethod
    def evaluate_conditions(
        conditions: dict[str, Any] | None, context: dict[str, Any] | None
    ) -> bool:
        """Evaluate ABAC conditions against runtime context.

        Supports time windows, IP allowlists, device types, and custom attributes.

        Args:
            conditions: Conditions stored in tuple (JSON dict)
            context: Runtime context provided by caller

        Returns:
            True if conditions are satisfied (or no conditions exist)
        """
        if not conditions:
            return True

        if not context:
            logger.warning("ABAC conditions exist but no context provided - DENYING access")
            return False

        # Time window check
        if "time_window" in conditions:
            current_time = context.get("time")
            if not current_time:
                logger.debug("Time window condition but no 'time' in context - DENY")
                return False

            start = conditions["time_window"].get("start")
            end = conditions["time_window"].get("end")
            if start and end:
                try:
                    if "T" in current_time:
                        time_part = current_time.split("T")[1]
                        current_time_cmp = time_part.split("-")[0].split("+")[0][:8]
                    else:
                        current_time_cmp = current_time

                    if "T" in start:
                        start_cmp = start.split("T")[1].split("-")[0].split("+")[0][:8]
                    else:
                        start_cmp = start

                    if "T" in end:
                        end_cmp = end.split("T")[1].split("-")[0].split("+")[0][:8]
                    else:
                        end_cmp = end

                    if not (start_cmp <= current_time_cmp <= end_cmp):
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(
                                "Time %s outside window [%s, %s] - DENY",
                                current_time_cmp,
                                start_cmp,
                                end_cmp,
                            )
                        return False
                except (ValueError, IndexError) as e:
                    logger.warning("Failed to parse time format: %s - DENY", e)
                    return False

        # IP allowlist check
        if "allowed_ips" in conditions:
            current_ip = context.get("ip")
            if not current_ip:
                logger.debug("IP allowlist condition but no 'ip' in context - DENY")
                return False

            try:
                import ipaddress

                allowed = False
                for cidr in conditions["allowed_ips"]:
                    try:
                        network = ipaddress.ip_network(cidr, strict=False)
                        if ipaddress.ip_address(current_ip) in network:
                            allowed = True
                            break
                    except ValueError:
                        logger.warning("Invalid CIDR in allowlist: %s", cidr)
                        continue

                if not allowed:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("IP %s not in allowlist - DENY", current_ip)
                    return False
            except ImportError:
                logger.error("ipaddress module not available - cannot evaluate IP conditions")
                return False

        # Device type check
        if "allowed_devices" in conditions:
            current_device = context.get("device")
            if current_device not in conditions["allowed_devices"]:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Device %s not in allowed list %s - DENY",
                        current_device,
                        conditions["allowed_devices"],
                    )
                return False

        # Custom attribute checks
        if "attributes" in conditions:
            for key, expected_value in conditions["attributes"].items():
                actual_value = context.get(key)
                if actual_value != expected_value:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "Attribute %s: expected %s, got %s - DENY",
                            key,
                            expected_value,
                            actual_value,
                        )
                    return False

        return True

    # ------------------------------------------------------------------
    # Direct tuple lookup (partial - SQL queries only)
    # ------------------------------------------------------------------

    def find_direct_tuple_by_subject(
        self,
        cursor: Any,
        subject: Entity,
        relation: str,
        obj: Entity,
        zone_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find a direct concrete-subject tuple (SQL query only).

        This is the first check in _find_direct_relation_tuple: direct subject match
        and wildcard match. Does NOT handle userset-as-subject (check 3) — that
        requires the permission computation layer.

        Args:
            cursor: Database cursor (caller manages connection)
            subject: Subject entity
            relation: Relation type
            obj: Object entity
            zone_id: Optional zone ID

        Returns:
            Tuple dict or None if not found
        """
        now = datetime.now(UTC).isoformat()

        # Check 1: Direct concrete subject
        if zone_id is None:
            cursor.execute(
                self.fix_sql_placeholders(
                    """
                    SELECT tuple_id, subject_type, subject_id, subject_relation,
                           relation, object_type, object_id, conditions, expires_at
                    FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND subject_relation IS NULL
                      AND relation = ?
                      AND object_type = ? AND object_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                      AND zone_id IS NULL
                    LIMIT 1
                    """
                ),
                (subject.entity_type, subject.entity_id, relation, obj.entity_type, obj.entity_id, now),
            )
        else:
            cursor.execute(
                self.fix_sql_placeholders(
                    """
                    SELECT tuple_id, subject_type, subject_id, subject_relation,
                           relation, object_type, object_id, conditions, expires_at
                    FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND subject_relation IS NULL
                      AND relation = ?
                      AND object_type = ? AND object_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                      AND zone_id = ?
                    LIMIT 1
                    """
                ),
                (
                    subject.entity_type,
                    subject.entity_id,
                    relation,
                    obj.entity_type,
                    obj.entity_id,
                    now,
                    zone_id,
                ),
            )

        row = cursor.fetchone()
        if row:
            # Evaluate ABAC conditions if present
            conditions_json = row["conditions"]
            if conditions_json:
                try:
                    conds = json.loads(conditions_json) if isinstance(conditions_json, str) else conditions_json
                    # Return None with a flag if conditions need context evaluation
                    # The caller (manager) will handle context-based evaluation
                    return dict(row) if not conds else dict(row)
                except (json.JSONDecodeError, TypeError):
                    pass
            return dict(row)

        # Check 2: Wildcard/public access
        if (subject.entity_type, subject.entity_id) != WILDCARD_SUBJECT:
            wildcard_entity = Entity(WILDCARD_SUBJECT[0], WILDCARD_SUBJECT[1])

            if zone_id is None:
                cursor.execute(
                    self.fix_sql_placeholders(
                        """
                        SELECT tuple_id, subject_type, subject_id, subject_relation,
                               relation, object_type, object_id, conditions, expires_at
                        FROM rebac_tuples
                        WHERE subject_type = ? AND subject_id = ?
                          AND subject_relation IS NULL
                          AND relation = ?
                          AND object_type = ? AND object_id = ?
                          AND (expires_at IS NULL OR expires_at >= ?)
                          AND zone_id IS NULL
                        LIMIT 1
                        """
                    ),
                    (
                        wildcard_entity.entity_type,
                        wildcard_entity.entity_id,
                        relation,
                        obj.entity_type,
                        obj.entity_id,
                        now,
                    ),
                )
            else:
                cursor.execute(
                    self.fix_sql_placeholders(
                        """
                        SELECT tuple_id, subject_type, subject_id, subject_relation,
                               relation, object_type, object_id, conditions, expires_at
                        FROM rebac_tuples
                        WHERE subject_type = ? AND subject_id = ?
                          AND subject_relation IS NULL
                          AND relation = ?
                          AND object_type = ? AND object_id = ?
                          AND (expires_at IS NULL OR expires_at >= ?)
                          AND zone_id = ?
                        LIMIT 1
                        """
                    ),
                    (
                        wildcard_entity.entity_type,
                        wildcard_entity.entity_id,
                        relation,
                        obj.entity_type,
                        obj.entity_id,
                        now,
                        zone_id,
                    ),
                )

            row = cursor.fetchone()
            if row:
                return dict(row)

            # Check 2b: Cross-zone wildcard access (Issue #1064)
            if zone_id is not None:
                cursor.execute(
                    self.fix_sql_placeholders(
                        """
                        SELECT tuple_id, subject_type, subject_id, subject_relation,
                               relation, object_type, object_id, conditions, expires_at
                        FROM rebac_tuples
                        WHERE subject_type = ? AND subject_id = ?
                          AND subject_relation IS NULL
                          AND relation = ?
                          AND object_type = ? AND object_id = ?
                          AND (expires_at IS NULL OR expires_at >= ?)
                        LIMIT 1
                        """
                    ),
                    (
                        wildcard_entity.entity_type,
                        wildcard_entity.entity_id,
                        relation,
                        obj.entity_type,
                        obj.entity_id,
                        now,
                    ),
                )
                row = cursor.fetchone()
                if row:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("Cross-zone wildcard access: *:* -> %s -> %s", relation, obj)
                    return dict(row)

        return None
