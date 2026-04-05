"""Cache data models for connector backends.

Defines data structures for cache entries, sync results, and cached reads.
Originally extracted from cache logic (#1628) to separate data definitions.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

# Backend version constant for immutable content (e.g., Gmail emails that never change)
IMMUTABLE_VERSION = "immutable"

# Maximum file size to cache (default 100MB)
MAX_CACHE_FILE_SIZE: int = 100 * 1024 * 1024

# Maximum text size to store as 'full' (default 10MB)
MAX_FULL_TEXT_SIZE: int = 10 * 1024 * 1024

# Summary size for large files (default 100KB)
SUMMARY_SIZE: int = 100 * 1024


@dataclass
class SyncResult:
    """Result of a sync operation."""

    files_scanned: int = 0
    files_synced: int = 0
    files_skipped: int = 0
    bytes_synced: int = 0
    embeddings_generated: int = 0
    errors: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"SyncResult(scanned={self.files_scanned}, synced={self.files_synced}, "
            f"skipped={self.files_skipped}, bytes={self.bytes_synced}, "
            f"embeddings={self.embeddings_generated}, errors={len(self.errors)})"
        )


@dataclass
class CacheEntry:
    """A cached content entry with lazy loading.

    The content_binary field uses lazy loading - raw bytes are stored
    in _content_binary_raw and only assigned when content_binary is accessed.
    This avoids memory overhead when content isn't actually read.
    """

    cache_id: str
    path_id: str
    content_text: str | None
    _content_binary: bytes | None  # Binary content (cached after first access)
    content_hash: str
    content_type: str
    original_size: int
    cached_size: int
    backend_version: str | None
    synced_at: datetime
    stale: bool
    parsed_from: str | None = None
    parse_metadata: dict | None = None
    _content_binary_raw: bytes | None = None  # Raw bytes for lazy loading

    @property
    def content_binary(self) -> bytes | None:
        """Get binary content (lazy load on first access)."""
        if self._content_binary is None and self._content_binary_raw:
            self._content_binary = self._content_binary_raw
        return self._content_binary

    @content_binary.setter
    def content_binary(self, value: bytes | None) -> None:
        """Set binary content directly."""
        self._content_binary = value
        self._content_binary_raw = None  # Clear raw since we have the value

    # --- Factory classmethods ---

    @classmethod
    def from_l1_content(
        cls,
        content_bytes: bytes,
        content_hash: str,
        now: datetime | None = None,
    ) -> "CacheEntry":
        """Create a CacheEntry from L1 content hit (get_content result)."""
        now = now or datetime.now(UTC)
        return cls(
            cache_id="",
            path_id="",
            content_text=None,
            _content_binary=bytes(content_bytes),
            content_hash=content_hash,
            content_type="full",
            original_size=len(content_bytes),
            cached_size=len(content_bytes),
            backend_version=None,
            synced_at=now,
            stale=False,
        )

    @classmethod
    def from_l1_metadata(
        cls,
        path_id: str,
        content_hash: str,
        original_size: int,
        now: datetime | None = None,
    ) -> "CacheEntry":
        """Create a CacheEntry from L1 metadata hit (get result)."""
        now = now or datetime.now(UTC)
        return cls(
            cache_id="",
            path_id=path_id,
            content_text=None,
            _content_binary=None,
            content_hash=content_hash,
            content_type="full",
            original_size=original_size,
            cached_size=0,
            backend_version=None,
            synced_at=now,
            stale=False,
        )

    @classmethod
    def from_disk_meta(
        cls,
        meta: dict,
        content_text: str | None = None,
        content_binary_raw: bytes | None = None,
    ) -> "CacheEntry":
        """Create a CacheEntry from disk metadata sidecar."""
        return cls(
            cache_id="",
            path_id=meta.get("path_id", ""),
            content_text=content_text,
            _content_binary=None,
            content_hash=meta.get("content_hash", ""),
            content_type=meta.get("content_type", "full"),
            original_size=meta.get("original_size", 0),
            cached_size=meta.get("cached_size", 0),
            backend_version=meta.get("backend_version"),
            synced_at=datetime.fromisoformat(meta["synced_at"])
            if meta.get("synced_at")
            else datetime.now(UTC),
            stale=meta.get("stale", False),
            parsed_from=meta.get("parsed_from"),
            parse_metadata=meta.get("parse_metadata"),
            _content_binary_raw=content_binary_raw,
        )

    @classmethod
    def from_write(
        cls,
        path_id: str,
        content: bytes,
        content_hash: str,
        content_text: str | None,
        content_type: str,
        original_size: int,
        cached_size: int,
        backend_version: str | None = None,
        parsed_from: str | None = None,
        parse_metadata: dict | None = None,
        cache_id: str = "",
        max_cache_file_size: int = MAX_CACHE_FILE_SIZE,
        now: datetime | None = None,
    ) -> "CacheEntry":
        """Create a CacheEntry from a write operation."""
        now = now or datetime.now(UTC)
        return cls(
            cache_id=cache_id,
            path_id=path_id,
            content_text=content_text,
            _content_binary=content if original_size <= max_cache_file_size else None,
            content_hash=content_hash,
            content_type=content_type,
            original_size=original_size,
            cached_size=cached_size,
            backend_version=backend_version,
            synced_at=now,
            stale=False,
            parsed_from=parsed_from,
            parse_metadata=parse_metadata,
        )


@dataclass
class CachedReadResult:
    """Result of a cached read operation.

    Contains both the content and metadata needed for HTTP caching (ETag, etc.).
    """

    content: bytes
    content_hash: str  # Can be used as ETag
    from_cache: bool  # True if served from cache, False if fetched from backend
    cache_entry: "CacheEntry | None" = None  # Full cache entry if available
