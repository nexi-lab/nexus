"""ReBAC Manager for relationship-based access control.

This module implements the core ReBAC APIs:
- Check API: Fast permission checks with graph traversal and caching
- Write API: Create relationship tuples with changelog tracking
- Delete API: Remove relationship tuples with cache invalidation
- Expand API: Find all subjects with a given permission

Based on Google Zanzibar design with optimizations for embedded/local use.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from nexus.core.rebac import (
    DEFAULT_FILE_NAMESPACE,
    DEFAULT_GROUP_NAMESPACE,
    DEFAULT_MEMORY_NAMESPACE,
    DEFAULT_PLAYBOOK_NAMESPACE,
    DEFAULT_SKILL_NAMESPACE,
    DEFAULT_TRAJECTORY_NAMESPACE,
    WILDCARD_SUBJECT,
    Entity,
    NamespaceConfig,
)
from nexus.core.rebac_cache import ReBACPermissionCache
from nexus.core.rebac_fast import check_permissions_bulk_with_fallback, is_rust_available

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

# P0-6: Logger for security-critical denials
logger = logging.getLogger(__name__)


class ReBACManager:
    """Manager for ReBAC operations.

    Provides Zanzibar-style relationship-based access control with:
    - Direct tuple lookup
    - Recursive graph traversal
    - Permission expansion via namespace configs
    - Caching with TTL and invalidation
    - Cycle detection

    Attributes:
        engine: SQLAlchemy database engine (supports SQLite and PostgreSQL)
        cache_ttl_seconds: Time-to-live for cache entries (default: 300 = 5 minutes)
        max_depth: Maximum graph traversal depth (default: 10)
    """

    def __init__(
        self,
        engine: Engine,
        cache_ttl_seconds: int = 300,
        max_depth: int = 50,
        enable_l1_cache: bool = True,
        l1_cache_size: int = 10000,
        l1_cache_ttl: int = 60,
        enable_metrics: bool = True,
        enable_adaptive_ttl: bool = False,
    ):
        """Initialize ReBAC manager.

        Args:
            engine: SQLAlchemy database engine
            cache_ttl_seconds: L2 cache TTL in seconds (default: 5 minutes)
            max_depth: Maximum graph traversal depth (default: 10 hops)
            enable_l1_cache: Enable in-memory L1 cache (default: True)
            l1_cache_size: L1 cache max entries (default: 10k)
            l1_cache_ttl: L1 cache TTL in seconds (default: 60s)
            enable_metrics: Track cache metrics (default: True)
            enable_adaptive_ttl: Adjust TTL based on write frequency (default: False)
        """
        self.engine = engine
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_depth = max_depth
        self._last_cleanup_time: datetime | None = None
        self._namespaces_initialized = False  # Track if default namespaces were initialized

        # Initialize L1 in-memory cache
        self._l1_cache: ReBACPermissionCache | None = None
        if enable_l1_cache:
            self._l1_cache = ReBACPermissionCache(
                max_size=l1_cache_size,
                ttl_seconds=l1_cache_ttl,
                enable_metrics=enable_metrics,
                enable_adaptive_ttl=enable_adaptive_ttl,
            )
            logger.info(
                f"L1 cache enabled: max_size={l1_cache_size}, ttl={l1_cache_ttl}s, "
                f"metrics={enable_metrics}, adaptive_ttl={enable_adaptive_ttl}"
            )

        # Use SQLAlchemy sessionmaker for proper connection management
        from sqlalchemy.orm import sessionmaker

        self.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    def _get_connection(self) -> Any:
        """Get raw DB-API connection from SQLAlchemy engine.

        Creates connections on-demand rather than holding them open.
        Initialize namespaces on first actual use (not during init).
        """
        # Get a fresh connection each time - don't hold it
        conn = self.engine.raw_connection()
        return conn

    @contextmanager
    def _connection(self) -> Any:
        """Context manager for database connections.

        Ensures connections are properly closed after use.

        Usage:
            with self._connection() as conn:
                cursor = self._create_cursor(conn)
                cursor.execute(...)
                conn.commit()
        """
        conn = self._get_connection()
        try:
            yield conn
        finally:
            conn.close()

    def _create_cursor(self, conn: Any) -> Any:
        """Create a cursor with appropriate cursor factory for the database type.

        For PostgreSQL: Uses RealDictCursor to return dict-like rows
        For SQLite: Ensures Row factory is set for dict-like access

        Args:
            conn: DB-API connection object

        Returns:
            Database cursor
        """
        # Detect database type based on underlying DBAPI connection
        # SQLAlchemy wraps connections in _ConnectionFairy, need to check dbapi_connection
        actual_conn = conn.dbapi_connection if hasattr(conn, "dbapi_connection") else conn
        conn_module = type(actual_conn).__module__

        # Check if this is a PostgreSQL connection (psycopg2)
        if "psycopg2" in conn_module:
            try:
                import psycopg2.extras

                return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            except (ImportError, AttributeError):
                return conn.cursor()
        elif "sqlite3" in conn_module:
            # SQLite: Ensure Row factory is set for dict-like access
            import sqlite3

            if not hasattr(actual_conn, "row_factory") or actual_conn.row_factory is None:
                actual_conn.row_factory = sqlite3.Row
            return conn.cursor()
        else:
            # Other database - use default cursor
            return conn.cursor()

    def _ensure_namespaces_initialized(self) -> None:
        """Ensure default namespaces are initialized (called before first ReBAC operation)."""
        if not self._namespaces_initialized:
            import logging

            logger = logging.getLogger(__name__)
            logger.info("Initializing default namespaces...")

            conn = self.engine.raw_connection()
            try:
                self._initialize_default_namespaces_with_conn(conn)
                self._namespaces_initialized = True
                logger.info("Default namespaces initialized successfully")
            except Exception as e:
                logger.warning(f"Failed to initialize namespaces: {type(e).__name__}: {e}")
                import traceback

                logger.debug(traceback.format_exc())
            finally:
                conn.close()

    def _fix_sql_placeholders(self, sql: str) -> str:
        """Convert SQLite ? placeholders to PostgreSQL %s if needed.

        Args:
            sql: SQL query with ? placeholders

        Returns:
            SQL query with appropriate placeholders for the database dialect
        """
        dialect_name = self.engine.dialect.name
        if dialect_name == "postgresql":
            return sql.replace("?", "%s")
        return sql

    def _would_create_cycle_with_conn(
        self, conn: Any, subject: Entity, object_entity: Entity, tenant_id: str | None
    ) -> bool:
        """Check if creating a parent relation would create a cycle.

        A cycle exists if object is already an ancestor of subject.
        Example cycle: A -> B -> C -> A (would be created by adding A->parent->C)

        Uses a recursive CTE for efficient single-query cycle detection.
        This is 5-8x faster than iterative BFS for deep hierarchies.

        Args:
            subject: The child node (e.g., file A)
            object_entity: The parent node (e.g., file B)
            tenant_id: Optional tenant ID for isolation

        Returns:
            True if adding this relation would create a cycle, False otherwise
        """
        logger.debug(
            f"CYCLE CHECK: Want to create {subject.entity_type}:{subject.entity_id} -> parent -> "
            f"{object_entity.entity_type}:{object_entity.entity_id}"
        )

        cursor = self._create_cursor(conn)

        # Use recursive CTE to find all ancestors of object_entity in a single query
        # If subject is among the ancestors, adding subject->parent->object would create a cycle
        #
        # The CTE traverses: object_entity -> parent -> grandparent -> ... -> root
        # and checks if subject appears anywhere in that chain
        #
        # MAX_DEPTH prevents infinite loops in case of existing cycles in data

        max_depth = 50  # Same limit as GraphLimits.MAX_DEPTH

        if self.engine.dialect.name == "postgresql":
            # PostgreSQL syntax
            query = """
                WITH RECURSIVE ancestors AS (
                    -- Base case: start from the proposed parent (object_entity)
                    SELECT
                        object_type as ancestor_type,
                        object_id as ancestor_id,
                        1 as depth
                    FROM rebac_tuples
                    WHERE subject_type = %s
                      AND subject_id = %s
                      AND relation = 'parent'
                      AND (tenant_id = %s OR (tenant_id IS NULL AND %s IS NULL))

                    UNION ALL

                    -- Recursive case: find parents of current ancestors
                    SELECT
                        t.object_type,
                        t.object_id,
                        a.depth + 1
                    FROM rebac_tuples t
                    INNER JOIN ancestors a
                        ON t.subject_type = a.ancestor_type
                        AND t.subject_id = a.ancestor_id
                    WHERE t.relation = 'parent'
                      AND (t.tenant_id = %s OR (t.tenant_id IS NULL AND %s IS NULL))
                      AND a.depth < %s
                )
                SELECT 1 FROM ancestors
                WHERE ancestor_type = %s AND ancestor_id = %s
                LIMIT 1
            """
            params = (
                object_entity.entity_type,
                object_entity.entity_id,
                tenant_id,
                tenant_id,
                tenant_id,
                tenant_id,
                max_depth,
                subject.entity_type,
                subject.entity_id,
            )
        else:
            # SQLite syntax (supports recursive CTEs since 3.8.3)
            query = """
                WITH RECURSIVE ancestors AS (
                    -- Base case: start from the proposed parent (object_entity)
                    SELECT
                        object_type as ancestor_type,
                        object_id as ancestor_id,
                        1 as depth
                    FROM rebac_tuples
                    WHERE subject_type = ?
                      AND subject_id = ?
                      AND relation = 'parent'
                      AND (tenant_id = ? OR (tenant_id IS NULL AND ? IS NULL))

                    UNION ALL

                    -- Recursive case: find parents of current ancestors
                    SELECT
                        t.object_type,
                        t.object_id,
                        a.depth + 1
                    FROM rebac_tuples t
                    INNER JOIN ancestors a
                        ON t.subject_type = a.ancestor_type
                        AND t.subject_id = a.ancestor_id
                    WHERE t.relation = 'parent'
                      AND (t.tenant_id = ? OR (t.tenant_id IS NULL AND ? IS NULL))
                      AND a.depth < ?
                )
                SELECT 1 FROM ancestors
                WHERE ancestor_type = ? AND ancestor_id = ?
                LIMIT 1
            """
            params = (
                object_entity.entity_type,
                object_entity.entity_id,
                tenant_id,
                tenant_id,
                tenant_id,
                tenant_id,
                max_depth,
                subject.entity_type,
                subject.entity_id,
            )

        cursor.execute(query, params)
        result = cursor.fetchone()

        if result:
            logger.warning(
                f"Cycle detected: {subject.entity_type}:{subject.entity_id} is an ancestor of "
                f"{object_entity.entity_type}:{object_entity.entity_id}. Cannot create parent relation."
            )
            return True

        logger.debug("  No cycle detected")
        return False

    def _initialize_default_namespaces_with_conn(self, conn: Any) -> None:
        """Initialize default namespace configurations with given connection."""
        try:
            cursor = self._create_cursor(conn)

            # Check if rebac_namespaces table exists
            if self.engine.dialect.name == "sqlite":
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='rebac_namespaces'"
                )
            else:  # PostgreSQL
                cursor.execute("SELECT tablename FROM pg_tables WHERE tablename='rebac_namespaces'")

            if not cursor.fetchone():
                return  # Table doesn't exist yet

            # Check and create/update namespaces
            for ns_config in [
                DEFAULT_FILE_NAMESPACE,
                DEFAULT_GROUP_NAMESPACE,
                DEFAULT_MEMORY_NAMESPACE,
                DEFAULT_PLAYBOOK_NAMESPACE,
                DEFAULT_TRAJECTORY_NAMESPACE,
                DEFAULT_SKILL_NAMESPACE,
            ]:
                cursor.execute(
                    self._fix_sql_placeholders(
                        "SELECT namespace_id FROM rebac_namespaces WHERE object_type = ?"
                    ),
                    (ns_config.object_type,),
                )
                existing = cursor.fetchone()
                if not existing:
                    # Create namespace
                    cursor.execute(
                        self._fix_sql_placeholders(
                            "INSERT INTO rebac_namespaces (namespace_id, object_type, config, created_at, updated_at) VALUES (?, ?, ?, ?, ?)"
                        ),
                        (
                            ns_config.namespace_id,
                            ns_config.object_type,
                            json.dumps(ns_config.config),
                            datetime.now(UTC),
                            datetime.now(UTC),
                        ),
                    )
                else:
                    # BUGFIX for issue #338: Update existing namespace ONLY if it matches our default namespace_id
                    # This prevents overwriting custom namespaces created by tests or users
                    existing_namespace_id = existing["namespace_id"]
                    if existing_namespace_id == ns_config.namespace_id:
                        # This is our default namespace, update it to pick up config changes
                        cursor.execute(
                            self._fix_sql_placeholders(
                                "UPDATE rebac_namespaces SET config = ?, updated_at = ? WHERE namespace_id = ?"
                            ),
                            (
                                json.dumps(ns_config.config),
                                datetime.now(UTC),
                                ns_config.namespace_id,
                            ),
                        )
            conn.commit()
        except Exception as e:
            # If tables don't exist yet or other error, skip initialization
            import logging

            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to register default namespaces: {type(e).__name__}: {e}")
            import traceback

            logger.debug(traceback.format_exc())

    def _initialize_default_namespaces(self) -> None:
        """Initialize default namespace configurations if not present."""
        with self._connection() as conn:
            self._initialize_default_namespaces_with_conn(conn)

    def create_namespace(self, namespace: NamespaceConfig) -> None:
        """Create or update a namespace configuration.

        Args:
            namespace: Namespace configuration to create
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # Check if namespace exists
            cursor.execute(
                self._fix_sql_placeholders(
                    "SELECT namespace_id FROM rebac_namespaces WHERE object_type = ?"
                ),
                (namespace.object_type,),
            )
            existing = cursor.fetchone()

            if existing:
                # Update existing namespace
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        UPDATE rebac_namespaces
                        SET config = ?, updated_at = ?
                        WHERE object_type = ?
                        """
                    ),
                    (
                        json.dumps(namespace.config),
                        datetime.now(UTC).isoformat(),
                        namespace.object_type,
                    ),
                )
            else:
                # Insert new namespace
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        INSERT INTO rebac_namespaces (namespace_id, object_type, config, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """
                    ),
                    (
                        namespace.namespace_id,
                        namespace.object_type,
                        json.dumps(namespace.config),
                        namespace.created_at.isoformat(),
                        namespace.updated_at.isoformat(),
                    ),
                )

            conn.commit()

            # BUGFIX: Invalidate all cached checks for this namespace
            # When namespace config changes, cached permission checks may be stale
            self._invalidate_cache_for_namespace(namespace.object_type)

    def get_namespace(self, object_type: str) -> NamespaceConfig | None:
        """Get namespace configuration for an object type.

        Args:
            object_type: Type of object

        Returns:
            NamespaceConfig or None if not found
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT namespace_id, object_type, config, created_at, updated_at
                    FROM rebac_namespaces
                    WHERE object_type = ?
                    """
                ),
                (object_type,),
            )

            row = cursor.fetchone()
            if not row:
                return None

            # Both SQLite and PostgreSQL now return dict-like rows
            created_at = row["created_at"]
            updated_at = row["updated_at"]
            # SQLite returns ISO strings, PostgreSQL returns datetime objects
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at)
            if isinstance(updated_at, str):
                updated_at = datetime.fromisoformat(updated_at)

            return NamespaceConfig(
                namespace_id=row["namespace_id"],
                object_type=row["object_type"],
                config=json.loads(row["config"])
                if isinstance(row["config"], str)
                else row["config"],
                created_at=created_at,
                updated_at=updated_at,
            )

    def rebac_write(
        self,
        subject: tuple[str, str] | tuple[str, str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
        conditions: dict[str, Any] | None = None,
        tenant_id: str | None = None,
        subject_tenant_id: str | None = None,
        object_tenant_id: str | None = None,
    ) -> str:
        """Create a relationship tuple.

        Args:
            subject: (subject_type, subject_id) or (subject_type, subject_id, subject_relation) tuple
                    For userset-as-subject: ("group", "eng", "member") means "all members of group eng"
            relation: Relation type (e.g., 'member-of', 'owner-of')
            object: (object_type, object_id) tuple
            expires_at: Optional expiration time
            conditions: Optional JSON conditions
            tenant_id: Tenant ID for the tuple (P0-4: tenant isolation)
            subject_tenant_id: Subject's tenant ID (for cross-tenant validation)
            object_tenant_id: Object's tenant ID (for cross-tenant validation)

        Returns:
            Tuple ID of created relationship

        Raises:
            ValueError: If cross-tenant relationship is attempted

        Example:
            >>> # Concrete subject
            >>> manager.rebac_write(
            ...     subject=("agent", "alice_id"),
            ...     relation="member-of",
            ...     object=("group", "eng_team_id"),
            ...     tenant_id="org_acme"
            ... )
            >>> # Userset-as-subject
            >>> manager.rebac_write(
            ...     subject=("group", "eng_team_id", "member"),
            ...     relation="editor-of",
            ...     object=("file", "readme_txt"),
            ...     tenant_id="org_acme"
            ... )
        """
        # Ensure default namespaces are initialized
        self._ensure_namespaces_initialized()

        tuple_id = str(uuid.uuid4())

        # Parse subject (support userset-as-subject with 3-tuple)
        if len(subject) == 3:
            subject_type, subject_id, subject_relation = subject
            subject_entity = Entity(subject_type, subject_id)
        elif len(subject) == 2:
            subject_type, subject_id = subject
            subject_relation = None
            subject_entity = Entity(subject_type, subject_id)
        else:
            raise ValueError(f"subject must be 2-tuple or 3-tuple, got {len(subject)}-tuple")
        object_entity = Entity(object[0], object[1])

        # P0-4: Cross-tenant validation at write-time
        # Prevent cross-tenant relationship tuples (security critical!)
        #
        # SECURITY FIX: Check each tenant ID independently to prevent bypass via None
        # Previous logic: "if A and B and C" allowed bypass when B or C was None
        # New logic: Validate each provided tenant ID separately

        # If tuple has a tenant_id, validate subject's tenant matches (if provided)
        if (
            tenant_id is not None
            and subject_tenant_id is not None
            and subject_tenant_id != tenant_id
        ):
            raise ValueError(
                f"Cross-tenant relationship not allowed: subject tenant '{subject_tenant_id}' "
                f"!= tuple tenant '{tenant_id}'"
            )

        # If tuple has a tenant_id, validate object's tenant matches (if provided)
        if tenant_id is not None and object_tenant_id is not None and object_tenant_id != tenant_id:
            raise ValueError(
                f"Cross-tenant relationship not allowed: object tenant '{object_tenant_id}' "
                f"!= tuple tenant '{tenant_id}'"
            )

        with self._connection() as conn:
            # CYCLE DETECTION: Prevent cycles in parent relations
            # Check if this is a parent relation and would create a cycle
            # IMPORTANT: Must check inside the connection context to see existing tuples
            if relation == "parent" and self._would_create_cycle_with_conn(
                conn, subject_entity, object_entity, tenant_id
            ):
                raise ValueError(
                    f"Cycle detected: Creating parent relation from "
                    f"{subject_entity.entity_type}:{subject_entity.entity_id} to "
                    f"{object_entity.entity_type}:{object_entity.entity_id} would create a cycle"
                )
            cursor = self._create_cursor(conn)

            # Check if tuple already exists (idempotency fix)
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT tuple_id FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                    AND (subject_relation = ? OR (subject_relation IS NULL AND ? IS NULL))
                    AND relation = ?
                    AND object_type = ? AND object_id = ?
                    AND (tenant_id = ? OR (tenant_id IS NULL AND ? IS NULL))
                    """
                ),
                (
                    subject_entity.entity_type,
                    subject_entity.entity_id,
                    subject_relation,
                    subject_relation,
                    relation,
                    object_entity.entity_type,
                    object_entity.entity_id,
                    tenant_id,
                    tenant_id,
                ),
            )
            existing = cursor.fetchone()
            if existing:
                # Tuple already exists, return existing ID (idempotent)
                return cast(
                    str, existing[0] if isinstance(existing, tuple) else existing["tuple_id"]
                )

            # Insert tuple (P0-4: Include tenant_id fields for isolation)
            # v0.7.0: Include subject_relation for userset-as-subject support
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    INSERT INTO rebac_tuples (
                        tuple_id, subject_type, subject_id, subject_relation, relation,
                        object_type, object_id, created_at, expires_at, conditions,
                        tenant_id, subject_tenant_id, object_tenant_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    tuple_id,
                    subject_entity.entity_type,
                    subject_entity.entity_id,
                    subject_relation,
                    relation,
                    object_entity.entity_type,
                    object_entity.entity_id,
                    datetime.now(UTC).isoformat(),
                    expires_at.isoformat() if expires_at else None,
                    json.dumps(conditions) if conditions else None,
                    tenant_id,
                    subject_tenant_id,
                    object_tenant_id,
                ),
            )

            # Log to changelog
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    INSERT INTO rebac_changelog (
                        change_type, tuple_id, subject_type, subject_id,
                        relation, object_type, object_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    "INSERT",
                    tuple_id,
                    subject_entity.entity_type,
                    subject_entity.entity_id,
                    relation,
                    object_entity.entity_type,
                    object_entity.entity_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

            conn.commit()

            # Invalidate cache entries affected by this change
            # Pass expires_at to disable eager recomputation for expiring tuples
            self._invalidate_cache_for_tuple(
                subject_entity, relation, object_entity, tenant_id, subject_relation, expires_at
            )

            # CROSS-TENANT FIX: If subject is from a different tenant, also invalidate
            # cache for the subject's tenant. This is critical for cross-tenant shares
            # where the permission is granted in resource tenant but checked from user tenant.
            if subject_tenant_id is not None and subject_tenant_id != tenant_id:
                self._invalidate_cache_for_tuple(
                    subject_entity,
                    relation,
                    object_entity,
                    subject_tenant_id,
                    subject_relation,
                    expires_at,
                )

        return tuple_id

    def rebac_delete(self, tuple_id: str) -> bool:
        """Delete a relationship tuple.

        Args:
            tuple_id: ID of tuple to delete

        Returns:
            True if tuple was deleted, False if not found
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # Get tuple details before deleting (for changelog and cache invalidation)
            # P0-5: Filter expired tuples at read-time (prevent deleted/expired access leak)
            # BUGFIX: Use >= instead of > for exact expiration boundary
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT subject_type, subject_id, subject_relation, relation, object_type, object_id, tenant_id
                    FROM rebac_tuples
                    WHERE tuple_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (tuple_id, datetime.now(UTC).isoformat()),
            )
            row = cursor.fetchone()

            if not row:
                return False

            # Both SQLite and PostgreSQL now return dict-like rows
            subject = Entity(row["subject_type"], row["subject_id"])
            subject_relation = row["subject_relation"]
            relation = row["relation"]
            obj = Entity(row["object_type"], row["object_id"])
            tenant_id = row["tenant_id"]

            # Delete tuple
            cursor.execute(
                self._fix_sql_placeholders("DELETE FROM rebac_tuples WHERE tuple_id = ?"),
                (tuple_id,),
            )

            # Log to changelog
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    INSERT INTO rebac_changelog (
                        change_type, tuple_id, subject_type, subject_id,
                        relation, object_type, object_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    "DELETE",
                    tuple_id,
                    subject.entity_type,
                    subject.entity_id,
                    relation,
                    obj.entity_type,
                    obj.entity_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

            conn.commit()

            # Invalidate cache entries affected by this change
            self._invalidate_cache_for_tuple(subject, relation, obj, tenant_id, subject_relation)

        return True

    def update_object_path(
        self, old_path: str, new_path: str, object_type: str = "file", is_directory: bool = False
    ) -> int:
        """Update object_id and subject_id in ReBAC tuples when a file/directory is renamed or moved.

        This method ensures that permissions follow files when they are renamed or moved.
        For directories, it recursively updates all child paths.

        IMPORTANT: This updates BOTH object_id AND subject_id fields:
        - object_id: When the file/directory is the target of a permission
        - subject_id: When the file/directory is the source (e.g., parent relationships)

        Args:
            old_path: Original path
            new_path: New path after rename/move
            object_type: Type of object (default: "file")
            is_directory: If True, also update all child paths recursively

        Returns:
            Number of tuples updated

        Example:
            >>> # File rename
            >>> manager.update_object_path('/workspace/old.txt', '/workspace/new.txt')
            >>> # Directory move (updates all children)
            >>> manager.update_object_path('/workspace/old_dir', '/workspace/new_dir', is_directory=True)
        """
        updated_count = 0

        import logging

        logger = logging.getLogger(__name__)
        logger.debug(
            f"update_object_path called: old_path={old_path}, new_path={new_path}, is_directory={is_directory}"
        )

        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # STEP 1: Update tuples where the path is in object_id
            logger.debug(f"STEP 1: Looking for tuples with object_id matching {old_path}")
            if is_directory:
                # For directories, match exact path OR any child path
                # Use LIKE with escaped path to match /old_dir and /old_dir/*
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        SELECT tuple_id, subject_type, subject_id, subject_relation,
                               relation, object_type, object_id, tenant_id
                        FROM rebac_tuples
                        WHERE object_type = ?
                          AND (object_id = ? OR object_id LIKE ?)
                          AND (expires_at IS NULL OR expires_at >= ?)
                        """
                    ),
                    (
                        object_type,
                        old_path,
                        old_path + "/%",
                        datetime.now(UTC).isoformat(),
                    ),
                )
            else:
                # For files, only match exact path
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        SELECT tuple_id, subject_type, subject_id, subject_relation,
                               relation, object_type, object_id, tenant_id
                        FROM rebac_tuples
                        WHERE object_type = ?
                          AND object_id = ?
                          AND (expires_at IS NULL OR expires_at >= ?)
                        """
                    ),
                    (object_type, old_path, datetime.now(UTC).isoformat()),
                )

            rows = cursor.fetchall()
            logger.debug(f"STEP 1: Found {len(rows)} tuples with object_id to update")

            if rows:
                # PERF: Batch UPDATE with CASE statement (Issue #590)
                # Instead of N individual UPDATE queries, use a single UPDATE with CASE
                old_prefix_len = len(old_path)
                now_iso = datetime.now(UTC).isoformat()

                if is_directory:
                    # Batch update: exact match -> new_path, child paths -> new_path + suffix
                    cursor.execute(
                        self._fix_sql_placeholders(
                            """
                            UPDATE rebac_tuples
                            SET object_id = CASE
                                WHEN object_id = ? THEN ?
                                ELSE ? || SUBSTR(object_id, ?)
                            END
                            WHERE object_type = ?
                              AND (object_id = ? OR object_id LIKE ?)
                              AND (expires_at IS NULL OR expires_at >= ?)
                            """
                        ),
                        (
                            old_path,  # WHEN object_id = old_path
                            new_path,  # THEN new_path
                            new_path,  # ELSE new_path || SUBSTR(...)
                            old_prefix_len + 1,  # SUBSTR offset (1-indexed in SQL)
                            object_type,
                            old_path,
                            old_path + "/%",
                            now_iso,
                        ),
                    )
                else:
                    # Simple batch update for files (exact match only)
                    cursor.execute(
                        self._fix_sql_placeholders(
                            """
                            UPDATE rebac_tuples
                            SET object_id = ?
                            WHERE object_type = ?
                              AND object_id = ?
                              AND (expires_at IS NULL OR expires_at >= ?)
                            """
                        ),
                        (new_path, object_type, old_path, now_iso),
                    )

                logger.debug(f"STEP 1: Batch updated {cursor.rowcount} tuples")

                # PERF: Batch INSERT changelog entries
                changelog_entries = []
                for row in rows:
                    old_object_id = row["object_id"]
                    if is_directory and old_object_id.startswith(old_path + "/"):
                        new_object_id = new_path + old_object_id[old_prefix_len:]
                    else:
                        new_object_id = new_path

                    changelog_entries.append(
                        (
                            "UPDATE",
                            row["tuple_id"],
                            row["subject_type"],
                            row["subject_id"],
                            row["relation"],
                            object_type,
                            new_object_id,
                            now_iso,
                        )
                    )

                cursor.executemany(
                    self._fix_sql_placeholders(
                        """
                        INSERT INTO rebac_changelog (
                            change_type, tuple_id, subject_type, subject_id,
                            relation, object_type, object_id, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    changelog_entries,
                )

                # Invalidate caches (still need to iterate, but it's in-memory)
                for row in rows:
                    old_object_id = row["object_id"]
                    if is_directory and old_object_id.startswith(old_path + "/"):
                        new_object_id = new_path + old_object_id[old_prefix_len:]
                    else:
                        new_object_id = new_path

                    subject = Entity(row["subject_type"], row["subject_id"])
                    old_obj = Entity(object_type, old_object_id)
                    new_obj = Entity(object_type, new_object_id)
                    relation = row["relation"]
                    tenant_id = row["tenant_id"]
                    subject_relation = row["subject_relation"]

                    self._invalidate_cache_for_tuple(
                        subject, relation, old_obj, tenant_id, subject_relation, conn=conn
                    )
                    self._invalidate_cache_for_tuple(
                        subject, relation, new_obj, tenant_id, subject_relation, conn=conn
                    )

                updated_count += len(rows)

            # STEP 2: Update tuples where the path is in subject_id (e.g., parent relationships)
            # This is critical for file-to-file relationships like "file:X -> parent -> file:Y"
            logger.debug(f"STEP 2: Looking for tuples with subject_id matching {old_path}")
            if is_directory:
                # For directories, match exact path OR any child path in subject_id
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        SELECT tuple_id, subject_type, subject_id, subject_relation,
                               relation, object_type, object_id, tenant_id
                        FROM rebac_tuples
                        WHERE subject_type = ?
                          AND (subject_id = ? OR subject_id LIKE ?)
                          AND (expires_at IS NULL OR expires_at >= ?)
                        """
                    ),
                    (
                        object_type,
                        old_path,
                        old_path + "/%",
                        datetime.now(UTC).isoformat(),
                    ),
                )
            else:
                # For files, only match exact path in subject_id
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        SELECT tuple_id, subject_type, subject_id, subject_relation,
                               relation, object_type, object_id, tenant_id
                        FROM rebac_tuples
                        WHERE subject_type = ?
                          AND subject_id = ?
                          AND (expires_at IS NULL OR expires_at >= ?)
                        """
                    ),
                    (object_type, old_path, datetime.now(UTC).isoformat()),
                )

            subject_rows = cursor.fetchall()
            logger.debug(f"STEP 2: Found {len(subject_rows)} tuples with subject_id to update")

            if subject_rows:
                # PERF: Batch UPDATE with CASE statement (Issue #590)
                old_prefix_len = len(old_path)
                now_iso = datetime.now(UTC).isoformat()

                if is_directory:
                    # Batch update: exact match -> new_path, child paths -> new_path + suffix
                    cursor.execute(
                        self._fix_sql_placeholders(
                            """
                            UPDATE rebac_tuples
                            SET subject_id = CASE
                                WHEN subject_id = ? THEN ?
                                ELSE ? || SUBSTR(subject_id, ?)
                            END
                            WHERE subject_type = ?
                              AND (subject_id = ? OR subject_id LIKE ?)
                              AND (expires_at IS NULL OR expires_at >= ?)
                            """
                        ),
                        (
                            old_path,  # WHEN subject_id = old_path
                            new_path,  # THEN new_path
                            new_path,  # ELSE new_path || SUBSTR(...)
                            old_prefix_len + 1,  # SUBSTR offset (1-indexed in SQL)
                            object_type,
                            old_path,
                            old_path + "/%",
                            now_iso,
                        ),
                    )
                else:
                    # Simple batch update for files (exact match only)
                    cursor.execute(
                        self._fix_sql_placeholders(
                            """
                            UPDATE rebac_tuples
                            SET subject_id = ?
                            WHERE subject_type = ?
                              AND subject_id = ?
                              AND (expires_at IS NULL OR expires_at >= ?)
                            """
                        ),
                        (new_path, object_type, old_path, now_iso),
                    )

                logger.debug(f"STEP 2: Batch updated {cursor.rowcount} tuples")

                # PERF: Batch INSERT changelog entries
                changelog_entries = []
                for row in subject_rows:
                    old_subject_id = row["subject_id"]
                    if is_directory and old_subject_id.startswith(old_path + "/"):
                        new_subject_id = new_path + old_subject_id[old_prefix_len:]
                    else:
                        new_subject_id = new_path

                    changelog_entries.append(
                        (
                            "UPDATE",
                            row["tuple_id"],
                            object_type,
                            new_subject_id,
                            row["relation"],
                            row["object_type"],
                            row["object_id"],
                            now_iso,
                        )
                    )

                cursor.executemany(
                    self._fix_sql_placeholders(
                        """
                        INSERT INTO rebac_changelog (
                            change_type, tuple_id, subject_type, subject_id,
                            relation, object_type, object_id, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    changelog_entries,
                )

                # Invalidate caches (still need to iterate, but it's in-memory)
                for row in subject_rows:
                    old_subject_id = row["subject_id"]
                    if is_directory and old_subject_id.startswith(old_path + "/"):
                        new_subject_id = new_path + old_subject_id[old_prefix_len:]
                    else:
                        new_subject_id = new_path

                    old_subj = Entity(object_type, old_subject_id)
                    new_subj = Entity(object_type, new_subject_id)
                    obj = Entity(row["object_type"], row["object_id"])
                    relation = row["relation"]
                    tenant_id = row["tenant_id"]
                    subject_relation = row["subject_relation"]

                    self._invalidate_cache_for_tuple(
                        old_subj, relation, obj, tenant_id, subject_relation, conn=conn
                    )
                    self._invalidate_cache_for_tuple(
                        new_subj, relation, obj, tenant_id, subject_relation, conn=conn
                    )

                updated_count += len(subject_rows)

            conn.commit()
            logger.info(f"update_object_path complete: updated {updated_count} tuples total")

        return updated_count

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None = None,
        tenant_id: str | None = None,
    ) -> bool:
        """Check if subject has permission on object.

        Uses caching and recursive graph traversal to compute permissions.
        Supports ABAC-style contextual conditions (time, location, device, etc.).

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., 'read', 'write')
            object: (object_type, object_id) tuple
            context: Optional context for ABAC evaluation (time, ip, device, etc.)
            tenant_id: Optional tenant ID for multi-tenant isolation

        Returns:
            True if permission is granted, False otherwise

        Example:
            >>> # Basic check
            >>> manager.rebac_check(
            ...     subject=("agent", "alice_id"),
            ...     permission="read",
            ...     object=("file", "file_id")
            ... )
            True

            >>> # With ABAC context
            >>> manager.rebac_check(
            ...     subject=("agent", "contractor"),
            ...     permission="read",
            ...     object=("file", "sensitive"),
            ...     context={"time": "14:30", "ip": "10.0.1.5"}
            ... )
            True
        """
        # Ensure default namespaces are initialized
        self._ensure_namespaces_initialized()

        subject_entity = Entity(subject[0], subject[1])
        object_entity = Entity(object[0], object[1])

        logger.debug(
            f"🔍 REBAC CHECK: subject={subject_entity}, permission={permission}, object={object_entity}, tenant_id={tenant_id}"
        )

        # Clean up expired tuples first (this will invalidate affected caches)
        self._cleanup_expired_tuples_if_needed()

        # Check cache first (only if no context, since context makes checks dynamic)
        if context is None:
            cached = self._get_cached_check(subject_entity, permission, object_entity, tenant_id)
            if cached is not None:
                logger.debug(f"✅ CACHE HIT: result={cached}")
                return cached

        # Compute permission via graph traversal with context
        logger.debug("🔎 Computing permission (no cache hit, computing from graph)")
        result = self._compute_permission(
            subject_entity,
            permission,
            object_entity,
            visited=set(),
            depth=0,
            context=context,
            tenant_id=tenant_id,
        )

        logger.debug(f"{'✅' if result else '❌'} REBAC RESULT: {result}")

        # Cache result (only if no context)
        if context is None:
            self._cache_check_result(subject_entity, permission, object_entity, result, tenant_id)

        return result

    def rebac_check_batch(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
    ) -> list[bool]:
        """Batch permission checks for efficiency.

        Checks cache first for each check, then computes uncached checks.
        More efficient than individual checks when checking multiple permissions.

        Args:
            checks: List of (subject, permission, object) tuples to check

        Returns:
            List of boolean results in the same order as input

        Example:
            >>> results = manager.rebac_check_batch([
            ...     (("agent", "alice"), "read", ("file", "f1")),
            ...     (("agent", "alice"), "read", ("file", "f2")),
            ...     (("agent", "bob"), "write", ("file", "f3")),
            ... ])
            >>> # Returns: [True, False, True]
        """
        if not checks:
            return []

        # Clean up expired tuples first
        self._cleanup_expired_tuples_if_needed()

        # Map to track results by index
        results: dict[int, bool] = {}
        uncached_checks: list[tuple[int, Entity, str, Entity]] = []

        # Phase 1: Check cache for all checks
        for i, (subject, permission, obj) in enumerate(checks):
            subject_entity = Entity(subject[0], subject[1])
            object_entity = Entity(obj[0], obj[1])

            cached = self._get_cached_check(subject_entity, permission, object_entity)
            if cached is not None:
                results[i] = cached
            else:
                uncached_checks.append((i, subject_entity, permission, object_entity))

        # Phase 2: Compute uncached checks
        for i, subject_entity, permission, object_entity in uncached_checks:
            result = self._compute_permission(
                subject_entity, permission, object_entity, visited=set(), depth=0
            )
            self._cache_check_result(
                subject_entity, permission, object_entity, result, tenant_id=None
            )
            results[i] = result

        # Return results in original order
        return [results[i] for i in range(len(checks))]

    def rebac_check_batch_fast(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        use_rust: bool = True,
    ) -> list[bool]:
        """Batch permission checks with optional Rust acceleration.

        This method is identical to rebac_check_batch but uses Rust for bulk
        computation of uncached checks, providing 50-85x speedup for large batches.

        Args:
            checks: List of (subject, permission, object) tuples to check
            use_rust: Use Rust acceleration if available (default: True)

        Returns:
            List of boolean results in the same order as input

        Performance:
            - Python only: ~500µs per uncached check
            - Rust acceleration: ~6µs per uncached check (85x speedup)
            - Recommended for batches of 10+ checks

        Example:
            >>> results = manager.rebac_check_batch_fast([
            ...     (("agent", "alice"), "read", ("file", "f1")),
            ...     (("agent", "alice"), "read", ("file", "f2")),
            ...     (("agent", "bob"), "write", ("file", "f3")),
            ... ])
            >>> # Returns: [True, False, True]
        """
        if not checks:
            return []

        # Clean up expired tuples first
        self._cleanup_expired_tuples_if_needed()

        # Map to track results by index
        results: dict[int, bool] = {}
        uncached_checks: list[tuple[int, tuple[tuple[str, str], str, tuple[str, str]]]] = []

        # Phase 1: Check cache for all checks
        for i, (subject, permission, obj) in enumerate(checks):
            subject_entity = Entity(subject[0], subject[1])
            object_entity = Entity(obj[0], obj[1])

            cached = self._get_cached_check(subject_entity, permission, object_entity)
            if cached is not None:
                results[i] = cached
            else:
                uncached_checks.append((i, (subject, permission, obj)))

        logger.debug(
            f"🚀 Batch check: {len(checks)} total, {len(results)} cached, "
            f"{len(uncached_checks)} to compute (Rust={'enabled' if use_rust and is_rust_available() else 'disabled'})"
        )

        # Phase 2: Compute uncached checks
        if uncached_checks:
            if use_rust and is_rust_available() and len(uncached_checks) >= 10:
                # Use Rust for bulk computation (efficient for 10+ checks)
                logger.debug(
                    f"⚡ Using Rust acceleration for {len(uncached_checks)} uncached checks"
                )
                try:
                    rust_results = self._compute_batch_rust([check for _, check in uncached_checks])
                    for idx, (i, _) in enumerate(uncached_checks):
                        result = rust_results[idx]
                        results[i] = result
                        # Cache the result
                        subject, permission, obj = uncached_checks[idx][1]
                        subject_entity = Entity(subject[0], subject[1])
                        object_entity = Entity(obj[0], obj[1])
                        self._cache_check_result(
                            subject_entity, permission, object_entity, result, tenant_id=None
                        )
                except Exception as e:
                    logger.warning(f"Rust batch computation failed, falling back to Python: {e}")
                    # Fall back to Python computation
                    self._compute_batch_python(uncached_checks, results)
            else:
                # Use Python for small batches or when Rust is unavailable
                reason = (
                    "batch too small (<10)" if len(uncached_checks) < 10 else "Rust not available"
                )
                logger.debug(
                    f"🐍 Using Python computation for {len(uncached_checks)} checks ({reason})"
                )
                self._compute_batch_python(uncached_checks, results)

        # Return results in original order
        return [results[i] for i in range(len(checks))]

    def _compute_batch_python(
        self,
        uncached_checks: list[tuple[int, tuple[tuple[str, str], str, tuple[str, str]]]],
        results: dict[int, bool],
    ) -> None:
        """Compute uncached checks using Python (original implementation)."""
        for i, (subject, permission, obj) in uncached_checks:
            subject_entity = Entity(subject[0], subject[1])
            object_entity = Entity(obj[0], obj[1])
            result = self._compute_permission(
                subject_entity, permission, object_entity, visited=set(), depth=0
            )
            self._cache_check_result(
                subject_entity, permission, object_entity, result, tenant_id=None
            )
            results[i] = result

    def _compute_batch_rust(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
    ) -> list[bool]:
        """Compute multiple permissions using Rust acceleration.

        Args:
            checks: List of (subject, permission, object) tuples

        Returns:
            List of boolean results in same order as input
        """
        # Fetch all relevant tuples from database
        tuples = self._fetch_all_tuples_for_batch(checks)

        # Get all namespace configs needed
        object_types = {obj[0] for _, _, obj in checks}
        namespace_configs: dict[str, Any] = {}
        for obj_type in object_types:
            ns = self.get_namespace(obj_type)
            if ns:
                namespace_configs[obj_type] = {
                    "relations": ns.config.get("relations", {}),
                    "permissions": ns.config.get("permissions", {}),
                }

        # Call Rust extension
        rust_results_dict = check_permissions_bulk_with_fallback(
            checks, tuples, namespace_configs, force_python=False
        )

        # Convert dict results back to list in original order
        results = []
        for subject, permission, obj in checks:
            key = (subject[0], subject[1], permission, obj[0], obj[1])
            results.append(rust_results_dict.get(key, False))

        return results

    def _fetch_all_tuples_for_batch(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        """Fetch all ReBAC tuples that might be relevant for batch checks.

        This fetches a superset of tuples to minimize database queries.
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # For simplicity, fetch all tuples (can be optimized later)
            # In production, we'd want to filter by relevant subjects/objects
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT subject_type, subject_id, subject_relation,
                           relation, object_type, object_id
                    FROM rebac_tuples
                    WHERE (expiration_time IS NULL OR expiration_time > ?)
                    """
                ),
                (datetime.now(UTC),),
            )

            tuples = []
            for row in cursor.fetchall():
                tuples.append(
                    {
                        "subject_type": row["subject_type"],
                        "subject_id": row["subject_id"],
                        "subject_relation": row["subject_relation"],
                        "relation": row["relation"],
                        "object_type": row["object_type"],
                        "object_id": row["object_id"],
                    }
                )

            logger.debug(f"📦 Fetched {len(tuples)} tuples for batch computation")
            return tuples

    def rebac_explain(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Explain why a subject has or doesn't have permission on an object.

        This is a debugging/audit API that traces through the permission graph
        to explain the result of a permission check.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., 'read', 'write')
            object: (object_type, object_id) tuple
            tenant_id: Optional tenant ID for multi-tenant isolation

        Returns:
            Dictionary with:
            - result: bool - whether permission is granted
            - cached: bool - whether result came from cache
            - reason: str - human-readable explanation
            - paths: list[dict] - all checked paths through the graph
            - successful_path: dict | None - the path that granted access (if any)
            - metadata: dict - request metadata (timestamp, request_id, etc.)

        Example:
            >>> explanation = manager.rebac_explain(
            ...     subject=("agent", "alice_id"),
            ...     permission="read",
            ...     object=("file", "file_id")
            ... )
            >>> print(explanation)
            {
                "result": True,
                "cached": False,
                "reason": "alice has 'viewer' relation via parent inheritance",
                "paths": [
                    {
                        "permission": "read",
                        "expanded_to": ["viewer"],
                        "relation": "viewer",
                        "expanded_to": ["direct_viewer", "parent_viewer", "editor"],
                        "relation": "parent_viewer",
                        "tupleToUserset": {
                            "tupleset": "parent",
                            "found_parents": [("workspace", "ws1")],
                            "computedUserset": "viewer",
                            "found_direct_relation": True
                        }
                    }
                ],
                "successful_path": {...},
                "metadata": {
                    "timestamp": "2025-01-15T10:30:00.123456Z",
                    "request_id": "req_abc123",
                    "max_depth": 10
                }
            }
        """
        # Generate request ID and timestamp
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        timestamp = datetime.now(UTC).isoformat()

        subject_entity = Entity(subject[0], subject[1])
        object_entity = Entity(object[0], object[1])

        # Clean up expired tuples first
        self._cleanup_expired_tuples_if_needed()

        # Check cache first
        cached = self._get_cached_check(subject_entity, permission, object_entity)
        from_cache = cached is not None

        # Track all paths explored
        paths: list[dict[str, Any]] = []

        # Compute permission with path tracking
        result = self._compute_permission_with_explanation(
            subject_entity,
            permission,
            object_entity,
            visited=set(),
            depth=0,
            paths=paths,
            tenant_id=tenant_id,
        )

        # Find successful path (if any)
        successful_path = None
        for path in paths:
            if path.get("granted"):
                successful_path = path
                break

        # Generate human-readable reason
        if result:
            if from_cache:
                reason = f"{subject_entity} has '{permission}' on {object_entity} (from cache)"
            elif successful_path:
                reason = self._format_path_reason(
                    subject_entity, permission, object_entity, successful_path
                )
            else:
                reason = f"{subject_entity} has '{permission}' on {object_entity}"
        else:
            if from_cache:
                reason = (
                    f"{subject_entity} does NOT have '{permission}' on {object_entity} (from cache)"
                )
            else:
                reason = f"{subject_entity} does NOT have '{permission}' on {object_entity} - no valid path found"

        return {
            "result": result if not from_cache else cached,
            "cached": from_cache,
            "reason": reason,
            "paths": paths,
            "successful_path": successful_path,
            "metadata": {
                "timestamp": timestamp,
                "request_id": request_id,
                "max_depth": self.max_depth,
                "cache_ttl_seconds": self.cache_ttl_seconds,
            },
        }

    def _format_path_reason(
        self, subject: Entity, permission: str, obj: Entity, path: dict[str, Any]
    ) -> str:
        """Format a permission path into a human-readable reason.

        Args:
            subject: Subject entity
            permission: Permission checked
            obj: Object entity
            path: Path dictionary from _compute_permission_with_explanation

        Returns:
            Human-readable explanation string
        """
        parts = []
        parts.append(f"{subject} has '{permission}' on {obj}")

        # Extract key information from path
        if "expanded_to" in path:
            relations = path["expanded_to"]
            if relations:
                parts.append(f"(expanded to relations: {', '.join(relations)})")

        if "direct_relation" in path and path["direct_relation"]:
            parts.append("via direct relation")
        elif "tupleToUserset" in path:
            ttu = path["tupleToUserset"]
            if "found_parents" in ttu and ttu["found_parents"]:
                parent = ttu["found_parents"][0]
                parts.append(f"via parent {parent[0]}:{parent[1]}")
        elif "union" in path:
            parts.append("via union of relations")

        return " ".join(parts)

    def _compute_permission_with_explanation(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        visited: set[tuple[str, str, str, str, str]],
        depth: int,
        paths: list[dict[str, Any]],
        tenant_id: str | None = None,
    ) -> bool:
        """Compute permission with detailed path tracking for explanation.

        This is similar to _compute_permission but tracks all paths explored.

        Args:
            subject: Subject entity
            permission: Permission to check
            obj: Object entity
            visited: Set of visited nodes to detect cycles
            depth: Current traversal depth
            paths: List to accumulate path information
            tenant_id: Optional tenant ID for multi-tenant isolation

        Returns:
            True if permission is granted
        """
        # Initialize path entry
        path_entry: dict[str, Any] = {
            "subject": str(subject),
            "permission": permission,
            "object": str(obj),
            "depth": depth,
            "granted": False,
        }

        # Check depth limit
        if depth > self.max_depth:
            path_entry["error"] = f"Depth limit exceeded (max={self.max_depth})"
            paths.append(path_entry)
            return False

        # Check for cycles
        visit_key = (
            subject.entity_type,
            subject.entity_id,
            permission,
            obj.entity_type,
            obj.entity_id,
        )
        if visit_key in visited:
            path_entry["error"] = "Cycle detected"
            paths.append(path_entry)
            return False
        visited.add(visit_key)

        # Get namespace config
        namespace = self.get_namespace(obj.entity_type)
        if not namespace:
            # No namespace - check direct relation only
            tuple_info = self._find_direct_relation_tuple(
                subject, permission, obj, tenant_id=tenant_id
            )
            direct = tuple_info is not None
            path_entry["direct_relation"] = direct
            if tuple_info:
                path_entry["tuple"] = tuple_info
            path_entry["granted"] = direct
            paths.append(path_entry)
            return direct

        # Check if permission is defined explicitly
        if namespace.has_permission(permission):
            usersets = namespace.get_permission_usersets(permission)
            path_entry["expanded_to"] = usersets

            for userset in usersets:
                userset_sub_paths: list[dict[str, Any]] = []
                if self._compute_permission_with_explanation(
                    subject, userset, obj, visited.copy(), depth + 1, userset_sub_paths, tenant_id
                ):
                    path_entry["granted"] = True
                    path_entry["via_userset"] = userset
                    path_entry["sub_paths"] = userset_sub_paths
                    paths.append(path_entry)
                    return True

            paths.append(path_entry)
            return False

        # Check if permission is defined as a relation (legacy)
        rel_config = namespace.get_relation_config(permission)
        if not rel_config:
            # Not defined in namespace - check direct relation
            tuple_info = self._find_direct_relation_tuple(
                subject, permission, obj, tenant_id=tenant_id
            )
            direct = tuple_info is not None
            path_entry["direct_relation"] = direct
            if tuple_info:
                path_entry["tuple"] = tuple_info
            path_entry["granted"] = direct
            paths.append(path_entry)
            return direct

        # Handle union
        if namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            path_entry["union"] = union_relations

            for rel in union_relations:
                union_sub_paths: list[dict[str, Any]] = []
                if self._compute_permission_with_explanation(
                    subject, rel, obj, visited.copy(), depth + 1, union_sub_paths, tenant_id
                ):
                    path_entry["granted"] = True
                    path_entry["via_union_member"] = rel
                    path_entry["sub_paths"] = union_sub_paths
                    paths.append(path_entry)
                    return True

            paths.append(path_entry)
            return False

        # Handle intersection
        if namespace.has_intersection(permission):
            intersection_relations = namespace.get_intersection_relations(permission)
            path_entry["intersection"] = intersection_relations
            all_granted = True

            for rel in intersection_relations:
                intersection_sub_paths: list[dict[str, Any]] = []
                if not self._compute_permission_with_explanation(
                    subject, rel, obj, visited.copy(), depth + 1, intersection_sub_paths, tenant_id
                ):
                    all_granted = False
                    break

            path_entry["granted"] = all_granted
            paths.append(path_entry)
            return all_granted

        # Handle exclusion
        if namespace.has_exclusion(permission):
            excluded_rel = namespace.get_exclusion_relation(permission)
            if excluded_rel:
                exclusion_sub_paths: list[dict[str, Any]] = []
                has_excluded = self._compute_permission_with_explanation(
                    subject,
                    excluded_rel,
                    obj,
                    visited.copy(),
                    depth + 1,
                    exclusion_sub_paths,
                    tenant_id,
                )
                path_entry["exclusion"] = excluded_rel
                path_entry["granted"] = not has_excluded
                paths.append(path_entry)
                return not has_excluded

            paths.append(path_entry)
            return False

        # Handle tupleToUserset
        if namespace.has_tuple_to_userset(permission):
            ttu = namespace.get_tuple_to_userset(permission)
            if ttu:
                tupleset_relation = ttu["tupleset"]
                computed_userset = ttu["computedUserset"]

                # Pattern 1 (parent-style): Find objects where (obj, tupleset_relation, ?)
                related_objects = self._find_related_objects(obj, tupleset_relation)
                # Pattern 2 (group-style): Find subjects where (?, tupleset_relation, obj)
                related_subjects = self._find_subjects_with_relation(obj, tupleset_relation)

                path_entry["tupleToUserset"] = {
                    "tupleset": tupleset_relation,
                    "computedUserset": computed_userset,
                    "found_parents": [(o.entity_type, o.entity_id) for o in related_objects],
                    "found_subjects": [(s.entity_type, s.entity_id) for s in related_subjects],
                }

                # Check parent-style relations
                for related_obj in related_objects:
                    ttu_sub_paths: list[dict[str, Any]] = []
                    if self._compute_permission_with_explanation(
                        subject,
                        computed_userset,
                        related_obj,
                        visited.copy(),
                        depth + 1,
                        ttu_sub_paths,
                        tenant_id,
                    ):
                        path_entry["granted"] = True
                        path_entry["sub_paths"] = ttu_sub_paths
                        path_entry["pattern"] = "parent"
                        paths.append(path_entry)
                        return True

                # Check group-style relations
                for related_subj in related_subjects:
                    ttu_sub_paths = []
                    if self._compute_permission_with_explanation(
                        subject,
                        computed_userset,
                        related_subj,
                        visited.copy(),
                        depth + 1,
                        ttu_sub_paths,
                        tenant_id,
                    ):
                        path_entry["granted"] = True
                        path_entry["sub_paths"] = ttu_sub_paths
                        path_entry["pattern"] = "group"
                        paths.append(path_entry)
                        return True

            paths.append(path_entry)
            return False

        # Direct relation check
        tuple_info = self._find_direct_relation_tuple(subject, permission, obj, tenant_id=tenant_id)
        direct = tuple_info is not None
        path_entry["direct_relation"] = direct
        if tuple_info:
            path_entry["tuple"] = tuple_info
        path_entry["granted"] = direct
        paths.append(path_entry)
        return direct

    def _compute_permission(
        self,
        subject: Entity,
        permission: str | dict[str, Any],
        obj: Entity,
        visited: set[tuple[str, str, str, str, str]],
        depth: int,
        context: dict[str, Any] | None = None,
        tenant_id: str | None = None,
    ) -> bool:
        """Compute permission via graph traversal.

        Args:
            subject: Subject entity
            permission: Permission to check (can be string or userset dict)
            obj: Object entity
            visited: Set of visited (subject_type, subject_id, permission, object_type, object_id) to detect cycles
            depth: Current traversal depth
            context: Optional ABAC context for condition evaluation
            tenant_id: Optional tenant ID for multi-tenant isolation

        Returns:
            True if permission is granted
        """
        # P0-6: Explicit deny on graph traversal limit exceeded
        # Security policy: ALWAYS deny when graph is too deep (never allow)
        if depth > self.max_depth:
            logger.debug(
                f"ReBAC graph traversal depth limit exceeded (max={self.max_depth}): "
                f"DENYING permission '{permission}' for {subject} -> {obj}"
            )
            return False  # EXPLICIT DENY - never allow on limit exceed

        # P0-6: Check for cycles (prevent infinite loops)
        # Convert permission to hashable string for visit_key
        permission_key = (
            json.dumps(permission, sort_keys=True) if isinstance(permission, dict) else permission
        )
        visit_key = (
            subject.entity_type,
            subject.entity_id,
            permission_key,
            obj.entity_type,
            obj.entity_id,
        )
        if visit_key in visited:
            # Cycle detected - deny to prevent infinite loop
            logger.debug(
                f"ReBAC graph cycle detected: DENYING permission '{permission}' "
                f"for {subject} -> {obj} (already visited)"
            )
            return False  # EXPLICIT DENY - never allow cycles
        visited.add(visit_key)

        # Handle dict permission (userset rewrite rules from Zanzibar)
        if isinstance(permission, dict):
            # Handle "this" - direct relation check
            if "this" in permission:
                # Check if there's a direct tuple (any relation works for "this")
                # In Zanzibar, "this" means the relation itself
                # This is used when the relation config is like: {"union": [{"this": {}}, ...]}
                # For now, we treat "this" as checking the relation name from context
                # Since we don't have the relation name in dict form, skip "this" handling
                # The caller should pass the relation name as a string, not {"this": {}}
                return False

            # Handle "computed_userset" - check a specific relation on the same object
            if "computed_userset" in permission:
                computed = permission["computed_userset"]
                if isinstance(computed, dict):
                    # Extract relation from computed_userset
                    # Format: {"object": ".", "relation": "viewer"}
                    # "." means the same object
                    relation_name = computed.get("relation")
                    if relation_name:
                        # Recursively check the relation
                        return self._compute_permission(
                            subject,
                            relation_name,
                            obj,
                            visited.copy(),
                            depth + 1,
                            context,
                            tenant_id,
                        )
                return False

            # Unknown dict format - deny
            logger.warning(f"Unknown permission dict format: {permission}")
            return False

        # Get namespace config for object type
        namespace = self.get_namespace(obj.entity_type)
        if not namespace:
            # No namespace config - check for direct relation only
            logger.debug(
                f"  [depth={depth}] ⚠️ No namespace for {obj.entity_type}, checking direct relation"
            )
            return self._has_direct_relation(subject, permission, obj, context, tenant_id)

        logger.debug(f"  [depth={depth}] ✅ Found namespace for {obj.entity_type}")
        logger.debug(
            f"  [depth={depth}] 📊 ALL Relations in namespace: {list(namespace.config.get('relations', {}).keys())}"
        )
        logger.debug(
            f"  [depth={depth}] 📊 ALL Permissions in namespace: {list(namespace.config.get('permissions', {}).keys())}"
        )

        # P0-1: Use explicit permission-to-userset mapping (Zanzibar-style)
        # Check if permission is defined via "permissions" config (new way)
        if namespace.has_permission(permission):
            # Permission defined explicitly - check all usersets that grant it
            usersets = namespace.get_permission_usersets(permission)
            logger.debug(
                f"  [depth={depth}] 🔑 Permission '{permission}' defined in namespace '{obj.entity_type}'"
            )
            logger.debug(
                f"  [depth={depth}] 📋 Permission '{permission}' expands to usersets: {usersets}"
            )
            logger.debug(
                f"  [depth={depth}] 🧪 Checking {len(usersets)} usersets for {subject} on {obj}"
            )
            logger.debug(
                f"  [depth={depth}] 📊 NAMESPACE CONFIG for '{obj.entity_type}': relations={list(namespace.config.get('relations', {}).keys())}"
            )

            for i, userset in enumerate(usersets):
                logger.debug(
                    f"  [depth={depth}] 🔍 [{i + 1}/{len(usersets)}] Checking userset '{userset}'..."
                )
                result = self._compute_permission(
                    subject, userset, obj, visited.copy(), depth + 1, context, tenant_id
                )
                if result:
                    logger.debug(
                        f"  [depth={depth}] ✅ [{i + 1}/{len(usersets)}] GRANTED via userset '{userset}'"
                    )
                    return True
                else:
                    logger.debug(
                        f"  [depth={depth}] ❌ [{i + 1}/{len(usersets)}] DENIED for userset '{userset}'"
                    )

            logger.debug(
                f"  [depth={depth}] 🚫 ALL {len(usersets)} usersets DENIED - permission DENIED"
            )
            return False

        # Fallback: Check if permission is defined as a relation (legacy)
        rel_config = namespace.get_relation_config(permission)
        logger.debug(
            f"  [depth={depth}] 🔍 Checking relation config for '{permission}': {rel_config}"
        )
        if not rel_config:
            # Permission not defined in namespace - check for direct relation
            logger.debug(
                f"  [depth={depth}] ⚠️ No relation config for '{permission}', checking direct relation"
            )
            return self._has_direct_relation(subject, permission, obj, context, tenant_id)

        # Handle union (OR of multiple relations)
        if namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            logger.debug(
                f"  [depth={depth}] 🔗 Relation '{permission}' is UNION of: {union_relations}"
            )
            logger.debug(
                f"  [depth={depth}] 📋 Relation config for '{permission}': {namespace.get_relation_config(permission)}"
            )
            for i, rel in enumerate(union_relations):
                logger.debug(
                    f"  [depth={depth}] 🔍 [{i + 1}/{len(union_relations)}] Checking union relation '{rel}'..."
                )
                result = self._compute_permission(
                    subject, rel, obj, visited.copy(), depth + 1, context, tenant_id
                )
                if result:
                    logger.debug(
                        f"  [depth={depth}] ✅ [{i + 1}/{len(union_relations)}] GRANTED via union relation '{rel}'"
                    )
                    return True
                else:
                    logger.debug(
                        f"  [depth={depth}] ❌ [{i + 1}/{len(union_relations)}] DENIED for union relation '{rel}'"
                    )
            logger.debug(f"  [depth={depth}] 🚫 ALL union relations DENIED")
            return False

        # Handle intersection (AND of multiple relations)
        if namespace.has_intersection(permission):
            intersection_relations = namespace.get_intersection_relations(permission)
            # ALL relations must be true
            for rel in intersection_relations:
                if not self._compute_permission(
                    subject, rel, obj, visited.copy(), depth + 1, context, tenant_id
                ):
                    return False  # If any relation is False, whole intersection is False
            return True  # All relations were True

        # Handle exclusion (NOT relation - this implements DENY semantics)
        if namespace.has_exclusion(permission):
            excluded_rel = namespace.get_exclusion_relation(permission)
            if excluded_rel:
                # Must NOT have the excluded relation
                return not self._compute_permission(
                    subject, excluded_rel, obj, visited.copy(), depth + 1, context, tenant_id
                )
            return False

        # Handle tupleToUserset (indirect relation via another object)
        if namespace.has_tuple_to_userset(permission):
            ttu = namespace.get_tuple_to_userset(permission)
            logger.debug(f"  [depth={depth}] 🔄 tupleToUserset for '{permission}': {ttu}")
            if ttu:
                tupleset_relation = ttu["tupleset"]
                computed_userset = ttu["computedUserset"]

                # Pattern 1 (parent-style): Find objects where (obj, tupleset_relation, ?)
                # Example: (child_file, "parent", parent_dir) -> check subject has computed_userset on parent_dir
                related_objects = self._find_related_objects(obj, tupleset_relation)
                logger.debug(
                    f"  [depth={depth}] 🔍 Pattern 1 (parent): Found {len(related_objects)} related objects via tupleset '{tupleset_relation}': {[(o.entity_type, o.entity_id) for o in related_objects]}"
                )

                # Check if subject has computed_userset on any related object
                for i, related_obj in enumerate(related_objects):
                    logger.debug(
                        f"  [depth={depth}] 🔍 [{i + 1}/{len(related_objects)}] Checking if {subject} has '{computed_userset}' on {related_obj}..."
                    )
                    if self._compute_permission(
                        subject,
                        computed_userset,
                        related_obj,
                        visited.copy(),
                        depth + 1,
                        context,
                        tenant_id,
                    ):
                        logger.debug(
                            f"  [depth={depth}] ✅ GRANTED via tupleToUserset (parent pattern) through {related_obj}"
                        )
                        return True
                    else:
                        logger.debug(f"  [depth={depth}] ❌ DENIED for {related_obj}")

                # Pattern 2 (group-style): Find subjects where (?, tupleset_relation, obj)
                # Example: (group, "direct_viewer", file) -> check subject has computed_userset on group
                related_subjects = self._find_subjects_with_relation(obj, tupleset_relation)
                logger.debug(
                    f"  [depth={depth}] 🔍 Pattern 2 (group): Found {len(related_subjects)} subjects with '{tupleset_relation}' on {obj}: {[(s.entity_type, s.entity_id) for s in related_subjects]}"
                )

                # Check if subject has computed_userset on any related subject (typically group membership)
                for i, related_subj in enumerate(related_subjects):
                    logger.debug(
                        f"  [depth={depth}] 🔍 [{i + 1}/{len(related_subjects)}] Checking if {subject} has '{computed_userset}' on {related_subj}..."
                    )
                    if self._compute_permission(
                        subject,
                        computed_userset,
                        related_subj,
                        visited.copy(),
                        depth + 1,
                        context,
                        tenant_id,
                    ):
                        logger.debug(
                            f"  [depth={depth}] ✅ GRANTED via tupleToUserset (group pattern) through {related_subj}"
                        )
                        return True
                    else:
                        logger.debug(f"  [depth={depth}] ❌ DENIED for {related_subj}")

                logger.debug(
                    f"  [depth={depth}] 🚫 tupleToUserset: No related objects/subjects granted permission"
                )

            return False

        # Direct relation check (with optional context evaluation)
        return self._has_direct_relation(subject, permission, obj, context, tenant_id)

    def _has_direct_relation(
        self,
        subject: Entity,
        relation: str,
        obj: Entity,
        context: dict[str, Any] | None = None,
        tenant_id: str | None = None,
    ) -> bool:
        """Check if subject has direct relation to object.

        Checks both:
        1. Direct concrete subject tuple: (subject, relation, object)
        2. Userset-as-subject tuple: (subject_set#set_relation, relation, object)
           where subject has set_relation on subject_set

        If context is provided, evaluates tuple conditions (ABAC).

        Args:
            subject: Subject entity
            relation: Relation type
            obj: Object entity
            context: Optional ABAC context for condition evaluation
            tenant_id: Optional tenant ID for multi-tenant isolation

        Returns:
            True if direct relation exists and conditions are satisfied
        """
        logger.debug(
            f"    💾 Checking DATABASE for direct tuple: subject={subject}, relation={relation}, object={obj}, tenant_id={tenant_id}"
        )
        result = self._find_direct_relation_tuple(subject, relation, obj, context, tenant_id)
        if result is not None:
            logger.debug(f"    ✅ FOUND tuple: {result.get('tuple_id', 'unknown')}")
            return True
        else:
            logger.debug("    ❌ NO tuple found in database")
            return False

    def _find_direct_relation_tuple(
        self,
        subject: Entity,
        relation: str,
        obj: Entity,
        context: dict[str, Any] | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find direct relation tuple with full details.

        Returns tuple information for explain API.

        Args:
            subject: Subject entity
            relation: Relation type
            obj: Object entity
            context: Optional ABAC context for condition evaluation
            tenant_id: Optional tenant ID for multi-tenant isolation

        Returns:
            Tuple dict with id, subject, relation, object info, or None if not found
        """
        logger.debug(
            f"    _find_direct_relation_tuple: subject={subject}, relation={relation}, obj={obj}, tenant_id={tenant_id}"
        )

        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # BUGFIX: Use >= instead of > for exact expiration boundary
            # Check 1: Direct concrete subject (subject_relation IS NULL)
            # ABAC: Fetch conditions column to evaluate context
            # P0-4: Filter by tenant_id for multi-tenant isolation
            if tenant_id is None:
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        SELECT tuple_id, subject_type, subject_id, subject_relation,
                               relation, object_type, object_id, conditions, expires_at
                        FROM rebac_tuples
                        WHERE subject_type = ? AND subject_id = ?
                          AND subject_relation IS NULL
                          AND relation = ?
                          AND object_type = ? AND object_id = ?
                          AND (expires_at IS NULL OR expires_at >= ?)
                          AND tenant_id IS NULL
                        LIMIT 1
                        """
                    ),
                    (
                        subject.entity_type,
                        subject.entity_id,
                        relation,
                        obj.entity_type,
                        obj.entity_id,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            else:
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        SELECT tuple_id, subject_type, subject_id, subject_relation,
                               relation, object_type, object_id, conditions, expires_at
                        FROM rebac_tuples
                        WHERE subject_type = ? AND subject_id = ?
                          AND subject_relation IS NULL
                          AND relation = ?
                          AND object_type = ? AND object_id = ?
                          AND (expires_at IS NULL OR expires_at >= ?)
                          AND tenant_id = ?
                        LIMIT 1
                        """
                    ),
                    (
                        subject.entity_type,
                        subject.entity_id,
                        relation,
                        obj.entity_type,
                        obj.entity_id,
                        datetime.now(UTC).isoformat(),
                        tenant_id,
                    ),
                )

            row = cursor.fetchone()
            if row:
                logger.debug(f"    ✅ Found direct tuple for {subject} -> {relation} -> {obj}")
                # Tuple exists - now check conditions if context provided
                conditions_json = row["conditions"]

                if conditions_json:
                    try:
                        conditions = (
                            json.loads(conditions_json)
                            if isinstance(conditions_json, str)
                            else conditions_json
                        )
                        # Evaluate ABAC conditions
                        if not self._evaluate_conditions(conditions, context):
                            logger.debug(
                                f"Tuple exists but conditions not satisfied for {subject} -> {relation} -> {obj}"
                            )
                            return None  # Tuple exists but conditions failed
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.warning(f"Failed to parse conditions JSON: {e}")
                        # On parse error, treat as no conditions (allow)

                # Return tuple details
                return dict(row)
            else:
                logger.debug(f"    ❌ No direct tuple found for {subject} -> {relation} -> {obj}")

            # Check 2: Wildcard/public access
            # Check if wildcard subject (*:*) has the relation (public access)
            # Avoid infinite recursion by only checking wildcard if subject is not already wildcard
            # P0-4: Filter by tenant_id for multi-tenant isolation
            if (subject.entity_type, subject.entity_id) != WILDCARD_SUBJECT:
                wildcard_entity = Entity(WILDCARD_SUBJECT[0], WILDCARD_SUBJECT[1])
                if tenant_id is None:
                    cursor.execute(
                        self._fix_sql_placeholders(
                            """
                            SELECT tuple_id, subject_type, subject_id, subject_relation,
                                   relation, object_type, object_id, conditions, expires_at
                            FROM rebac_tuples
                            WHERE subject_type = ? AND subject_id = ?
                              AND subject_relation IS NULL
                              AND relation = ?
                              AND object_type = ? AND object_id = ?
                              AND (expires_at IS NULL OR expires_at >= ?)
                              AND tenant_id IS NULL
                            LIMIT 1
                            """
                        ),
                        (
                            wildcard_entity.entity_type,
                            wildcard_entity.entity_id,
                            relation,
                            obj.entity_type,
                            obj.entity_id,
                            datetime.now(UTC).isoformat(),
                        ),
                    )
                else:
                    cursor.execute(
                        self._fix_sql_placeholders(
                            """
                            SELECT tuple_id, subject_type, subject_id, subject_relation,
                                   relation, object_type, object_id, conditions, expires_at
                            FROM rebac_tuples
                            WHERE subject_type = ? AND subject_id = ?
                              AND subject_relation IS NULL
                              AND relation = ?
                              AND object_type = ? AND object_id = ?
                              AND (expires_at IS NULL OR expires_at >= ?)
                              AND tenant_id = ?
                            LIMIT 1
                            """
                        ),
                        (
                            wildcard_entity.entity_type,
                            wildcard_entity.entity_id,
                            relation,
                            obj.entity_type,
                            obj.entity_id,
                            datetime.now(UTC).isoformat(),
                            tenant_id,
                        ),
                    )
                row = cursor.fetchone()
                if row:
                    return dict(row)

            # Check 3: Userset-as-subject grants
            # Find tuples like (group:eng#member, editor-of, file:readme)
            # where subject has 'member' relation to 'group:eng'
            subject_sets = self._find_subject_sets(relation, obj, tenant_id)
            for set_type, set_id, set_relation in subject_sets:
                # Recursively check if subject has set_relation on the set entity
                if self._has_direct_relation(
                    subject, set_relation, Entity(set_type, set_id), context, tenant_id
                ):
                    # Return the userset tuple that granted access
                    cursor.execute(
                        self._fix_sql_placeholders(
                            """
                            SELECT tuple_id, subject_type, subject_id, subject_relation,
                                   relation, object_type, object_id, conditions, expires_at
                            FROM rebac_tuples
                            WHERE subject_type = ? AND subject_id = ?
                              AND subject_relation = ?
                              AND relation = ?
                              AND object_type = ? AND object_id = ?
                            LIMIT 1
                            """
                        ),
                        (set_type, set_id, set_relation, relation, obj.entity_type, obj.entity_id),
                    )
                    row = cursor.fetchone()
                    if row:
                        return dict(row)

            return None

    def _find_subject_sets(
        self, relation: str, obj: Entity, tenant_id: str | None = None
    ) -> list[tuple[str, str, str]]:
        """Find all subject sets that have a relation to an object.

        Subject sets are tuples with subject_relation set, like:
        (group:eng#member, editor-of, file:readme)

        This means "all members of group:eng have editor-of relation to file:readme"

        SECURITY FIX (P0): Enforces tenant_id filtering to prevent cross-tenant leaks.
        When tenant_id is None, queries for NULL tenant_id (single-tenant mode).

        Args:
            relation: Relation type
            obj: Object entity
            tenant_id: Optional tenant ID for multi-tenant isolation (None for single-tenant)

        Returns:
            List of (subject_type, subject_id, subject_relation) tuples
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # P0 SECURITY FIX: ALWAYS filter by tenant_id to prevent cross-tenant group membership leaks
            # When tenant_id is None, match NULL tenant_id (single-tenant mode)
            if tenant_id is None:
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        SELECT subject_type, subject_id, subject_relation
                        FROM rebac_tuples
                        WHERE tenant_id IS NULL
                          AND relation = ?
                          AND object_type = ? AND object_id = ?
                          AND subject_relation IS NOT NULL
                          AND (expires_at IS NULL OR expires_at >= ?)
                        """
                    ),
                    (
                        relation,
                        obj.entity_type,
                        obj.entity_id,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            else:
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        SELECT subject_type, subject_id, subject_relation
                        FROM rebac_tuples
                        WHERE tenant_id = ?
                          AND relation = ?
                          AND object_type = ? AND object_id = ?
                          AND subject_relation IS NOT NULL
                          AND (expires_at IS NULL OR expires_at >= ?)
                        """
                    ),
                    (
                        tenant_id,
                        relation,
                        obj.entity_type,
                        obj.entity_id,
                        datetime.now(UTC).isoformat(),
                    ),
                )

            results = []
            for row in cursor.fetchall():
                results.append((row["subject_type"], row["subject_id"], row["subject_relation"]))
            return results

    def _find_related_objects(self, obj: Entity, relation: str) -> list[Entity]:
        """Find all objects related to obj via relation.

        For tupleToUserset traversal, finds objects where: (obj, relation, object)
        Example: Finding parent of file X means finding tuples where:
          - subject = file X
          - relation = "parent"
          - object = parent directory

        Args:
            obj: Object entity (the subject of the tuple we're looking for)
            relation: Relation type (e.g., "parent")

        Returns:
            List of related object entities (the objects from matching tuples)
        """
        logger.debug(
            f"      🔎 _find_related_objects: Looking for tuples where subject={obj}, relation='{relation}'"
        )

        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # FIXED: Query for tuples where obj is the SUBJECT (not object)
            # This correctly handles parent relations: (child, "parent", parent)
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT object_type, object_id
                    FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND relation = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (
                    obj.entity_type,
                    obj.entity_id,
                    relation,
                    datetime.now(UTC).isoformat(),
                ),
            )

            results = []
            for row in cursor.fetchall():
                entity = Entity(row["object_type"], row["object_id"])
                results.append(entity)
                logger.debug(f"      ✅ Found related object: {entity}")

            if not results:
                logger.debug(f"      ❌ No related objects found for ({obj}, '{relation}', ?)")
            else:
                logger.debug(f"      📊 Total related objects found: {len(results)}")

            return results

    def _find_subjects_with_relation(self, obj: Entity, relation: str) -> list[Entity]:
        """Find all subjects that have a relation to obj.

        For group-style tupleToUserset traversal, finds subjects where: (subject, relation, obj)
        Example: Finding groups with direct_viewer on file X means finding tuples where:
          - subject = any (typically a group)
          - relation = "direct_viewer"
          - object = file X

        This is the reverse of _find_related_objects and is used for group permission
        inheritance patterns like: group_viewer -> find groups with direct_viewer -> check member.

        Args:
            obj: Object entity (the object in the tuple)
            relation: Relation type (e.g., "direct_viewer")

        Returns:
            List of subject entities (the subjects from matching tuples)
        """
        logger.debug(
            f"      🔎 _find_subjects_with_relation: Looking for tuples where (?, '{relation}', {obj})"
        )

        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # Query for tuples where obj is the OBJECT
            # This handles group relations: (group, "direct_viewer", file)
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT subject_type, subject_id
                    FROM rebac_tuples
                    WHERE object_type = ? AND object_id = ?
                      AND relation = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (
                    obj.entity_type,
                    obj.entity_id,
                    relation,
                    datetime.now(UTC).isoformat(),
                ),
            )

            results = []
            for row in cursor.fetchall():
                entity = Entity(row["subject_type"], row["subject_id"])
                results.append(entity)
                logger.debug(f"      ✅ Found subject with relation: {entity}")

            if not results:
                logger.debug(f"      ❌ No subjects found for (?, '{relation}', {obj})")
            else:
                logger.debug(f"      📊 Total subjects found: {len(results)}")

            return results

    def _evaluate_conditions(
        self, conditions: dict[str, Any] | None, context: dict[str, Any] | None
    ) -> bool:
        """Evaluate ABAC conditions against runtime context.

        Supports time windows, IP allowlists, device types, and custom attributes.

        Args:
            conditions: Conditions stored in tuple (JSON dict)
            context: Runtime context provided by caller

        Returns:
            True if conditions are satisfied (or no conditions exist)

        Examples:
            >>> conditions = {
            ...     "time_window": {"start": "09:00", "end": "17:00"},
            ...     "allowed_ips": ["10.0.0.0/8", "192.168.0.0/16"]
            ... }
            >>> context = {"time": "14:30", "ip": "10.0.1.5"}
            >>> self._evaluate_conditions(conditions, context)
            True

            >>> context = {"time": "20:00", "ip": "10.0.1.5"}
            >>> self._evaluate_conditions(conditions, context)
            False  # Outside time window
        """
        if not conditions:
            return True  # No conditions = always allowed

        if not context:
            logger.warning("ABAC conditions exist but no context provided - DENYING access")
            return False  # Conditions exist but no context = deny

        # Time window check
        if "time_window" in conditions:
            current_time = context.get("time")
            if not current_time:
                logger.debug("Time window condition but no 'time' in context - DENY")
                return False

            start = conditions["time_window"].get("start")
            end = conditions["time_window"].get("end")
            if start and end:
                # Support both ISO8601 and simple HH:MM format
                # ISO8601: "2025-10-25T14:30:00-07:00"
                # Simple: "14:30"
                # For ISO8601, extract time portion; for simple, use as-is
                try:
                    if "T" in current_time:  # ISO8601
                        # Extract time portion: "14:30:00-07:00"
                        time_part = current_time.split("T")[1]
                        # Extract just HH:MM:SS or HH:MM
                        current_time_cmp = time_part.split("-")[0].split("+")[0][:8]
                    else:  # Simple HH:MM
                        current_time_cmp = current_time

                    # Normalize start/end too
                    if "T" in start:
                        start_cmp = start.split("T")[1].split("-")[0].split("+")[0][:8]
                    else:
                        start_cmp = start

                    if "T" in end:
                        end_cmp = end.split("T")[1].split("-")[0].split("+")[0][:8]
                    else:
                        end_cmp = end

                    # String comparison works for HH:MM:SS format
                    if not (start_cmp <= current_time_cmp <= end_cmp):
                        logger.debug(
                            f"Time {current_time_cmp} outside window [{start_cmp}, {end_cmp}] - DENY"
                        )
                        return False
                except (ValueError, IndexError) as e:
                    logger.warning(f"Failed to parse time format: {e} - DENY")
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
                        logger.warning(f"Invalid CIDR in allowlist: {cidr}")
                        continue

                if not allowed:
                    logger.debug(f"IP {current_ip} not in allowlist - DENY")
                    return False
            except ImportError:
                logger.error("ipaddress module not available - cannot evaluate IP conditions")
                return False

        # Device type check
        if "allowed_devices" in conditions:
            current_device = context.get("device")
            if current_device not in conditions["allowed_devices"]:
                logger.debug(
                    f"Device {current_device} not in allowed list {conditions['allowed_devices']} - DENY"
                )
                return False

        # Custom attribute checks
        if "attributes" in conditions:
            for key, expected_value in conditions["attributes"].items():
                actual_value = context.get(key)
                if actual_value != expected_value:
                    logger.debug(
                        f"Attribute {key}: expected {expected_value}, got {actual_value} - DENY"
                    )
                    return False

        # All conditions satisfied
        return True

    def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
    ) -> list[tuple[str, str]]:
        """Find all subjects with a given permission on an object.

        Args:
            permission: Permission to check
            object: (object_type, object_id) tuple

        Returns:
            List of (subject_type, subject_id) tuples

        Example:
            >>> manager.rebac_expand(
            ...     permission="read",
            ...     object=("file", "file_id")
            ... )
            [("agent", "alice_id"), ("agent", "bob_id")]
        """
        object_entity = Entity(object[0], object[1])
        subjects: set[tuple[str, str]] = set()

        # Get namespace config
        namespace = self.get_namespace(object_entity.entity_type)
        if not namespace:
            # No namespace - return direct relations only
            return self._get_direct_subjects(permission, object_entity)

        # Recursively expand permission via namespace config
        self._expand_permission(
            permission, object_entity, namespace, subjects, visited=set(), depth=0
        )

        return list(subjects)

    def _expand_permission(
        self,
        permission: str,
        obj: Entity,
        namespace: NamespaceConfig,
        subjects: set[tuple[str, str]],
        visited: set[tuple[str, str, str]],
        depth: int,
    ) -> None:
        """Recursively expand permission to find all subjects.

        Args:
            permission: Permission to expand
            obj: Object entity
            namespace: Namespace configuration
            subjects: Set to accumulate subjects
            visited: Set of visited (permission, object_type, object_id) to detect cycles
            depth: Current traversal depth
        """
        # Check depth limit
        if depth > self.max_depth:
            return

        # Check for cycles
        visit_key = (permission, obj.entity_type, obj.entity_id)
        if visit_key in visited:
            return
        visited.add(visit_key)

        # Get relation config
        rel_config = namespace.get_relation_config(permission)
        if not rel_config:
            # Permission not defined in namespace - check for direct relations
            direct_subjects = self._get_direct_subjects(permission, obj)
            for subj in direct_subjects:
                subjects.add(subj)
            return

        # Handle union
        if namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            for rel in union_relations:
                self._expand_permission(rel, obj, namespace, subjects, visited.copy(), depth + 1)
            return

        # Handle intersection (find subjects that have ALL relations)
        if namespace.has_intersection(permission):
            intersection_relations = namespace.get_intersection_relations(permission)
            if not intersection_relations:
                return

            # Get subjects for each relation
            relation_subjects = []
            for rel in intersection_relations:
                rel_subjects: set[tuple[str, str]] = set()
                self._expand_permission(
                    rel, obj, namespace, rel_subjects, visited.copy(), depth + 1
                )
                relation_subjects.append(rel_subjects)

            # Find intersection (subjects that appear in ALL sets)
            if relation_subjects:
                common_subjects = set.intersection(*relation_subjects)
                for subj in common_subjects:
                    subjects.add(subj)
            return

        # Handle exclusion (find subjects that DON'T have the excluded relation)
        if namespace.has_exclusion(permission):
            # Note: Expand for exclusion is complex and potentially expensive
            # We would need to find all possible subjects, then filter out those with the excluded relation
            # For now, we skip expand for exclusion relations
            # TODO: Implement if needed for production use
            logger.warning(
                f"Expand API does not support exclusion relations yet: {permission} on {obj}"
            )
            return

        # Handle tupleToUserset
        if namespace.has_tuple_to_userset(permission):
            ttu = namespace.get_tuple_to_userset(permission)
            if ttu:
                tupleset_relation = ttu["tupleset"]
                computed_userset = ttu["computedUserset"]

                # Find all related objects
                related_objects = self._find_related_objects(obj, tupleset_relation)

                # Expand permission on related objects
                for related_obj in related_objects:
                    related_ns = self.get_namespace(related_obj.entity_type)
                    if related_ns:
                        self._expand_permission(
                            computed_userset,
                            related_obj,
                            related_ns,
                            subjects,
                            visited.copy(),
                            depth + 1,
                        )
            return

        # Direct relation - add all subjects
        direct_subjects = self._get_direct_subjects(permission, obj)
        for subj in direct_subjects:
            subjects.add(subj)

    def _get_direct_subjects(self, relation: str, obj: Entity) -> list[tuple[str, str]]:
        """Get all subjects with direct relation to object.

        Args:
            relation: Relation type
            obj: Object entity

        Returns:
            List of (subject_type, subject_id) tuples
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # BUGFIX: Use >= instead of > for exact expiration boundary
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT subject_type, subject_id
                    FROM rebac_tuples
                    WHERE relation = ?
                      AND object_type = ? AND object_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (
                    relation,
                    obj.entity_type,
                    obj.entity_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

            results = []
            for row in cursor.fetchall():
                results.append((row["subject_type"], row["subject_id"]))
            return results

    def _get_cached_check(
        self, subject: Entity, permission: str, obj: Entity, tenant_id: str | None = None
    ) -> bool | None:
        """Get cached permission check result.

        Checks L1 (in-memory) cache first, then L2 (database) cache.

        Args:
            subject: Subject entity
            permission: Permission
            obj: Object entity
            tenant_id: Optional tenant ID

        Returns:
            Cached result or None if not cached or expired
        """
        # Check L1 cache first (if enabled)
        if self._l1_cache:
            l1_result = self._l1_cache.get(
                subject.entity_type,
                subject.entity_id,
                permission,
                obj.entity_type,
                obj.entity_id,
                tenant_id,
            )
            if l1_result is not None:
                logger.debug("✅ L1 CACHE HIT")
                return l1_result

        # L1 miss - check L2 (database) cache
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT result, expires_at
                    FROM rebac_check_cache
                    WHERE subject_type = ? AND subject_id = ?
                      AND permission = ?
                      AND object_type = ? AND object_id = ?
                      AND expires_at > ?
                    """
                ),
                (
                    subject.entity_type,
                    subject.entity_id,
                    permission,
                    obj.entity_type,
                    obj.entity_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

            row = cursor.fetchone()
            if row:
                result = bool(row["result"])
                logger.debug("✅ L2 CACHE HIT (populating L1)")

                # Populate L1 cache from L2
                if self._l1_cache:
                    self._l1_cache.set(
                        subject.entity_type,
                        subject.entity_id,
                        permission,
                        obj.entity_type,
                        obj.entity_id,
                        result,
                        tenant_id,
                    )

                return result
            return None

    def _cache_check_result(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        result: bool,
        tenant_id: str | None = None,
        conn: Any | None = None,
    ) -> None:
        """Cache permission check result in both L1 and L2 caches.

        Args:
            subject: Subject entity
            permission: Permission
            obj: Object entity
            result: Check result
            tenant_id: Optional tenant ID for multi-tenant isolation
        """
        # Cache in L1 first (faster)
        if self._l1_cache:
            self._l1_cache.set(
                subject.entity_type,
                subject.entity_id,
                permission,
                obj.entity_type,
                obj.entity_id,
                result,
                tenant_id,
            )

        # Then cache in L2 (database)
        cache_id = str(uuid.uuid4())
        computed_at = datetime.now(UTC)
        expires_at = computed_at + timedelta(seconds=self.cache_ttl_seconds)

        # Use "default" tenant if not specified (for backward compatibility)
        effective_tenant_id = tenant_id if tenant_id is not None else "default"

        # Use provided connection or create new one (avoids SQLite lock contention)
        should_close = conn is None
        if conn is None:
            conn = self._get_connection()
        try:
            cursor = self._create_cursor(conn)

            # Delete existing cache entry if present
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    DELETE FROM rebac_check_cache
                    WHERE tenant_id = ?
                      AND subject_type = ? AND subject_id = ?
                      AND permission = ?
                      AND object_type = ? AND object_id = ?
                    """
                ),
                (
                    effective_tenant_id,
                    subject.entity_type,
                    subject.entity_id,
                    permission,
                    obj.entity_type,
                    obj.entity_id,
                ),
            )

            # Insert new cache entry
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    INSERT INTO rebac_check_cache (
                        cache_id, tenant_id, subject_type, subject_id, permission,
                        object_type, object_id, result, computed_at, expires_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    cache_id,
                    effective_tenant_id,
                    subject.entity_type,
                    subject.entity_id,
                    permission,
                    obj.entity_type,
                    obj.entity_id,
                    int(result),  # Convert boolean to int for PostgreSQL compatibility
                    computed_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )

            conn.commit()
        finally:
            if should_close:
                conn.close()

    def _invalidate_cache_for_tuple(
        self,
        subject: Entity,
        relation: str,
        obj: Entity,
        tenant_id: str | None = None,
        subject_relation: str | None = None,
        expires_at: datetime | None = None,
        conn: Any | None = None,
    ) -> None:
        """Invalidate and optionally recompute cache entries affected by tuple change.

        When a tuple is added or removed, we need to invalidate cache entries that
        might be affected. This uses PRECISE invalidation to minimize cache churn:

        1. Direct: Invalidate (subject, *, object) - permissions on this specific pair
        2. Transitive (if subject has subject_relation): Invalidate members of this group
        3. Transitive (for object): Invalidate derived permissions on related objects

        OPTIMIZATION: For simple direct relations, we RECOMPUTE and UPDATE the cache
        instead of just invalidating. This means the next read is instant (<1ms) instead
        of requiring expensive graph traversal (50-500ms).

        Args:
            subject: Subject entity
            relation: Relation type (used for precise invalidation)
            obj: Object entity
            tenant_id: Optional tenant ID for tenant-scoped invalidation
            subject_relation: Optional subject relation for userset-as-subject
            expires_at: Optional expiration time (disables eager recomputation)
        """
        # Use "default" tenant if not specified
        effective_tenant_id = tenant_id if tenant_id is not None else "default"

        # Track write for adaptive TTL (Phase 4)
        if self._l1_cache:
            self._l1_cache.track_write(obj.entity_id)

        # Use provided connection or create new one (avoids SQLite lock contention)
        should_close = conn is None
        if conn is None:
            conn = self._get_connection()
        try:
            cursor = self._create_cursor(conn)

            # 1. DIRECT: For simple direct relations, try to eagerly recompute permissions
            #    instead of just invalidating. This avoids cache miss on next read.
            #
            # Only do eager recomputation for:
            # - Direct relations (not group-based)
            # - Not hierarchy relations (parent/member)
            # - Single subject-object pair (not wildcards)
            # - NOT expiring tuples (cache would become stale when tuple expires)
            should_eager_recompute = (
                expires_at is None  # Not an expiring tuple
                and subject_relation is None  # Not a userset-as-subject
                and relation not in ("member-of", "member", "parent")  # Not hierarchy
                and subject.entity_type != "*"  # Not wildcard
                and subject.entity_id != "*"
            )

            if should_eager_recompute:
                # Get the namespace to find which permissions this relation grants
                namespace = self.get_namespace(obj.entity_type)
                if namespace and namespace.config and "relations" in namespace.config:
                    # Find permissions that this relation affects
                    affected_permissions = []
                    relations = namespace.config.get("relations", {})
                    for perm, rel_spec in relations.items():
                        # Check if this permission uses our relation
                        if (
                            isinstance(rel_spec, dict)
                            and "union" in rel_spec
                            and relation in rel_spec["union"]
                        ):
                            affected_permissions.append(perm)

                    # Eagerly recompute and update cache for these permissions
                    for permission in affected_permissions[:5]:  # Limit to 5 most common
                        try:
                            # Recompute the permission
                            result = self._compute_permission(
                                subject,
                                permission,
                                obj,
                                visited=set(),
                                depth=0,
                                tenant_id=tenant_id,
                            )
                            # Update cache immediately (not invalidate)
                            self._cache_check_result(
                                subject, permission, obj, result, tenant_id, conn=conn
                            )
                            logger.debug(
                                f"Eager cache update: ({subject}, {permission}, {obj}) = {result}"
                            )
                        except Exception as e:
                            # If recomputation fails, fall back to invalidation
                            logger.debug(
                                f"Eager recomputation failed, falling back to invalidation: {e}"
                            )
                            break

            # If we didn't do eager recomputation, invalidate as usual
            if not should_eager_recompute:
                # L1 cache invalidation
                if self._l1_cache:
                    self._l1_cache.invalidate_subject_object_pair(
                        subject.entity_type,
                        subject.entity_id,
                        obj.entity_type,
                        obj.entity_id,
                        tenant_id,
                    )

                # L2 cache invalidation
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        DELETE FROM rebac_check_cache
                        WHERE tenant_id = ?
                          AND subject_type = ? AND subject_id = ?
                          AND object_type = ? AND object_id = ?
                        """
                    ),
                    (
                        effective_tenant_id,
                        subject.entity_type,
                        subject.entity_id,
                        obj.entity_type,
                        obj.entity_id,
                    ),
                )

            # 2. TRANSITIVE (Groups): If subject is a group/set (has subject_relation),
            #    invalidate cache for potential members of this group accessing the object
            #    Example: If we add "group:eng#member can edit file:doc", then cache entries
            #    for (alice, *, file:doc) need invalidation IF alice is in group:eng
            #
            # Note: We could query for actual members, but that's expensive. Instead,
            # we invalidate (*, *, object) only when the tuple involves a subject set.
            # This is still more precise than invalidating ALL subject entries.
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT subject_relation FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND relation = ?
                      AND object_type = ? AND object_id = ?
                    LIMIT 1
                    """
                ),
                (subject.entity_type, subject.entity_id, relation, obj.entity_type, obj.entity_id),
            )
            row = cursor.fetchone()
            has_subject_relation = row and row["subject_relation"]

            if has_subject_relation:
                # This is a group-based permission - invalidate all cache for this object
                # because we don't know who's in the group without expensive queries

                # L1 cache invalidation
                if self._l1_cache:
                    self._l1_cache.invalidate_object(obj.entity_type, obj.entity_id, tenant_id)

                # L2 cache invalidation
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        DELETE FROM rebac_check_cache
                        WHERE tenant_id = ?
                          AND object_type = ? AND object_id = ?
                        """
                    ),
                    (effective_tenant_id, obj.entity_type, obj.entity_id),
                )

            # 3. TRANSITIVE (Hierarchy): If this is a group membership change (e.g., adding alice to group:eng),
            #    invalidate cache entries where the subject might gain permissions via this group
            #    Example: If we add "alice member-of group:eng", and "group:eng#member can edit file:doc",
            #    then (alice, edit, file:doc) cache needs invalidation
            if relation in ("member-of", "member", "parent"):
                # Subject joined a group or hierarchy - invalidate subject's permissions

                # L1 cache invalidation
                if self._l1_cache:
                    self._l1_cache.invalidate_subject(
                        subject.entity_type, subject.entity_id, tenant_id
                    )

                # L2 cache invalidation
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        DELETE FROM rebac_check_cache
                        WHERE tenant_id = ?
                          AND subject_type = ? AND subject_id = ?
                        """
                    ),
                    (effective_tenant_id, subject.entity_type, subject.entity_id),
                )

            # 4. PARENT PERMISSION CHANGE: If this tuple grants/changes permissions on a parent path,
            #    invalidate cache for ALL child paths that inherit via parent_owner/parent_editor/parent_viewer
            #    Example: If we add "admin direct_owner file:/workspace", then cache entries for
            #    file:/workspace/project/* need invalidation because they inherit via parent_owner
            if obj.entity_type == "file" and relation in (
                "direct_owner",
                "direct_editor",
                "direct_viewer",
                "owner",
                "editor",
                "viewer",
                # Cross-tenant sharing relations (PR #647)
                "shared-viewer",
                "shared-editor",
                "shared-owner",
            ):
                # Invalidate all cache entries for paths that are children of this object
                # Match object_id that starts with obj.entity_id/ (children)

                # L1 cache invalidation - invalidate prefix
                if self._l1_cache:
                    self._l1_cache.invalidate_object_prefix(
                        obj.entity_type, obj.entity_id, tenant_id
                    )

                # L2 cache invalidation
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        DELETE FROM rebac_check_cache
                        WHERE tenant_id = ?
                          AND object_type = ?
                          AND (object_id = ? OR object_id LIKE ?)
                        """
                    ),
                    (effective_tenant_id, obj.entity_type, obj.entity_id, obj.entity_id + "/%"),
                )
                logger.debug(
                    f"Invalidated cache for {obj} and all children (parent permission change)"
                )

            # 5. USERSET-AS-SUBJECT: If subject_relation is present (like "group:eng#member"),
            #    this grants access to ALL members of that group. Since we don't know who's in the group
            #    without expensive queries, invalidate ALL cache (aggressive but safe).
            #    Example: "group:project1-editors#member direct_editor file:/workspace" means any member
            #    of project1-editors now has access, so invalidate everything to be safe.
            if subject_relation is not None:
                logger.debug(
                    f"Userset-as-subject detected ({subject}#{subject_relation}), clearing ALL cache for safety"
                )

                # L1 cache invalidation - clear all for this tenant
                if self._l1_cache:
                    self._l1_cache.clear()  # Conservative: clear entire L1 cache

                # L2 cache invalidation
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        DELETE FROM rebac_check_cache
                        WHERE tenant_id = ?
                        """
                    ),
                    (effective_tenant_id,),
                )

            conn.commit()
        finally:
            if should_close:
                conn.close()

    def _invalidate_cache_for_namespace(self, object_type: str) -> None:
        """Invalidate all cache entries for objects of a given type in both L1 and L2.

        When a namespace configuration is updated, all cached permission checks
        for objects of that type may be stale and must be invalidated.

        Args:
            object_type: Type of object whose namespace was updated
        """
        # L1 cache invalidation - clear all (conservative approach)
        if self._l1_cache:
            self._l1_cache.clear()
            logger.info(f"Cleared L1 cache due to namespace '{object_type}' config update")

        # L2 cache invalidation
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # Invalidate all cache entries for this object type
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    DELETE FROM rebac_check_cache
                    WHERE object_type = ?
                    """
                ),
                (object_type,),
            )

            conn.commit()
            logger.debug(
                f"Invalidated all cached checks for namespace '{object_type}' "
                f"due to config update (deleted {cursor.rowcount} cache entries)"
            )

    def _cleanup_expired_tuples_if_needed(self) -> None:
        """Clean up expired tuples if enough time has passed since last cleanup.

        This method throttles cleanup operations to avoid checking on every rebac_check call.
        Only cleans up if more than 1 second has passed since last cleanup.
        """
        now = datetime.now(UTC)

        # Throttle cleanup - only run if more than 1 second since last cleanup
        if self._last_cleanup_time is not None:
            time_since_cleanup = (now - self._last_cleanup_time).total_seconds()
            if time_since_cleanup < 1.0:
                return

        # Update last cleanup time
        self._last_cleanup_time = now

        # Clean up expired tuples (this will also invalidate caches)
        self.cleanup_expired_tuples()

    def cleanup_expired_cache(self) -> int:
        """Remove expired cache entries.

        Returns:
            Number of cache entries removed
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            cursor.execute(
                self._fix_sql_placeholders("DELETE FROM rebac_check_cache WHERE expires_at <= ?"),
                (datetime.now(UTC).isoformat(),),
            )

            conn.commit()
            return int(cursor.rowcount) if cursor.rowcount else 0

    def cleanup_expired_tuples(self) -> int:
        """Remove expired relationship tuples.

        Returns:
            Number of tuples removed
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # Get expired tuples for changelog
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT tuple_id, subject_type, subject_id, subject_relation, relation, object_type, object_id, tenant_id
                    FROM rebac_tuples
                    WHERE expires_at IS NOT NULL AND expires_at <= ?
                    """
                ),
                (datetime.now(UTC).isoformat(),),
            )

            expired_tuples = cursor.fetchall()

            # Delete expired tuples
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    DELETE FROM rebac_tuples
                    WHERE expires_at IS NOT NULL AND expires_at <= ?
                    """
                ),
                (datetime.now(UTC).isoformat(),),
            )

            # Log to changelog and invalidate caches for expired tuples
            for row in expired_tuples:
                # Both SQLite and PostgreSQL now return dict-like rows
                tuple_id = row["tuple_id"]
                subject_type = row["subject_type"]
                subject_id = row["subject_id"]
                subject_relation = row["subject_relation"]
                relation = row["relation"]
                object_type = row["object_type"]
                object_id = row["object_id"]
                tenant_id = row["tenant_id"]

                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        INSERT INTO rebac_changelog (
                            change_type, tuple_id, subject_type, subject_id,
                            relation, object_type, object_id, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (
                        "DELETE",
                        tuple_id,
                        subject_type,
                        subject_id,
                        relation,
                        object_type,
                        object_id,
                        datetime.now(UTC).isoformat(),
                    ),
                )

                # Invalidate cache for this tuple
                # Pass a dummy expires_at to prevent eager recomputation during cleanup
                subject = Entity(subject_type, subject_id)
                obj = Entity(object_type, object_id)
                self._invalidate_cache_for_tuple(
                    subject,
                    relation,
                    obj,
                    tenant_id,
                    subject_relation,
                    expires_at=datetime.now(UTC),
                )

            conn.commit()
            return len(expired_tuples)

    def get_cache_stats(self) -> dict[str, Any]:
        """Get cache statistics for monitoring and debugging.

        Returns comprehensive statistics about both L1 (in-memory) and L2 (database)
        cache performance, including hit rates, sizes, and latency metrics.

        Returns:
            Dictionary with cache statistics:
                - l1_enabled: Whether L1 cache is enabled
                - l1_stats: L1 cache statistics (if enabled)
                - l2_enabled: Whether L2 cache is enabled (always True)
                - l2_size: Number of entries in L2 cache
                - l2_ttl_seconds: L2 cache TTL

        Example:
            >>> stats = manager.get_cache_stats()
            >>> print(f"L1 hit rate: {stats['l1_stats']['hit_rate_percent']}%")
            >>> print(f"L1 avg latency: {stats['l1_stats']['avg_lookup_time_ms']}ms")
            >>> print(f"L2 cache size: {stats['l2_size']} entries")
        """
        stats: dict[str, Any] = {
            "l1_enabled": self._l1_cache is not None,
            "l2_enabled": True,
            "l2_ttl_seconds": self.cache_ttl_seconds,
        }

        # L1 cache stats
        if self._l1_cache:
            stats["l1_stats"] = self._l1_cache.get_stats()
        else:
            stats["l1_stats"] = None

        # L2 cache stats (query database)
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # Count total entries in L2 cache
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT COUNT(*) as count
                    FROM rebac_check_cache
                    WHERE expires_at > ?
                    """
                ),
                (datetime.now(UTC).isoformat(),),
            )
            row = cursor.fetchone()
            stats["l2_size"] = row["count"] if row else 0

        return stats

    def reset_cache_stats(self) -> None:
        """Reset cache statistics counters.

        Useful for benchmarking and monitoring. Resets hit/miss counters
        and timing metrics for L1 cache.

        Note: Only resets metrics, does not clear cache entries.
        """
        if self._l1_cache:
            self._l1_cache.reset_stats()
            logger.info("Cache statistics reset")

    def close(self) -> None:
        """Close database connection.

        Note: With fresh connections, there's nothing to close here.
        Connections are closed immediately after each operation.
        """
        pass
