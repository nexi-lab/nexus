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

Design decisions (reviewed and approved):
    - Visibility only — no permissions on MountEntry (ReBAC handles all permission questions)
    - Single rebac_list_objects() call per cache rebuild (no N+1)
    - Cache: TTLCache with zone revision quantization (same consistency as ReBAC L1)
    - O(log m) visibility check via sorted prefix set + bisect
    - Thread-safe via threading.Lock (no stampede prevention — rebuild is 1-5ms)
    - Admin/system bypass handled by PermissionEnforcer, not here

References:
    - AGENT-OS-DEEP-RESEARCH.md Part 11 (Final Architecture), Part 10.1 (Plan 9 namespaces)
    - Issue #1239: Per-subject namespace mounts from ReBAC grants
    - Issue #909: Zone revision quantization for cache invalidation
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

    Invariant: exactly ONE rebac_list_objects() call per cache rebuild.
    The mount table answers one question: "is this path visible to this subject?"
    All permission questions go to ReBAC (defense in depth).

    The cache uses zone revision quantization (same mechanism as ReBAC L1 cache,
    Issue #909) for invalidation. Within a revision window, the cached mount table
    is considered fresh. ReBAC still catches revoked access immediately at the
    fine-grained check layer.

    Thread-safe via threading.Lock. No stampede prevention — the rebuild cost
    (1-5ms) is cheap enough for concurrent rebuilds.

    Args:
        rebac_manager: EnhancedReBACManager for rebac_list_objects() and zone revision
        cache_maxsize: Maximum number of subjects in the mount table cache (default: 10,000)
        cache_ttl: TTL in seconds for cache entries (default: 300s, safety net)
        revision_window: Number of revisions per quantization bucket (default: 10)
    """

    def __init__(
        self,
        rebac_manager: EnhancedReBACManager,
        cache_maxsize: int = 10_000,
        cache_ttl: int = 300,
        revision_window: int = 10,
    ) -> None:
        self._rebac_manager = rebac_manager
        self._revision_window = revision_window
        self._lock = threading.Lock()

        # Cache: (subject_type, subject_id) → (mount_entries, zone_revision, zone_id, grants_hash)
        # TTLCache provides both LRU eviction (maxsize) and TTL expiration (safety net)
        self._cache: TTLCache[
            tuple[str, str], tuple[list[MountEntry], int, str | None, str]
        ] = TTLCache(maxsize=cache_maxsize, ttl=cache_ttl)

        # Metrics
        self._hits = 0
        self._misses = 0
        self._rebuilds = 0

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
        with self._lock:
            cached = self._cache.get(subject)

        if cached is not None:
            mount_entries, cached_revision, cached_zone, _grants_hash = cached
            # Check if cache is fresh via revision quantization
            if cached_zone == zone_id and self._is_cache_fresh(cached_revision, zone_id):
                self._hits += 1
                return mount_entries
            # Stale — fall through to rebuild

        # Cache miss or stale — rebuild
        return self._rebuild_mount_table(subject, zone_id)

    def is_visible(
        self,
        subject: tuple[str, str],
        path: str,
        zone_id: str | None = None,
    ) -> bool:
        """Check if a path is visible to a subject (O(log m) via bisect).

        This is the core namespace check. If the path is not under any mount
        entry for this subject, it is invisible (should return 404, not 403).

        Fail-closed: if the subject has no mount entries, nothing is visible.

        Args:
            subject: (subject_type, subject_id) tuple
            path: Virtual path to check (e.g., "/workspace/project-alpha/file.txt")
            zone_id: Zone ID for multi-zone isolation

        Returns:
            True if the path is under a mounted prefix, False if invisible.
        """
        mount_table = self.get_mount_table(subject, zone_id)
        if not mount_table:
            return False  # Fail-closed: no mounts → nothing visible

        # Binary search for the rightmost mount entry <= path
        mount_paths = [m.virtual_path for m in mount_table]
        idx = bisect.bisect_right(mount_paths, path)

        # Check if the entry at idx-1 is a prefix of path
        if idx > 0:
            candidate = mount_paths[idx - 1]
            if path == candidate or path.startswith(candidate + "/"):
                return True

        return False

    def invalidate(self, subject: tuple[str, str]) -> None:
        """Explicitly invalidate a subject's cached mount table.

        Typically not needed — zone revision quantization handles invalidation
        automatically. Use this for immediate invalidation when needed.

        Args:
            subject: (subject_type, subject_id) tuple to invalidate
        """
        with self._lock:
            self._cache.pop(subject, None)

    def invalidate_all(self) -> None:
        """Clear the entire mount table cache.

        Use sparingly — typically zone revision quantization is sufficient.
        """
        with self._lock:
            self._cache.clear()

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

        _mount_entries, _revision, _zone, grants_hash = cached
        return grants_hash

    @property
    def metrics(self) -> dict[str, Any]:
        """Return cache metrics for monitoring."""
        return {
            "hits": self._hits,
            "misses": self._misses,
            "rebuilds": self._rebuilds,
            "cache_size": len(self._cache),
            "cache_maxsize": self._cache.maxsize,
        }

    def _rebuild_mount_table(
        self,
        subject: tuple[str, str],
        zone_id: str | None,
    ) -> list[MountEntry]:
        """Rebuild the mount table from ReBAC grants.

        Exactly ONE rebac_list_objects() call per rebuild (invariant).

        Args:
            subject: (subject_type, subject_id) tuple
            zone_id: Zone ID for multi-zone isolation

        Returns:
            Sorted list of MountEntry
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
            return []

        # Build mount entries from granted paths
        mount_entries = build_mount_entries(object_paths)

        # Compute grants_hash — deterministic, order-independent (Decision #14A)
        sorted_paths = sorted(f"{t}:{p}" for t, p in object_paths)
        grants_hash = hashlib.sha256("|".join(sorted_paths).encode()).hexdigest()[:16]

        # Get current zone revision for cache freshness tracking
        try:
            current_revision = self._rebac_manager._get_zone_revision(zone_id)
        except Exception:
            logger.warning(f"[NAMESPACE] Failed to get zone revision for {zone_id}, using 0")
            current_revision = 0

        # Cache the result (4-tuple: mount_entries, revision, zone_id, grants_hash)
        with self._lock:
            self._cache[subject] = (mount_entries, current_revision, zone_id, grants_hash)

        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.debug(
            f"[NAMESPACE] Rebuilt mount table for {subject_type}:{subject_id}: "
            f"{len(mount_entries)} mounts from {len(object_paths)} objects in {elapsed_ms:.1f}ms"
        )

        return mount_entries

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
