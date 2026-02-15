"""Read Set Tracking for Query Dependencies (Issue #1166).

This module implements read set tracking to enable precise cache invalidation
and efficient subscription updates. Instead of coarse-grained path patterns
(e.g., `**/*.md`) that notify ALL subscribers, read sets track exactly which
resources each query reads, enabling targeted invalidation.

Architecture:
    - ReadSetEntry: Individual resource access record
    - ReadSet: Collection of entries for a single query/subscription
    - ReadSetRegistry: Global registry with reverse index for O(1) lookups

Inspired by:
    - Google Zanzibar: Zookie consistency tokens
    - Convex: Automatic dependency tracking
    - Skip Framework: Intrinsic invalidation
    - Incremental View Maintenance: Delta propagation

Example:
    >>> from nexus.core.read_set import ReadSet, ReadSetEntry
    >>>
    >>> # Create read set for a query
    >>> read_set = ReadSet(query_id="q1", zone_id="zone1")
    >>> read_set.record_read("file", "/inbox/a.txt", revision=10)
    >>> read_set.record_read("file", "/inbox/b.txt", revision=15)
    >>> read_set.record_read("directory", "/inbox/", revision=5)
    >>>
    >>> # Check if a write affects this read set
    >>> read_set.overlaps_with_write("/inbox/c.txt", revision=20)
    True  # Because /inbox/ directory was read
    >>>
    >>> read_set.overlaps_with_write("/docs/x.txt", revision=20)
    False  # No overlap

See also:
    - https://authzed.com/docs/spicedb/concepts/consistency (Zanzibar zookies)
    - https://docs.convex.dev/functions/query-functions (Convex reactivity)
    - https://skiplabs.io/blog/cache_invalidation (Skip invalidation)
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)


class AccessType(StrEnum):
    """Type of access performed on a resource."""

    CONTENT = "content"  # Read file content
    METADATA = "metadata"  # Read file metadata (stat)
    LIST = "list"  # List directory contents
    EXISTS = "exists"  # Check if file exists


class ResourceType(StrEnum):
    """Type of resource being accessed."""

    FILE = "file"
    DIRECTORY = "directory"
    METADATA = "metadata"


@dataclass(slots=True)
class ReadSetEntry:
    """Individual resource access record within a read set.

    Tracks a single resource read during query execution, including
    the revision at read time for staleness detection.

    Attributes:
        resource_type: Type of resource (file, directory, metadata)
        resource_id: Path or unique identifier of the resource
        revision: Filesystem revision at the time of read
        access_type: Type of access (content, metadata, list, exists)
        timestamp: When the read occurred (epoch seconds)

    Example:
        >>> entry = ReadSetEntry(
        ...     resource_type="file",
        ...     resource_id="/inbox/message.txt",
        ...     revision=42,
        ...     access_type="content"
        ... )
        >>> entry.is_stale(current_revision=50)
        True
    """

    resource_type: str
    resource_id: str
    revision: int
    access_type: str = AccessType.CONTENT
    timestamp: float = field(default_factory=time.time)

    def is_stale(self, current_revision: int) -> bool:
        """Check if this entry is stale compared to current revision.

        Args:
            current_revision: The current revision of the resource

        Returns:
            True if resource has been modified since this read
        """
        return current_revision > self.revision

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "revision": self.revision,
            "access_type": self.access_type,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReadSetEntry:
        """Create from dictionary."""
        return cls(
            resource_type=data["resource_type"],
            resource_id=data["resource_id"],
            revision=data["revision"],
            access_type=data.get("access_type", AccessType.CONTENT),
            timestamp=data.get("timestamp", time.time()),
        )


@dataclass
class ReadSet:
    """Collection of resource reads for a single query/subscription.

    Tracks all resources accessed during a query execution, enabling
    precise cache invalidation when any dependency changes.

    The read set uses internal indexing for O(1) overlap detection:
    - _path_set: Hash set of all resource paths for direct lookups
    - _directory_set: Set of directory paths for containment checks

    Attributes:
        query_id: Unique identifier for this query/subscription
        zone_id: Zone this read set belongs to
        entries: List of individual read entries
        created_at: When this read set was created (epoch seconds)
        expires_at: When this read set expires (for TTL-based cleanup)

    Example:
        >>> rs = ReadSet(query_id="sub_123", zone_id="org_acme")
        >>> rs.record_read("file", "/docs/readme.md", 10)
        >>> rs.record_read("directory", "/docs/", 10)
        >>>
        >>> # Check write overlap
        >>> rs.overlaps_with_write("/docs/new.md", 15)
        True  # Overlaps with /docs/ directory read
    """

    query_id: str
    zone_id: str
    entries: list[ReadSetEntry] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None  # None = no expiry

    # Internal indexes for O(1) lookup (not serialized)
    _path_set: set[str] = field(default_factory=set, repr=False, compare=False)
    _directory_set: set[str] = field(default_factory=set, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Rebuild indexes from entries if provided."""
        for entry in self.entries:
            self._index_entry(entry)

    def _index_entry(self, entry: ReadSetEntry) -> None:
        """Add entry to internal indexes."""
        self._path_set.add(entry.resource_id)
        if entry.resource_type == ResourceType.DIRECTORY or entry.access_type == AccessType.LIST:
            # Normalize directory path (ensure trailing slash)
            dir_path = entry.resource_id.rstrip("/") + "/"
            self._directory_set.add(dir_path)

    def record_read(
        self,
        resource_type: str,
        resource_id: str,
        revision: int,
        access_type: str = AccessType.CONTENT,
    ) -> ReadSetEntry:
        """Record a resource read.

        Args:
            resource_type: Type of resource (file, directory, metadata)
            resource_id: Path or identifier of the resource
            revision: Current revision of the resource
            access_type: Type of access performed

        Returns:
            The created ReadSetEntry
        """
        entry = ReadSetEntry(
            resource_type=resource_type,
            resource_id=resource_id,
            revision=revision,
            access_type=access_type,
        )
        self.entries.append(entry)
        self._index_entry(entry)
        return entry

    def overlaps_with_write(self, write_path: str, write_revision: int) -> bool:
        """Check if a write operation affects this read set.

        Uses O(1) hash lookups for direct path matches and O(d) for
        directory containment checks (where d = depth of write_path).

        Args:
            write_path: Path that was written to
            write_revision: Revision of the write operation

        Returns:
            True if any entry in this read set is affected by the write

        Example:
            >>> rs = ReadSet(query_id="q1", zone_id="t1")
            >>> rs.record_read("file", "/inbox/a.txt", 10)
            >>> rs.record_read("directory", "/inbox/", 5)
            >>>
            >>> rs.overlaps_with_write("/inbox/a.txt", 15)
            True  # Direct match
            >>>
            >>> rs.overlaps_with_write("/inbox/new.txt", 15)
            True  # Inside read directory
            >>>
            >>> rs.overlaps_with_write("/docs/x.txt", 15)
            False  # No overlap
        """
        # O(1) direct path match
        if write_path in self._path_set:
            # Check if write is actually newer
            for entry in self.entries:
                if entry.resource_id == write_path and write_revision > entry.revision:
                    return True
            # Path matched but revision not newer for the direct entry.
            # IMPORTANT: Do NOT return False here — the same path may also
            # be inside a read directory whose listing IS stale.
            # Fall through to directory containment check below.

        # O(d) directory containment check
        # Check if write_path is inside any directory we read
        normalized_write = write_path.rstrip("/")
        for dir_path in self._directory_set:
            if normalized_write.startswith(dir_path) or normalized_write + "/" == dir_path:
                return True

        return False

    def get_affected_entries(self, write_path: str, write_revision: int) -> list[ReadSetEntry]:
        """Get all entries affected by a write operation.

        Args:
            write_path: Path that was written to
            write_revision: Revision of the write operation

        Returns:
            List of ReadSetEntry objects that are affected
        """
        affected = []
        normalized_write = write_path.rstrip("/")

        for entry in self.entries:
            # Direct match
            if entry.resource_id == write_path and write_revision > entry.revision:
                affected.append(entry)
                continue

            # Directory containment
            if (
                entry.resource_type == ResourceType.DIRECTORY
                or entry.access_type == AccessType.LIST
            ):
                dir_path = entry.resource_id.rstrip("/") + "/"
                if normalized_write.startswith(dir_path):
                    affected.append(entry)

        return affected

    def is_expired(self) -> bool:
        """Check if this read set has expired."""
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def age_seconds(self) -> float:
        """Get age of this read set in seconds."""
        return time.time() - self.created_at

    def __len__(self) -> int:
        """Return number of entries in the read set."""
        return len(self.entries)

    def __iter__(self) -> Iterator[ReadSetEntry]:
        """Iterate over entries."""
        return iter(self.entries)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "query_id": self.query_id,
            "zone_id": self.zone_id,
            "entries": [e.to_dict() for e in self.entries],
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReadSet:
        """Create from dictionary."""
        entries = [ReadSetEntry.from_dict(e) for e in data.get("entries", [])]
        return cls(
            query_id=data["query_id"],
            zone_id=data["zone_id"],
            entries=entries,
            created_at=data.get("created_at", time.time()),
            expires_at=data.get("expires_at"),
        )

    @classmethod
    def create(cls, zone_id: str, ttl_seconds: float | None = None) -> ReadSet:
        """Factory method to create a new read set with auto-generated ID.

        Args:
            zone_id: Zone this read set belongs to
            ttl_seconds: Optional TTL in seconds (None = no expiry)

        Returns:
            New ReadSet instance
        """
        now = time.time()
        return cls(
            query_id=str(uuid.uuid4()),
            zone_id=zone_id,
            created_at=now,
            expires_at=now + ttl_seconds if ttl_seconds else None,
        )


