"""Compact file metadata for memory-efficient storage at scale.

This module implements memory-optimized metadata structures targeting
~64-100 bytes per file (vs ~200-300 bytes for FileMetadata).

Issue #911: Implement CompactFileMetadata with __slots__ for 3x memory reduction.

Key optimizations:
1. __slots__ eliminates per-instance __dict__ (~100 bytes saved)
2. String interning deduplicates path/hash strings across instances
3. Unix timestamps (int) instead of datetime objects (~40 bytes saved)
4. Flag packing combines boolean/enum fields into single int

References:
- JuiceFS achieves ~100 bytes/file: https://juicefs.com/en/blog/engineering/reduce-metadata-memory-usage
- Python __slots__: https://docs.python.org/3/reference/datamodel.html#slots
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.core.metadata import FileMetadata


class StringInternPool:
    """Thread-safe string interning pool for memory deduplication.

    Maps strings to integer IDs, allowing CompactFileMetadata to store
    small integers instead of full string objects. Strings are also
    interned via sys.intern() for additional memory savings.

    Memory model:
    - Each unique string stored once in _id_to_str list
    - Each reference is just an 8-byte integer
    - At 1M files with 100K unique paths: saves ~150MB vs storing strings

    Thread safety: All operations are protected by RLock.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._id_to_str: list[str] = []
        self._str_to_id: dict[str, int] = {}

    def intern(self, s: str) -> int:
        """Intern a string and return its integer ID.

        Args:
            s: String to intern

        Returns:
            Integer ID for the string (reused if string already interned)
        """
        with self._lock:
            if s in self._str_to_id:
                return self._str_to_id[s]

            # Use sys.intern for additional memory savings on the string itself
            interned_str = sys.intern(s)
            str_id = len(self._id_to_str)
            self._id_to_str.append(interned_str)
            self._str_to_id[interned_str] = str_id
            return str_id

    def get(self, str_id: int) -> str:
        """Get string by its integer ID.

        Args:
            str_id: Integer ID returned by intern()

        Returns:
            The original string

        Raises:
            IndexError: If str_id is invalid
        """
        with self._lock:
            return self._id_to_str[str_id]

    def get_or_none(self, str_id: int) -> str | None:
        """Get string by ID, returning None for invalid IDs.

        Args:
            str_id: Integer ID, or -1 for None

        Returns:
            The string, or None if str_id is -1 or invalid
        """
        if str_id < 0:
            return None
        with self._lock:
            if str_id >= len(self._id_to_str):
                return None
            return self._id_to_str[str_id]

    def __len__(self) -> int:
        """Return number of interned strings."""
        with self._lock:
            return len(self._id_to_str)

    def memory_estimate(self) -> int:
        """Estimate memory usage in bytes."""
        with self._lock:
            # Rough estimate: 50 bytes per string average + dict overhead
            return len(self._id_to_str) * 50 + len(self._str_to_id) * 100


# Global interning pools (singleton pattern for cross-instance deduplication)
# These are module-level to maximize string sharing across all CompactFileMetadata instances
_path_pool = StringInternPool()
_backend_pool = StringInternPool()
_hash_pool = StringInternPool()
_mime_pool = StringInternPool()
_zone_pool = StringInternPool()
_user_pool = StringInternPool()


def get_intern_pools() -> dict[str, StringInternPool]:
    """Get all interning pools for debugging/stats."""
    return {
        "path": _path_pool,
        "backend": _backend_pool,
        "hash": _hash_pool,
        "mime": _mime_pool,
        "zone": _zone_pool,
        "user": _user_pool,
    }


def get_pool_stats() -> dict[str, dict[str, int]]:
    """Get memory statistics for all interning pools."""
    pools = get_intern_pools()
    return {
        name: {"count": len(pool), "memory_estimate": pool.memory_estimate()}
        for name, pool in pools.items()
    }


# Flag bit positions for packing multiple fields into single int
_FLAG_IS_DIRECTORY = 1 << 0  # Bit 0: is_directory
# Bits 1-31 reserved for future use (permissions, file type enum, etc.)


