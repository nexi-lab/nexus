"""File-based content cache for L2 storage.

Stores cached content on disk instead of PostgreSQL for:
- Faster reads (mmap, OS page cache)
- Lower cost (disk vs managed DB)
- Zoekt compatibility (direct file indexing)

PostgreSQL still stores metadata (path, hash, size, synced_at).
Content is stored in: {cache_dir}/.cache/{tenant_id}/{path_hash}/{filename}

Performance optimization:
- Uses Bloom filter for fast cache miss detection (avoids disk I/O)
- 99%+ of cache misses skip disk access entirely

Usage:
    file_cache = FileContentCache("/app/data")

    # Write content
    file_cache.write("tenant1", "/mnt/gcs/file.txt", b"content")

    # Read content
    content = file_cache.read("tenant1", "/mnt/gcs/file.txt")

    # Delete content
    file_cache.delete("tenant1", "/mnt/gcs/file.txt")
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus_fast import BloomFilter

logger = logging.getLogger(__name__)

# Default Bloom filter capacity (can be overridden)
DEFAULT_BLOOM_CAPACITY = 100_000
DEFAULT_BLOOM_FP_RATE = 0.01  # 1% false positive rate

# Default cache directory name
CACHE_DIR_NAME = ".cache"


class FileContentCache:
    """File-based content cache for L2 storage.

    Stores content on disk in a structured directory layout:
    {base_dir}/.cache/{tenant_id}/{path_hash[:2]}/{path_hash[2:4]}/{path_hash}.bin

    Uses hash-based sharding to avoid too many files in a single directory.

    Performance optimization:
    - Uses Bloom filter to avoid disk I/O for cache misses
    - Bloom filter has ~1% false positive rate (harmless - falls through to disk check)
    - No false negatives (never skips existing files)
    """

    _bloom: BloomFilter | None

    def __init__(
        self,
        base_dir: str | Path,
        bloom_capacity: int = DEFAULT_BLOOM_CAPACITY,
        bloom_fp_rate: float = DEFAULT_BLOOM_FP_RATE,
    ):
        """Initialize file cache.

        Args:
            base_dir: Base directory for cache storage (e.g., /app/data)
            bloom_capacity: Expected number of cached items (default: 100,000)
            bloom_fp_rate: Target false positive rate (default: 0.01 = 1%)
        """
        self.base_dir = Path(base_dir)
        self.cache_dir = self.base_dir / CACHE_DIR_NAME
        self._bloom = None
        self._bloom_capacity = bloom_capacity
        self._bloom_fp_rate = bloom_fp_rate
        self._ensure_cache_dir()
        self._init_bloom_filter()

    def _ensure_cache_dir(self) -> None:
        """Create cache directory if it doesn't exist."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _init_bloom_filter(self) -> None:
        """Initialize Bloom filter for fast cache miss detection."""
        try:
            from nexus_fast import BloomFilter

            self._bloom = BloomFilter(self._bloom_capacity, self._bloom_fp_rate)
            self._populate_bloom_from_disk()
            logger.debug(
                f"Bloom filter initialized: capacity={self._bloom_capacity}, "
                f"fp_rate={self._bloom_fp_rate}, memory={self._bloom.memory_bytes} bytes"
            )
        except ImportError:
            logger.warning("nexus_fast not available, Bloom filter disabled")
            self._bloom = None
        except Exception as e:
            logger.warning(f"Failed to initialize Bloom filter: {e}")
            self._bloom = None

    def _populate_bloom_from_disk(self) -> None:
        """Populate Bloom filter from existing cache entries on disk.

        Scans the cache directory and adds all cached paths to the Bloom filter.
        This is called on startup to ensure the filter reflects disk state.
        """
        if self._bloom is None or not self.cache_dir.exists():
            return

        keys: list[str] = []
        try:
            for tenant_dir in self.cache_dir.iterdir():
                if not tenant_dir.is_dir():
                    continue
                tenant_id = tenant_dir.name
                # Scan all .bin files in tenant directory
                for cache_file in tenant_dir.rglob("*.bin"):
                    # Extract hash from filename (remove .bin extension)
                    file_hash = cache_file.stem
                    # We store keys as "tenant_id:hash" since we can't recover original path
                    # This works because _bloom_key uses the same format
                    keys.append(f"{tenant_id}:{file_hash}")

            if keys:
                self._bloom.add_bulk(keys)
                logger.info(f"Bloom filter populated with {len(keys)} entries from disk")
        except Exception as e:
            logger.warning(f"Failed to populate Bloom filter from disk: {e}")

    def _bloom_key(self, tenant_id: str, virtual_path: str) -> str:
        """Generate Bloom filter key for a cache entry.

        Uses tenant_id:path_hash format to match what we store on disk.
        """
        path_hash = self._path_hash(virtual_path)
        return f"{tenant_id}:{path_hash}"

    def _bloom_check(self, tenant_id: str, virtual_path: str) -> bool:
        """Check Bloom filter for possible existence.

        Returns:
            True if entry might exist (need to check disk)
            False if entry definitely does not exist (skip disk I/O)
        """
        if self._bloom is None:
            return True  # No Bloom filter, always check disk
        return bool(self._bloom.might_exist(self._bloom_key(tenant_id, virtual_path)))

    def _bloom_add(self, tenant_id: str, virtual_path: str) -> None:
        """Add entry to Bloom filter after writing to disk."""
        if self._bloom is not None:
            self._bloom.add(self._bloom_key(tenant_id, virtual_path))

    def _path_hash(self, virtual_path: str) -> str:
        """Generate a hash for the virtual path.

        Uses SHA-256 truncated to 32 chars for reasonable uniqueness
        while keeping filenames manageable.
        """
        return hashlib.sha256(virtual_path.encode()).hexdigest()[:32]

    def _get_cache_path(self, tenant_id: str, virtual_path: str) -> Path:
        """Get the file path for cached content.

        Uses hash-based sharding: {tenant}/{hash[:2]}/{hash[2:4]}/{hash}.bin
        This prevents too many files in a single directory (max 256 per level).
        """
        path_hash = self._path_hash(virtual_path)
        return self.cache_dir / tenant_id / path_hash[:2] / path_hash[2:4] / f"{path_hash}.bin"

    def _get_text_cache_path(self, tenant_id: str, virtual_path: str) -> Path:
        """Get the file path for cached parsed text."""
        path_hash = self._path_hash(virtual_path)
        return self.cache_dir / tenant_id / path_hash[:2] / path_hash[2:4] / f"{path_hash}.txt"

    def write(
        self,
        tenant_id: str,
        virtual_path: str,
        content: bytes,
        text_content: str | None = None,
    ) -> Path:
        """Write content to cache.

        Args:
            tenant_id: Tenant ID
            virtual_path: Virtual file path
            content: Binary content to cache
            text_content: Optional parsed text content

        Returns:
            Path to the cached binary file
        """
        # Write binary content
        cache_path = self._get_cache_path(tenant_id, virtual_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            cache_path.write_bytes(content)
            logger.debug(f"Cached {len(content)} bytes to {cache_path}")
        except Exception as e:
            logger.error(f"Failed to write cache file {cache_path}: {e}")
            raise

        # Write text content if provided
        if text_content:
            text_path = self._get_text_cache_path(tenant_id, virtual_path)
            try:
                text_path.write_text(text_content, encoding="utf-8")
            except Exception as e:
                logger.warning(f"Failed to write text cache {text_path}: {e}")

        # Add to Bloom filter for fast future lookups
        self._bloom_add(tenant_id, virtual_path)

        return cache_path

    def read(self, tenant_id: str, virtual_path: str) -> bytes | None:
        """Read binary content from cache.

        Uses Bloom filter for fast cache miss detection - avoids disk I/O
        for entries that definitely don't exist.

        Uses mmap-based reading via nexus_fast for better performance:
        - Leverages OS page cache efficiently
        - 20-70% faster for medium to large files

        Args:
            tenant_id: Tenant ID
            virtual_path: Virtual file path

        Returns:
            Cached content bytes, or None if not cached
        """
        # Fast path: Bloom filter says entry definitely doesn't exist
        if not self._bloom_check(tenant_id, virtual_path):
            return None

        cache_path = self._get_cache_path(tenant_id, virtual_path)

        try:
            from nexus_fast import read_file

            result: bytes | None = read_file(str(cache_path))
            return result
        except ImportError:
            # Fallback to standard read if nexus_fast not available
            if not cache_path.exists():
                return None
            try:
                return cache_path.read_bytes()
            except Exception as e:
                logger.warning(f"Failed to read cache file {cache_path}: {e}")
                return None
        except Exception as e:
            logger.warning(f"Failed to read cache file {cache_path}: {e}")
            return None

    def read_text(self, tenant_id: str, virtual_path: str) -> str | None:
        """Read parsed text content from cache.

        Uses Bloom filter for fast cache miss detection - avoids disk I/O
        for entries that definitely don't exist.

        Args:
            tenant_id: Tenant ID
            virtual_path: Virtual file path

        Returns:
            Cached text content, or None if not cached
        """
        # Fast path: Bloom filter says entry definitely doesn't exist
        # (text file uses same path hash as binary file)
        if not self._bloom_check(tenant_id, virtual_path):
            return None

        text_path = self._get_text_cache_path(tenant_id, virtual_path)

        if not text_path.exists():
            return None

        try:
            return text_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to read text cache {text_path}: {e}")
            return None

    def read_bulk(
        self,
        tenant_id: str,
        virtual_paths: list[str],
    ) -> dict[str, bytes]:
        """Read multiple files from cache.

        Uses parallel mmap-based reading via nexus_fast for better performance
        when reading many files (10+ files uses parallel I/O).

        Args:
            tenant_id: Tenant ID
            virtual_paths: List of virtual file paths

        Returns:
            Dict mapping virtual_path to content (only for cached files)
        """
        # Filter out paths that definitely don't exist (via Bloom filter)
        paths_to_check = [p for p in virtual_paths if self._bloom_check(tenant_id, p)]

        if not paths_to_check:
            return {}

        # Build mapping: cache_path -> virtual_path
        cache_to_virtual: dict[str, str] = {}
        cache_paths: list[str] = []
        for vpath in paths_to_check:
            cache_path = str(self._get_cache_path(tenant_id, vpath))
            cache_to_virtual[cache_path] = vpath
            cache_paths.append(cache_path)

        try:
            from nexus_fast import read_files_bulk

            # Parallel mmap read
            cache_contents = read_files_bulk(cache_paths)

            # Map back to virtual paths
            result: dict[str, bytes] = {}
            for cache_path, content in cache_contents.items():
                virtual_path = cache_to_virtual.get(cache_path)
                if virtual_path:
                    result[virtual_path] = content
            return result
        except ImportError:
            # Fallback to sequential read if nexus_fast not available
            result = {}
            for path in paths_to_check:
                content = self.read(tenant_id, path)
                if content is not None:
                    result[path] = content
            return result

    def read_text_bulk(
        self,
        tenant_id: str,
        virtual_paths: list[str],
    ) -> dict[str, str]:
        """Read multiple text files from cache.

        Args:
            tenant_id: Tenant ID
            virtual_paths: List of virtual file paths

        Returns:
            Dict mapping virtual_path to text content (only for cached files)
        """
        result = {}
        for path in virtual_paths:
            text = self.read_text(tenant_id, path)
            if text is not None:
                result[path] = text
        return result

    def exists(self, tenant_id: str, virtual_path: str) -> bool:
        """Check if content is cached.

        Uses Bloom filter for fast cache miss detection - avoids disk I/O
        for entries that definitely don't exist.

        Args:
            tenant_id: Tenant ID
            virtual_path: Virtual file path

        Returns:
            True if content is cached
        """
        # Fast path: Bloom filter says entry definitely doesn't exist
        if not self._bloom_check(tenant_id, virtual_path):
            return False

        return self._get_cache_path(tenant_id, virtual_path).exists()

    def delete(self, tenant_id: str, virtual_path: str) -> bool:
        """Delete cached content.

        Args:
            tenant_id: Tenant ID
            virtual_path: Virtual file path

        Returns:
            True if content was deleted
        """
        deleted = False

        # Delete binary cache
        cache_path = self._get_cache_path(tenant_id, virtual_path)
        if cache_path.exists():
            try:
                cache_path.unlink()
                deleted = True
            except Exception as e:
                logger.warning(f"Failed to delete cache file {cache_path}: {e}")

        # Delete text cache
        text_path = self._get_text_cache_path(tenant_id, virtual_path)
        if text_path.exists():
            try:
                text_path.unlink()
            except Exception as e:
                logger.warning(f"Failed to delete text cache {text_path}: {e}")

        return deleted

    def delete_tenant(self, tenant_id: str) -> int:
        """Delete all cached content for a tenant.

        Args:
            tenant_id: Tenant ID

        Returns:
            Number of files deleted
        """
        tenant_dir = self.cache_dir / tenant_id
        if not tenant_dir.exists():
            return 0

        count = sum(1 for _ in tenant_dir.rglob("*") if _.is_file())

        try:
            shutil.rmtree(tenant_dir)
            logger.info(f"Deleted {count} cached files for tenant {tenant_id}")
        except Exception as e:
            logger.error(f"Failed to delete tenant cache {tenant_dir}: {e}")
            return 0

        return count

    def get_cache_stats(self) -> dict:
        """Get cache statistics.

        Returns:
            Dict with cache stats (file count, total size, by tenant)
        """
        stats: dict[str, Any] = {
            "total_files": 0,
            "total_size_bytes": 0,
            "tenants": {},
        }

        if not self.cache_dir.exists():
            return stats

        for tenant_dir in self.cache_dir.iterdir():
            if not tenant_dir.is_dir():
                continue

            tenant_id = tenant_dir.name
            tenant_files = 0
            tenant_size = 0

            for cache_file in tenant_dir.rglob("*.bin"):
                tenant_files += 1
                tenant_size += cache_file.stat().st_size

            stats["tenants"][tenant_id] = {
                "files": tenant_files,
                "size_bytes": tenant_size,
            }
            stats["total_files"] += tenant_files
            stats["total_size_bytes"] += tenant_size

        return stats

    def get_zoekt_index_path(self) -> Path:
        """Get the path that Zoekt should index.

        Returns:
            Path to the cache directory for Zoekt indexing
        """
        return self.cache_dir


# Global instance (initialized lazily)
_file_cache: FileContentCache | None = None


def get_file_cache(base_dir: str | Path | None = None) -> FileContentCache:
    """Get the global file cache instance.

    Args:
        base_dir: Base directory (only used for first initialization)

    Returns:
        FileContentCache instance
    """
    global _file_cache

    if _file_cache is None:
        if base_dir is None:
            base_dir = os.getenv("NEXUS_DATA_DIR", "./nexus-data")
        _file_cache = FileContentCache(base_dir)

    return _file_cache
