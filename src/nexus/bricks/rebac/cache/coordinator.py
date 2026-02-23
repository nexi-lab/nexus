"""Cache Coordinator - Unified cache invalidation orchestrator.

Consolidates scattered cache invalidation logic from ReBACManager
into a single coordinator that manages all cache layers.

When a permission tuple is written/deleted, the coordinator ensures
all affected caches are properly invalidated in the correct order:
1. Zone graph cache (in-memory tuple cache)
2. L1 permission check cache (targeted by subject + object)
3. Boundary cache (permission inheritance boundaries)
4. Directory visibility cache (dir listing optimization)
5. Iterator cache (pagination cursors)
6. Namespace cache — dcache + mount table (Issue #1244)
7. Leopard cache (transitive group closure) - via callbacks
8. Tiger cache (materialized bitmaps) - via callbacks

Also handles:
- L2 (database) cache invalidation with precise targeting (Issue #2179 Step 2.5)
- Eager cache recomputation for simple direct relations (PR #969)
- Expired tuple/cache cleanup (maintenance)
- Cache statistics (monitoring)

Related: Issue #1459 (decomposition), Issue #2179 (rebac-brick), Issue #1244, Issue #1077
"""

import logging
import time as time_module
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.bricks.rebac.domain import RELATION_TO_PERMISSIONS
from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping

    from nexus.bricks.rebac.cache.boundary import PermissionBoundaryCache
    from nexus.bricks.rebac.cache.iterator import IteratorCache
    from nexus.bricks.rebac.cache.result_cache import ReBACPermissionCache
    from nexus.bricks.rebac.domain import Entity

logger = logging.getLogger(__name__)


