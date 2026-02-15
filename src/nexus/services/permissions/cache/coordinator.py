"""Cache Coordinator - Unified cache invalidation orchestrator.

Consolidates scattered cache invalidation logic from EnhancedReBACManager
into a single coordinator that manages all cache layers.

When a permission tuple is written/deleted, the coordinator ensures
all affected caches are properly invalidated in the correct order:
1. Zone graph cache (in-memory tuple cache)
2. L1 permission check cache (targeted by subject + object)
3. Boundary cache (permission inheritance boundaries)
4. Directory visibility cache (dir listing optimization)
5. Iterator cache (pagination cursors)
6. Leopard cache (transitive group closure) - via callbacks
7. Tiger cache (materialized bitmaps) - via callbacks

Related: Issue #1459 (decomposition), Issue #1077, Issue #922, Issue #919
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from nexus.services.permissions.cache.boundary import PermissionBoundaryCache
    from nexus.services.permissions.cache.iterator import IteratorCache
    from nexus.services.permissions.cache.result_cache import ReBACPermissionCache

logger = logging.getLogger(__name__)

# Relations that map to common permission names for boundary cache invalidation
_RELATION_TO_PERMISSIONS: dict[str, list[str]] = {
    "owner": ["read", "write", "admin", "owner"],
    "direct_owner": ["read", "write", "admin", "owner"],
    "editor": ["read", "write"],
    "writer": ["read", "write"],
    "can_write": ["read", "write"],
    "viewer": ["read"],
    "reader": ["read"],
    "can_read": ["read"],
    "admin": ["read", "write", "admin"],
    "member": ["read"],
    "member-of": ["read"],
}


class CacheCoordinator:
    """Unified cache invalidation orchestrator.

    Replaces scattered invalidation calls in EnhancedReBACManager with
    a single entry point for cache coherence.

    Example:
        coordinator = CacheCoordinator(
            l1_cache=l1_cache,
            boundary_cache=boundary_cache,
            iterator_cache=iterator_cache,
        )

        # On write:
        coordinator.invalidate_for_write(
            zone_id="default",
            subject=("user", "alice"),
            relation="editor",
            object=("file", "/doc.txt"),
        )
    """

    def __init__(
        self,
        l1_cache: ReBACPermissionCache | None = None,
        boundary_cache: PermissionBoundaryCache | None = None,
        iterator_cache: IteratorCache | None = None,
        zone_graph_cache: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the coordinator.

        Args:
            l1_cache: L1 in-memory permission check cache
            boundary_cache: Permission boundary cache
            iterator_cache: Paginated query iterator cache
            zone_graph_cache: Zone tuple graph cache dict (shared reference)
        """
        self._l1_cache = l1_cache
        self._boundary_cache = boundary_cache
        self._iterator_cache = iterator_cache
        self._zone_graph_cache = zone_graph_cache

        # Callback registries for external caches (boundary, visibility, etc.)
        self._boundary_invalidators: list[
            tuple[str, Callable[[str, str, str, str, str], None]]
        ] = []
        self._visibility_invalidators: list[tuple[str, Callable[[str, str], None]]] = []

        # Metrics
        self._invalidation_count = 0
        self._zone_graph_invalidations = 0
        self._l1_invalidations = 0
        self._boundary_invalidations = 0
        self._visibility_invalidations = 0
        self._iterator_invalidations = 0

    # ------------------------------------------------------------------
    # Cache setters (for lazy initialization)
    # ------------------------------------------------------------------

    def set_l1_cache(self, cache: ReBACPermissionCache) -> None:
        """Set the L1 permission check cache."""
        self._l1_cache = cache

    def set_boundary_cache(self, cache: PermissionBoundaryCache) -> None:
        """Set the boundary cache."""
        self._boundary_cache = cache

    def set_iterator_cache(self, cache: IteratorCache) -> None:
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
        callback: Callable[[str, str, str, str, str], None],
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
        callback: Callable[[str, str], None],
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

    # ------------------------------------------------------------------
    # Unified invalidation
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

        # 5. Iterator cache (zone-level)
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
    # Internal helpers
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
        permissions = _RELATION_TO_PERMISSIONS.get(relation, [relation])

        self._boundary_invalidations += 1

        for callback_id, callback in self._boundary_invalidators:
            for permission in permissions:
                try:
                    callback(zone_id, subject_type, subject_id, permission, object_id)
                except Exception:
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
            except Exception:
                logger.debug(
                    "[CacheCoordinator] Visibility invalidator %s failed for %s",
                    callback_id,
                    object_id,
                )

    def _invalidate_iterator(self, zone_id: str) -> None:
        """Invalidate iterator cache for a zone."""
        if self._iterator_cache is None:
            return

        self._iterator_invalidations += 1
        self._iterator_cache.invalidate_zone(zone_id)

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Get coordinator statistics."""
        return {
            "total_invalidations": self._invalidation_count,
            "zone_graph_invalidations": self._zone_graph_invalidations,
            "l1_invalidations": self._l1_invalidations,
            "boundary_invalidations": self._boundary_invalidations,
            "visibility_invalidations": self._visibility_invalidations,
            "iterator_invalidations": self._iterator_invalidations,
            "registered_boundary_invalidators": len(self._boundary_invalidators),
            "registered_visibility_invalidators": len(self._visibility_invalidators),
        }

    def reset_stats(self) -> None:
        """Reset metrics counters."""
        self._invalidation_count = 0
        self._zone_graph_invalidations = 0
        self._l1_invalidations = 0
        self._boundary_invalidations = 0
        self._visibility_invalidations = 0
        self._iterator_invalidations = 0
