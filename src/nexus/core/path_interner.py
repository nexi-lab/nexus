"""Path interning for memory-efficient path storage (Issue #912).

Many file paths share common prefixes (e.g., `/tenant1/user1/projects/...`).
Storing full paths wastes memory. This module provides:

1. PathInterner: Simple string interning for O(1) equality and reduced allocations
2. SegmentedPathInterner: Advanced prefix deduplication via path segment interning

Memory savings:
- Simple interning: 10-20x reduction (4-byte int vs 50-100 byte string)
- Segmented interning: Additional 50-60% for deeply nested paths with shared prefixes

References:
- JuiceFS memory optimization: 90% reduction via compact formats
- Rust nexus_fast: Already uses string-interner for permissions (lib.rs:26-29)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


class PathInterner:
    """Thread-safe path string interning for memory efficiency.

    Stores each unique path string once and returns integer IDs for O(1) equality
    checks and reduced memory usage.

    Usage:
        interner = PathInterner()
        id1 = interner.intern("/workspace/project/file.txt")
        id2 = interner.intern("/workspace/project/file.txt")
        assert id1 == id2  # Same path -> same ID (O(1) comparison)

        path = interner.get(id1)  # Retrieve original string

    Thread Safety:
        All operations are protected by a reentrant lock for safe concurrent access.

    Memory Layout:
        - _strings: dict[str, int] - Forward mapping (path -> id)
        - _ids: list[str] - Reverse mapping (id -> path)
        - Each interned path: 4-byte int ID vs 50-100 byte string
    """

    __slots__ = ("_strings", "_ids", "_lock")

    def __init__(self) -> None:
        """Initialize an empty path interner."""
        self._strings: dict[str, int] = {}
        self._ids: list[str] = []
        self._lock = threading.RLock()

    def intern(self, path: str) -> int:
        """Intern a path string, returning its unique integer ID.

        If the path was previously interned, returns the existing ID.
        Otherwise, assigns a new ID and stores the path.

        Args:
            path: The file path to intern (should be normalized)

        Returns:
            Integer ID for the path (0-indexed, monotonically increasing)

        Complexity: O(1) average case (hash table lookup)
        """
        with self._lock:
            existing_id = self._strings.get(path)
            if existing_id is not None:
                return existing_id

            new_id = len(self._ids)
            self._strings[path] = new_id
            self._ids.append(path)
            return new_id

    def get(self, path_id: int) -> str:
        """Retrieve the path string for a given ID.

        Args:
            path_id: The integer ID returned by intern()

        Returns:
            The original path string

        Raises:
            IndexError: If path_id is out of range
        """
        with self._lock:
            return self._ids[path_id]

    def get_id(self, path: str) -> int | None:
        """Get the ID for a path without interning it.

        Args:
            path: The file path to look up

        Returns:
            Integer ID if path was previously interned, None otherwise
        """
        with self._lock:
            return self._strings.get(path)

    def contains(self, path: str) -> bool:
        """Check if a path has been interned.

        Args:
            path: The file path to check

        Returns:
            True if path was previously interned
        """
        with self._lock:
            return path in self._strings

    def __len__(self) -> int:
        """Return the number of interned paths."""
        with self._lock:
            return len(self._ids)

    def __iter__(self) -> Iterator[str]:
        """Iterate over all interned paths."""
        with self._lock:
            # Return a copy to avoid issues with concurrent modification
            return iter(list(self._ids))

    def clear(self) -> None:
        """Clear all interned paths."""
        with self._lock:
            self._strings.clear()
            self._ids.clear()

    def stats(self) -> dict[str, int]:
        """Get statistics about the interner.

        Returns:
            Dictionary with:
            - count: Number of interned paths
            - memory_saved_estimate: Estimated bytes saved vs storing strings
        """
        with self._lock:
            count = len(self._ids)
            # Estimate: average path is 60 bytes, we store 4-byte int + dict overhead
            # Savings = (avg_path_len - 4) * count - dict_overhead
            total_string_bytes = sum(len(p) for p in self._ids)
            # Approximate: storing full strings everywhere vs int IDs
            # Assume each path referenced ~3 times on average (cache, metadata, index)
            references_per_path = 3
            string_memory = total_string_bytes * references_per_path
            int_memory = count * 4 * references_per_path + total_string_bytes  # IDs + one copy
            memory_saved = max(0, string_memory - int_memory)

            return {
                "count": count,
                "total_string_bytes": total_string_bytes,
                "memory_saved_estimate": memory_saved,
            }


class SegmentedPathInterner:
    """Advanced path interner with prefix deduplication via segment interning.

    Instead of storing full paths, this interner:
    1. Splits paths into segments: "/a/b/c" -> ["a", "b", "c"]
    2. Interns each segment separately
    3. Stores paths as tuples of segment IDs

    This provides additional memory savings when many paths share common prefixes,
    which is typical in file systems with deep directory structures.

    Example savings for 10,000 files under `/workspace/project/src/`:
    - Full strings: 10,000 × 30 chars = 300KB for prefix alone
    - Segmented: 3 segments stored once (~50 bytes) + 10,000 × 12 bytes = ~120KB
    - Savings: ~60% reduction

    Usage:
        interner = SegmentedPathInterner()
        id1 = interner.intern("/workspace/project/src/file1.txt")
        id2 = interner.intern("/workspace/project/src/file2.txt")
        # Segments "workspace", "project", "src" stored only once
    """

    __slots__ = ("_segments", "_segment_ids", "_paths", "_path_segments", "_lock")

    def __init__(self) -> None:
        """Initialize an empty segmented path interner."""
        self._segments: dict[str, int] = {}  # segment string -> segment ID
        self._segment_ids: list[str] = []  # segment ID -> segment string
        self._paths: dict[tuple[int, ...], int] = {}  # segment ID tuple -> path ID
        self._path_segments: list[tuple[int, ...]] = []  # path ID -> segment ID tuple
        self._lock = threading.RLock()

    def _intern_segment(self, segment: str) -> int:
        """Intern a single path segment (internal, assumes lock held)."""
        existing_id = self._segments.get(segment)
        if existing_id is not None:
            return existing_id

        new_id = len(self._segment_ids)
        self._segments[segment] = new_id
        self._segment_ids.append(segment)
        return new_id

    def intern(self, path: str) -> int:
        """Intern a path, returning its unique integer ID.

        Args:
            path: The file path to intern (absolute path starting with /)

        Returns:
            Integer ID for the path
        """
        with self._lock:
            # Handle root path specially
            if path == "/":
                segments: tuple[int, ...] = ()
            else:
                # Split path into segments, filtering empty strings
                # "/a/b/c" -> ["a", "b", "c"]
                parts = [p for p in path.split("/") if p]
                segments = tuple(self._intern_segment(s) for s in parts)

            existing_id = self._paths.get(segments)
            if existing_id is not None:
                return existing_id

            new_id = len(self._path_segments)
            self._paths[segments] = new_id
            self._path_segments.append(segments)
            return new_id

    def get(self, path_id: int) -> str:
        """Retrieve the path string for a given ID.

        Args:
            path_id: The integer ID returned by intern()

        Returns:
            The original path string (reconstructed from segments)

        Raises:
            IndexError: If path_id is out of range
        """
        with self._lock:
            segments = self._path_segments[path_id]
            if not segments:
                return "/"
            return "/" + "/".join(self._segment_ids[seg_id] for seg_id in segments)

    def get_id(self, path: str) -> int | None:
        """Get the ID for a path without interning it.

        Args:
            path: The file path to look up

        Returns:
            Integer ID if path was previously interned, None otherwise
        """
        with self._lock:
            if path == "/":
                segments: tuple[int, ...] = ()
            else:
                parts = [p for p in path.split("/") if p]
                # Check if all segments are interned
                segment_ids = []
                for part in parts:
                    seg_id = self._segments.get(part)
                    if seg_id is None:
                        return None
                    segment_ids.append(seg_id)
                segments = tuple(segment_ids)

            return self._paths.get(segments)

    def contains(self, path: str) -> bool:
        """Check if a path has been interned."""
        return self.get_id(path) is not None

    def __len__(self) -> int:
        """Return the number of interned paths."""
        with self._lock:
            return len(self._path_segments)

    def clear(self) -> None:
        """Clear all interned paths and segments."""
        with self._lock:
            self._segments.clear()
            self._segment_ids.clear()
            self._paths.clear()
            self._path_segments.clear()

    def stats(self) -> dict[str, int]:
        """Get statistics about the interner.

        Returns:
            Dictionary with segment and path counts, and memory savings estimate
        """
        with self._lock:
            path_count = len(self._path_segments)
            segment_count = len(self._segment_ids)

            # Calculate memory usage
            total_segment_bytes = sum(len(s) for s in self._segment_ids)
            total_path_segments = sum(len(segs) for segs in self._path_segments)

            # Reconstruct full paths to compare
            full_path_bytes = 0
            for segments in self._path_segments:
                if not segments:
                    full_path_bytes += 1  # "/"
                else:
                    # "/" + segment1 + "/" + segment2 + ...
                    path_len = 1 + sum(len(self._segment_ids[seg_id]) + 1 for seg_id in segments)
                    full_path_bytes += path_len - 1  # Remove trailing /

            # Segmented storage: segment strings + path tuples (4 bytes per segment ID)
            segmented_memory = total_segment_bytes + total_path_segments * 4

            # Estimate savings assuming 3 references per path
            references_per_path = 3
            string_memory = full_path_bytes * references_per_path
            int_memory = path_count * 4 * references_per_path + segmented_memory

            return {
                "path_count": path_count,
                "segment_count": segment_count,
                "unique_segments": segment_count,
                "total_segment_bytes": total_segment_bytes,
                "full_path_bytes": full_path_bytes,
                "segmented_memory": segmented_memory,
                "memory_saved_estimate": max(0, string_memory - int_memory),
            }


# Global interner instances (singleton pattern)
# Use module-level instances for sharing across the application

_global_path_interner: PathInterner | None = None
_global_segmented_interner: SegmentedPathInterner | None = None
_interner_lock = threading.Lock()


def get_path_interner() -> PathInterner:
    """Get the global PathInterner instance (thread-safe singleton).

    Returns:
        The global PathInterner instance
    """
    global _global_path_interner
    if _global_path_interner is None:
        with _interner_lock:
            if _global_path_interner is None:
                _global_path_interner = PathInterner()
    return _global_path_interner


def get_segmented_interner() -> SegmentedPathInterner:
    """Get the global SegmentedPathInterner instance (thread-safe singleton).

    Returns:
        The global SegmentedPathInterner instance
    """
    global _global_segmented_interner
    if _global_segmented_interner is None:
        with _interner_lock:
            if _global_segmented_interner is None:
                _global_segmented_interner = SegmentedPathInterner()
    return _global_segmented_interner


def reset_global_interners() -> None:
    """Reset global interners (primarily for testing).

    Warning: This should only be called in tests or during shutdown.
    Existing path IDs will become invalid.
    """
    global _global_path_interner, _global_segmented_interner
    with _interner_lock:
        if _global_path_interner is not None:
            _global_path_interner.clear()
            _global_path_interner = None
        if _global_segmented_interner is not None:
            _global_segmented_interner.clear()
            _global_segmented_interner = None


@dataclass(slots=True)
class CompactFileMetadata:
    """Memory-efficient file metadata using interned paths (Issue #912).

    Uses integer path IDs instead of string paths for:
    - 10-20x memory reduction per path reference
    - O(1) path equality checks (integer comparison vs string)
    - Reduced GC pressure from fewer string allocations

    This class is designed to work alongside the existing FileMetadata class.
    Use CompactFileMetadata for in-memory storage and caching, then convert
    to FileMetadata when needed for API responses or database operations.

    Attributes:
        path_id: Interned path ID (4 bytes vs ~50-100 bytes for string)
        backend_name: Backend identifier (kept as string, few unique values)
        physical_path: Physical storage path/hash (unique per file, not interned)
        size: File size in bytes
        etag: Content hash (unique per content, not interned)
        mime_type: MIME type string (few unique values)
        created_at_ts: Creation timestamp as Unix epoch (float)
        modified_at_ts: Modification timestamp as Unix epoch (float)
        version: File version number
        zone_id: Tenant identifier (could be interned in future)
        created_by: Creator identifier
        is_directory: Whether this represents a directory
    """

    path_id: int
    backend_name: str
    physical_path: str
    size: int
    etag: str | None = None
    mime_type: str | None = None
    created_at_ts: float | None = None  # Unix timestamp instead of datetime
    modified_at_ts: float | None = None  # Unix timestamp instead of datetime
    version: int = 1
    zone_id: str | None = None
    created_by: str | None = None
    is_directory: bool = False

    def get_path(self, interner: PathInterner | None = None) -> str:
        """Get the path string from the interned ID.

        Args:
            interner: PathInterner to use. If None, uses global interner.

        Returns:
            The original path string
        """
        if interner is None:
            interner = get_path_interner()
        return interner.get(self.path_id)

    def to_file_metadata(self, interner: PathInterner | None = None) -> FileMetadata:
        """Convert to standard FileMetadata.

        Args:
            interner: PathInterner to use. If None, uses global interner.

        Returns:
            FileMetadata instance with full path string
        """
        from datetime import datetime

        from nexus.core._metadata_generated import FileMetadata

        if interner is None:
            interner = get_path_interner()

        created_at = None
        if self.created_at_ts is not None:
            created_at = datetime.fromtimestamp(self.created_at_ts, tz=UTC)

        modified_at = None
        if self.modified_at_ts is not None:
            modified_at = datetime.fromtimestamp(self.modified_at_ts, tz=UTC)

        return FileMetadata(
            path=interner.get(self.path_id),
            backend_name=self.backend_name,
            physical_path=self.physical_path,
            size=self.size,
            etag=self.etag,
            mime_type=self.mime_type,
            created_at=created_at,
            modified_at=modified_at,
            version=self.version,
            zone_id=self.zone_id,
            created_by=self.created_by,
            is_directory=self.is_directory,
        )

    @classmethod
    def from_file_metadata(
        cls,
        metadata: FileMetadata,
        interner: PathInterner | None = None,
    ) -> CompactFileMetadata:
        """Create CompactFileMetadata from standard FileMetadata.

        Args:
            metadata: Source FileMetadata instance
            interner: PathInterner to use. If None, uses global interner.

        Returns:
            CompactFileMetadata with interned path ID
        """
        if interner is None:
            interner = get_path_interner()

        path_id = interner.intern(metadata.path)

        created_at_ts = None
        if metadata.created_at is not None:
            created_at_ts = metadata.created_at.timestamp()

        modified_at_ts = None
        if metadata.modified_at is not None:
            modified_at_ts = metadata.modified_at.timestamp()

        return cls(
            path_id=path_id,
            backend_name=metadata.backend_name,
            physical_path=metadata.physical_path,
            size=metadata.size,
            etag=metadata.etag,
            mime_type=metadata.mime_type,
            created_at_ts=created_at_ts,
            modified_at_ts=modified_at_ts,
            version=metadata.version,
            zone_id=metadata.zone_id,
            created_by=metadata.created_by,
            is_directory=metadata.is_directory,
        )


# Import FileMetadata for type checking
if TYPE_CHECKING:
    from nexus.core._metadata_generated import FileMetadata
