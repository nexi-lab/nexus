"""Per-subject namespace mounts from ReBAC grants (Agent OS Phase 0, Issue #1239).

Implements the Plan 9-style namespace model where each subject sees only the paths
it has been granted access to. Unmounted paths are **invisible** (404 Not Found),
not denied (403 Forbidden). This is the namespace-as-security model.

Architecture:
    ReBAC grants (source of truth) → NamespaceManager builds mount table →
    PermissionEnforcer checks visibility → PathRouter dispatches to backend →
    ReBAC checks fine-grained permissions (defense in depth)

The namespace IS the capability set (Fuchsia/Zircon model): a subject without
a mount for /admin literally cannot name that resource.

Cache layers (Issue #1244 — dcache pattern, inspired by Linux VFS dcache):
    1. dcache: Per-(subject, path) resolution cache with O(1) dict lookup.
       Positive entries (visible=True) cached with 300s TTL.
       Negative entries (visible=False) cached with 60s TTL (security: shorter to
       ensure newly-granted paths become visible quickly).
       Key-based zone revision quantization for invalidation.
    2. Mount table: Per-subject sorted prefix set with O(log m) bisect.
       Rebuilt from rebac_list_objects() on cache miss.

Design decisions (reviewed and approved):
    - Visibility only — no permissions on MountEntry (ReBAC handles all permission questions)
    - Single rebac_list_objects() call per cache rebuild (no N+1)
    - Cache: TTLCache with zone revision quantization (same consistency as ReBAC L1)
    - O(log m) visibility check via sorted prefix set + bisect
    - Thread-safe via threading.Lock (no stampede prevention — rebuild is 1-5ms)
    - Admin/system bypass handled by PermissionEnforcer, not here
    - dcache uses separate lock from mount table (no contention between layers)
    - dcache uses key-based revision quantization (revision bucket in key, not post-lookup)
    - Negative entries use shorter TTL (60s) for security (Issue #1244)

References:
    - AGENT-OS-DEEP-RESEARCH.md Part 11 (Final Architecture), Part 10.1 (Plan 9 namespaces)
    - Issue #1239: Per-subject namespace mounts from ReBAC grants
    - Issue #1244: Namespace resolution cache — dcache pattern (Phase 0)
    - Issue #909: Zone revision quantization for cache invalidation
    - Linux VFS dcache: https://docs.kernel.org/filesystems/path-lookup.html
"""

from __future__ import annotations

import bisect
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cachetools import TTLCache

if TYPE_CHECKING:
    from nexus.core.rebac_manager_enhanced import EnhancedReBACManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MountEntry:
    """A namespace mount entry representing a visible path for a subject.

    Visibility only — no permissions field. ReBAC handles all permission questions.
    No backend/real_path — PathRouter handles routing.

    Attributes:
        virtual_path: Virtual path visible to the subject (e.g., "/workspace/project-alpha")
    """

    virtual_path: str