class CacheCoordinator:
    """Unified cache invalidation orchestrator.

    Replaces scattered invalidation calls in ReBACManager with
    a single entry point for cache coherence.

    Example:
        coordinator = CacheCoordinator(
            l1_cache=l1_cache,
            boundary_cache=boundary_cache,
            iterator_cache=iterator_cache,
        )

        # On write:
        coordinator.invalidate_for_write(
            zone_id="root",
            subject=("user", "alice"),
            relation="editor",
            object=("file", "/doc.txt"),
        )
    """

    def __init__(
        self,
        l1_cache: "ReBACPermissionCache | None" = None,
        boundary_cache: "PermissionBoundaryCache | None" = None,
        iterator_cache: "IteratorCache | None" = None,
        zone_graph_cache: "MutableMapping[str, Any] | None" = None,
        *,
        # Database access callbacks (Issue #2179 Step 2.5)
        connection_factory: "Callable[..., Any] | None" = None,
        get_connection: "Callable[[], Any] | None" = None,
        close_connection: "Callable[[Any], None] | None" = None,
        create_cursor: "Callable[[Any], Any] | None" = None,
        fix_sql: "Callable[[str], str] | None" = None,
        # Eager recompute callbacks
        get_namespace_cb: "Callable[[str], Any] | None" = None,
        compute_permission_cb: "Callable[..., bool] | None" = None,
        cache_check_result_cb: "Callable[..., None] | None" = None,
        # Stats / cleanup
        cache_ttl_seconds: int = 300,
        get_tuple_version: "Callable[[], int] | None" = None,
        set_tuple_version: "Callable[[int], None] | None" = None,
    ) -> None:
        """Initialize the coordinator.

        Args:
            l1_cache: L1 in-memory permission check cache
            boundary_cache: Permission boundary cache
            iterator_cache: Paginated query iterator cache
            zone_graph_cache: Zone tuple graph cache dict (shared reference)
            connection_factory: Context manager for database connections
            get_connection: Get a raw DBAPI connection
            close_connection: Close a raw DBAPI connection
            create_cursor: Create a cursor from a connection
            fix_sql: Adapt SQL placeholders for the DB dialect
            get_namespace_cb: Lookup namespace config by object type
            compute_permission_cb: Compute a permission (for eager recompute)
            cache_check_result_cb: Store a check result in cache
            cache_ttl_seconds: L2 cache TTL for stats reporting
            get_tuple_version: Get current tuple version counter
            set_tuple_version: Set tuple version counter
        """
        self._l1_cache = l1_cache
        self._boundary_cache = boundary_cache
        self._iterator_cache = iterator_cache
        self._zone_graph_cache = zone_graph_cache

        # Database access
        self._connection_factory = connection_factory
        self._get_connection = get_connection
        self._close_connection = close_connection
        self._create_cursor = create_cursor
        self._fix_sql = fix_sql

        # Eager recompute
        self._get_namespace_cb = get_namespace_cb
        self._compute_permission_cb = compute_permission_cb
        self._cache_check_result_cb = cache_check_result_cb

        # Stats / cleanup
        self._cache_ttl_seconds = cache_ttl_seconds
        self._get_tuple_version = get_tuple_version
        self._set_tuple_version = set_tuple_version

        # Callback registries for external caches (boundary, visibility, etc.)
        self._boundary_invalidators: list[
            tuple[str, Callable[[str, str, str, str, str], None]]
        ] = []
        self._visibility_invalidators: list[tuple[str, Callable[[str, str], None]]] = []
        # Namespace cache invalidators: callback(subject_type, subject_id, zone_id)
        # Used by NamespaceManager to invalidate dcache + mount table on grant/revoke (Issue #1244)
        self._namespace_invalidators: list[tuple[str, Callable[[str, str, str], None]]] = []

        # Metrics
        self._invalidation_count = 0
        self._zone_graph_invalidations = 0
        self._l1_invalidations = 0
        self._boundary_invalidations = 0
        self._visibility_invalidations = 0
        self._namespace_invalidations = 0
        self._iterator_invalidations = 0

    # ------------------------------------------------------------------
    # Cache setters (for lazy initialization)
    # ------------------------------------------------------------------

    def set_l1_cache(self, cache: "ReBACPermissionCache") -> None:
        """Set the L1 permission check cache."""
        self._l1_cache = cache

    def set_boundary_cache(self, cache: "PermissionBoundaryCache") -> None:
        """Set the boundary cache."""
        self._boundary_cache = cache

    def set_iterator_cache(self, cache: "IteratorCache") -> None:
        """Set the iterator cache."""
        self._iterator_cache = cache

    def set_zone_graph_cache(self, cache: dict[str, Any]) -> None:
        """Set the zone graph cache (shared dict reference)."""
        self._zone_graph_cache = cache

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def register_boundary_invalidator(
        self,
        callback_id: str,
        callback: "Callable[[str, str, str, str, str], None]",
    ) -> None:
        """Register a boundary cache invalidation callback.

        Args:
            callback_id: Unique identifier for this callback
            callback: Function(zone_id, subject_type, subject_id, permission, object_path)
        """
        for cid, _ in self._boundary_invalidators:
            if cid == callback_id:
                return  # Already registered
        self._boundary_invalidators.append((callback_id, callback))

    def unregister_boundary_invalidator(self, callback_id: str) -> bool:
        """Unregister a boundary cache invalidation callback."""
        for i, (cid, _) in enumerate(self._boundary_invalidators):
            if cid == callback_id:
                self._boundary_invalidators.pop(i)
                return True
        return False

    def register_visibility_invalidator(
        self,
        callback_id: str,
        callback: "Callable[[str, str], None]",
    ) -> None:
        """Register a directory visibility cache invalidation callback.

        Args:
            callback_id: Unique identifier for this callback
            callback: Function(zone_id, object_path)
        """
        for cid, _ in self._visibility_invalidators:
            if cid == callback_id:
                return  # Already registered
        self._visibility_invalidators.append((callback_id, callback))

    def unregister_visibility_invalidator(self, callback_id: str) -> bool:
        """Unregister a visibility cache invalidation callback."""
        for i, (cid, _) in enumerate(self._visibility_invalidators):
            if cid == callback_id:
                self._visibility_invalidators.pop(i)
                return True
        return False

    def register_namespace_invalidator(
        self,
        callback_id: str,
        callback: "Callable[[str, str, str], None]",
    ) -> None:
        """Register a namespace cache invalidation callback (Issue #1244).

        Called on every rebac_write/rebac_delete to immediately invalidate the
        affected subject's dcache + mount table entries.

        Args:
            callback_id: Unique identifier for this callback
            callback: Function(subject_type, subject_id, zone_id)
        """
        for cid, _ in self._namespace_invalidators:
            if cid == callback_id:
                return  # Already registered
        self._namespace_invalidators.append((callback_id, callback))

    def unregister_namespace_invalidator(self, callback_id: str) -> bool:
        """Unregister a namespace cache invalidation callback."""
        for i, (cid, _) in enumerate(self._namespace_invalidators):
            if cid == callback_id:
                self._namespace_invalidators.pop(i)
                return True
        return False

    # ------------------------------------------------------------------
    # Unified invalidation (L1 + callback-based)
    # ------------------------------------------------------------------

    def invalidate_for_write(
        self,
        zone_id: str,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],  # noqa: A002
    ) -> None:
        """Invalidate all caches after a permission write.

        This is the single entry point that replaces scattered invalidation
        calls across rebac_write(), rebac_write_batch(), and rebac_delete().

        Args:
            zone_id: Zone where the tuple was written
            subject: (subject_type, subject_id)
            relation: Relation that was written
            object: (object_type, object_id)
        """
        self._invalidation_count += 1
        subject_type, subject_id = subject
        object_type, object_id = object

        # 1. Zone graph cache
        self._invalidate_zone_graph(zone_id)

        # 2. L1 permission check cache (targeted)
        self._invalidate_l1(subject_type, subject_id, object_type, object_id, zone_id)

        # 3. Boundary cache (external callbacks)
        self._notify_boundary_invalidators(
            zone_id, subject_type, subject_id, relation, object_type, object_id
        )

        # 4. Directory visibility cache (external callbacks)
        self._notify_visibility_invalidators(zone_id, object_type, object_id)

        # 5. Namespace cache — dcache + mount table (Issue #1244)
        self.notify_namespace_invalidators(zone_id, subject_type, subject_id)

        # 6. Iterator cache (zone-level)
        self._invalidate_iterator(zone_id)

    def invalidate_zone_graph(self, zone_id: str | None = None) -> None:
        """Invalidate zone graph cache.

        Public method for direct zone graph invalidation (e.g., cross-zone shares).

        Args:
            zone_id: Specific zone to invalidate, or None to clear all
        """
        self._invalidate_zone_graph(zone_id)

    def invalidate_all(self, zone_id: str | None = None) -> None:
        """Nuclear option: invalidate all caches for a zone (or all zones).

        Use sparingly - prefer targeted invalidation via invalidate_for_write().

        Args:
            zone_id: Zone to invalidate, or None for all zones
        """
        self._invalidate_zone_graph(zone_id)

        if self._l1_cache:
            self._l1_cache.clear()

        if self._boundary_cache:
            self._boundary_cache.clear()

        if self._iterator_cache:
            if zone_id:
                self._iterator_cache.invalidate_zone(zone_id)
            else:
                self._iterator_cache.clear()

    # ------------------------------------------------------------------
    # Deep invalidation (L1 + L2 database) — Issue #2179 Step 2.5
    # ------------------------------------------------------------------

    def invalidate_for_tuple_change(
        self,
        subject: "Entity",
        relation: str,
        obj: "Entity",
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
            conn: Optional database connection to reuse
        """
        effective_zone_id = zone_id if zone_id is not None else ROOT_ZONE_ID

        # Track write for adaptive TTL (Phase 4)
        if self._l1_cache:
            self._l1_cache.track_write(obj.entity_id)

        # Use provided connection or create new one (avoids SQLite lock contention)
        should_close = conn is None
        if conn is None and self._get_connection is not None:
            conn = self._get_connection()
        if conn is None:
            return  # No DB access configured

        try:
            cursor = self._create_cursor(conn) if self._create_cursor else None
            if cursor is None:
                return

            # 1. DIRECT: For simple direct relations, try to eagerly recompute
            should_eager_recompute = (
                expires_at is None
                and subject_relation is None
                and relation not in ("member-of", "member", "parent")
                and subject.entity_type != "*"
                and subject.entity_id != "*"
            )

            # BUG FIX (PR #969): ALWAYS invalidate L1 cache first
            if self._l1_cache:
                self._l1_cache.invalidate_subject_object_pair(
                    subject.entity_type,
                    subject.entity_id,
                    obj.entity_type,
                    obj.entity_id,
                    zone_id,
                )

            if should_eager_recompute:
                self._eager_recompute(subject, relation, obj, zone_id, conn)

            # If we didn't do eager recomputation, invalidate L2 cache
            if not should_eager_recompute and self._fix_sql:
                cursor.execute(
                    self._fix_sql(
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

            # 2. TRANSITIVE (Groups): If subject is a group/set, invalidate cache
            #    for potential members accessing the object
            if self._fix_sql:
                cursor.execute(
                    self._fix_sql(
                        """
                        SELECT subject_relation FROM rebac_tuples
                        WHERE subject_type = ? AND subject_id = ?
                          AND relation = ?
                          AND object_type = ? AND object_id = ?
                        LIMIT 1
                        """
                    ),
                    (
                        subject.entity_type,
                        subject.entity_id,
                        relation,
                        obj.entity_type,
                        obj.entity_id,
                    ),
                )
                row = cursor.fetchone()
                has_subject_relation = row and row["subject_relation"]

                if has_subject_relation:
                    if self._l1_cache:
                        self._l1_cache.invalidate_object(obj.entity_type, obj.entity_id, zone_id)
                    cursor.execute(
                        self._fix_sql(
                            """
                            DELETE FROM rebac_check_cache
                            WHERE zone_id = ?
                              AND object_type = ? AND object_id = ?
                            """
                        ),
                        (effective_zone_id, obj.entity_type, obj.entity_id),
                    )

            # 3. TRANSITIVE (Hierarchy): membership changes invalidate subject's permissions
            if relation in ("member-of", "member", "parent"):
                if self._l1_cache:
                    self._l1_cache.invalidate_subject(
                        subject.entity_type, subject.entity_id, zone_id
                    )
                if self._fix_sql:
                    cursor.execute(
                        self._fix_sql(
                            """
                            DELETE FROM rebac_check_cache
                            WHERE zone_id = ?
                              AND subject_type = ? AND subject_id = ?
                            """
                        ),
                        (effective_zone_id, subject.entity_type, subject.entity_id),
                    )

            # 4. PARENT PERMISSION CHANGE: invalidate child paths
            if obj.entity_type == "file" and relation in (
                "direct_owner",
                "direct_editor",
                "direct_viewer",
                "owner",
                "editor",
                "viewer",
                "shared-viewer",
                "shared-editor",
                "shared-owner",
            ):
                if self._l1_cache:
                    self._l1_cache.invalidate_object_prefix(obj.entity_type, obj.entity_id, zone_id)
                if self._fix_sql:
                    cursor.execute(
                        self._fix_sql(
                            """
                            DELETE FROM rebac_check_cache
                            WHERE zone_id = ?
                              AND object_type = ?
                              AND (object_id = ? OR object_id LIKE ?)
                            """
                        ),
                        (
                            effective_zone_id,
                            obj.entity_type,
                            obj.entity_id,
                            obj.entity_id + "/%",
                        ),
                    )
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "Invalidated cache for %s and all children (parent permission change)",
                            obj,
                        )

            # 5. USERSET-AS-SUBJECT: clear ALL cache (aggressive but safe)
            if subject_relation is not None:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Userset-as-subject detected (%s#%s), clearing ALL cache for safety",
                        subject,
                        subject_relation,
                    )
                if self._l1_cache:
                    self._l1_cache.clear()
                if self._fix_sql:
                    cursor.execute(
                        self._fix_sql(
                            """
                            DELETE FROM rebac_check_cache
                            WHERE zone_id = ?
                            """
                        ),
                        (effective_zone_id,),
                    )

            conn.commit()
        finally:
            if should_close and self._close_connection is not None:
                self._close_connection(conn)

    def _eager_recompute(
        self,
        subject: "Entity",
        relation: str,
        obj: "Entity",
        zone_id: str | None,
        conn: Any,
    ) -> None:
        """Eagerly recompute and cache permissions for simple direct relations.

        Instead of just invalidating, we recompute the permission so the next
        read is instant (<1ms) instead of requiring graph traversal (50-500ms).
        """
        if not (
            self._get_namespace_cb and self._compute_permission_cb and self._cache_check_result_cb
        ):
            return

        namespace = self._get_namespace_cb(obj.entity_type)
        if not (namespace and namespace.config and "relations" in namespace.config):
            return

        # Find permissions that this relation affects
        affected_permissions: list[str] = []
        relations = namespace.config.get("relations", {})
        for perm, rel_spec in relations.items():
            if isinstance(rel_spec, dict) and "union" in rel_spec and relation in rel_spec["union"]:
                affected_permissions.append(perm)

        for permission in affected_permissions[:5]:  # Limit to 5 most common
            try:
                start_time = time_module.perf_counter()
                result = self._compute_permission_cb(
                    subject,
                    permission,
                    obj,
                    set(),  # visited
                    0,  # depth
                    None,  # context
                    zone_id,
                )
                delta = time_module.perf_counter() - start_time
                self._cache_check_result_cb(subject, permission, obj, result, zone_id, conn, delta)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Eager cache update: (%s, %s, %s) = %s",
                        subject,
                        permission,
                        obj,
                        result,
                    )
            except Exception as e:  # fail-safe: fall back to invalidation
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("Eager recomputation failed, falling back to invalidation: %s", e)
                break

    def invalidate_for_namespace_change(self, object_type: str) -> None:
        """Invalidate all cache entries for objects of a given type in both L1 and L2.

        When a namespace configuration is updated, all cached permission checks
        for objects of that type may be stale and must be invalidated.

        Args:
            object_type: Type of object whose namespace was updated
        """
        # L1 cache invalidation - clear all (conservative approach)
        if self._l1_cache:
            self._l1_cache.clear()
            logger.info("Cleared L1 cache due to namespace '%s' config update", object_type)

        # L2 cache invalidation
        if self._connection_factory and self._fix_sql and self._create_cursor:
            with self._connection_factory() as conn:
                cursor = self._create_cursor(conn)
                cursor.execute(
                    self._fix_sql(
                        """
                        DELETE FROM rebac_check_cache
                        WHERE object_type = ?
                        """
                    ),
                    (object_type,),
                )
                conn.commit()
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Invalidated all cached checks for namespace '%s' "
                        "due to config update (deleted %s cache entries)",
                        object_type,
                        cursor.rowcount,
                    )

    # ------------------------------------------------------------------
    # Maintenance — Issue #2179 Step 2.5
    # ------------------------------------------------------------------

    def cleanup_expired_cache(self) -> int:
        """Remove expired cache entries.

        Returns:
            Number of cache entries removed
        """
        if not (self._connection_factory and self._fix_sql and self._create_cursor):
            return 0

        with self._connection_factory() as conn:
            cursor = self._create_cursor(conn)
            cursor.execute(
                self._fix_sql("DELETE FROM rebac_check_cache WHERE expires_at <= ?"),
                (datetime.now(UTC).isoformat(),),
            )
            conn.commit()
            return int(cursor.rowcount) if cursor.rowcount else 0

    def cleanup_expired_tuples(self) -> int:
        """Remove expired relationship tuples and invalidate their caches.

        Returns:
            Number of tuples removed
        """
        if not (self._connection_factory and self._fix_sql and self._create_cursor):
            return 0

        from nexus.bricks.rebac.domain import Entity

        with self._connection_factory() as conn:
            cursor = self._create_cursor(conn)

            # Get expired tuples for changelog
            cursor.execute(
                self._fix_sql(
                    """
                    SELECT tuple_id, subject_type, subject_id, subject_relation,
                           relation, object_type, object_id, zone_id
                    FROM rebac_tuples
                    WHERE expires_at IS NOT NULL AND expires_at <= ?
                    """
                ),
                (datetime.now(UTC).isoformat(),),
            )
            expired_tuples = cursor.fetchall()

            # Delete expired tuples
            cursor.execute(
                self._fix_sql(
                    """
                    DELETE FROM rebac_tuples
                    WHERE expires_at IS NOT NULL AND expires_at <= ?
                    """
                ),
                (datetime.now(UTC).isoformat(),),
            )

            # Log to changelog and invalidate caches for expired tuples
            for row in expired_tuples:
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
                    self._fix_sql(
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
                        zone_id or ROOT_ZONE_ID,
                        datetime.now(UTC).isoformat(),
                    ),
                )

                # Invalidate cache for this tuple
                # Pass a dummy expires_at to prevent eager recomputation during cleanup
                # FIX: Pass conn to avoid opening new connection (pool exhaustion)
                subject = Entity(subject_type, subject_id)
                obj = Entity(object_type, object_id)
                self.invalidate_for_tuple_change(
                    subject,
                    relation,
                    obj,
                    zone_id,
                    subject_relation,
                    expires_at=datetime.now(UTC),
                    conn=conn,
                )

            conn.commit()
            if expired_tuples and self._set_tuple_version and self._get_tuple_version:
                self._set_tuple_version(self._get_tuple_version() + 1)
            return len(expired_tuples)

    # ------------------------------------------------------------------
    # Stats and monitoring — Issue #2179 Step 2.5
    # ------------------------------------------------------------------

    def get_cache_stats(self) -> dict[str, Any]:
        """Get cache statistics for monitoring and debugging.

        Returns comprehensive statistics about both L1 (in-memory) and L2 (database)
        cache performance, including hit rates, sizes, and latency metrics.
        """
        stats: dict[str, Any] = {
            "l1_enabled": self._l1_cache is not None,
            "l2_enabled": True,
            "l2_ttl_seconds": self._cache_ttl_seconds,
        }

        # L1 cache stats
        if self._l1_cache:
            stats["l1_stats"] = self._l1_cache.get_stats()
        else:
            stats["l1_stats"] = None

        # L2 cache stats (query database)
        if self._connection_factory and self._fix_sql and self._create_cursor:
            with self._connection_factory(readonly=True) as conn:
                cursor = self._create_cursor(conn)
                cursor.execute(
                    self._fix_sql(
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
        else:
            stats["l2_size"] = 0

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

    # ------------------------------------------------------------------
    # Internal helpers (L1 + zone graph)
    # ------------------------------------------------------------------

    def _invalidate_zone_graph(self, zone_id: str | None = None) -> None:
        """Invalidate zone graph cache entries."""
        if self._zone_graph_cache is None:
            return

        self._zone_graph_invalidations += 1

        if zone_id is None:
            self._zone_graph_cache.clear()
        elif zone_id in self._zone_graph_cache:
            del self._zone_graph_cache[zone_id]

    def _invalidate_l1(
        self,
        subject_type: str,
        subject_id: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> None:
        """Invalidate L1 permission cache for subject and object."""
        if self._l1_cache is None:
            return

        self._l1_invalidations += 1
        self._l1_cache.invalidate_subject(subject_type, subject_id, zone_id)
        self._l1_cache.invalidate_object(object_type, object_id, zone_id)

    def _notify_boundary_invalidators(
        self,
        zone_id: str,
        subject_type: str,
        subject_id: str,
        relation: str,
        object_type: str,
        object_id: str,
    ) -> None:
        """Notify boundary cache invalidators."""
        if not self._boundary_invalidators:
            return

        # Only invalidate for file objects
        if object_type not in ("file", "memory", "resource"):
            return

        # Map relation to permissions
        permissions = RELATION_TO_PERMISSIONS.get(relation, [relation])

        self._boundary_invalidations += 1

        for callback_id, callback in self._boundary_invalidators:
            for permission in permissions:
                try:
                    callback(zone_id, subject_type, subject_id, permission, object_id)
                except Exception:  # fail-safe: callback errors must not break invalidation loop
                    logger.debug(
                        "[CacheCoordinator] Boundary invalidator %s failed for %s",
                        callback_id,
                        permission,
                    )

        # Also invalidate the internal boundary cache
        if self._boundary_cache:
            for permission in permissions:
                self._boundary_cache.invalidate_permission_change(
                    zone_id, subject_type, subject_id, permission, object_id
                )

    def _notify_visibility_invalidators(
        self,
        zone_id: str,
        object_type: str,
        object_id: str,
    ) -> None:
        """Notify directory visibility cache invalidators."""
        if not self._visibility_invalidators:
            return

        # Only invalidate for file objects
        if object_type not in ("file", "memory", "resource"):
            return

        self._visibility_invalidations += 1

        for callback_id, callback in self._visibility_invalidators:
            try:
                callback(zone_id, object_id)
            except Exception:  # fail-safe: callback errors must not break invalidation loop
                logger.debug(
                    "[CacheCoordinator] Visibility invalidator %s failed for %s",
                    callback_id,
                    object_id,
                )

    def notify_namespace_invalidators(
        self,
        zone_id: str,
        subject_type: str,
        subject_id: str,
    ) -> None:
        """Notify namespace cache invalidators (Issue #1244).

        Public entry point for namespace cache invalidation. When a permission
        tuple changes for a subject, invalidate that subject's dcache + mount
        table so visibility reflects the new grants immediately.

        Args:
            zone_id: Zone where the tuple was written
            subject_type: Type of the subject whose cache should be invalidated
            subject_id: ID of the subject whose cache should be invalidated
        """
        if not self._namespace_invalidators:
            return

        self._namespace_invalidations += 1

        for callback_id, callback in self._namespace_invalidators:
            try:
                callback(subject_type, subject_id, zone_id)
            except Exception:  # fail-safe: callback errors must not break invalidation loop
                logger.debug(
                    "[CacheCoordinator] Namespace invalidator %s failed for %s:%s",
                    callback_id,
                    subject_type,
                    subject_id,
                )

    def _invalidate_iterator(self, zone_id: str) -> None:
        """Invalidate iterator cache for a zone."""
        if self._iterator_cache is None:
            return

        self._iterator_invalidations += 1
        self._iterator_cache.invalidate_zone(zone_id)

    # ------------------------------------------------------------------
    # Coordinator metrics
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Get coordinator statistics."""
        return {
            "total_invalidations": self._invalidation_count,
            "zone_graph_invalidations": self._zone_graph_invalidations,
            "l1_invalidations": self._l1_invalidations,
            "boundary_invalidations": self._boundary_invalidations,
            "visibility_invalidations": self._visibility_invalidations,
            "namespace_invalidations": self._namespace_invalidations,
            "iterator_invalidations": self._iterator_invalidations,
            "registered_boundary_invalidators": len(self._boundary_invalidators),
            "registered_visibility_invalidators": len(self._visibility_invalidators),
            "registered_namespace_invalidators": len(self._namespace_invalidators),
        }

    def reset_stats(self) -> None:
        """Reset metrics counters."""
        self._invalidation_count = 0
        self._zone_graph_invalidations = 0
        self._l1_invalidations = 0
        self._boundary_invalidations = 0
        self._visibility_invalidations = 0
        self._namespace_invalidations = 0
        self._iterator_invalidations = 0