@dataclass(slots=True, frozen=True)
class CompactFileMetadata:
    """Memory-efficient file metadata using integer IDs and packed fields.

    Memory layout (~48-64 bytes vs ~200-300 bytes for FileMetadata):
    - path_id: 8 bytes (int reference to interned path)
    - backend_id: 8 bytes (int reference to interned backend)
    - physical_path_id: 8 bytes (int reference to interned physical path)
    - size: 8 bytes (file size)
    - hash_id: 8 bytes (int reference to interned hash, -1 for None)
    - mime_id: 8 bytes (int reference to interned mime, -1 for None)
    - created_ts: 8 bytes (unix timestamp, 0 for None)
    - modified_ts: 8 bytes (unix timestamp, 0 for None)
    - version: 8 bytes (version number)
    - zone_id: 8 bytes (int reference to interned zone, -1 for None)
    - user_id: 8 bytes (int reference to interned user, -1 for None)
    - flags: 8 bytes (packed booleans)

    Total: 96 bytes (still 2-3x smaller than FileMetadata due to no __dict__)

    Attributes:
        path_id: Interned path string ID
        backend_id: Interned backend name ID
        physical_path_id: Interned physical path ID
        size: File size in bytes
        hash_id: Interned content hash ID (-1 for None)
        mime_id: Interned MIME type ID (-1 for None)
        created_ts: Creation timestamp as Unix epoch seconds (0 for None)
        modified_ts: Modification timestamp as Unix epoch seconds (0 for None)
        version: Version number
        zone_id: Interned zone ID (-1 for None)
        user_id: Interned user/creator ID (-1 for None)
        flags: Packed boolean flags
    """

    path_id: int
    backend_id: int
    physical_path_id: int
    size: int
    hash_id: int  # -1 for None
    mime_id: int  # -1 for None
    created_ts: float  # Unix timestamp with microseconds, 0.0 for None
    modified_ts: float  # Unix timestamp with microseconds, 0.0 for None
    version: int
    zone_id: int  # -1 for None
    user_id: int  # -1 for None
    flags: int

    @classmethod
    def from_file_metadata(cls, metadata: FileMetadata) -> CompactFileMetadata:
        """Convert FileMetadata to CompactFileMetadata.

        Args:
            metadata: Full FileMetadata object

        Returns:
            CompactFileMetadata with interned strings and packed fields
        """
        # Intern strings (or use -1 for None)
        path_id = _path_pool.intern(metadata.path)
        backend_id = _backend_pool.intern(metadata.backend_name)
        physical_path_id = _path_pool.intern(metadata.physical_path)
        hash_id = _hash_pool.intern(metadata.etag) if metadata.etag else -1
        mime_id = _mime_pool.intern(metadata.mime_type) if metadata.mime_type else -1
        zone_id = _zone_pool.intern(metadata.zone_id) if metadata.zone_id else -1
        user_id = _user_pool.intern(metadata.created_by) if metadata.created_by else -1

        # Convert datetimes to Unix timestamps (preserve microseconds as float)
        created_ts = metadata.created_at.timestamp() if metadata.created_at else 0.0
        modified_ts = metadata.modified_at.timestamp() if metadata.modified_at else 0.0

        # Pack boolean flags
        flags = 0
        if metadata.is_directory:
            flags |= _FLAG_IS_DIRECTORY

        return cls(
            path_id=path_id,
            backend_id=backend_id,
            physical_path_id=physical_path_id,
            size=metadata.size,
            hash_id=hash_id,
            mime_id=mime_id,
            created_ts=created_ts,
            modified_ts=modified_ts,
            version=metadata.version,
            zone_id=zone_id,
            user_id=user_id,
            flags=flags,
        )

    def to_file_metadata(self) -> FileMetadata:
        """Convert back to FileMetadata.

        Returns:
            Full FileMetadata object with resolved strings and datetimes
        """
        from nexus.core.metadata import FileMetadata

        # Resolve interned strings
        path = _path_pool.get(self.path_id)
        backend_name = _backend_pool.get(self.backend_id)
        physical_path = _path_pool.get(self.physical_path_id)
        etag = _hash_pool.get_or_none(self.hash_id)
        mime_type = _mime_pool.get_or_none(self.mime_id)
        zone_id_str = _zone_pool.get_or_none(self.zone_id)
        created_by = _user_pool.get_or_none(self.user_id)

        # Convert timestamps to datetimes
        created_at = (
            datetime.fromtimestamp(self.created_ts, tz=UTC) if self.created_ts > 0 else None
        )
        modified_at = (
            datetime.fromtimestamp(self.modified_ts, tz=UTC) if self.modified_ts > 0 else None
        )

        # Unpack flags
        is_directory = bool(self.flags & _FLAG_IS_DIRECTORY)

        return FileMetadata(
            path=path,
            backend_name=backend_name,
            physical_path=physical_path,
            size=self.size,
            etag=etag,
            mime_type=mime_type,
            created_at=created_at,
            modified_at=modified_at,
            version=self.version,
            zone_id=zone_id_str,
            created_by=created_by,
            is_directory=is_directory,
        )

    @property
    def path(self) -> str:
        """Get the file path (convenience property)."""
        return _path_pool.get(self.path_id)

    @property
    def is_directory(self) -> bool:
        """Check if this is a directory."""
        return bool(self.flags & _FLAG_IS_DIRECTORY)


def clear_intern_pools() -> None:
    """Clear all interning pools. Use only for testing."""
    global _path_pool, _backend_pool, _hash_pool, _mime_pool, _zone_pool, _user_pool
    _path_pool = StringInternPool()
    _backend_pool = StringInternPool()
    _hash_pool = StringInternPool()
    _mime_pool = StringInternPool()
    _zone_pool = StringInternPool()
    _user_pool = StringInternPool()