def build_mount_entries(object_paths: list[tuple[str, str]]) -> list[MountEntry]:
    """Build mount entries from ReBAC-granted object paths.

    Pure function — no side effects, no database access. Takes a list of
    (object_type, object_id) tuples from rebac_list_objects() and aggregates
    them into mount prefixes at the grant boundary.

    Algorithm:
        1. Extract parent directories from file paths (file grant → parent dir mount)
        2. Deduplicate by hierarchy (parent subsumes child)
        3. Sort for bisect-based lookup

    Args:
        object_paths: List of (object_type, object_id) tuples from rebac_list_objects().
            object_id is a virtual path like "/workspace/project-alpha/data.csv".

    Returns:
        Sorted list of MountEntry for bisect-based is_visible() lookup.

    Examples:
        >>> build_mount_entries([("file", "/workspace/proj/a.txt"), ("file", "/workspace/proj/b.txt")])
        [MountEntry(virtual_path='/workspace/proj')]

        >>> build_mount_entries([("file", "/workspace/a/x.txt"), ("file", "/workspace/b/y.txt")])
        [MountEntry(virtual_path='/workspace/a'), MountEntry(virtual_path='/workspace/b')]

        >>> build_mount_entries([])
        []
    """
    dirs: set[str] = set()

    for obj_type, obj_id in object_paths:
        if obj_type != "file":
            continue

        # Normalize: strip trailing slash
        path = obj_id.rstrip("/")
        if not path:
            continue

        # Determine the mount point:
        # - If path looks like a directory grant (ends with / in original, or is a
        #   top-level namespace like /workspace), mount at the path itself
        # - If path looks like a file grant, mount at its parent directory
        parent = os.path.dirname(path)
        if parent and parent != path:
            # path has a parent — could be a file or subdirectory
            # We mount at the parent directory level
            dirs.add(parent)
        else:
            # path is a root-level entry (e.g., "/workspace")
            dirs.add(path)

    # Deduplicate by hierarchy: if /workspace/a and /workspace/a/b both present,
    # keep only /workspace/a (parent subsumes child)
    sorted_dirs = sorted(dirs)
    deduplicated: list[str] = []
    for d in sorted_dirs:
        # Check if any existing entry is a prefix of this one
        if not any(d == existing or d.startswith(existing + "/") for existing in deduplicated):
            deduplicated.append(d)

    return [MountEntry(virtual_path=d) for d in deduplicated]


