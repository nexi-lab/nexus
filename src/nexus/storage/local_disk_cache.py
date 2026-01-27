"""Local disk cache for FUSE operations (Issue #1072).

Provides a persistent local SSD cache layer between in-memory caches and
remote backends (GCS, S3, PostgreSQL). Optimized for FUSE read operations
with sub-millisecond latency for cached content.

Architecture:
    L1: In-memory cache (FUSECacheManager, ContentCache)
        ↓ miss
    L2: LocalDiskCache (THIS) - SSD-backed, 10-100GB  ← NEW
        ↓ miss
    L3: FileContentCache + Redis/Dragonfly
        ↓ miss
    L4: Backend storage (GCS, S3, local)

Key features:
- Configurable size limit (default: 10GB)
- CLOCK eviction algorithm (low overhead, LRU-like behavior)
- Content-addressable storage (CAS) by SHA-256 hash
- Bloom filter for O(1) cache miss detection
- Block-level support for large file partial reads
- Access time tracking for intelligent eviction

Performance expectations:
- Cached read latency: 0.1-1ms (vs 10-50ms network)
- Throughput: 500+ MB/s (vs 50-200 MB/s network)
- Cache miss detection: O(1) via Bloom filter
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus_fast import BloomFilter

logger = logging.getLogger(__name__)

# Default configuration
DEFAULT_CACHE_DIR = "/var/cache/nexus/local"
DEFAULT_MAX_SIZE_GB = 10.0
DEFAULT_BLOCK_SIZE = 4 * 1024 * 1024  # 4MB blocks
DEFAULT_BLOOM_CAPACITY = 1_000_000
DEFAULT_BLOOM_FP_RATE = 0.01

# Metadata file format version
METADATA_VERSION = 1


@dataclass
class CacheEntry:
    """Metadata for a cached content entry."""

    content_hash: str
    size_bytes: int
    created_at: float
    last_accessed: float
    access_count: int = 1
    clock_bit: bool = True  # For CLOCK eviction algorithm
    priority: int = 0  # Higher = less likely to evict

    def touch(self) -> None:
        """Update access time and set clock bit."""
        self.last_accessed = time.time()
        self.access_count += 1
        self.clock_bit = True


@dataclass
class CacheStats:
    """Statistics for cache operations."""

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    bytes_written: int = 0
    bytes_read: int = 0
    bytes_evicted: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0


class LocalDiskCache:
    """
    Persistent local disk cache optimized for FUSE operations.

    Uses CLOCK eviction algorithm for efficient LRU-like behavior with
    minimal overhead. Content is stored by SHA-256 hash (content-addressable).

    Example:
        >>> cache = LocalDiskCache(cache_dir="/tmp/nexus-cache", max_size_gb=5.0)
        >>> cache.put("abc123...", b"file content")
        >>> content = cache.get("abc123...")  # Fast SSD read
        >>> cache.get_stats()
        {'hits': 1, 'misses': 0, 'size_bytes': 12, ...}
    """

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        max_size_gb: float = DEFAULT_MAX_SIZE_GB,
        block_size: int = DEFAULT_BLOCK_SIZE,
        bloom_capacity: int = DEFAULT_BLOOM_CAPACITY,
        bloom_fp_rate: float = DEFAULT_BLOOM_FP_RATE,
    ):
        """Initialize local disk cache.

        Args:
            cache_dir: Directory for cache storage. Defaults to /var/cache/nexus/local
                       or NEXUS_LOCAL_CACHE_DIR environment variable.
            max_size_gb: Maximum cache size in gigabytes (default: 10GB)
            block_size: Block size for large file storage (default: 4MB)
            bloom_capacity: Expected number of cached items for Bloom filter
            bloom_fp_rate: Target false positive rate for Bloom filter
        """
        # Resolve cache directory
        if cache_dir is None:
            cache_dir = os.getenv("NEXUS_LOCAL_CACHE_DIR", DEFAULT_CACHE_DIR)
        self.cache_dir = Path(cache_dir)
        self.content_dir = self.cache_dir / "content"
        self.blocks_dir = self.cache_dir / "blocks"
        self.metadata_path = self.cache_dir / "metadata.bin"

        self.max_size_bytes = int(max_size_gb * 1024 * 1024 * 1024)
        self.block_size = block_size
        self._current_size_bytes = 0

        # Thread safety
        self._lock = threading.RLock()

        # Cache entries indexed by content_hash
        self._entries: dict[str, CacheEntry] = {}

        # CLOCK eviction: circular buffer of content hashes
        self._clock_hand = 0
        self._clock_order: list[str] = []

        # Bloom filter for fast miss detection
        self._bloom: BloomFilter | None = None
        self._bloom_capacity = bloom_capacity
        self._bloom_fp_rate = bloom_fp_rate

        # Statistics
        self._stats = CacheStats()

        # Initialize
        self._ensure_dirs()
        self._init_bloom_filter()
        self._load_metadata()

        logger.info(
            f"LocalDiskCache initialized: dir={self.cache_dir}, "
            f"max_size={max_size_gb}GB, entries={len(self._entries)}, "
            f"current_size={self._current_size_bytes / (1024 * 1024):.1f}MB"
        )

    def _ensure_dirs(self) -> None:
        """Create cache directories if they don't exist."""
        self.content_dir.mkdir(parents=True, exist_ok=True)
        self.blocks_dir.mkdir(parents=True, exist_ok=True)

    def _init_bloom_filter(self) -> None:
        """Initialize Bloom filter for fast cache miss detection."""
        try:
            from nexus_fast import BloomFilter

            self._bloom = BloomFilter(self._bloom_capacity, self._bloom_fp_rate)
            logger.debug(
                f"Bloom filter initialized: capacity={self._bloom_capacity}, "
                f"fp_rate={self._bloom_fp_rate}"
            )
        except ImportError:
            logger.warning("nexus_fast not available, Bloom filter disabled")
            self._bloom = None

    def _make_cache_key(self, content_hash: str, tenant_id: str | None = None) -> str:
        """Create tenant-isolated cache key.

        For multi-tenant security, cache entries are isolated by tenant_id.
        This prevents content leakage across tenant boundaries.

        Args:
            content_hash: SHA-256 hash of content
            tenant_id: Tenant ID for isolation (None = default tenant)

        Returns:
            Cache key in format "{tenant_id}:{content_hash}" or just "{content_hash}"
        """
        if tenant_id:
            return f"{tenant_id}:{content_hash}"
        return content_hash

    def _get_content_path(self, cache_key: str) -> Path:
        """Get file path for cached content.

        Uses hash-based sharding: {key[:2]}/{key[2:4]}/{key}.bin
        This prevents too many files in a single directory.
        """
        # Use last 64 chars for path (content_hash part) to maintain sharding
        hash_part = cache_key[-64:] if len(cache_key) > 64 else cache_key
        return self.content_dir / hash_part[:2] / hash_part[2:4] / f"{cache_key}.bin"

    def _get_block_path(self, cache_key: str, block_idx: int) -> Path:
        """Get file path for a specific block of large content."""
        hash_part = cache_key[-64:] if len(cache_key) > 64 else cache_key
        return self.blocks_dir / hash_part[:2] / hash_part[2:4] / f"{cache_key}.{block_idx:04d}.bin"

    def get(self, content_hash: str, tenant_id: str | None = None) -> bytes | None:
        """Get content from cache by hash.

        Uses Bloom filter for fast miss detection, avoiding disk I/O
        for entries that definitely don't exist.

        Args:
            content_hash: SHA-256 hash of content to retrieve
            tenant_id: Tenant ID for multi-tenant isolation

        Returns:
            Content bytes if cached, None otherwise
        """
        cache_key = self._make_cache_key(content_hash, tenant_id)

        # Fast path: Bloom filter says definitely not cached
        if self._bloom is not None and not self._bloom.might_exist(cache_key):
            self._stats.misses += 1
            return None

        with self._lock:
            entry = self._entries.get(cache_key)
            if entry is None:
                self._stats.misses += 1
                return None

            # Read from disk
            content_path = self._get_content_path(cache_key)
            try:
                content = content_path.read_bytes()
                entry.touch()
                self._stats.hits += 1
                self._stats.bytes_read += len(content)
                return content
            except FileNotFoundError:
                # Entry exists in metadata but file is missing - clean up
                self._remove_entry(cache_key)
                self._stats.misses += 1
                return None
            except Exception as e:
                logger.warning(f"Failed to read cached content {cache_key}: {e}")
                self._stats.misses += 1
                return None

    def get_block(
        self, content_hash: str, block_idx: int, tenant_id: str | None = None
    ) -> bytes | None:
        """Get a specific block of large content.

        For large files, content is split into blocks for efficient
        partial reads (e.g., video seeking, archive extraction).

        Args:
            content_hash: SHA-256 hash of full content
            block_idx: Block index (0-based)
            tenant_id: Tenant ID for multi-tenant isolation

        Returns:
            Block bytes if cached, None otherwise
        """
        cache_key = self._make_cache_key(content_hash, tenant_id)

        # Fast path: Bloom filter says definitely not cached
        if self._bloom is not None and not self._bloom.might_exist(cache_key):
            self._stats.misses += 1
            return None

        with self._lock:
            entry = self._entries.get(cache_key)
            if entry is None:
                self._stats.misses += 1
                return None

            block_path = self._get_block_path(cache_key, block_idx)
            try:
                content = block_path.read_bytes()
                entry.touch()
                self._stats.hits += 1
                self._stats.bytes_read += len(content)
                return content
            except FileNotFoundError:
                # Block not cached, caller should fall back to full read
                self._stats.misses += 1
                return None
            except Exception as e:
                logger.warning(f"Failed to read cached block {cache_key}:{block_idx}: {e}")
                self._stats.misses += 1
                return None

    def put(
        self,
        content_hash: str,
        content: bytes,
        *,
        tenant_id: str | None = None,
        priority: int = 0,
        store_blocks: bool = False,
    ) -> bool:
        """Store content in cache.

        Automatically evicts least-recently-used entries if cache is full.

        Args:
            content_hash: SHA-256 hash of content
            content: Content bytes to cache
            tenant_id: Tenant ID for multi-tenant isolation
            priority: Higher priority = less likely to evict (0=normal)
            store_blocks: If True, also store as blocks for partial reads

        Returns:
            True if stored successfully, False otherwise
        """
        cache_key = self._make_cache_key(content_hash, tenant_id)
        content_size = len(content)

        # Don't cache content larger than entire cache
        if content_size > self.max_size_bytes:
            logger.debug(f"Content too large to cache: {content_size} > {self.max_size_bytes}")
            return False

        with self._lock:
            # Check if already cached
            if cache_key in self._entries:
                # Update access time
                self._entries[cache_key].touch()
                return True

            # Evict if necessary
            bytes_needed = content_size
            if self._current_size_bytes + bytes_needed > self.max_size_bytes:
                evicted = self._evict_clock(bytes_needed)
                if self._current_size_bytes + bytes_needed > self.max_size_bytes:
                    logger.warning(
                        f"Could not free enough space: needed={bytes_needed}, "
                        f"evicted={evicted}, current={self._current_size_bytes}"
                    )
                    return False

            # Write to disk
            content_path = self._get_content_path(cache_key)
            try:
                content_path.parent.mkdir(parents=True, exist_ok=True)
                content_path.write_bytes(content)
            except Exception as e:
                logger.error(f"Failed to write cached content {cache_key}: {e}")
                return False

            # Store blocks if requested (for large files)
            if store_blocks and content_size > self.block_size:
                self._store_blocks(cache_key, content)

            # Update metadata
            now = time.time()
            entry = CacheEntry(
                content_hash=cache_key,
                size_bytes=content_size,
                created_at=now,
                last_accessed=now,
                priority=priority,
            )
            self._entries[cache_key] = entry
            self._clock_order.append(cache_key)
            self._current_size_bytes += content_size
            self._stats.bytes_written += content_size

            # Add to Bloom filter
            if self._bloom is not None:
                self._bloom.add(cache_key)

            logger.debug(f"Cached {content_size} bytes: {cache_key[:16]}...")
            return True

    def _store_blocks(self, cache_key: str, content: bytes) -> None:
        """Store content as blocks for partial reads."""
        for block_idx, offset in enumerate(range(0, len(content), self.block_size)):
            block = content[offset : offset + self.block_size]
            block_path = self._get_block_path(cache_key, block_idx)
            try:
                block_path.parent.mkdir(parents=True, exist_ok=True)
                block_path.write_bytes(block)
            except Exception as e:
                logger.warning(f"Failed to write block {cache_key}:{block_idx}: {e}")

    def remove(self, content_hash: str, tenant_id: str | None = None) -> bool:
        """Remove content from cache.

        Args:
            content_hash: SHA-256 hash of content to remove
            tenant_id: Tenant ID for multi-tenant isolation

        Returns:
            True if removed, False if not found
        """
        cache_key = self._make_cache_key(content_hash, tenant_id)
        with self._lock:
            return self._remove_entry(cache_key)

    def _remove_entry(self, content_hash: str) -> bool:
        """Internal: Remove entry from cache (must hold lock)."""
        entry = self._entries.pop(content_hash, None)
        if entry is None:
            return False

        # Remove from clock order
        with contextlib.suppress(ValueError):
            self._clock_order.remove(content_hash)

        # Delete files
        content_path = self._get_content_path(content_hash)
        try:
            content_path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Failed to delete cached content {content_hash}: {e}")

        # Delete blocks if they exist
        for block_idx in range(0, 1000):  # Max 1000 blocks
            block_path = self._get_block_path(content_hash, block_idx)
            if not block_path.exists():
                break
            with contextlib.suppress(Exception):
                block_path.unlink()

        self._current_size_bytes -= entry.size_bytes
        self._stats.evictions += 1
        self._stats.bytes_evicted += entry.size_bytes

        return True

    def _evict_clock(self, bytes_needed: int) -> int:
        """Evict entries using CLOCK algorithm until enough space is freed.

        CLOCK is an efficient approximation of LRU:
        - Entries have a "clock bit" that is set on access
        - On eviction, scan entries in circular order
        - If clock bit is set, clear it and move on (give second chance)
        - If clock bit is clear, evict the entry

        Args:
            bytes_needed: Minimum bytes to free

        Returns:
            Number of bytes actually freed
        """
        bytes_freed = 0
        entries_scanned = 0
        max_scans = len(self._clock_order) * 2  # Prevent infinite loop

        while bytes_freed < bytes_needed and entries_scanned < max_scans:
            if not self._clock_order:
                break

            # Wrap around
            if self._clock_hand >= len(self._clock_order):
                self._clock_hand = 0

            content_hash = self._clock_order[self._clock_hand]
            entry = self._entries.get(content_hash)

            if entry is None:
                # Orphaned entry in clock order
                self._clock_order.pop(self._clock_hand)
                continue

            entries_scanned += 1

            # Skip high-priority entries on first pass
            if entry.priority > 0 and entries_scanned < len(self._clock_order):
                self._clock_hand += 1
                continue

            if entry.clock_bit:
                # Give second chance
                entry.clock_bit = False
                self._clock_hand += 1
            else:
                # Evict this entry
                size = entry.size_bytes
                self._remove_entry(content_hash)
                bytes_freed += size
                logger.debug(f"Evicted {content_hash[:16]}... ({size} bytes)")

        if bytes_freed > 0:
            logger.info(
                f"CLOCK eviction freed {bytes_freed} bytes ({entries_scanned} entries scanned)"
            )

        return bytes_freed

    def clear(self) -> int:
        """Clear all cached content.

        Returns:
            Number of entries cleared
        """
        with self._lock:
            count = len(self._entries)

            # Delete all files
            try:
                shutil.rmtree(self.content_dir, ignore_errors=True)
                shutil.rmtree(self.blocks_dir, ignore_errors=True)
            except Exception as e:
                logger.warning(f"Failed to clear cache directories: {e}")

            # Reset state
            self._entries.clear()
            self._clock_order.clear()
            self._clock_hand = 0
            self._current_size_bytes = 0

            # Reinitialize Bloom filter
            self._init_bloom_filter()

            # Recreate directories
            self._ensure_dirs()

            logger.info(f"LocalDiskCache cleared: {count} entries removed")
            return count

    def exists(self, content_hash: str, tenant_id: str | None = None) -> bool:
        """Check if content is cached (without reading).

        Uses Bloom filter for fast negative lookups.

        Args:
            content_hash: SHA-256 hash to check
            tenant_id: Tenant ID for multi-tenant isolation

        Returns:
            True if cached, False otherwise
        """
        cache_key = self._make_cache_key(content_hash, tenant_id)

        # Fast path: Bloom filter says definitely not cached
        if self._bloom is not None and not self._bloom.might_exist(cache_key):
            return False

        with self._lock:
            return cache_key in self._entries

    def get_stats(self) -> dict[str, Any]:
        """Get cache statistics.

        Returns:
            Dictionary with cache statistics
        """
        with self._lock:
            return {
                "entries": len(self._entries),
                "size_bytes": self._current_size_bytes,
                "size_mb": self._current_size_bytes / (1024 * 1024),
                "max_size_mb": self.max_size_bytes / (1024 * 1024),
                "utilization": self._current_size_bytes / self.max_size_bytes
                if self.max_size_bytes > 0
                else 0,
                "hits": self._stats.hits,
                "misses": self._stats.misses,
                "hit_rate": self._stats.hit_rate,
                "evictions": self._stats.evictions,
                "bytes_written": self._stats.bytes_written,
                "bytes_read": self._stats.bytes_read,
                "bytes_evicted": self._stats.bytes_evicted,
            }

    def warm(self, content_hashes: list[str], read_func: Any) -> int:
        """Pre-populate cache with content.

        Useful for warming cache with predicted reads (e.g., recently
        accessed files, files in working directory).

        Args:
            content_hashes: List of content hashes to warm
            read_func: Callable that takes content_hash and returns bytes

        Returns:
            Number of entries warmed
        """
        warmed = 0
        for content_hash in content_hashes:
            if self.exists(content_hash):
                continue

            try:
                content = read_func(content_hash)
                if content and self.put(content_hash, content):
                    warmed += 1
            except Exception as e:
                logger.debug(f"Failed to warm {content_hash}: {e}")

        logger.info(f"Cache warm complete: {warmed}/{len(content_hashes)} entries")
        return warmed

    # =========================================================================
    # Persistence
    # =========================================================================

    def _load_metadata(self) -> None:
        """Load cache metadata from disk on startup."""
        if not self.metadata_path.exists():
            # Scan directory for existing files
            self._scan_content_dir()
            return

        try:
            with open(self.metadata_path, "rb") as f:
                # Read version
                version = struct.unpack("!I", f.read(4))[0]
                if version != METADATA_VERSION:
                    logger.warning(f"Metadata version mismatch: {version} != {METADATA_VERSION}")
                    self._scan_content_dir()
                    return

                # Read entry count
                entry_count = struct.unpack("!I", f.read(4))[0]

                # Read entries
                for _ in range(entry_count):
                    # Hash (32 bytes hex = 64 bytes)
                    content_hash = f.read(64).decode("ascii")
                    # Size, created, accessed, access_count, priority
                    size, created, accessed, access_count, priority = struct.unpack(
                        "!QddIi", f.read(8 + 8 + 8 + 4 + 4)
                    )

                    entry = CacheEntry(
                        content_hash=content_hash,
                        size_bytes=size,
                        created_at=created,
                        last_accessed=accessed,
                        access_count=access_count,
                        priority=priority,
                        clock_bit=False,  # Start with second chance used
                    )

                    # Verify file exists
                    content_path = self._get_content_path(content_hash)
                    if content_path.exists():
                        self._entries[content_hash] = entry
                        self._clock_order.append(content_hash)
                        self._current_size_bytes += size

                        # Add to Bloom filter
                        if self._bloom is not None:
                            self._bloom.add(content_hash)

            logger.info(f"Loaded {len(self._entries)} cache entries from metadata")

        except Exception as e:
            logger.warning(f"Failed to load cache metadata: {e}")
            self._scan_content_dir()

    def _scan_content_dir(self) -> None:
        """Scan content directory to rebuild metadata."""
        logger.info("Scanning content directory to rebuild metadata...")

        for cache_file in self.content_dir.rglob("*.bin"):
            try:
                content_hash = cache_file.stem
                if len(content_hash) != 64:  # SHA-256 hex length
                    continue

                size = cache_file.stat().st_size
                mtime = cache_file.stat().st_mtime

                entry = CacheEntry(
                    content_hash=content_hash,
                    size_bytes=size,
                    created_at=mtime,
                    last_accessed=mtime,
                    clock_bit=False,
                )

                self._entries[content_hash] = entry
                self._clock_order.append(content_hash)
                self._current_size_bytes += size

                if self._bloom is not None:
                    self._bloom.add(content_hash)

            except Exception as e:
                logger.debug(f"Failed to scan {cache_file}: {e}")

        logger.info(f"Scanned {len(self._entries)} cache entries")

    def save_metadata(self) -> None:
        """Save cache metadata to disk for persistence across restarts."""
        with self._lock:
            try:
                with open(self.metadata_path, "wb") as f:
                    # Version
                    f.write(struct.pack("!I", METADATA_VERSION))

                    # Entry count
                    f.write(struct.pack("!I", len(self._entries)))

                    # Entries
                    for content_hash, entry in self._entries.items():
                        f.write(content_hash.encode("ascii"))
                        f.write(
                            struct.pack(
                                "!QddIi",
                                entry.size_bytes,
                                entry.created_at,
                                entry.last_accessed,
                                entry.access_count,
                                entry.priority,
                            )
                        )

                logger.debug(f"Saved {len(self._entries)} cache entries to metadata")

            except Exception as e:
                logger.error(f"Failed to save cache metadata: {e}")

    def close(self) -> None:
        """Close cache and save metadata."""
        self.save_metadata()
        logger.info("LocalDiskCache closed")


