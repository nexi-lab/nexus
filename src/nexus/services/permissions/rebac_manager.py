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
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from nexus.core.rebac import (
    CROSS_ZONE_ALLOWED_RELATIONS,
    DEFAULT_FILE_NAMESPACE,
    DEFAULT_GROUP_NAMESPACE,
    DEFAULT_MEMORY_NAMESPACE,
    DEFAULT_PLAYBOOK_NAMESPACE,
    DEFAULT_SKILL_NAMESPACE,
    DEFAULT_TRAJECTORY_NAMESPACE,
    Entity,
    NamespaceConfig,
)
from nexus.services.permissions.graph.expand import ExpandEngine
from nexus.services.permissions.graph.traversal import PermissionComputer
from nexus.services.permissions.rebac_cache import ReBACPermissionCache
from nexus.services.permissions.rebac_fast import (
    check_permissions_bulk_with_fallback,
    is_rust_available,
)
from nexus.services.permissions.tuples.repository import TupleRepository

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

# P0-6: Logger for security-critical denials
logger = logging.getLogger(__name__)


class ReBACManager:
    """Manager for ReBAC operations.

    .. deprecated:: Phase 2
        Direct instantiation of ReBACManager is deprecated.
        Use :class:`EnhancedReBACManager` for production code, which includes:
        - P0 fixes (consistency levels, zone isolation, graph limits)
        - Leopard optimization (O(1) group lookups)
        - Tiger cache (advanced caching)
        - DoS protection (timeouts, fan-out limits)

        See REBAC_CONSOLIDATION_ANALYSIS.md for migration guide.

    Provides Zanzibar-style relationship-based access control with:
    - Direct tuple lookup
    - Recursive graph traversal
    - Permission expansion via namespace configs
    - Caching with TTL and invalidation
    - Cycle detection

    Note:
        This class serves as the base for ZoneAwareReBACManager and
        EnhancedReBACManager. Direct instantiation is supported for legacy
        code and testing, but new code should use EnhancedReBACManager.

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
        l1_cache_size: int = 50000,  # Increased from 10000 to handle bulk list operations
        l1_cache_ttl: int = 300,
        enable_metrics: bool = True,
        enable_adaptive_ttl: bool = False,
        l1_cache_quantization_interval: int = 0,  # DEPRECATED: Use l1_cache_revision_window
        l1_cache_revision_window: int = 10,
    ):
        """Initialize ReBAC manager.

        Args:
            engine: SQLAlchemy database engine
            cache_ttl_seconds: L2 cache TTL in seconds (default: 5 minutes)
            max_depth: Maximum graph traversal depth (default: 10 hops)
            enable_l1_cache: Enable in-memory L1 cache (default: True)
            l1_cache_size: L1 cache max entries (default: 10k)
            l1_cache_ttl: L1 cache TTL in seconds (default: 300s)
            enable_metrics: Track cache metrics (default: True)
            enable_adaptive_ttl: Adjust TTL based on write frequency (default: False)
            l1_cache_quantization_interval: DEPRECATED - was broken (Issue #909). Ignored.
            l1_cache_revision_window: Number of revisions per cache key bucket (default: 10).
                Cache keys remain stable within a revision window. See Issue #909.
        """
        self.engine = engine
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_depth = max_depth
        self._last_cleanup_time: datetime | None = None
        self._namespaces_initialized = False  # Track if default namespaces were initialized
        self._tuple_version: int = 0  # Track tuple changes for Rust graph cache invalidation

        # Issue #1459: Compose TupleRepository for data access delegation
        self._repo = TupleRepository(engine)

        # Issue #1459 Phase 8: Compose graph traversal and expand engines
        self._computer = PermissionComputer(self._repo, self.get_namespace, max_depth)
        self._expander = ExpandEngine(self._repo, self.get_namespace, max_depth)

        # Deprecation warning for direct ReBACManager instantiation (Phase 2 Task 2.3)
        # Only warn if instantiated directly (not via subclass inheritance)
        if type(self).__name__ == "ReBACManager":
            import warnings

            warnings.warn(
                "Direct instantiation of ReBACManager is deprecated. "
                "Use EnhancedReBACManager for production code (includes P0 fixes, "
                "Leopard optimization, Tiger cache, and graph limits). "
                "See REBAC_CONSOLIDATION_ANALYSIS.md for migration guide.",
                DeprecationWarning,
                stacklevel=2,
            )

        # Deprecation warning for old parameter (Issue #909)
        if l1_cache_quantization_interval > 0:
            import warnings

            warnings.warn(
                "l1_cache_quantization_interval is deprecated and was broken (Issue #909). "
                "Use l1_cache_revision_window for revision-based quantization.",
                DeprecationWarning,
                stacklevel=2,
            )

        # Initialize L1 in-memory cache with revision-based quantization (Issue #909)
        self._l1_cache: ReBACPermissionCache | None = None
        if enable_l1_cache:
            self._l1_cache = ReBACPermissionCache(
                max_size=l1_cache_size,
                ttl_seconds=l1_cache_ttl,
                enable_metrics=enable_metrics,
                enable_adaptive_ttl=enable_adaptive_ttl,
                revision_quantization_window=l1_cache_revision_window,
            )
            # Wire up revision fetcher for revision-based cache keys
            self._l1_cache.set_revision_fetcher(lambda zone_id: self._get_zone_revision(zone_id))
            logger.info(
                f"L1 cache enabled: max_size={l1_cache_size}, ttl={l1_cache_ttl}s, "
                f"metrics={enable_metrics}, adaptive_ttl={enable_adaptive_ttl}, "
                f"revision_window={l1_cache_revision_window}"
            )

        # Use SQLAlchemy sessionmaker for proper connection management
        from sqlalchemy.orm import sessionmaker

        self.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

        # Backward-compat aliases for code accessing _conn_map / _pg_version directly
        self._conn_map = self._repo._conn_map
        self._pg_version = self._repo._pg_version

    def _get_connection(self) -> Any:
        """Get a DBAPI connection from the pool.

        Delegates to TupleRepository (Issue #1459).
        """
        return self._repo.get_connection()

    def _close_connection(self, conn: Any) -> None:
        """Close a connection obtained from _get_connection().

        Delegates to TupleRepository (Issue #1459).
        """
        self._repo.close_connection(conn)

    @property
    def supports_old_new_returning(self) -> bool:
        """Check if database supports OLD/NEW in RETURNING clauses.

        Delegates to TupleRepository (Issue #1459).
        """
        return self._repo.supports_old_new_returning

    def _get_zone_revision(self, zone_id: str | None, conn: Any | None = None) -> int:
        """Get current revision for a zone. Delegates to TupleRepository (Issue #1459)."""
        return self._repo.get_zone_revision(zone_id, conn)

    def _increment_zone_revision(self, zone_id: str | None, conn: Any) -> int:
        """Increment and return the new revision. Delegates to TupleRepository (Issue #1459)."""
        return self._repo.increment_zone_revision(zone_id, conn)

    @contextmanager
    def _connection(self) -> Any:
        """Context manager for database connections. Delegates to TupleRepository (Issue #1459)."""
        with self._repo.connection() as conn:
            yield conn

    def _create_cursor(self, conn: Any) -> Any:
        """Create a cursor with appropriate cursor factory. Delegates to TupleRepository (Issue #1459)."""
        return self._repo.create_cursor(conn)

    def _ensure_namespaces_initialized(self) -> None:
        """Ensure default namespaces are initialized (called before first ReBAC operation)."""
        if not self._namespaces_initialized:
            import logging

            logger = logging.getLogger(__name__)
            logger.info("Initializing default namespaces...")

            # Use engine.connect() to leverage pool_pre_ping for stale connection detection
            with self.engine.connect() as sa_conn:
                try:
                    dbapi_conn = sa_conn.connection.dbapi_connection
                    self._initialize_default_namespaces_with_conn(dbapi_conn)
                    sa_conn.commit()
                    self._namespaces_initialized = True
                    logger.info("Default namespaces initialized successfully")
                except Exception as e:
                    sa_conn.rollback()
                    logger.warning(f"Failed to initialize namespaces: {type(e).__name__}: {e}")
                    import traceback

                    logger.debug(traceback.format_exc())

    def _fix_sql_placeholders(self, sql: str) -> str:
        """Convert SQLite ? placeholders to PostgreSQL %s. Delegates to TupleRepository (Issue #1459)."""
        return self._repo.fix_sql_placeholders(sql)

    def _would_create_cycle_with_conn(
        self, conn: Any, subject: Entity, object_entity: Entity, zone_id: str | None
    ) -> bool:
        """Check if creating a parent relation would create a cycle.

        Delegates to TupleRepository (Issue #1459).
        """
        return self._repo.would_create_cycle(conn, subject, object_entity, zone_id)

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
        zone_id: str | None = None,  # Issue #773: Defaults to "default" internally
        subject_zone_id: str | None = None,  # Defaults to zone_id if not provided
        object_zone_id: str | None = None,  # Defaults to zone_id if not provided
    ) -> str:
        """Create a relationship tuple.

        Args:
            subject: (subject_type, subject_id) or (subject_type, subject_id, subject_relation) tuple
                    For userset-as-subject: ("group", "eng", "member") means "all members of group eng"
            relation: Relation type (e.g., 'member-of', 'owner-of')
            object: (object_type, object_id) tuple
            expires_at: Optional expiration time
            conditions: Optional JSON conditions
            zone_id: Zone ID for the tuple (P0-4: zone isolation)
            subject_zone_id: Subject's zone ID (for cross-zone validation)
            object_zone_id: Object's zone ID (for cross-zone validation)

        Returns:
            Tuple ID of created relationship

        Raises:
            ValueError: If cross-zone relationship is attempted

        Example:
            >>> # Concrete subject
            >>> manager.rebac_write(
            ...     subject=("agent", "alice_id"),
            ...     relation="member-of",
            ...     object=("group", "eng_team_id"),
            ...     zone_id="org_acme"
            ... )
            >>> # Userset-as-subject
            >>> manager.rebac_write(
            ...     subject=("group", "eng_team_id", "member"),
            ...     relation="editor-of",
            ...     object=("file", "readme_txt"),
            ...     zone_id="org_acme"
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

        # Issue #773: Default zone_id to "default" if not provided
        if zone_id is None:
            zone_id = "default"
        # Default subject/object zone to main zone_id if not provided
        if subject_zone_id is None:
            subject_zone_id = zone_id
        if object_zone_id is None:
            object_zone_id = zone_id

        # P0-4: Cross-zone validation at write-time (delegated to helper)
        self._validate_cross_zone(zone_id, subject_zone_id, object_zone_id)

        with self._connection() as conn:
            # CYCLE DETECTION: Prevent cycles in parent relations
            # Check if this is a parent relation and would create a cycle
            # IMPORTANT: Must check inside the connection context to see existing tuples
            if relation == "parent" and self._would_create_cycle_with_conn(
                conn, subject_entity, object_entity, zone_id
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
                    AND (zone_id = ? OR (zone_id IS NULL AND ? IS NULL))
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
                    zone_id,
                    zone_id,
                ),
            )
            existing = cursor.fetchone()
            if existing:
                # Tuple already exists, return existing ID (idempotent)
                return cast(
                    str, existing[0] if isinstance(existing, tuple) else existing["tuple_id"]
                )

            # Insert tuple (P0-4: Include zone_id fields for isolation)
            # v0.7.0: Include subject_relation for userset-as-subject support
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    INSERT INTO rebac_tuples (
                        tuple_id, subject_type, subject_id, subject_relation, relation,
                        object_type, object_id, created_at, expires_at, conditions,
                        zone_id, subject_zone_id, object_zone_id
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
                    zone_id,
                    subject_zone_id,
                    object_zone_id,
                ),
            )

            # Log to changelog (Issue #773: include zone_id)
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    INSERT INTO rebac_changelog (
                        change_type, tuple_id, subject_type, subject_id,
                        relation, object_type, object_id, zone_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    zone_id or "default",
                    datetime.now(UTC).isoformat(),
                ),
            )

            # Increment zone revision before commit for atomicity (Issue #909)
            self._increment_zone_revision(zone_id, conn)

            conn.commit()
            self._tuple_version += 1  # Invalidate Rust graph cache

            # Invalidate cache entries affected by this change
            # Pass expires_at to disable eager recomputation for expiring tuples
            # FIX: Pass conn to avoid opening new connection (pool exhaustion)
            self._invalidate_cache_for_tuple(
                subject_entity,
                relation,
                object_entity,
                zone_id,
                subject_relation,
                expires_at,
                conn=conn,
            )

            # CROSS-ZONE FIX: If subject is from a different zone, also invalidate
            # cache for the subject's zone. This is critical for cross-zone shares
            # where the permission is granted in resource zone but checked from user zone.
            if subject_zone_id is not None and subject_zone_id != zone_id:
                self._invalidate_cache_for_tuple(
                    subject_entity,
                    relation,
                    object_entity,
                    subject_zone_id,
                    subject_relation,
                    expires_at,
                    conn=conn,  # FIX: Reuse connection
                )

        return tuple_id

    def _validate_cross_zone(
        self,
        zone_id: str | None,
        subject_zone_id: str | None,
        object_zone_id: str | None,
    ) -> None:
        """Validate cross-zone relationships. Delegates to TupleRepository (Issue #1459)."""
        TupleRepository.validate_cross_zone(zone_id, subject_zone_id, object_zone_id)

    def rebac_write_batch(
        self,
        tuples: list[dict[str, Any]],
    ) -> int:
        """Create multiple relationship tuples in a single transaction (batch operation).

        This is much more efficient than calling rebac_write() multiple times
        because it uses a single database transaction and bulk operations.

        Args:
            tuples: List of dicts with keys:
                - subject: (type, id) or (type, id, relation) tuple
                - relation: str
                - object: (type, id) tuple
                - zone_id: str | None (optional, defaults to "default")
                - expires_at: datetime | None (optional)
                - conditions: dict | None (optional)
                - subject_zone_id: str | None (optional)
                - object_zone_id: str | None (optional)

        Returns:
            Number of tuples created (excluding duplicates)

        Example:
            >>> manager.rebac_write_batch([
            ...     {
            ...         "subject": ("file", "/a/b/c.txt"),
            ...         "relation": "parent",
            ...         "object": ("file", "/a/b"),
            ...         "zone_id": "org_123"
            ...     },
            ...     {
            ...         "subject": ("file", "/a/b"),
            ...         "relation": "parent",
            ...         "object": ("file", "/a"),
            ...         "zone_id": "org_123"
            ...     }
            ... ])
            2
        """
        if not tuples:
            return 0

        # Ensure default namespaces are initialized
        self._ensure_namespaces_initialized()

        created_count = 0
        now = datetime.now(UTC).isoformat()

        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            try:
                # Step 1: Parse and validate all tuples
                parsed_tuples: list[dict[str, Any]] = []
                for t in tuples:
                    subject = t["subject"]
                    relation = t["relation"]
                    obj = t["object"]
                    zone_id = t.get("zone_id")
                    expires_at = t.get("expires_at")
                    conditions = t.get("conditions")
                    subject_zone_id = t.get("subject_zone_id")
                    object_zone_id = t.get("object_zone_id")

                    # Parse subject (support userset-as-subject with 3-tuple)
                    if len(subject) == 3:
                        subject_type, subject_id, subject_relation = subject
                        subject_entity = Entity(subject_type, subject_id)
                    elif len(subject) == 2:
                        subject_type, subject_id = subject
                        subject_relation = None
                        subject_entity = Entity(subject_type, subject_id)
                    else:
                        raise ValueError(
                            f"subject must be 2-tuple or 3-tuple, got {len(subject)}-tuple"
                        )

                    object_entity = Entity(obj[0], obj[1])

                    # Issue #773: Default zone_id values if not provided
                    if zone_id is None:
                        zone_id = "default"
                    if subject_zone_id is None:
                        subject_zone_id = zone_id
                    if object_zone_id is None:
                        object_zone_id = zone_id

                    # P0-4: Cross-zone validation (delegated to helper)
                    self._validate_cross_zone(zone_id, subject_zone_id, object_zone_id)

                    # CYCLE DETECTION: For parent relations, check for cycles
                    if relation == "parent" and self._would_create_cycle_with_conn(
                        conn, subject_entity, object_entity, zone_id
                    ):
                        logger.warning(
                            f"Skipping tuple creation - cycle detected: "
                            f"{subject_entity.entity_type}:{subject_entity.entity_id} -> "
                            f"{object_entity.entity_type}:{object_entity.entity_id}"
                        )
                        continue

                    parsed_tuples.append(
                        {
                            "tuple_id": str(uuid.uuid4()),
                            "subject_type": subject_type,
                            "subject_id": subject_id,
                            "subject_relation": subject_relation,
                            "subject_entity": subject_entity,
                            "relation": relation,
                            "object_type": obj[0],
                            "object_id": obj[1],
                            "object_entity": object_entity,
                            "zone_id": zone_id,
                            "expires_at": expires_at,
                            "conditions": conditions,
                            "subject_zone_id": subject_zone_id,
                            "object_zone_id": object_zone_id,
                        }
                    )

                if not parsed_tuples:
                    return 0

                # Step 2: Bulk check which tuples already exist
                existing_tuples = self._bulk_check_tuples_exist(cursor, parsed_tuples)

                # Step 3: Filter out existing tuples and create new ones
                tuples_to_create = []
                for pt in parsed_tuples:
                    key = (
                        (pt["subject_type"], pt["subject_id"], pt["subject_relation"]),
                        pt["relation"],
                        (pt["object_type"], pt["object_id"]),
                        pt["zone_id"],
                    )
                    if key not in existing_tuples:
                        tuples_to_create.append(pt)

                if not tuples_to_create:
                    return 0

                # Step 4: PERF OPTIMIZATION - Bulk insert tuples using executemany()
                # This is 10-50x faster than individual execute() calls
                tuple_insert_sql = self._fix_sql_placeholders(
                    """
                    INSERT INTO rebac_tuples (
                        tuple_id, subject_type, subject_id, subject_relation, relation,
                        object_type, object_id, created_at, expires_at, conditions,
                        zone_id, subject_zone_id, object_zone_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                )

                # Prepare all tuple data for bulk insert
                tuple_data = [
                    (
                        pt["tuple_id"],
                        pt["subject_type"],
                        pt["subject_id"],
                        pt["subject_relation"],
                        pt["relation"],
                        pt["object_type"],
                        pt["object_id"],
                        now,
                        pt["expires_at"].isoformat() if pt["expires_at"] else None,
                        json.dumps(pt["conditions"]) if pt["conditions"] else None,
                        pt["zone_id"],
                        pt["subject_zone_id"],
                        pt["object_zone_id"],
                    )
                    for pt in tuples_to_create
                ]

                # Bulk insert all tuples in one call
                cursor.executemany(tuple_insert_sql, tuple_data)

                # Step 5: PERF OPTIMIZATION - Bulk insert changelog entries
                changelog_insert_sql = self._fix_sql_placeholders(
                    """
                    INSERT INTO rebac_changelog (
                        change_type, tuple_id, subject_type, subject_id,
                        relation, object_type, object_id, zone_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                )

                changelog_data = [
                    (
                        "INSERT",
                        pt["tuple_id"],
                        pt["subject_type"],
                        pt["subject_id"],
                        pt["relation"],
                        pt["object_type"],
                        pt["object_id"],
                        pt["zone_id"] or "default",
                        now,
                    )
                    for pt in tuples_to_create
                ]

                cursor.executemany(changelog_insert_sql, changelog_data)

                created_count = len(tuples_to_create)

                # Step 6: PERF OPTIMIZATION - Batch cache invalidation
                # Collect unique (subject, relation, object, zone) combinations
                # and invalidate once per combination instead of per tuple
                invalidation_keys: set[tuple[str, str, str, str, str, str | None]] = set()
                for pt in tuples_to_create:
                    inv_key: tuple[str, str, str, str, str, str | None] = (
                        pt["subject_entity"].entity_type,
                        pt["subject_entity"].entity_id,
                        pt["relation"],
                        pt["object_entity"].entity_type,
                        pt["object_entity"].entity_id,
                        pt["zone_id"],
                    )
                    invalidation_keys.add(inv_key)

                    # Cross-zone invalidation
                    if pt["subject_zone_id"] and pt["subject_zone_id"] != pt["zone_id"]:
                        cross_inv_key: tuple[str, str, str, str, str, str | None] = (
                            pt["subject_entity"].entity_type,
                            pt["subject_entity"].entity_id,
                            pt["relation"],
                            pt["object_entity"].entity_type,
                            pt["object_entity"].entity_id,
                            pt["subject_zone_id"],
                        )
                        invalidation_keys.add(cross_inv_key)

                # PERF OPTIMIZATION: For batch writes, use simple invalidation (no eager recompute)
                # Eager recomputation is expensive and defeats the purpose of batching
                # The next permission check will rebuild the cache as needed

                # L1 cache: invalidate all affected subject-object pairs
                if self._l1_cache:
                    for inv_key in invalidation_keys:
                        subj_type, subj_id, _rel, obj_type, obj_id, tid = inv_key
                        self._l1_cache.invalidate_subject_object_pair(
                            subj_type, subj_id, obj_type, obj_id, tid
                        )

                # L2 cache: bulk delete affected entries
                if invalidation_keys:
                    # Build bulk delete for all subject-object pairs
                    delete_conditions = []
                    delete_params: list[str] = []
                    for inv_key in invalidation_keys:
                        subj_type, subj_id, _rel, obj_type, obj_id, tid = inv_key
                        delete_conditions.append(
                            "(zone_id = ? AND subject_type = ? AND subject_id = ? "
                            "AND object_type = ? AND object_id = ?)"
                        )
                        delete_params.extend(
                            [tid or "default", subj_type, subj_id, obj_type, obj_id]
                        )

                    # Chunk the deletes to avoid too large SQL
                    CHUNK_SIZE = 50
                    for i in range(0, len(delete_conditions), CHUNK_SIZE):
                        chunk_conditions = delete_conditions[i : i + CHUNK_SIZE]
                        chunk_params = delete_params[i * 5 : (i + CHUNK_SIZE) * 5]

                        if chunk_conditions:
                            delete_sql = f"""
                                DELETE FROM rebac_check_cache
                                WHERE {" OR ".join(chunk_conditions)}
                            """
                            cursor.execute(self._fix_sql_placeholders(delete_sql), chunk_params)

                # Increment revision for all affected zones before commit (Issue #909)
                if created_count > 0:
                    affected_zones = set()
                    for pt in parsed_tuples:
                        affected_zones.add(pt["zone_id"] or "default")
                        if pt["subject_zone_id"] and pt["subject_zone_id"] != pt["zone_id"]:
                            affected_zones.add(pt["subject_zone_id"])
                    for zone in affected_zones:
                        self._increment_zone_revision(zone, conn)

                # Commit transaction after all inserts succeed
                conn.commit()
                if created_count > 0:
                    self._tuple_version += 1  # Invalidate Rust graph cache

            except Exception as e:
                # Rollback transaction on any error to maintain consistency
                conn.rollback()
                logger.error(
                    f"Failed to batch create {len(tuples)} tuples: {type(e).__name__}: {e}",
                    exc_info=True,
                )
                raise

        return created_count

    def _bulk_check_tuples_exist(
        self,
        cursor: Any,
        parsed_tuples: list[dict[str, Any]],
    ) -> set[tuple]:
        """Check which tuples already exist. Delegates to TupleRepository (Issue #1459)."""
        return self._repo.bulk_check_tuples_exist(cursor, parsed_tuples)

    def rebac_delete(self, tuple_id: str) -> bool:
        """Delete a relationship tuple.

        Args:
            tuple_id: ID of tuple to delete

        Returns:
            True if tuple was deleted, False if not found
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)
            now = datetime.now(UTC).isoformat()

            # PostgreSQL: Use DELETE...RETURNING to get deleted row in single query
            # This eliminates the SELECT+DELETE round-trip for better performance
            # Note: DELETE only has one row version, so no need for OLD prefix
            if self.engine.dialect.name == "postgresql":
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        DELETE FROM rebac_tuples
                        WHERE tuple_id = ?
                          AND (expires_at IS NULL OR expires_at >= ?)
                        RETURNING
                            subject_type,
                            subject_id,
                            subject_relation,
                            relation,
                            object_type,
                            object_id,
                            zone_id
                        """
                    ),
                    (tuple_id, now),
                )
                row = cursor.fetchone()
            else:
                # SQLite / older PostgreSQL: SELECT then DELETE (2 queries)
                # P0-5: Filter expired tuples at read-time (prevent deleted/expired access leak)
                # BUGFIX: Use >= instead of > for exact expiration boundary
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        SELECT subject_type, subject_id, subject_relation, relation, object_type, object_id, zone_id
                        FROM rebac_tuples
                        WHERE tuple_id = ?
                          AND (expires_at IS NULL OR expires_at >= ?)
                        """
                    ),
                    (tuple_id, now),
                )
                row = cursor.fetchone()

                if row:
                    # Delete tuple
                    cursor.execute(
                        self._fix_sql_placeholders("DELETE FROM rebac_tuples WHERE tuple_id = ?"),
                        (tuple_id,),
                    )

            if not row:
                return False

            # Both SQLite and PostgreSQL now return dict-like rows
            subject = Entity(row["subject_type"], row["subject_id"])
            subject_relation = row["subject_relation"]
            relation = row["relation"]
            obj = Entity(row["object_type"], row["object_id"])
            zone_id = row["zone_id"]

            # Log to changelog (Issue #773: include zone_id)
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    INSERT INTO rebac_changelog (
                        change_type, tuple_id, subject_type, subject_id,
                        relation, object_type, object_id, zone_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    zone_id or "default",
                    now,
                ),
            )

            # Increment zone revision before commit for atomicity (Issue #909)
            self._increment_zone_revision(zone_id, conn)

            conn.commit()
            self._tuple_version += 1  # Invalidate Rust graph cache

            # Invalidate cache entries affected by this change
            # FIX: Pass conn to avoid opening new connection (pool exhaustion)
            self._invalidate_cache_for_tuple(
                subject, relation, obj, zone_id, subject_relation, conn=conn
            )

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
        logger.info(
            f"update_object_path: {old_path} -> {new_path}, object_type={object_type}, is_directory={is_directory}"
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
                               relation, object_type, object_id, zone_id
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
                               relation, object_type, object_id, zone_id
                        FROM rebac_tuples
                        WHERE object_type = ?
                          AND object_id = ?
                          AND (expires_at IS NULL OR expires_at >= ?)
                        """
                    ),
                    (object_type, old_path, datetime.now(UTC).isoformat()),
                )

            rows = cursor.fetchall()
            logger.debug(f"update_object_path: Found {len(rows)} tuples with object_id to update")

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

                logger.debug(f"update_object_path: Batch UPDATE affected {cursor.rowcount} rows")

                # PERF: Batch INSERT changelog entries (Issue #773: include zone_id)
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
                            row["zone_id"] or "default",
                            now_iso,
                        )
                    )

                cursor.executemany(
                    self._fix_sql_placeholders(
                        """
                        INSERT INTO rebac_changelog (
                            change_type, tuple_id, subject_type, subject_id,
                            relation, object_type, object_id, zone_id, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    zone_id = row["zone_id"]
                    subject_relation = row["subject_relation"]

                    self._invalidate_cache_for_tuple(
                        subject, relation, old_obj, zone_id, subject_relation, conn=conn
                    )

                    # BUG FIX (PR #969): Also invalidate Tiger Cache for the subject
                    # Tiger Cache stores materialized permissions - when a file is renamed,
                    # the cached permissions for the subject are stale and must be invalidated
                    if hasattr(self, "tiger_invalidate_cache"):
                        try:
                            self.tiger_invalidate_cache(
                                subject=(subject.entity_type, subject.entity_id),
                                resource_type=old_obj.entity_type,
                                zone_id=zone_id or "default",
                            )
                        except Exception as e:
                            logger.warning(f"Tiger Cache invalidation failed during rename: {e}")

                    self._invalidate_cache_for_tuple(
                        subject, relation, new_obj, zone_id, subject_relation, conn=conn
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
                               relation, object_type, object_id, zone_id
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
                               relation, object_type, object_id, zone_id
                        FROM rebac_tuples
                        WHERE subject_type = ?
                          AND subject_id = ?
                          AND (expires_at IS NULL OR expires_at >= ?)
                        """
                    ),
                    (object_type, old_path, datetime.now(UTC).isoformat()),
                )

            subject_rows = cursor.fetchall()
            logger.debug(
                f"update_object_path: Found {len(subject_rows)} tuples with subject_id to update"
            )

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

                logger.debug(
                    f"update_object_path: Batch UPDATE (subject_id) affected {cursor.rowcount} rows"
                )

                # PERF: Batch INSERT changelog entries (Issue #773: include zone_id)
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
                            row["zone_id"] or "default",
                            now_iso,
                        )
                    )

                cursor.executemany(
                    self._fix_sql_placeholders(
                        """
                        INSERT INTO rebac_changelog (
                            change_type, tuple_id, subject_type, subject_id,
                            relation, object_type, object_id, zone_id, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    zone_id = row["zone_id"]
                    subject_relation = row["subject_relation"]

                    self._invalidate_cache_for_tuple(
                        old_subj, relation, obj, zone_id, subject_relation, conn=conn
                    )
                    self._invalidate_cache_for_tuple(
                        new_subj, relation, obj, zone_id, subject_relation, conn=conn
                    )

                updated_count += len(subject_rows)

            # BUG FIX: Also update Tiger Resource Map when files are renamed
            # The resource map maps (resource_type, resource_id, zone_id) -> integer ID
            # If the old path is still in the resource map, Tiger Cache checks may return
            # stale results because the bitmap might still reference the old resource_int_id
            if hasattr(self, "_tiger_cache") and self._tiger_cache:
                try:
                    # Delete old path entries from resource map (database)
                    if is_directory:
                        cursor.execute(
                            self._fix_sql_placeholders(
                                """
                                DELETE FROM tiger_resource_map
                                WHERE resource_type = ?
                                  AND (resource_id = ? OR resource_id LIKE ?)
                                """
                            ),
                            (object_type, old_path, old_path + "/%"),
                        )
                    else:
                        cursor.execute(
                            self._fix_sql_placeholders(
                                """
                                DELETE FROM tiger_resource_map
                                WHERE resource_type = ? AND resource_id = ?
                                """
                            ),
                            (object_type, old_path),
                        )
                    deleted_resource_map_entries = cursor.rowcount
                    if deleted_resource_map_entries > 0:
                        logger.info(
                            f"[UPDATE-OBJECT-PATH] Deleted {deleted_resource_map_entries} entries from tiger_resource_map"
                        )

                    # Also clear the in-memory resource map cache for the old path
                    resource_map = self._tiger_cache._resource_map
                    if hasattr(resource_map, "_uuid_to_int"):
                        keys_to_remove = []
                        for key in resource_map._uuid_to_int:
                            res_type, res_id, zone = key
                            if res_type == object_type:
                                if is_directory:
                                    if res_id == old_path or res_id.startswith(old_path + "/"):
                                        keys_to_remove.append(key)
                                else:
                                    if res_id == old_path:
                                        keys_to_remove.append(key)
                        for key in keys_to_remove:
                            int_id = resource_map._uuid_to_int.pop(key, None)
                            if int_id is not None and hasattr(resource_map, "_int_to_uuid"):
                                resource_map._int_to_uuid.pop(int_id, None)
                except Exception as e:
                    logger.warning(f"[UPDATE-OBJECT-PATH] Failed to update tiger_resource_map: {e}")

            conn.commit()
            if updated_count > 0:
                self._tuple_version += 1  # Invalidate Rust graph cache
            logger.info(f"update_object_path complete: updated {updated_count} tuples total")

        return updated_count

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
    ) -> bool:
        """Check if subject has permission on object.

        Uses caching and recursive graph traversal to compute permissions.
        Supports ABAC-style contextual conditions (time, location, device, etc.).

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., 'read', 'write')
            object: (object_type, object_id) tuple
            context: Optional context for ABAC evaluation (time, ip, device, etc.)
            zone_id: Optional zone ID for multi-zone isolation

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

        # Issue #773: Default zone_id to "default" if not provided
        if zone_id is None:
            zone_id = "default"

        subject_entity = Entity(subject[0], subject[1])
        object_entity = Entity(object[0], object[1])

        logger.debug(
            f"🔍 REBAC CHECK: subject={subject_entity}, permission={permission}, object={object_entity}, zone_id={zone_id}"
        )

        # Clean up expired tuples first (this will invalidate affected caches)
        self._cleanup_expired_tuples_if_needed()

        # Check cache first with refresh-ahead (Issue #932)
        # Only if no context, since context makes checks dynamic
        if context is None:
            # Use refresh-ahead pattern to proactively refresh cache before expiry
            if self._l1_cache:
                cached, needs_refresh, cache_key = self._l1_cache.get_with_refresh_check(
                    subject_entity.entity_type,
                    subject_entity.entity_id,
                    permission,
                    object_entity.entity_type,
                    object_entity.entity_id,
                    zone_id,
                )
                if cached is not None:
                    logger.debug(f"✅ CACHE HIT: result={cached}, needs_refresh={needs_refresh}")
                    if needs_refresh:
                        # Schedule background refresh without blocking
                        self._schedule_background_refresh(
                            cache_key, subject, permission, object, zone_id
                        )
                    return cached
            else:
                # Fallback to old method if no L1 cache
                cached = self._get_cached_check(subject_entity, permission, object_entity, zone_id)
                if cached is not None:
                    logger.debug(f"✅ CACHE HIT: result={cached}")
                    return cached

            # Cache miss - use stampede prevention (Issue #878)
            # Only one request computes while others wait
            if self._l1_cache:
                should_compute, cache_key = self._l1_cache.try_acquire_compute(
                    subject_entity.entity_type,
                    subject_entity.entity_id,
                    permission,
                    object_entity.entity_type,
                    object_entity.entity_id,
                    zone_id,
                )

                if not should_compute:
                    # Another request is computing - wait for it
                    logger.debug("⏳ STAMPEDE: Waiting for another request to compute")
                    wait_result = self._l1_cache.wait_for_compute(cache_key)
                    if wait_result is not None:
                        logger.debug(f"✅ STAMPEDE: Got result from leader: {wait_result}")
                        return wait_result
                    # Timeout or error - fall through to compute ourselves
                    logger.debug("⚠️ STAMPEDE: Wait timeout, computing ourselves")

                # We're the leader - compute and release
                try:
                    logger.debug("🔎 Computing permission (no cache hit, computing from graph)")
                    import time as time_module

                    start_time = time_module.perf_counter()
                    result = self._compute_permission(
                        subject_entity,
                        permission,
                        object_entity,
                        visited=set(),
                        depth=0,
                        context=context,
                        zone_id=zone_id,
                    )
                    delta = time_module.perf_counter() - start_time
                    logger.debug(f"{'✅' if result else '❌'} REBAC RESULT: {result}")

                    # Cache result and release lock with delta for XFetch (Issue #718)
                    self._l1_cache.release_compute(
                        cache_key,
                        result,
                        subject_entity.entity_type,
                        subject_entity.entity_id,
                        permission,
                        object_entity.entity_type,
                        object_entity.entity_id,
                        zone_id,
                        delta=delta,
                    )
                    # Also cache in L2
                    self._cache_check_result(
                        subject_entity, permission, object_entity, result, zone_id, delta=delta
                    )
                    return result
                except Exception:
                    # On error, cancel the compute lock so others don't wait forever
                    self._l1_cache.cancel_compute(cache_key)
                    raise

        # Context-based check or no L1 cache - compute directly (no stampede prevention)
        logger.debug("🔎 Computing permission (no cache hit, computing from graph)")
        import time as time_module

        start_time = time_module.perf_counter()
        result = self._compute_permission(
            subject_entity,
            permission,
            object_entity,
            visited=set(),
            depth=0,
            context=context,
            zone_id=zone_id,
        )
        delta = time_module.perf_counter() - start_time

        logger.debug(f"{'✅' if result else '❌'} REBAC RESULT: {result}")

        # Cache result (only if no context) with delta for XFetch (Issue #718)
        if context is None:
            self._cache_check_result(
                subject_entity, permission, object_entity, result, zone_id, delta=delta
            )

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

        # Phase 2: Compute uncached checks with delta tracking for XFetch (Issue #718)
        import time as time_module

        for i, subject_entity, permission, object_entity in uncached_checks:
            start_time = time_module.perf_counter()
            result = self._compute_permission(
                subject_entity, permission, object_entity, visited=set(), depth=0
            )
            delta = time_module.perf_counter() - start_time
            self._cache_check_result(
                subject_entity, permission, object_entity, result, zone_id=None, delta=delta
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
                    import time as time_module

                    start_time = time_module.perf_counter()
                    rust_results = self._compute_batch_rust([check for _, check in uncached_checks])
                    total_delta = time_module.perf_counter() - start_time
                    # Approximate per-check delta (Rust computes in bulk)
                    avg_delta = total_delta / len(uncached_checks) if uncached_checks else 0.0

                    for idx, (i, _) in enumerate(uncached_checks):
                        result = rust_results[idx]
                        results[i] = result
                        # Cache the result with XFetch delta (Issue #718)
                        subject, permission, obj = uncached_checks[idx][1]
                        subject_entity = Entity(subject[0], subject[1])
                        object_entity = Entity(obj[0], obj[1])
                        self._cache_check_result(
                            subject_entity,
                            permission,
                            object_entity,
                            result,
                            zone_id=None,
                            delta=avg_delta,
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
        import time as time_module

        for i, (subject, permission, obj) in uncached_checks:
            subject_entity = Entity(subject[0], subject[1])
            object_entity = Entity(obj[0], obj[1])
            start_time = time_module.perf_counter()
            result = self._compute_permission(
                subject_entity, permission, object_entity, visited=set(), depth=0
            )
            delta = time_module.perf_counter() - start_time
            self._cache_check_result(
                subject_entity, permission, object_entity, result, zone_id=None, delta=delta
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

        # Call Rust extension with tuple version for graph caching
        rust_results_dict = check_permissions_bulk_with_fallback(
            checks, tuples, namespace_configs, force_python=False, tuple_version=self._tuple_version
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
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Explain why a subject has or doesn't have permission on an object.

        This is a debugging/audit API that traces through the permission graph
        to explain the result of a permission check.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., 'read', 'write')
            object: (object_type, object_id) tuple
            zone_id: Optional zone ID for multi-zone isolation

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
            zone_id=zone_id,
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

        Delegates to PermissionComputer (Issue #1459 Phase 8).
        """
        return PermissionComputer.format_path_reason(subject, permission, obj, path)

    def _compute_permission_with_explanation(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        visited: set[tuple[str, str, str, str, str]],
        depth: int,
        paths: list[dict[str, Any]],
        zone_id: str | None = None,
    ) -> bool:
        """Compute permission with path tracking. Delegates to PermissionComputer (Issue #1459)."""
        return self._computer.compute_permission_with_explanation(
            subject, permission, obj, visited, depth, paths, zone_id
        )

    def _compute_permission(
        self,
        subject: Entity,
        permission: str | dict[str, Any],
        obj: Entity,
        visited: set[tuple[str, str, str, str, str]],
        depth: int,
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
    ) -> bool:
        """Compute permission via graph traversal. Delegates to PermissionComputer (Issue #1459)."""
        return self._computer.compute_permission(
            subject, permission, obj, visited, depth, context, zone_id
        )

    def _has_direct_relation(
        self,
        subject: Entity,
        relation: str,
        obj: Entity,
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
    ) -> bool:
        """Check if subject has direct relation. Delegates to PermissionComputer (Issue #1459)."""
        return self._computer.has_direct_relation(subject, relation, obj, context, zone_id)

    def _find_direct_relation_tuple(
        self,
        subject: Entity,
        relation: str,
        obj: Entity,
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find direct relation tuple. Delegates to PermissionComputer (Issue #1459)."""
        return self._computer.find_direct_relation_tuple(subject, relation, obj, context, zone_id)

    def _find_subject_sets(
        self, relation: str, obj: Entity, zone_id: str | None = None
    ) -> list[tuple[str, str, str]]:
        """Find all subject sets with a relation to an object. Delegates to TupleRepository."""
        return self._repo.find_subject_sets(relation, obj, zone_id)

    def _find_related_objects(self, obj: Entity, relation: str) -> list[Entity]:
        """Find all objects related to obj via relation. Delegates to TupleRepository."""
        return self._repo.find_related_objects(obj, relation)

    def _find_subjects_with_relation(self, obj: Entity, relation: str) -> list[Entity]:
        """Find all subjects with a relation to obj. Delegates to TupleRepository."""
        return self._repo.find_subjects_with_relation(obj, relation)

    def _evaluate_conditions(
        self, conditions: dict[str, Any] | None, context: dict[str, Any] | None
    ) -> bool:
        """Evaluate ABAC conditions against runtime context. Delegates to TupleRepository."""
        return TupleRepository.evaluate_conditions(conditions, context)

    def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
    ) -> list[tuple[str, str]]:
        """Find all subjects with a given permission on an object.

        Delegates to ExpandEngine (Issue #1459 Phase 8).
        """
        return self._expander.expand(permission, object)

    def get_cross_zone_shared_paths(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str,
        object_type: str = "file",
        prefix: str = "",
    ) -> list[str]:
        """Return distinct object_id paths shared with a subject from other zones.

        Queries rebac_tuples for cross-zone sharing relations (shared-viewer,
        shared-editor, shared-owner) where the subject matches and the zone
        differs from the given zone_id. Optionally filters by object_type and
        path prefix.

        Args:
            subject_type: Subject entity type (e.g., "user").
            subject_id: Subject entity ID.
            zone_id: Current zone to exclude from results.
            object_type: Object type filter (default: "file").
            prefix: If non-empty, only return paths starting with this prefix.

        Returns:
            List of distinct object_id strings matching the criteria.
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)
            cross_zone_relations = list(CROSS_ZONE_ALLOWED_RELATIONS)
            placeholders = ", ".join("?" * len(cross_zone_relations))
            query = f"""
                SELECT DISTINCT object_id
                FROM rebac_tuples
                WHERE relation IN ({placeholders})
                  AND subject_type = ? AND subject_id = ?
                  AND object_type = ?
                  AND zone_id != ?
                  AND (expires_at IS NULL OR expires_at > ?)
            """
            params: tuple[Any, ...] = (
                *cross_zone_relations,
                subject_type,
                subject_id,
                object_type,
                zone_id,
                datetime.now(UTC).isoformat(),
            )
            if prefix:
                query += " AND object_id LIKE ?"
                params = (*params, f"{prefix}%")

            cursor.execute(self._fix_sql_placeholders(query), params)
            paths = []
            for row in cursor.fetchall():
                path = row["object_id"] if isinstance(row, dict) else row[0]
                paths.append(path)
            return paths

    def _get_direct_subjects(self, relation: str, obj: Entity) -> list[tuple[str, str]]:
        """Get all subjects with direct relation to object. Delegates to TupleRepository."""
        return self._repo.get_direct_subjects(relation, obj)

    def _get_cached_check(
        self, subject: Entity, permission: str, obj: Entity, zone_id: str | None = None
    ) -> bool | None:
        """Get cached permission check result.

        Checks L1 (in-memory) cache first, then L2 (database) cache.

        Args:
            subject: Subject entity
            permission: Permission
            obj: Object entity
            zone_id: Optional zone ID

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
                zone_id,
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
                        zone_id,
                    )

                return result
            return None

    # ============================================================
    # Background Refresh (Issue #932)
    # ============================================================

    def _schedule_background_refresh(
        self,
        cache_key: str,
        subject: tuple[str, str],
        permission: str,
        obj: tuple[str, str],
        zone_id: str | None,
    ) -> None:
        """Schedule a background refresh for a cache entry.

        This is called when a cache hit occurs but the entry is past its
        refresh threshold. The cached value is returned immediately while
        a background thread refreshes the cache.

        Args:
            cache_key: Cache key being refreshed
            subject: (subject_type, subject_id) tuple
            permission: Permission to check
            obj: (object_type, object_id) tuple
            zone_id: Optional zone ID
        """
        if not self._l1_cache:
            return

        if not self._l1_cache.mark_refresh_in_progress(cache_key):
            # Already being refreshed by another thread
            return

        # Start background refresh in a daemon thread
        thread = threading.Thread(
            target=self._background_refresh_worker,
            args=(cache_key, subject, permission, obj, zone_id),
            daemon=True,
            name=f"rebac-refresh-{cache_key[:20]}",
        )
        thread.start()
        logger.debug(f"🔄 REFRESH: Scheduled background refresh for {cache_key[:50]}...")

    def _background_refresh_worker(
        self,
        cache_key: str,
        subject: tuple[str, str],
        permission: str,
        obj: tuple[str, str],
        zone_id: str | None,
    ) -> None:
        """Worker thread that refreshes a cache entry in the background.

        Args:
            cache_key: Cache key being refreshed
            subject: (subject_type, subject_id) tuple
            permission: Permission to check
            obj: (object_type, object_id) tuple
            zone_id: Optional zone ID
        """
        try:
            subject_entity = Entity(subject[0], subject[1])
            object_entity = Entity(obj[0], obj[1])

            # Compute permission (bypassing cache) and measure delta for XFetch
            import time as time_module

            start_time = time_module.perf_counter()
            result = self._compute_permission(
                subject_entity,
                permission,
                object_entity,
                visited=set(),
                depth=0,
                context=None,
                zone_id=zone_id,
            )
            delta = time_module.perf_counter() - start_time

            # Update cache with delta for XFetch (Issue #718)
            if self._l1_cache:
                self._l1_cache.set(
                    subject[0],
                    subject[1],
                    permission,
                    obj[0],
                    obj[1],
                    result,
                    zone_id,
                    delta=delta,
                )

            # Also update L2 cache
            self._cache_check_result(subject_entity, permission, object_entity, result, zone_id)

            logger.debug(f"✅ REFRESH: Background refresh complete for {cache_key[:50]}...")
        except Exception as e:
            logger.warning(f"⚠️ REFRESH: Background refresh failed for {cache_key[:50]}: {e}")
        finally:
            if self._l1_cache:
                self._l1_cache.complete_refresh(cache_key)

    def _cache_check_result(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        result: bool,
        zone_id: str | None = None,
        conn: Any | None = None,
        delta: float = 0.0,
    ) -> None:
        """Cache permission check result in both L1 and L2 caches.

        Args:
            subject: Subject entity
            permission: Permission
            obj: Object entity
            result: Check result
            zone_id: Optional zone ID for multi-zone isolation
            conn: Optional database connection
            delta: Recomputation time in seconds for XFetch (Issue #718)
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
                zone_id,
                delta=delta,
            )

        # Then cache in L2 (database)
        cache_id = str(uuid.uuid4())
        computed_at = datetime.now(UTC)
        expires_at = computed_at + timedelta(seconds=self.cache_ttl_seconds)

        # Use "default" zone if not specified (for backward compatibility)
        effective_zone_id = zone_id if zone_id is not None else "default"

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
                    WHERE zone_id = ?
                      AND subject_type = ? AND subject_id = ?
                      AND permission = ?
                      AND object_type = ? AND object_id = ?
                    """
                ),
                (
                    effective_zone_id,
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
                        cache_id, zone_id, subject_type, subject_id, permission,
                        object_type, object_id, result, computed_at, expires_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    cache_id,
                    effective_zone_id,
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
                self._close_connection(conn)

    def _invalidate_cache_for_tuple(
        self,
        subject: Entity,
        relation: str,
        obj: Entity,
        zone_id: str | None = None,
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
            zone_id: Optional zone ID for zone-scoped invalidation
            subject_relation: Optional subject relation for userset-as-subject
            expires_at: Optional expiration time (disables eager recomputation)
        """
        # Use "default" zone if not specified
        effective_zone_id = zone_id if zone_id is not None else "default"

        import logging

        logger = logging.getLogger(__name__)

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

            # BUG FIX (PR #969): ALWAYS invalidate L1 cache first, regardless of eager recompute
            # The eager recompute only updates L2 (database) cache, but L1 (in-memory) cache
            # will still have stale entries. We must invalidate L1 before any recomputation.
            if self._l1_cache:
                self._l1_cache.invalidate_subject_object_pair(
                    subject.entity_type,
                    subject.entity_id,
                    obj.entity_type,
                    obj.entity_id,
                    zone_id,
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
                    import time as time_module

                    for permission in affected_permissions[:5]:  # Limit to 5 most common
                        try:
                            # Recompute the permission with delta tracking for XFetch (Issue #718)
                            start_time = time_module.perf_counter()
                            result = self._compute_permission(
                                subject,
                                permission,
                                obj,
                                visited=set(),
                                depth=0,
                                zone_id=zone_id,
                            )
                            delta = time_module.perf_counter() - start_time
                            # Update cache immediately (not invalidate)
                            self._cache_check_result(
                                subject, permission, obj, result, zone_id, conn=conn, delta=delta
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

            # If we didn't do eager recomputation, also invalidate L2 cache
            # (L1 was already invalidated above)
            if not should_eager_recompute:
                # L2 cache invalidation
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        DELETE FROM rebac_check_cache
                        WHERE zone_id = ?
                          AND subject_type = ? AND subject_id = ?
                          AND object_type = ? AND object_id = ?
                        """
                    ),
                    (
                        effective_zone_id,
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
                    self._l1_cache.invalidate_object(obj.entity_type, obj.entity_id, zone_id)

                # L2 cache invalidation
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        DELETE FROM rebac_check_cache
                        WHERE zone_id = ?
                          AND object_type = ? AND object_id = ?
                        """
                    ),
                    (effective_zone_id, obj.entity_type, obj.entity_id),
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
                        subject.entity_type, subject.entity_id, zone_id
                    )

                # L2 cache invalidation
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        DELETE FROM rebac_check_cache
                        WHERE zone_id = ?
                          AND subject_type = ? AND subject_id = ?
                        """
                    ),
                    (effective_zone_id, subject.entity_type, subject.entity_id),
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
                # Cross-zone sharing relations (PR #647)
                "shared-viewer",
                "shared-editor",
                "shared-owner",
            ):
                # Invalidate all cache entries for paths that are children of this object
                # Match object_id that starts with obj.entity_id/ (children)

                # L1 cache invalidation - invalidate prefix
                if self._l1_cache:
                    self._l1_cache.invalidate_object_prefix(obj.entity_type, obj.entity_id, zone_id)

                # L2 cache invalidation
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        DELETE FROM rebac_check_cache
                        WHERE zone_id = ?
                          AND object_type = ?
                          AND (object_id = ? OR object_id LIKE ?)
                        """
                    ),
                    (effective_zone_id, obj.entity_type, obj.entity_id, obj.entity_id + "/%"),
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

                # L1 cache invalidation - clear all for this zone
                if self._l1_cache:
                    self._l1_cache.clear()  # Conservative: clear entire L1 cache

                # L2 cache invalidation
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        DELETE FROM rebac_check_cache
                        WHERE zone_id = ?
                        """
                    ),
                    (effective_zone_id,),
                )

            conn.commit()
        finally:
            if should_close:
                self._close_connection(conn)

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
                    SELECT tuple_id, subject_type, subject_id, subject_relation, relation, object_type, object_id, zone_id
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
                zone_id = row["zone_id"]

                # Issue #773: include zone_id in changelog
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        INSERT INTO rebac_changelog (
                            change_type, tuple_id, subject_type, subject_id,
                            relation, object_type, object_id, zone_id, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        zone_id or "default",
                        datetime.now(UTC).isoformat(),
                    ),
                )

                # Invalidate cache for this tuple
                # Pass a dummy expires_at to prevent eager recomputation during cleanup
                # FIX: Pass conn to avoid opening new connection (pool exhaustion)
                subject = Entity(subject_type, subject_id)
                obj = Entity(object_type, object_id)
                self._invalidate_cache_for_tuple(
                    subject,
                    relation,
                    obj,
                    zone_id,
                    subject_relation,
                    expires_at=datetime.now(UTC),
                    conn=conn,
                )

            conn.commit()
            if expired_tuples:
                self._tuple_version += 1  # Invalidate Rust graph cache
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