class NamespaceManager:
    """Per-subject namespace manager — builds and caches mount tables from ReBAC grants.

    Two-layer cache (Issue #1244 — dcache pattern):
        Layer 1 (dcache): Per-(subject, path) resolution results. O(1) lookup via dict.
            Positive entries (visible=True) and negative entries (visible=False) are
            stored in separate TTLCaches with different TTLs. Key includes revision
            bucket for automatic staleness on grant changes.
        Layer 2 (mount table): Per-subject sorted prefix set. O(log m) bisect.
            Rebuilt from rebac_list_objects() on cache miss. Pre-computes mount_paths.

    Invariant: exactly ONE rebac_list_objects() call per mount table rebuild.

    Thread-safe via two separate locks (dcache_lock + mount table lock).
    No stampede prevention — rebuild cost (1-5ms) is cheap.

    Args:
        rebac_manager: EnhancedReBACManager for rebac_list_objects() and zone revision
        cache_maxsize: Maximum number of subjects in the mount table cache (default: 10,000)
        cache_ttl: TTL in seconds for mount table cache entries (default: 300s, safety net)
        revision_window: Number of revisions per quantization bucket (default: 10)
        dcache_maxsize: Maximum entries in each dcache (positive/negative) (default: 100,000)
        dcache_positive_ttl: TTL for positive dcache entries in seconds (default: 300)
        dcache_negative_ttl: TTL for negative dcache entries in seconds (default: 60)
    """

    # Type alias for dcache key: (subject_type, subject_id, path, zone_id, revision_bucket)
    _DCacheKey = tuple[str, str, str, str | None, int]

    def __init__(
        self,
        rebac_manager: EnhancedReBACManager,
        cache_maxsize: int = 10_000,
        cache_ttl: int = 300,
        revision_window: int = 10,
        dcache_maxsize: int = 100_000,
        dcache_positive_ttl: int = 300,
        dcache_negative_ttl: int = 60,
    ) -> None:
        self._rebac_manager = rebac_manager
        self._revision_window = revision_window

        # --- Mount table cache (Layer 2) ---
        self._lock = threading.Lock()

        # Cache: (subject_type, subject_id) → (mount_entries, mount_paths, zone_revision, zone_id, grants_hash)
        # mount_paths is pre-computed [m.virtual_path for m in mount_entries] to avoid
        # re-creating this list on every is_visible() call (Issue #1244 optimization).
        # TTLCache provides both LRU eviction (maxsize) and TTL expiration (safety net)
        self._cache: TTLCache[
            tuple[str, str], tuple[list[MountEntry], list[str], int, str | None, str]
        ] = TTLCache(maxsize=cache_maxsize, ttl=cache_ttl)

        # --- dcache (Layer 1) — per-path resolution cache (Issue #1244) ---
        self._dcache_lock = threading.Lock()

        # Positive entries: path is visible to subject. Longer TTL (stable).
        self._dcache_positive: TTLCache[NamespaceManager._DCacheKey, bool] = TTLCache(
            maxsize=dcache_maxsize, ttl=dcache_positive_ttl
        )
        # Negative entries: path is NOT visible to subject. Shorter TTL (security:
        # newly-granted paths must become visible within negative TTL).
        self._dcache_negative: TTLCache[NamespaceManager._DCacheKey, bool] = TTLCache(
            maxsize=dcache_maxsize // 2, ttl=dcache_negative_ttl
        )

        # --- Metrics ---
        self._hits = 0
        self._misses = 0
        self._rebuilds = 0
        self._dcache_hits = 0
        self._dcache_misses = 0
        self._dcache_negative_hits = 0

    def get_mount_table(
        self,
        subject: tuple[str, str],
        zone_id: str | None = None,
    ) -> list[MountEntry]:
        """Get the mount table for a subject, building or refreshing from cache.

        Args:
            subject: (subject_type, subject_id) tuple, e.g., ("user", "alice")
            zone_id: Zone ID for multi-zone isolation

        Returns:
            Sorted list of MountEntry representing visible paths for this subject.
            Empty list means no paths are visible (fail-closed).
        """
        mount_entries, _mount_paths = self._get_mount_data(subject, zone_id)
        return mount_entries

    def is_visible(
        self,
        subject: tuple[str, str],
        path: str,
        zone_id: str | None = None,
    ) -> bool:
        """Check if a path is visible to a subject.

        Lookup order (Issue #1244 dcache pattern):
            1. dcache positive → return True (O(1))
            2. dcache negative → return False (O(1))
            3. Mount table bisect → cache result in dcache → return (O(log m))

        Fail-closed: if the subject has no mount entries, nothing is visible.

        Args:
            subject: (subject_type, subject_id) tuple
            path: Virtual path to check (e.g., "/workspace/project-alpha/file.txt")
            zone_id: Zone ID for multi-zone isolation

        Returns:
            True if the path is under a mounted prefix, False if invisible.
        """
        dcache_key = self._dcache_key(subject, path, zone_id)

        # Layer 1: dcache lookup (O(1))
        with self._dcache_lock:
            positive = self._dcache_positive.get(dcache_key)
            if positive is not None:
                self._dcache_hits += 1
                return True

            negative = self._dcache_negative.get(dcache_key)
            if negative is not None:
                self._dcache_hits += 1
                self._dcache_negative_hits += 1
                return False

        self._dcache_misses += 1

        # Layer 2: mount table bisect (O(log m))
        _entries, mount_paths = self._get_mount_data(subject, zone_id)
        result = self._check_path_in_mount_paths(mount_paths, path)

        # Populate dcache with result
        with self._dcache_lock:
            if result:
                self._dcache_positive[dcache_key] = True
            else:
                self._dcache_negative[dcache_key] = False

        return result

    def filter_visible(
        self,
        subject: tuple[str, str],
        paths: list[str],
        zone_id: str | None = None,
    ) -> list[str]:
        """Batch-filter paths by namespace visibility (Issue #1244).

        Optimized for filter_list() hot path: acquires dcache lock once for all
        lookups, then mount table lock only for misses. Avoids N individual
        is_visible() calls with N lock acquire/release pairs.

        Args:
            subject: (subject_type, subject_id) tuple
            paths: List of virtual paths to filter
            zone_id: Zone ID for multi-zone isolation

        Returns:
            Filtered list containing only visible paths, in the same order
            as the input list (order is preserved).
        """
        if not paths:
            return []

        # Build all dcache keys (one _get_zone_revision call for revision bucket)
        revision_bucket = self._get_current_revision_bucket(zone_id)
        subject_type, subject_id = subject

        # Per-path visibility: True=visible, False=invisible, None=need bisect
        visibility: list[bool | None] = [None] * len(paths)
        miss_indices: list[int] = []

        # Batch dcache lookup — single lock acquisition
        with self._dcache_lock:
            for i, path in enumerate(paths):
                key = (subject_type, subject_id, path, zone_id, revision_bucket)

                positive = self._dcache_positive.get(key)
                if positive is not None:
                    self._dcache_hits += 1
                    visibility[i] = True
                    continue

                negative = self._dcache_negative.get(key)
                if negative is not None:
                    self._dcache_hits += 1
                    self._dcache_negative_hits += 1
                    visibility[i] = False
                    continue

                self._dcache_misses += 1
                miss_indices.append(i)

        # Resolve misses via mount table bisect
        if miss_indices:
            _entries, mount_paths = self._get_mount_data(subject, zone_id)

            new_positive: list[NamespaceManager._DCacheKey] = []
            new_negative: list[NamespaceManager._DCacheKey] = []

            for i in miss_indices:
                result = self._check_path_in_mount_paths(mount_paths, paths[i])
                visibility[i] = result
                key = (subject_type, subject_id, paths[i], zone_id, revision_bucket)
                if result:
                    new_positive.append(key)
                else:
                    new_negative.append(key)

            # Batch dcache population — single lock acquisition
            with self._dcache_lock:
                for key in new_positive:
                    self._dcache_positive[key] = True
                for key in new_negative:
                    self._dcache_negative[key] = False

        # Build result preserving input order
        return [path for path, vis in zip(paths, visibility, strict=True) if vis]

    def invalidate_dcache(self, subject: tuple[str, str] | None = None) -> None:
        """Invalidate dcache entries.

        Safety valve for immediate invalidation. Normally, key-based revision
        quantization handles staleness automatically — entries with old revision
        buckets simply miss on lookup.

        Args:
            subject: If provided, only clear entries for this subject.
                If None, clear entire dcache.
        """
        with self._dcache_lock:
            if subject is None:
                self._dcache_positive.clear()
                self._dcache_negative.clear()
            else:
                # Scan and remove entries matching this subject
                # dcache key: (subject_type, subject_id, path, zone_id, revision_bucket)
                keys_to_remove = [
                    k for k in self._dcache_positive if k[0] == subject[0] and k[1] == subject[1]
                ]
                for k in keys_to_remove:
                    self._dcache_positive.pop(k, None)
                keys_to_remove = [
                    k for k in self._dcache_negative if k[0] == subject[0] and k[1] == subject[1]
                ]
                for k in keys_to_remove:
                    self._dcache_negative.pop(k, None)

    def invalidate(self, subject: tuple[str, str]) -> None:
        """Explicitly invalidate a subject's cached mount table and dcache entries.

        Typically not needed — zone revision quantization handles invalidation
        automatically. Use this for immediate invalidation when needed.

        Args:
            subject: (subject_type, subject_id) tuple to invalidate
        """
        with self._lock:
            self._cache.pop(subject, None)
        self.invalidate_dcache(subject)

    def invalidate_all(self) -> None:
        """Clear the entire mount table cache and dcache.

        Use sparingly — typically zone revision quantization is sufficient.
        """
        with self._lock:
            self._cache.clear()
        self.invalidate_dcache()

    def get_grants_hash(
        self,
        subject: tuple[str, str],
        zone_id: str | None = None,  # noqa: ARG002
    ) -> str | None:
        """Get the grants_hash for a subject's cached mount table.

        The grants_hash is a SHA-256 digest (truncated to 16 hex chars) of the
        sorted, canonical representation of all granted paths. This enables
        downstream caches to detect when grants have changed without rebuilding
        the full mount table (Twizzler persistent namespace pattern, Decision #14A).

        Args:
            subject: (subject_type, subject_id) tuple
            zone_id: Zone ID (unused, for future multi-zone hash partitioning)

        Returns:
            16-char hex string if cached, None if subject is not in cache.
        """
        with self._lock:
            cached = self._cache.get(subject)

        if cached is None:
            return None

        _mount_entries, _mount_paths, _revision, _zone, grants_hash = cached
        return grants_hash

    @property
    def metrics(self) -> dict[str, Any]:
        """Return cache metrics for monitoring."""
        return {
            "mount_table_hits": self._hits,
            "mount_table_misses": self._misses,
            "mount_table_rebuilds": self._rebuilds,
            "mount_table_size": len(self._cache),
            "mount_table_maxsize": self._cache.maxsize,
            "dcache_hits": self._dcache_hits,
            "dcache_misses": self._dcache_misses,
            "dcache_negative_hits": self._dcache_negative_hits,
            "dcache_positive_size": len(self._dcache_positive),
            "dcache_negative_size": len(self._dcache_negative),
            "dcache_positive_maxsize": self._dcache_positive.maxsize,
            "dcache_negative_maxsize": self._dcache_negative.maxsize,
            # Backward compatibility aliases
            "hits": self._hits,
            "misses": self._misses,
            "rebuilds": self._rebuilds,
            "cache_size": len(self._cache),
            "cache_maxsize": self._cache.maxsize,
        }

    def _dcache_key(
        self,
        subject: tuple[str, str],
        path: str,
        zone_id: str | None,
    ) -> _DCacheKey:
        """Build dcache key with revision bucket for automatic staleness.

        Key-based revision quantization (same pattern as rebac_cache.py): the revision
        bucket is part of the key, so entries with old buckets simply miss on lookup
        and get evicted by TTLCache LRU. No post-lookup freshness check needed.

        Args:
            subject: (subject_type, subject_id) tuple
            path: Virtual path
            zone_id: Zone ID

        Returns:
            5-tuple dcache key: (subject_type, subject_id, path, zone_id, revision_bucket)
        """
        revision_bucket = self._get_current_revision_bucket(zone_id)
        return (subject[0], subject[1], path, zone_id, revision_bucket)

    def _get_current_revision_bucket(self, zone_id: str | None) -> int:
        """Get the current revision bucket for key-based quantization.

        Args:
            zone_id: Zone ID to check revision for

        Returns:
            Integer revision bucket. Returns 0 on error (fail-safe: cache miss).
        """
        try:
            revision = self._rebac_manager._get_zone_revision(zone_id)
        except Exception:
            return 0
        return revision // self._revision_window

    def _get_mount_data(
        self,
        subject: tuple[str, str],
        zone_id: str | None,
    ) -> tuple[list[MountEntry], list[str]]:
        """Get (mount_entries, mount_paths) from cache, rebuilding if stale.

        Shared lookup logic used by both get_mount_table() and is_visible().
        Returns pre-computed mount_paths to avoid re-creating the list on every call.

        Args:
            subject: (subject_type, subject_id) tuple
            zone_id: Zone ID for multi-zone isolation

        Returns:
            Tuple of (mount_entries, mount_paths). Empty lists if no grants.
        """
        with self._lock:
            cached = self._cache.get(subject)

        if cached is not None:
            mount_entries, mount_paths, cached_revision, cached_zone, _grants_hash = cached
            if cached_zone == zone_id and self._is_cache_fresh(cached_revision, zone_id):
                self._hits += 1
                return mount_entries, mount_paths

        return self._rebuild_mount_table(subject, zone_id)

    @staticmethod
    def _check_path_in_mount_paths(mount_paths: list[str], path: str) -> bool:
        """O(log m) bisect check of path against sorted mount prefixes.

        Args:
            mount_paths: Sorted list of mount prefix strings
            path: Virtual path to check

        Returns:
            True if path is under a mounted prefix, False otherwise.
        """
        if not mount_paths:
            return False
        idx = bisect.bisect_right(mount_paths, path)
        if idx > 0:
            candidate = mount_paths[idx - 1]
            if path == candidate or path.startswith(candidate + "/"):
                return True
        return False

    def _rebuild_mount_table(
        self,
        subject: tuple[str, str],
        zone_id: str | None,
    ) -> tuple[list[MountEntry], list[str]]:
        """Rebuild the mount table from ReBAC grants.

        Exactly ONE rebac_list_objects() call per rebuild (invariant).

        Args:
            subject: (subject_type, subject_id) tuple
            zone_id: Zone ID for multi-zone isolation

        Returns:
            Tuple of (sorted MountEntry list, pre-computed mount_paths list)
        """
        self._misses += 1
        self._rebuilds += 1
        start = time.perf_counter()

        subject_type, subject_id = subject

        # Single query: what files can this subject read?
        # rebac_list_objects returns list of (object_type, object_id) tuples
        try:
            object_paths = self._rebac_manager.rebac_list_objects(
                subject=subject,
                permission="read",
                object_type="file",
                zone_id=zone_id,
                limit=10_000,  # Generous limit — most subjects have <1000 grants
            )
        except Exception:
            logger.exception(
                f"[NAMESPACE] Failed to rebuild mount table for {subject_type}:{subject_id}"
            )
            # Fail-closed: return empty mount table on error
            return [], []

        # Build mount entries from granted paths
        mount_entries = build_mount_entries(object_paths)

        # Pre-compute mount_paths for O(log m) bisect — avoids re-creating
        # this list on every is_visible() call (Issue #1244)
        mount_paths = [m.virtual_path for m in mount_entries]

        # Compute grants_hash — deterministic, order-independent (Decision #14A)
        sorted_paths = sorted(f"{t}:{p}" for t, p in object_paths)
        grants_hash = hashlib.sha256("|".join(sorted_paths).encode()).hexdigest()[:16]

        # Get current zone revision for cache freshness tracking
        try:
            current_revision = self._rebac_manager._get_zone_revision(zone_id)
        except Exception:
            logger.warning(f"[NAMESPACE] Failed to get zone revision for {zone_id}, using 0")
            current_revision = 0

        # Cache the result (5-tuple: mount_entries, mount_paths, revision, zone_id, grants_hash)
        with self._lock:
            self._cache[subject] = (
                mount_entries,
                mount_paths,
                current_revision,
                zone_id,
                grants_hash,
            )

        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.debug(
            f"[NAMESPACE] Rebuilt mount table for {subject_type}:{subject_id}: "
            f"{len(mount_entries)} mounts from {len(object_paths)} objects in {elapsed_ms:.1f}ms"
        )

        return mount_entries, mount_paths

    def _is_cache_fresh(self, cached_revision: int, zone_id: str | None) -> bool:
        """Check if cached revision is in the same quantization bucket as current.

        Uses the same revision quantization approach as ReBAC L1 cache (Issue #909).
        Within a revision window, the cache is considered fresh.

        Args:
            cached_revision: Revision number when cache was built
            zone_id: Zone ID to check revision for

        Returns:
            True if cache is fresh (same revision bucket), False if stale.
        """
        try:
            current_revision = self._rebac_manager._get_zone_revision(zone_id)
        except Exception:
            logger.warning(
                "[NAMESPACE] Failed to get zone revision for freshness check, treating as stale"
            )
            return False

        cached_bucket = cached_revision // self._revision_window
        current_bucket = current_revision // self._revision_window
        return cached_bucket == current_bucket