class ReadSetRegistry:
    """Global registry of active read sets with reverse indexing.

    Provides O(1) lookup of which queries are affected by a write operation
    using a reverse index from resource paths to query IDs.

    Thread-safe for concurrent access.

    Attributes:
        _read_sets: Map of query_id -> ReadSet
        _reverse_index: Map of resource_path -> set of query_ids
        _directory_index: Map of directory_path -> set of query_ids

    Example:
        >>> registry = ReadSetRegistry()
        >>>
        >>> # Register a subscription's read set
        >>> rs = ReadSet(query_id="sub_1", zone_id="t1")
        >>> rs.record_read("file", "/inbox/a.txt", 10)
        >>> rs.record_read("directory", "/inbox/", 5)
        >>> registry.register(rs)
        >>>
        >>> # Find affected queries when a write occurs
        >>> affected = registry.get_affected_queries("/inbox/new.txt", 15)
        >>> print(affected)
        {'sub_1'}  # Because /inbox/ directory was read
    """

    def __init__(self, default_ttl_seconds: float = 3600.0):
        """Initialize the registry.

        Args:
            default_ttl_seconds: Default TTL for read sets (1 hour)
        """
        self._read_sets: dict[str, ReadSet] = {}
        self._reverse_index: dict[str, set[str]] = {}  # path -> query_ids
        self._directory_index: dict[str, set[str]] = {}  # dir_path -> query_ids
        self._zone_index: dict[str, set[str]] = {}  # zone_id -> query_ids
        self._default_ttl = default_ttl_seconds
        self._lock = threading.RLock()

        # Stats
        self._stats = {
            "registers": 0,
            "unregisters": 0,
            "lookups": 0,
            "hits": 0,
            "cleanups": 0,
        }

    def register(self, read_set: ReadSet) -> None:
        """Register a read set for a subscription/query.

        Args:
            read_set: The ReadSet to register
        """
        with self._lock:
            query_id = read_set.query_id

            # Remove old registration if exists
            if query_id in self._read_sets:
                self._unregister_internal(query_id)

            # Store read set
            self._read_sets[query_id] = read_set
            self._stats["registers"] += 1

            # Build reverse indexes
            for entry in read_set.entries:
                path = entry.resource_id

                # Path index
                if path not in self._reverse_index:
                    self._reverse_index[path] = set()
                self._reverse_index[path].add(query_id)

                # Directory index
                if (
                    entry.resource_type == ResourceType.DIRECTORY
                    or entry.access_type == AccessType.LIST
                ):
                    dir_path = path.rstrip("/") + "/"
                    if dir_path not in self._directory_index:
                        self._directory_index[dir_path] = set()
                    self._directory_index[dir_path].add(query_id)

            # Zone index
            zone_id = read_set.zone_id
            if zone_id not in self._zone_index:
                self._zone_index[zone_id] = set()
            self._zone_index[zone_id].add(query_id)

            logger.debug(
                f"[ReadSetRegistry] Registered {query_id} with {len(read_set)} entries "
                f"for zone {zone_id}"
            )

    def unregister(self, query_id: str) -> bool:
        """Unregister a read set.

        Args:
            query_id: The query ID to unregister

        Returns:
            True if found and removed, False if not found
        """
        with self._lock:
            return self._unregister_internal(query_id)

    def _unregister_internal(self, query_id: str) -> bool:
        """Internal unregister (must hold lock)."""
        if query_id not in self._read_sets:
            return False

        read_set = self._read_sets.pop(query_id)
        self._stats["unregisters"] += 1

        # Remove from reverse indexes
        for entry in read_set.entries:
            path = entry.resource_id

            if path in self._reverse_index:
                self._reverse_index[path].discard(query_id)
                if not self._reverse_index[path]:
                    del self._reverse_index[path]

            if (
                entry.resource_type == ResourceType.DIRECTORY
                or entry.access_type == AccessType.LIST
            ):
                dir_path = path.rstrip("/") + "/"
                if dir_path in self._directory_index:
                    self._directory_index[dir_path].discard(query_id)
                    if not self._directory_index[dir_path]:
                        del self._directory_index[dir_path]

        # Remove from zone index
        zone_id = read_set.zone_id
        if zone_id in self._zone_index:
            self._zone_index[zone_id].discard(query_id)
            if not self._zone_index[zone_id]:
                del self._zone_index[zone_id]

        logger.debug(f"[ReadSetRegistry] Unregistered {query_id}")
        return True

    def get_affected_queries(
        self,
        write_path: str,
        write_revision: int,
        zone_id: str | None = None,
    ) -> set[str]:
        """Get all query IDs affected by a write operation.

        Uses O(1) reverse index lookups plus O(d) directory traversal
        where d is the depth of the write path.

        Args:
            write_path: Path that was written to
            write_revision: Revision of the write operation
            zone_id: Optional zone filter (only return queries for this zone)

        Returns:
            Set of query IDs whose read sets overlap with the write
        """
        with self._lock:
            self._stats["lookups"] += 1
            affected: set[str] = set()

            # Direct path match - O(1)
            if write_path in self._reverse_index:
                for query_id in self._reverse_index[write_path]:
                    read_set = self._read_sets.get(query_id)
                    if (
                        read_set
                        and read_set.overlaps_with_write(write_path, write_revision)
                        and (zone_id is None or read_set.zone_id == zone_id)
                    ):
                        affected.add(query_id)

            # Directory containment - O(d) where d = path depth
            # Walk up the path checking each ancestor directory
            current = write_path.rstrip("/")
            while current:
                dir_path = current + "/"
                if dir_path in self._directory_index:
                    for query_id in self._directory_index[dir_path]:
                        read_set = self._read_sets.get(query_id)
                        if read_set and (zone_id is None or read_set.zone_id == zone_id):
                            affected.add(query_id)

                # Move to parent
                parent = os.path.dirname(current)
                if parent == current:
                    break
                current = parent

            if affected:
                self._stats["hits"] += 1
                logger.debug(
                    f"[ReadSetRegistry] Write to {write_path}@{write_revision} "
                    f"affects {len(affected)} queries"
                )

            return affected

    def get_read_set(self, query_id: str) -> ReadSet | None:
        """Get a read set by query ID.

        Args:
            query_id: The query ID to look up

        Returns:
            ReadSet if found, None otherwise
        """
        with self._lock:
            return self._read_sets.get(query_id)

    def get_queries_for_zone(self, zone_id: str) -> set[str]:
        """Get all query IDs for a zone.

        Args:
            zone_id: The zone ID

        Returns:
            Set of query IDs registered for this zone
        """
        with self._lock:
            return self._zone_index.get(zone_id, set()).copy()

    def cleanup_expired(self) -> int:
        """Remove expired read sets.

        Returns:
            Number of read sets removed
        """
        with self._lock:
            expired = [
                query_id for query_id, read_set in self._read_sets.items() if read_set.is_expired()
            ]

            for query_id in expired:
                self._unregister_internal(query_id)

            if expired:
                self._stats["cleanups"] += len(expired)
                logger.info(f"[ReadSetRegistry] Cleaned up {len(expired)} expired read sets")

            return len(expired)

    def clear(self) -> None:
        """Clear all registered read sets."""
        with self._lock:
            self._read_sets.clear()
            self._reverse_index.clear()
            self._directory_index.clear()
            self._zone_index.clear()
            logger.info("[ReadSetRegistry] Cleared all read sets")

    def get_stats(self) -> dict[str, Any]:
        """Get registry statistics.

        Returns:
            Dictionary with stats including counts and hit rates
        """
        with self._lock:
            lookups = self._stats["lookups"]
            hits = self._stats["hits"]
            return {
                "read_sets_count": len(self._read_sets),
                "paths_indexed": len(self._reverse_index),
                "directories_indexed": len(self._directory_index),
                "zones_count": len(self._zone_index),
                "registers": self._stats["registers"],
                "unregisters": self._stats["unregisters"],
                "lookups": lookups,
                "hits": hits,
                "hit_rate_percent": (hits / lookups * 100) if lookups > 0 else 0.0,
                "cleanups": self._stats["cleanups"],
            }

    def __len__(self) -> int:
        """Return number of registered read sets."""
        with self._lock:
            return len(self._read_sets)


# Module-level singleton for global access
_global_registry: ReadSetRegistry | None = None
_registry_lock = threading.Lock()


def get_global_registry() -> ReadSetRegistry:
    """Get or create the global ReadSetRegistry singleton.

    Returns:
        The global ReadSetRegistry instance
    """
    global _global_registry
    if _global_registry is None:
        with _registry_lock:
            if _global_registry is None:
                _global_registry = ReadSetRegistry()
    return _global_registry


def set_global_registry(registry: ReadSetRegistry | None) -> None:
    """Set the global ReadSetRegistry (for testing).

    Args:
        registry: Registry to set, or None to clear
    """
    global _global_registry
    with _registry_lock:
        _global_registry = registry