# =========================================================================
# Global instance management
# =========================================================================

_default_cache: LocalDiskCache | None = None
_cache_lock = threading.Lock()


def get_local_disk_cache(
    cache_dir: str | Path | None = None,
    max_size_gb: float | None = None,
) -> LocalDiskCache:
    """Get or create the global LocalDiskCache instance.

    Args:
        cache_dir: Cache directory (only used on first call)
        max_size_gb: Max size in GB (only used on first call)

    Returns:
        LocalDiskCache instance
    """
    global _default_cache

    if _default_cache is None:
        with _cache_lock:
            if _default_cache is None:
                # Read from environment if not specified
                if cache_dir is None:
                    cache_dir = os.getenv("NEXUS_LOCAL_CACHE_DIR")
                if max_size_gb is None:
                    max_size_gb = float(
                        os.getenv("NEXUS_LOCAL_CACHE_SIZE_GB", str(DEFAULT_MAX_SIZE_GB))
                    )

                _default_cache = LocalDiskCache(
                    cache_dir=cache_dir,
                    max_size_gb=max_size_gb,
                )

    return _default_cache


def set_local_disk_cache(cache: LocalDiskCache | None) -> None:
    """Set the global LocalDiskCache instance."""
    global _default_cache
    with _cache_lock:
        _default_cache = cache


def close_local_disk_cache() -> None:
    """Close and clear the global LocalDiskCache instance."""
    global _default_cache
    with _cache_lock:
        if _default_cache is not None:
            _default_cache.close()
            _default_cache = None
