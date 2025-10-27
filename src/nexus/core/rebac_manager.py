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
from typing import TYPE_CHECKING, Any

from nexus.core.rebac import (
    DEFAULT_FILE_NAMESPACE,
    DEFAULT_GROUP_NAMESPACE,
    DEFAULT_MEMORY_NAMESPACE,
    WILDCARD_SUBJECT,
    Entity,
    NamespaceConfig,
)

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
        max_depth: int = 10,
    ):
        """Initialize ReBAC manager.

        Args:
            engine: SQLAlchemy database engine
            cache_ttl_seconds: Cache TTL in seconds (default: 5 minutes)
            max_depth: Maximum graph traversal depth (default: 10 hops)
        """
        self.engine = engine
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_depth = max_depth
        self._last_cleanup_time: datetime | None = None
        self._namespaces_initialized = False  # Track if default namespaces were initialized
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
                cursor = conn.cursor()
                cursor.execute(...)
                conn.commit()
        """
        conn = self._get_connection()
        try:
            yield conn
        finally:
            conn.close()

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

    def _initialize_default_namespaces_with_conn(self, conn: Any) -> None:
        """Initialize default namespace configurations with given connection."""
        try:
            cursor = conn.cursor()

            # Check if rebac_namespaces table exists
            if self.engine.dialect.name == "sqlite":
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='rebac_namespaces'"
                )
            else:  # PostgreSQL
                cursor.execute("SELECT tablename FROM pg_tables WHERE tablename='rebac_namespaces'")

            if not cursor.fetchone():
                return  # Table doesn't exist yet

            # Check and create namespaces
            for ns_config in [
                DEFAULT_FILE_NAMESPACE,
                DEFAULT_GROUP_NAMESPACE,
                DEFAULT_MEMORY_NAMESPACE,
            ]:
                cursor.execute(
                    self._fix_sql_placeholders(
                        "SELECT object_type FROM rebac_namespaces WHERE object_type = ?"
                    ),
                    (ns_config.object_type,),
                )
                if not cursor.fetchone():
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
            cursor = conn.cursor()

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
            cursor = conn.cursor()

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

            # Handle both dict-like (sqlite3.Row) and tuple access
            if hasattr(row, "keys"):
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
            else:
                # PostgreSQL returns tuples
                created_at = row[3]
                updated_at = row[4]
                if isinstance(created_at, str):
                    created_at = datetime.fromisoformat(created_at)
                if isinstance(updated_at, str):
                    updated_at = datetime.fromisoformat(updated_at)

                return NamespaceConfig(
                    namespace_id=row[0],
                    object_type=row[1],
                    config=json.loads(row[2]) if isinstance(row[2], str) else row[2],
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
        print("DEBUG: rebac_write called, calling _ensure_namespaces_initialized...")  # DEBUG
        self._ensure_namespaces_initialized()
        print(
            f"DEBUG: rebac_write after _ensure_namespaces_initialized, flag={self._namespaces_initialized}"
        )  # DEBUG

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
            cursor = conn.cursor()

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
            self._invalidate_cache_for_tuple(subject_entity, relation, object_entity, tenant_id)

        return tuple_id

    def rebac_delete(self, tuple_id: str) -> bool:
        """Delete a relationship tuple.

        Args:
            tuple_id: ID of tuple to delete

        Returns:
            True if tuple was deleted, False if not found
        """
        with self._connection() as conn:
            cursor = conn.cursor()

            # Get tuple details before deleting (for changelog and cache invalidation)
            # P0-5: Filter expired tuples at read-time (prevent deleted/expired access leak)
            # BUGFIX: Use >= instead of > for exact expiration boundary
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT subject_type, subject_id, relation, object_type, object_id, tenant_id
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

            # Handle both dict-like (SQLite) and tuple (PostgreSQL) access
            if hasattr(row, "keys"):
                subject = Entity(row["subject_type"], row["subject_id"])
                relation = row["relation"]
                obj = Entity(row["object_type"], row["object_id"])
                tenant_id = row["tenant_id"]
            else:
                subject = Entity(row[0], row[1])
                relation = row[2]
                obj = Entity(row[3], row[4])
                tenant_id = row[5]

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
            self._invalidate_cache_for_tuple(subject, relation, obj, tenant_id)

        return True

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

        # Clean up expired tuples first (this will invalidate affected caches)
        self._cleanup_expired_tuples_if_needed()

        # Check cache first (only if no context, since context makes checks dynamic)
        if context is None:
            cached = self._get_cached_check(subject_entity, permission, object_entity)
            if cached is not None:
                return cached

        # Compute permission via graph traversal with context
        result = self._compute_permission(
            subject_entity,
            permission,
            object_entity,
            visited=set(),
            depth=0,
            context=context,
            tenant_id=tenant_id,
        )

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

    def rebac_explain(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
    ) -> dict[str, Any]:
        """Explain why a subject has or doesn't have permission on an object.

        This is a debugging/audit API that traces through the permission graph
        to explain the result of a permission check.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., 'read', 'write')
            object: (object_type, object_id) tuple

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
            subject_entity, permission, object_entity, visited=set(), depth=0, paths=paths
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
            tuple_info = self._find_direct_relation_tuple(subject, permission, obj)
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
                    subject, userset, obj, visited.copy(), depth + 1, userset_sub_paths
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
            tuple_info = self._find_direct_relation_tuple(subject, permission, obj)
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
                    subject, rel, obj, visited.copy(), depth + 1, union_sub_paths
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
                    subject, rel, obj, visited.copy(), depth + 1, intersection_sub_paths
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
                    subject, excluded_rel, obj, visited.copy(), depth + 1, exclusion_sub_paths
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

                related_objects = self._find_related_objects(obj, tupleset_relation)
                path_entry["tupleToUserset"] = {
                    "tupleset": tupleset_relation,
                    "computedUserset": computed_userset,
                    "found_parents": [(o.entity_type, o.entity_id) for o in related_objects],
                }

                for related_obj in related_objects:
                    ttu_sub_paths: list[dict[str, Any]] = []
                    if self._compute_permission_with_explanation(
                        subject,
                        computed_userset,
                        related_obj,
                        visited.copy(),
                        depth + 1,
                        ttu_sub_paths,
                    ):
                        path_entry["granted"] = True
                        path_entry["sub_paths"] = ttu_sub_paths
                        paths.append(path_entry)
                        return True

            paths.append(path_entry)
            return False

        # Direct relation check
        tuple_info = self._find_direct_relation_tuple(subject, permission, obj)
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
            logger.warning(
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
            return self._has_direct_relation(subject, permission, obj, context, tenant_id)

        # P0-1: Use explicit permission-to-userset mapping (Zanzibar-style)
        # Check if permission is defined via "permissions" config (new way)
        if namespace.has_permission(permission):
            # Permission defined explicitly - check all usersets that grant it
            usersets = namespace.get_permission_usersets(permission)
            for userset in usersets:
                if self._compute_permission(
                    subject, userset, obj, visited.copy(), depth + 1, context, tenant_id
                ):
                    return True
            return False

        # Fallback: Check if permission is defined as a relation (legacy)
        rel_config = namespace.get_relation_config(permission)
        if not rel_config:
            # Permission not defined in namespace - check for direct relation
            return self._has_direct_relation(subject, permission, obj, context, tenant_id)

        # Handle union (OR of multiple relations)
        if namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            for rel in union_relations:
                if self._compute_permission(
                    subject, rel, obj, visited.copy(), depth + 1, context, tenant_id
                ):
                    return True
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
            if ttu:
                tupleset_relation = ttu["tupleset"]
                computed_userset = ttu["computedUserset"]

                # Find all objects related via tupleset
                related_objects = self._find_related_objects(obj, tupleset_relation)

                # Check if subject has computed_userset on any related object
                for related_obj in related_objects:
                    if self._compute_permission(
                        subject,
                        computed_userset,
                        related_obj,
                        visited.copy(),
                        depth + 1,
                        context,
                        tenant_id,
                    ):
                        return True

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
        result = self._find_direct_relation_tuple(subject, relation, obj, context, tenant_id)
        return result is not None

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
        with self._connection() as conn:
            cursor = conn.cursor()

            # BUGFIX: Use >= instead of > for exact expiration boundary
            # Check 1: Direct concrete subject (subject_relation IS NULL)
            # ABAC: Fetch conditions column to evaluate context
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

            row = cursor.fetchone()
            if row:
                # Tuple exists - now check conditions if context provided
                conditions_json = row["conditions"] if hasattr(row, "keys") else row[8]

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
                if hasattr(row, "keys"):
                    return dict(row)
                else:
                    return {
                        "tuple_id": row[0],
                        "subject_type": row[1],
                        "subject_id": row[2],
                        "subject_relation": row[3],
                        "relation": row[4],
                        "object_type": row[5],
                        "object_id": row[6],
                        "conditions": row[7],
                        "expires_at": row[8],
                    }

            # Check 2: Wildcard/public access
            # Check if wildcard subject (*:*) has the relation (public access)
            # Avoid infinite recursion by only checking wildcard if subject is not already wildcard
            if (subject.entity_type, subject.entity_id) != WILDCARD_SUBJECT:
                wildcard_entity = Entity(WILDCARD_SUBJECT[0], WILDCARD_SUBJECT[1])
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
                row = cursor.fetchone()
                if row:
                    if hasattr(row, "keys"):
                        return dict(row)
                    else:
                        return {
                            "tuple_id": row[0],
                            "subject_type": row[1],
                            "subject_id": row[2],
                            "subject_relation": row[3],
                            "relation": row[4],
                            "object_type": row[5],
                            "object_id": row[6],
                            "conditions": row[7],
                            "expires_at": row[8],
                        }

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
                        if hasattr(row, "keys"):
                            return dict(row)
                        else:
                            return {
                                "tuple_id": row[0],
                                "subject_type": row[1],
                                "subject_id": row[2],
                                "subject_relation": row[3],
                                "relation": row[4],
                                "object_type": row[5],
                                "object_id": row[6],
                                "conditions": row[7],
                                "expires_at": row[8],
                            }

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
            cursor = conn.cursor()

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
                if hasattr(row, "keys"):
                    results.append(
                        (row["subject_type"], row["subject_id"], row["subject_relation"])
                    )
                else:
                    results.append((row[0], row[1], row[2]))
            return results

    def _find_related_objects(self, obj: Entity, relation: str) -> list[Entity]:
        """Find all objects related to obj via relation.

        Args:
            obj: Object entity
            relation: Relation type

        Returns:
            List of related object entities
        """
        with self._connection() as conn:
            cursor = conn.cursor()

            # BUGFIX: Use >= instead of > for exact expiration boundary
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
                if hasattr(row, "keys"):
                    results.append(Entity(row["subject_type"], row["subject_id"]))
                else:
                    results.append(Entity(row[0], row[1]))
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
            cursor = conn.cursor()

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
                if hasattr(row, "keys"):
                    results.append((row["subject_type"], row["subject_id"]))
                else:
                    results.append((row[0], row[1]))
            return results

    def _get_cached_check(self, subject: Entity, permission: str, obj: Entity) -> bool | None:
        """Get cached permission check result.

        Args:
            subject: Subject entity
            permission: Permission
            obj: Object entity

        Returns:
            Cached result or None if not cached or expired
        """
        with self._connection() as conn:
            cursor = conn.cursor()

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
                result = row["result"] if hasattr(row, "keys") else row[0]
                return bool(result)
            return None

    def _cache_check_result(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        result: bool,
        tenant_id: str | None = None,
    ) -> None:
        """Cache permission check result.

        Args:
            subject: Subject entity
            permission: Permission
            obj: Object entity
            result: Check result
            tenant_id: Optional tenant ID for multi-tenant isolation
        """
        cache_id = str(uuid.uuid4())
        computed_at = datetime.now(UTC)
        expires_at = computed_at + timedelta(seconds=self.cache_ttl_seconds)

        # Use "default" tenant if not specified (for backward compatibility)
        effective_tenant_id = tenant_id if tenant_id is not None else "default"

        with self._connection() as conn:
            cursor = conn.cursor()

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

    def _invalidate_cache_for_tuple(
        self, subject: Entity, relation: str, obj: Entity, tenant_id: str | None = None
    ) -> None:
        """Invalidate cache entries affected by tuple change.

        When a tuple is added or removed, we need to invalidate cache entries that
        might be affected. This uses PRECISE invalidation to minimize cache churn:

        1. Direct: Invalidate (subject, *, object) - permissions on this specific pair
        2. Transitive (if subject has subject_relation): Invalidate members of this group
        3. Transitive (for object): Invalidate derived permissions on related objects

        PERFORMANCE FIX: Previous implementation invalidated ALL cache entries for
        the subject and object, causing massive cache churn. This implementation is
        much more precise, invalidating only entries that could actually be affected.

        Args:
            subject: Subject entity
            relation: Relation type (used for precise invalidation)
            obj: Object entity
            tenant_id: Optional tenant ID for tenant-scoped invalidation
        """
        # Use "default" tenant if not specified
        effective_tenant_id = tenant_id if tenant_id is not None else "default"

        with self._connection() as conn:
            cursor = conn.cursor()

            # 1. DIRECT: Invalidate cache entries for this specific subject-object pair
            #    This handles direct permission checks like "can alice read doc1"
            #    IMPORTANT: Also filter by tenant_id for proper isolation
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
            has_subject_relation = row and (
                row["subject_relation"] if hasattr(row, "keys") else row[0]
            )

            if has_subject_relation:
                # This is a group-based permission - invalidate all cache for this object
                # because we don't know who's in the group without expensive queries
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

            conn.commit()

    def _invalidate_cache_for_namespace(self, object_type: str) -> None:
        """Invalidate all cache entries for objects of a given type.

        When a namespace configuration is updated, all cached permission checks
        for objects of that type may be stale and must be invalidated.

        Args:
            object_type: Type of object whose namespace was updated
        """
        with self._connection() as conn:
            cursor = conn.cursor()

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
            logger.info(
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
            cursor = conn.cursor()

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
            cursor = conn.cursor()

            # Get expired tuples for changelog
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT tuple_id, subject_type, subject_id, relation, object_type, object_id, tenant_id
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
                # Handle both dict-like and tuple access
                if hasattr(row, "keys"):
                    tuple_id = row["tuple_id"]
                    subject_type = row["subject_type"]
                    subject_id = row["subject_id"]
                    relation = row["relation"]
                    object_type = row["object_type"]
                    object_id = row["object_id"]
                    tenant_id = row["tenant_id"]
                else:
                    tuple_id = row[0]
                    subject_type = row[1]
                    subject_id = row[2]
                    relation = row[3]
                    object_type = row[4]
                    object_id = row[5]
                    tenant_id = row[6]

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
                subject = Entity(subject_type, subject_id)
                obj = Entity(object_type, object_id)
                self._invalidate_cache_for_tuple(subject, relation, obj, tenant_id)

            conn.commit()
            return len(expired_tuples)

    def close(self) -> None:
        """Close database connection.

        Note: With fresh connections, there's nothing to close here.
        Connections are closed immediately after each operation.
        """
        pass
