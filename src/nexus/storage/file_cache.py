"""File-based content cache for L2 storage.

Stores cached content on disk instead of PostgreSQL for:
- Faster reads (mmap, OS page cache)
- Lower cost (disk vs managed DB)
- Zoekt compatibility (direct file indexing)

PostgreSQL still stores metadata (path, hash, size, synced_at).
Content is stored in: {cache_dir}/.cache/{tenant_id}/{path_hash}/{filename}

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

import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import blake3

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Default cache directory name
CACHE_DIR_NAME = ".cache"


class FileContentCache:
    """File-based content cache for L2 storage.

    Stores content on disk in a structured directory layout:
    {base_dir}/.cache/{tenant_id}/{path_hash[:2]}/{path_hash[2:4]}/{path_hash}.bin

    Uses hash-based sharding to avoid too many files in a single directory.
    """

    def __init__(self, base_dir: str | Path):
        """Initialize file cache.

        Args:
            base_dir: Base directory for cache storage (e.g., /app/data)
        """
        self.base_dir = Path(base_dir)
        self.cache_dir = self.base_dir / CACHE_DIR_NAME
        self._ensure_cache_dir()

    def _ensure_cache_dir(self) -> None:
        """Create cache directory if it doesn't exist."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path_hash(self, virtual_path: str) -> str:
        """Generate a hash for the virtual path.

        Uses BLAKE3 truncated to 32 chars for reasonable uniqueness
        while keeping filenames manageable. BLAKE3 is ~10x faster than SHA-256.
        """
        hash_hex: str = blake3.blake3(virtual_path.encode()).hexdigest()
        return hash_hex[:32]

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

        return cache_path

    def read(self, tenant_id: str, virtual_path: str) -> bytes | None:
        """Read binary content from cache.

        Args:
            tenant_id: Tenant ID
            virtual_path: Virtual file path

        Returns:
            Cached content bytes, or None if not cached
        """
        cache_path = self._get_cache_path(tenant_id, virtual_path)

        if not cache_path.exists():
            return None

        try:
            return cache_path.read_bytes()
        except Exception as e:
            logger.warning(f"Failed to read cache file {cache_path}: {e}")
            return None

    def read_text(self, tenant_id: str, virtual_path: str) -> str | None:
        """Read parsed text content from cache.

        Args:
            tenant_id: Tenant ID
            virtual_path: Virtual file path

        Returns:
            Cached text content, or None if not cached
        """
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

        Args:
            tenant_id: Tenant ID
            virtual_paths: List of virtual file paths

        Returns:
            Dict mapping virtual_path to content (only for cached files)
        """
        result = {}
        for path in virtual_paths:
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

        Args:
            tenant_id: Tenant ID
            virtual_path: Virtual file path

        Returns:
            True if content is cached
        """
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
