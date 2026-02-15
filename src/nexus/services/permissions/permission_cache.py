"""Permission cache coordinator for Nexus (Issue #899).

Owns all permission caching layers:
- Tiger bitmap cache (O(1) bitmap filtering)
- Leopard directory index (cached accessible directories)
- Permission boundary cache (O(1) inheritance checks)
- Bitmap completeness cache (skip fallback when bitmap is complete)
- L1 hotspot detector (proactive cache prefetching)

Extracted from PermissionEnforcer to enable reuse across
_check_rebac_batched() and filter_list() code paths.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.services.permissions.hotspot_detector import HotspotDetector
    from nexus.services.permissions.permission_boundary_cache import PermissionBoundaryCache
    from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager

logger = logging.getLogger(__name__)


class PermissionCacheCoordinator:
    """Owns all permission caching for the permission enforcer.

    Centralizes cache management so that both _check_rebac() and filter_list()
    use the same caches, producing consistent results (Issue #899, #4A).
    """

    def __init__(
        self,
        rebac_manager: EnhancedReBACManager | None = None,
        *,
        boundary_cache: PermissionBoundaryCache | None = None,
        enable_boundary_cache: bool = True,
        hotspot_detector: HotspotDetector | None = None,
        enable_hotspot_tracking: bool = True,
        bitmap_completeness_ttl: float = 3600.0,
        leopard_dir_ttl: float = 3600.0,
    ) -> None:
        self._rebac_manager = rebac_manager

        # Issue #922: Permission boundary cache
        self._boundary_cache: PermissionBoundaryCache | None = None
        if boundary_cache is not None:
            self._boundary_cache = boundary_cache
        elif enable_boundary_cache:
            from nexus.services.permissions.permission_boundary_cache import (
                PermissionBoundaryCache,
            )

            self._boundary_cache = PermissionBoundaryCache()

        # Issue #921: Hotspot detection
        self._hotspot_detector: HotspotDetector | None = None
        if hotspot_detector is not None:
            self._hotspot_detector = hotspot_detector
        elif enable_hotspot_tracking:
            from nexus.services.permissions.hotspot_detector import HotspotDetector

            self._hotspot_detector = HotspotDetector()

        # perf19: Bitmap completeness cache
        # Tracks users whose Tiger bitmap contains ALL their permissions
        self._bitmap_completeness_cache: dict[tuple[str, str, str], tuple[bool, float]] = {}
        self._bitmap_completeness_ttl = bitmap_completeness_ttl

        # perf19: Leopard Directory Index (Option 4)
        # Caches which directories a user can access (for inheritance checks)
        self._leopard_dir_index: dict[tuple[str, str, str], tuple[set[str], float]] = {}
        self._leopard_dir_ttl = leopard_dir_ttl

    @property
    def boundary_cache(self) -> PermissionBoundaryCache | None:
        return self._boundary_cache

    @property
    def hotspot_detector(self) -> HotspotDetector | None:
        return self._hotspot_detector

    # =========================================================================
    # Tiger Bitmap Filtering
    # =========================================================================

    def try_bitmap_filter(
        self,
        paths: list[str],
        subject: tuple[str, str],
        zone_id: str,
    ) -> tuple[list[str], list[str]] | None:
        """Try O(1) Tiger bitmap filtering.

        Returns:
            (allowed, remaining) tuple, or None if bitmap not available.
        """
        tiger_cache = getattr(self._rebac_manager, "_tiger_cache", None)
        if tiger_cache is None or not paths:
            return None

        try:
            subject_type, subject_id = subject
            bitmap_bytes = tiger_cache.get_bitmap_bytes(
                subject_type=subject_type,
                subject_id=subject_id,
                permission="read",
                resource_type="file",
                zone_id=zone_id,
            )
            if bitmap_bytes is None:
                return None

            # Get path int IDs from resource map
            resource_map = tiger_cache._resource_map
            resource_keys = [("file", path) for path in paths]
            with resource_map._engine.connect() as conn:
                int_id_map = resource_map.bulk_get_int_ids(resource_keys, conn)

            path_to_int: dict[str, int] = {}
            int_to_path: dict[int, str] = {}
            for path in paths:
                int_id = int_id_map.get(("file", path))
                if int_id is not None:
                    path_to_int[path] = int_id
                    int_to_path[int_id] = path

            if not path_to_int:
                return None

            from pyroaring import BitMap as RoaringBitmap

            bitmap = RoaringBitmap.deserialize(bitmap_bytes)

            # Filter path int IDs against bitmap - O(1) per check
            filtered = [
                int_to_path[idx]
                for idx in path_to_int.values()
                if idx in bitmap and idx in int_to_path
            ]

            # Paths not in map or not in bitmap need fallback
            paths_not_in_map = [p for p in paths if p not in path_to_int]
            paths_in_map_not_granted = [p for p in paths if p in path_to_int and p not in filtered]
            remaining = paths_not_in_map + paths_in_map_not_granted

            return (filtered, remaining)

        except ImportError:
            logger.debug("[TIGER-BITMAP] pyroaring not available")
            return None
        except Exception as e:
            logger.warning(f"[TIGER-BITMAP] Bitmap filtering failed: {e}")
            return None

    # =========================================================================
    # Bitmap Completeness Check
    # =========================================================================

    def is_bitmap_complete(
        self,
        subject: tuple[str, str],
        zone_id: str,
    ) -> bool:
        """Check if this user's bitmap contains all their permissions (no dir grants)."""
        subject_type, subject_id = subject
        key = (subject_type, subject_id, zone_id)
        cached = self._bitmap_completeness_cache.get(key)
        if cached:
            is_complete, cached_at = cached
            if is_complete and (time.time() - cached_at) < self._bitmap_completeness_ttl:
                return True

        # Check if user has directory grants in Tiger cache
        tiger_cache = getattr(self._rebac_manager, "_tiger_cache", None)
        if tiger_cache is None:
            return False

        dir_bitmap_bytes = tiger_cache.get_bitmap_bytes(
            subject_type=subject_type,
            subject_id=subject_id,
            permission="read",
            resource_type="directory",
            zone_id=zone_id,
        )
        if dir_bitmap_bytes is None:
            # No directory grants -> bitmap is complete
            self._bitmap_completeness_cache[key] = (True, time.time())
            return True

        return False

    def mark_bitmap_complete(
        self,
        subject: tuple[str, str],
        zone_id: str,
    ) -> None:
        """Mark a user's bitmap as complete (all permissions are direct grants)."""
        subject_type, subject_id = subject
        key = (subject_type, subject_id, zone_id)
        self._bitmap_completeness_cache[key] = (True, time.time())

    # =========================================================================
    # Leopard Directory Index
    # =========================================================================

    def try_leopard_lookup(
        self,
        paths: list[str],
        subject: tuple[str, str],
        zone_id: str,
    ) -> tuple[list[str], list[str]]:
        """Try Leopard directory index for cached accessible directories.

        Returns:
            (allowed, remaining) tuple.
        """
        subject_type, subject_id = subject
        key = (subject_type, subject_id, zone_id)
        cached = self._leopard_dir_index.get(key)
        if not cached:
            return ([], paths)

        accessible_dirs, cached_at = cached
        if (time.time() - cached_at) >= self._leopard_dir_ttl or not accessible_dirs:
            return ([], paths)

        allowed: list[str] = []
        remaining: list[str] = []
        for p in paths:
            current = p
            found = False
            while current and current != "/":
                parent = os.path.dirname(current) or "/"
                if parent in accessible_dirs:
                    allowed.append(p)
                    found = True
                    break
                current = parent
            if not found:
                remaining.append(p)

        if allowed:
            logger.info(
                f"[LEOPARD-INDEX] Allowed {len(allowed)} paths via cached directory grants, "
                f"{len(remaining)} remaining"
            )

        return (allowed, remaining)

    def record_accessible_dirs(
        self,
        dirs: set[str],
        subject: tuple[str, str],
        zone_id: str,
    ) -> None:
        """Update Leopard index with newly discovered accessible directories."""
        if not dirs:
            return

        subject_type, subject_id = subject
        key = (subject_type, subject_id, zone_id)
        cached = self._leopard_dir_index.get(key)

        existing_dirs: set[str] = set()
        if cached and (time.time() - cached[1]) < self._leopard_dir_ttl:
            existing_dirs = cached[0]

        new_dirs = existing_dirs | dirs
        self._leopard_dir_index[key] = (new_dirs, time.time())
        logger.info(
            f"[LEOPARD-INDEX] Cached {len(dirs)} accessible directories "
            f"for {subject_type}:{subject_id} (total: {len(new_dirs)})"
        )

    # =========================================================================
    # Boundary Cache Delegation
    # =========================================================================

    def try_boundary_cache(
        self,
        path: str,
        subject: tuple[str, str],
        zone_id: str,
        permission: str,
    ) -> str | None:
        """Try boundary cache for single path. Returns granting ancestor or None."""
        if not self._boundary_cache:
            return None
        subject_type, subject_id = subject
        return self._boundary_cache.get_boundary(
            zone_id, subject_type, subject_id, permission, path
        )

    # =========================================================================
    # Hotspot Recording
    # =========================================================================

    def record_hotspot(
        self,
        subject: tuple[str, str],
        resource_type: str,
        permission: str,
        zone_id: str,
    ) -> None:
        """Record access for hotspot detection."""
        if self._hotspot_detector:
            self._hotspot_detector.record_access(
                subject_type=subject[0],
                subject_id=subject[1],
                resource_type=resource_type,
                permission=permission,
                zone_id=zone_id,
            )

    # =========================================================================
    # Invalidation
    # =========================================================================

    def invalidate(
        self,
        subject_type: str | None = None,
        subject_id: str | None = None,
        zone_id: str | None = None,
    ) -> None:
        """Invalidate caches for subject (or all)."""
        if subject_type and subject_id and zone_id:
            key = (subject_type, subject_id, zone_id)
            self._bitmap_completeness_cache.pop(key, None)
            self._leopard_dir_index.pop(key, None)
        else:
            self._bitmap_completeness_cache.clear()
            self._leopard_dir_index.clear()

    def get_boundary_cache_stats(self) -> dict[str, Any] | None:
        """Get boundary cache statistics."""
        if self._boundary_cache is None:
            return None
        return self._boundary_cache.get_stats()

    def get_hotspot_stats(self) -> dict[str, Any] | None:
        """Get hotspot detection statistics."""
        if self._hotspot_detector is None:
            return None
        return self._hotspot_detector.get_stats()
