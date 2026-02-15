"""Permission Boundary Cache for O(1) inheritance checks.

This module implements caching for permission inheritance boundaries,
inspired by JuiceFS's 64MB chunk organization. Instead of walking up
the directory tree for every file (O(depth) per file), we cache the
nearest ancestor with an explicit permission grant.

Issue #922: Add permission boundary caching for O(1) inheritance checks

Architecture:
    For a file at /workspace/project/src/utils/helper.py:
    - Without cache: Check each parent until grant found (5 rebac checks)
    - With cache: Look up cached boundary → /workspace/ (1 cache lookup)

    This reduces O(n × depth) to O(n) for directory listings with n files.

Example:
    User has READ on /workspace/
    File at /workspace/project/src/utils/helper.py

    First access:
        1. Walk up tree: helper.py → utils → src → project → workspace
        2. Find grant at /workspace/
        3. Cache boundary: helper.py → /workspace/

    Subsequent access to /workspace/project/src/main.py:
        1. Walk up until cached boundary found in path
        2. /workspace/ is ancestor → check /workspace/ directly
        3. O(depth) → O(1)
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Any

from cachetools import TTLCache

if TYPE_CHECKING:
    pass

from nexus.services.permissions.utils.zone import normalize_zone_id

logger = logging.getLogger(__name__)


class PermissionBoundaryCache:
    """Cache nearest ancestor with explicit permission grant.

    Inspired by JuiceFS chunk organization - cache at logical boundaries
    to avoid repeated traversal.

    Thread-safe implementation using TTLCache with automatic expiration.

    Key structure:
        Primary key: (zone_id, subject_type, subject_id, permission)
        Value: dict[path → boundary_path]

    Example:
        >>> cache = PermissionBoundaryCache()
        >>> # User alice has READ on /workspace/
        >>> cache.set_boundary("zone1", "user", "alice", "read",
        ...     "/workspace/project/file.py", "/workspace/")
        >>> # Later lookup
        >>> boundary = cache.get_boundary("zone1", "user", "alice", "read",
        ...     "/workspace/project/other.py")
        >>> # Returns "/workspace/" because it's an ancestor
    """

    def __init__(
        self,
        max_size: int = 50_000,
        ttl_seconds: int = 300,
        enable_metrics: bool = True,
    ):
        """Initialize permission boundary cache.

        Args:
            max_size: Maximum number of subject-permission entries (default: 50k)
                Each entry can contain multiple path→boundary mappings.
            ttl_seconds: Time-to-live for cache entries (default: 300s / 5min)
            enable_metrics: Track hit rates and performance (default: True)
        """
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._enable_metrics = enable_metrics
        self._lock = threading.RLock()

        # Primary cache: (zone, subject_type, subject_id, permission) → {path: boundary}
        # Using TTLCache for automatic expiration
        self._boundaries: TTLCache[tuple[str, str, str, str], dict[str, str]] = TTLCache(
            maxsize=max_size, ttl=ttl_seconds
        )

        # Metrics
        self._hits = 0
        self._misses = 0
        self._sets = 0
        self._invalidations = 0
        self._ancestor_hits = 0  # Hits from ancestor lookup (not exact path)

    def _normalize_path(self, path: str) -> str:
        """Normalize path by removing trailing slashes (except for root)."""
        if path == "/" or not path:
            return "/"
        return path.rstrip("/")

    def get_boundary(
        self,
        zone_id: str,
        subject_type: str,
        subject_id: str,
        permission: str,
        path: str,
    ) -> str | None:
        """O(1) lookup for permission boundary.

        Checks if there's a cached boundary for this path or any ancestor.

        Args:
            zone_id: Zone ID for multi-zone isolation
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: Subject identifier
            permission: Permission type (e.g., "read", "write")
            path: File path to look up

        Returns:
            Boundary path if found (e.g., "/workspace/"), None if not cached
        """
        key = (normalize_zone_id(zone_id), subject_type, subject_id, permission)
        normalized_path = self._normalize_path(path)

        with self._lock:
            boundaries = self._boundaries.get(key)

            if boundaries is None:
                if self._enable_metrics:
                    self._misses += 1
                return None

            # Check exact path first
            if normalized_path in boundaries:
                if self._enable_metrics:
                    self._hits += 1
                    # Log every 100th hit at INFO level for visibility
                    if self._hits % 100 == 0:
                        logger.info(
                            f"[BoundaryCache] {self._hits} cache hits so far "
                            f"(hit_rate: {self._hits / (self._hits + self._misses) * 100:.1f}%)"
                        )
                logger.debug(
                    f"[BoundaryCache] HIT exact: {subject_type}:{subject_id} "
                    f"{permission} {path} → {boundaries[normalized_path]}"
                )
                return boundaries[normalized_path]

            # Check if any ancestor is a known boundary
            # This is the key optimization: if we cached /workspace/project/a.py → /workspace/
            # then /workspace/project/b.py should also resolve to /workspace/
            current = normalized_path
            while current and current != "/":
                current = os.path.dirname(current)
                if not current:
                    current = "/"

                if current in boundaries:
                    # Found boundary in ancestor path
                    # The boundary for this ancestor IS the boundary for our path too
                    boundary = boundaries[current]
                    if self._enable_metrics:
                        self._hits += 1
                        self._ancestor_hits += 1
                    logger.debug(
                        f"[BoundaryCache] HIT ancestor: {subject_type}:{subject_id} "
                        f"{permission} {path} → {boundary} (via {current})"
                    )
                    return boundary

                if current == "/":
                    break

            if self._enable_metrics:
                self._misses += 1
            return None

    def set_boundary(
        self,
        zone_id: str,
        subject_type: str,
        subject_id: str,
        permission: str,
        path: str,
        boundary_path: str,
    ) -> None:
        """Cache the boundary for this path.

        Args:
            zone_id: Zone ID for multi-zone isolation
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: Subject identifier
            permission: Permission type (e.g., "read", "write")
            path: File path that was checked
            boundary_path: The ancestor path where grant was found
        """
        key = (normalize_zone_id(zone_id), subject_type, subject_id, permission)
        normalized_path = self._normalize_path(path)
        normalized_boundary = self._normalize_path(boundary_path)

        with self._lock:
            if key not in self._boundaries:
                self._boundaries[key] = {}

            self._boundaries[key][normalized_path] = normalized_boundary

            if self._enable_metrics:
                self._sets += 1
                # Log first SET and every 50th at INFO level
                if self._sets == 1 or self._sets % 50 == 0:
                    logger.info(
                        f"[BoundaryCache] Cached {self._sets} boundaries "
                        f"(latest: {normalized_path} → {normalized_boundary})"
                    )

            logger.debug(
                f"[BoundaryCache] SET: {subject_type}:{subject_id} "
                f"{permission} {normalized_path} → {normalized_boundary}"
            )

    def invalidate_subject(
        self,
        zone_id: str,
        subject_type: str,
        subject_id: str,
    ) -> int:
        """Invalidate all boundaries for a subject (permission changed).

        Called when a subject's permissions are modified (grant/revoke).

        Args:
            zone_id: Zone ID
            subject_type: Type of subject
            subject_id: Subject identifier

        Returns:
            Number of entries invalidated
        """
        effective_zone = normalize_zone_id(zone_id)
        count = 0

        with self._lock:
            # Find all keys for this subject across all permissions
            keys_to_remove = [
                k
                for k in list(self._boundaries.keys())
                if k[0] == effective_zone and k[1] == subject_type and k[2] == subject_id
            ]

            for key in keys_to_remove:
                if key in self._boundaries:
                    count += len(self._boundaries[key])
                    del self._boundaries[key]

            if self._enable_metrics:
                self._invalidations += count

            if count > 0:
                logger.debug(
                    f"[BoundaryCache] INVALIDATE subject: {subject_type}:{subject_id} "
                    f"({count} entries)"
                )

            return count

    def invalidate_path_prefix(
        self,
        zone_id: str,
        path_prefix: str,
    ) -> int:
        """Invalidate boundaries under a path (permission grant changed).

        Called when a permission grant is added/removed at a specific path.
        This invalidates all cached boundaries that:
        1. Are under the path_prefix (descendants)
        2. Point to the path_prefix (dependents)

        Args:
            zone_id: Zone ID
            path_prefix: Path where permission changed (e.g., "/workspace/")

        Returns:
            Number of entries invalidated
        """
        effective_zone = normalize_zone_id(zone_id)
        # Normalize path prefix for consistent matching
        normalized_prefix = path_prefix.rstrip("/")
        if not normalized_prefix:
            normalized_prefix = "/"
        count = 0

        with self._lock:
            for key, boundaries in list(self._boundaries.items()):
                if key[0] != effective_zone:
                    continue

                # Find paths to remove from this subject's boundaries
                paths_to_remove = []
                for cached_path, boundary in list(boundaries.items()):
                    # Remove if:
                    # 1. cached_path starts with prefix (descendant)
                    # 2. boundary starts with prefix (pointing to changed location)
                    # 3. cached_path equals prefix
                    # 4. boundary equals prefix
                    should_remove = (
                        cached_path.startswith(normalized_prefix + "/")
                        or cached_path == normalized_prefix
                        or boundary.startswith(normalized_prefix + "/")
                        or boundary == normalized_prefix
                    )
                    if should_remove:
                        paths_to_remove.append(cached_path)

                for p in paths_to_remove:
                    del boundaries[p]
                    count += 1

                # Clean up empty entries
                if not boundaries and key in self._boundaries:
                    del self._boundaries[key]

            if self._enable_metrics:
                self._invalidations += count

            if count > 0:
                logger.debug(
                    f"[BoundaryCache] INVALIDATE path prefix: {path_prefix} ({count} entries)"
                )

            return count

    def invalidate_permission_change(
        self,
        zone_id: str,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_path: str,
    ) -> int:
        """Invalidate cache when a specific permission tuple changes.

        More precise invalidation than invalidate_subject or invalidate_path_prefix.
        Used when we know exactly which permission changed.

        Args:
            zone_id: Zone ID
            subject_type: Type of subject
            subject_id: Subject identifier
            permission: Permission that changed
            object_path: Path where permission was granted/revoked

        Returns:
            Number of entries invalidated
        """
        effective_zone = normalize_zone_id(zone_id)
        key = (effective_zone, subject_type, subject_id, permission)
        normalized_path = object_path.rstrip("/") or "/"
        count = 0

        with self._lock:
            if key not in self._boundaries:
                return 0

            boundaries = self._boundaries[key]
            paths_to_remove = []

            for cached_path, boundary in list(boundaries.items()):
                # Invalidate if:
                # 1. The cached path is under the changed path
                # 2. The boundary points to or under the changed path
                should_remove = (
                    cached_path.startswith(normalized_path + "/")
                    or cached_path == normalized_path
                    or boundary.startswith(normalized_path + "/")
                    or boundary == normalized_path
                )
                if should_remove:
                    paths_to_remove.append(cached_path)

            for p in paths_to_remove:
                del boundaries[p]
                count += 1

            # Clean up empty entries
            if not boundaries:
                del self._boundaries[key]

            if self._enable_metrics:
                self._invalidations += count

            if count > 0:
                logger.debug(
                    f"[BoundaryCache] INVALIDATE permission: {subject_type}:{subject_id} "
                    f"{permission} @ {object_path} ({count} entries)"
                )

            return count

    def clear(self) -> None:
        """Clear all cache entries."""
        with self._lock:
            self._boundaries.clear()
            logger.info("[BoundaryCache] Cache cleared")

    def get_stats(self) -> dict[str, Any]:
        """Get cache statistics.

        Returns:
            Dictionary with cache statistics including hit rate
        """
        with self._lock:
            total_requests = self._hits + self._misses
            hit_rate = (self._hits / total_requests * 100) if total_requests > 0 else 0.0
            ancestor_hit_rate = (self._ancestor_hits / self._hits * 100) if self._hits > 0 else 0.0

            # Count total path mappings across all subjects
            total_mappings = sum(len(b) for b in self._boundaries.values())

            return {
                "max_size": self._max_size,
                "current_subjects": len(self._boundaries),
                "total_mappings": total_mappings,
                "ttl_seconds": self._ttl_seconds,
                "hits": self._hits,
                "misses": self._misses,
                "ancestor_hits": self._ancestor_hits,
                "ancestor_hit_rate_percent": round(ancestor_hit_rate, 2),
                "sets": self._sets,
                "invalidations": self._invalidations,
                "hit_rate_percent": round(hit_rate, 2),
                "total_requests": total_requests,
                "enable_metrics": self._enable_metrics,
            }

    def reset_stats(self) -> None:
        """Reset metrics counters."""
        with self._lock:
            self._hits = 0
            self._misses = 0
            self._sets = 0
            self._invalidations = 0
            self._ancestor_hits = 0
            logger.info("[BoundaryCache] Stats reset")
