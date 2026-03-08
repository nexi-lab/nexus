"""Cache mixin for connectors — thin adapter.

Delegates all cache logic to CacheService and BackendIOService.
Preserves the full public API for backward compatibility with all
7 connectors (GCS, S3, Local, Gmail, Slack, HN, GCalendar).

Part of: #506, #510 (cache layer epic), #1628 (SRP refactor)
"""

import logging
from typing import TYPE_CHECKING, Any

from nexus.backends.cache.models import (
    IMMUTABLE_VERSION,
    MAX_CACHE_FILE_SIZE,
    MAX_FULL_TEXT_SIZE,
    SUMMARY_SIZE,
    CachedReadResult,
    CacheEntry,
    SyncResult,
)
from nexus.contracts.types import OperationContext

if TYPE_CHECKING:
    from nexus_fast import L1MetadataCache
    from sqlalchemy.orm import Session

    from nexus.backends.cache.service import CacheService as _CacheServiceType

logger = logging.getLogger(__name__)

# Re-export for backward compatibility
__all__ = [
    "IMMUTABLE_VERSION",
    "MAX_CACHE_FILE_SIZE",
    "MAX_FULL_TEXT_SIZE",
    "SUMMARY_SIZE",
    "CacheConnectorMixin",
    "CacheEntry",
    "CachedReadResult",
    "SyncResult",
]


class CacheConnectorMixin:
    """Mixin that adds cache support to connectors.

    Thin adapter that delegates to CacheService and BackendIOService.
    Preserves all method signatures for backward compatibility.

    Provides a two-level cache:
    - L1: In-memory Rust metadata cache (fast, per-instance, lost on restart)
    - L2: Disk-based content + metadata sidecar (FileContentCache)

    Usage:
        class PathGCSBackend(PathBackend, CacheConnectorMixin):
            pass
    """

    # L1-only mode: skip L2 (PostgreSQL) caching entirely
    l1_only: bool = False

    # Callback for sync completion notifications (e.g., zoekt reindex). Issue #1520.
    on_sync_callback: Any | None = None

    # Maximum file size to cache (default 100MB)
    MAX_CACHE_FILE_SIZE: int = MAX_CACHE_FILE_SIZE

    # Maximum text size to store as 'full' (default 10MB)
    MAX_FULL_TEXT_SIZE: int = MAX_FULL_TEXT_SIZE

    # Summary size for large files (default 100KB)
    SUMMARY_SIZE: int = SUMMARY_SIZE

    # =========================================================================
    # L1 Metadata Cache (Rust) — class-level, shared across all connectors
    # =========================================================================

    _l1_cache: "L1MetadataCache | None" = None
    _l1_max_entries: int = 100_000
    _l1_default_ttl: int = 300

    @classmethod
    def _get_l1_cache(cls) -> "L1MetadataCache | None":
        """Get or create the shared L1 metadata cache (Rust-based)."""
        if cls._l1_cache is None:
            try:
                from nexus_fast import L1MetadataCache

                cls._l1_cache = L1MetadataCache(
                    max_entries=cls._l1_max_entries,
                    default_ttl=cls._l1_default_ttl,
                )
                logger.info(
                    "[CACHE] L1 Rust cache initialized: max_entries=%d, default_ttl=%ds",
                    cls._l1_max_entries,
                    cls._l1_default_ttl,
                )
            except ImportError:
                logger.warning("[CACHE] nexus_fast not available, L1 cache disabled")
                return None
        return cls._l1_cache

    @classmethod
    def set_l1_cache_config(cls, max_entries: int = 100_000, default_ttl: int = 300) -> None:
        """Configure the L1 cache. Must be called before first use."""
        cls._l1_max_entries = max_entries
        cls._l1_default_ttl = default_ttl
        if cls._l1_cache is not None:
            cls._l1_cache = None

    @classmethod
    def get_l1_cache_stats(cls) -> dict[str, Any]:
        """Get L1 cache statistics."""
        if cls._l1_cache is None:
            return {
                "entries": 0,
                "hits": 0,
                "misses": 0,
                "hit_rate": 0.0,
                "max_entries": cls._l1_max_entries,
                "default_ttl": cls._l1_default_ttl,
            }
        stats: dict[str, Any] = cls._l1_cache.stats()
        return stats

    @classmethod
    def clear_l1_cache(cls) -> None:
        """Clear the L1 metadata cache."""
        if cls._l1_cache is not None:
            cls._l1_cache.clear()

    # =========================================================================
    # Lazy CacheService creation
    # =========================================================================

    @property
    def _cache_service(self) -> "_CacheServiceType":
        """Lazy-create CacheService on first access."""
        if not hasattr(self, "_cache_service_instance"):
            from nexus.backends.cache.service import CacheService
            from nexus.backends.misc.backend_io import BackendIOService

            self._cache_service_instance = CacheService(
                connector=self,
                l1_cache=self._get_l1_cache(),
                backend_io=BackendIOService(self),
            )
        return self._cache_service_instance

    # =========================================================================
    # Delegated methods — preserve all original signatures
    # =========================================================================

    def _has_caching(self) -> bool:
        """Check if any caching is enabled (L1 or L1+L2)."""
        return self._cache_service.has_caching()

    def _has_l2_caching(self) -> bool:
        """Check if L2 (disk) caching is enabled."""
        return self._cache_service.has_l2_caching()

    def _get_cache_path(self, context: OperationContext | None) -> str | None:
        """Get the cache key path from context."""
        return self._cache_service.get_cache_path(context)

    def _get_db_session(self) -> "Session":
        """Get database session."""
        return self._cache_service.get_db_session()

    def _get_path_id(self, path: str, session: "Session") -> str | None:
        """Get path_id for a virtual path."""
        return self._cache_service.get_path_id(path, session)

    def _get_path_ids_bulk(self, paths: list[str], session: "Session") -> dict[str, str]:
        """Get path_ids for multiple virtual paths in a single query."""
        return self._cache_service.get_path_ids_bulk(paths, session)

    def _read_from_cache(
        self, path: str, original: bool = False, zone_id: str | None = None
    ) -> CacheEntry | None:
        """Read content from cache (L1 then L2)."""
        return self._cache_service.read_from_cache(path, original, zone_id=zone_id)

    def _read_bulk_from_cache(
        self, paths: list[str], original: bool = False, zone_id: str | None = None
    ) -> dict[str, CacheEntry]:
        """Read multiple entries from cache in bulk (L1 + L2)."""
        return self._cache_service.read_bulk_from_cache(paths, original, zone_id=zone_id)

    def read_content_bulk(
        self, paths: list[str], context: OperationContext | None = None
    ) -> dict[str, bytes]:
        """Read multiple files' content in bulk, using cache where available."""
        return self._cache_service.read_content_bulk(paths, context)

    def _write_to_cache(
        self,
        path: str,
        content: bytes,
        content_text: str | None = None,
        content_type: str = "full",
        backend_version: str | None = None,
        parsed_from: str | None = None,
        parse_metadata: dict | None = None,
        zone_id: str | None = None,
    ) -> CacheEntry:
        """Write content to cache."""
        return self._cache_service.write_to_cache(
            path=path,
            content=content,
            content_text=content_text,
            content_type=content_type,
            backend_version=backend_version,
            parsed_from=parsed_from,
            parse_metadata=parse_metadata,
            zone_id=zone_id,
        )

    def _batch_write_to_cache(self, entries: list[dict]) -> list[CacheEntry]:
        """Write multiple entries to cache in a single transaction."""
        return self._cache_service.bulk_write_to_cache(entries)

    def read_content_with_cache(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> CachedReadResult:
        """Read content with automatic L1/L2 caching."""
        return self._cache_service.read_content_with_cache(content_hash, context)

    def get_content_hash(self, path: str) -> str | None:
        """Get content hash (ETag) for a path from cache."""
        return self._cache_service.get_content_hash(path)

    def _invalidate_cache(
        self,
        path: str | None = None,
        mount_prefix: str | None = None,
        delete: bool = False,
        zone_id: str | None = None,
    ) -> int:
        """Invalidate cache entries (L1 memory + L2 disk)."""
        return self._cache_service.invalidate_cache(path, mount_prefix, delete, zone_id=zone_id)

    def _check_version(
        self,
        path: str,
        expected_version: str,
        context: OperationContext | None = None,
    ) -> bool:
        """Check if backend version matches expected."""
        return self._cache_service.check_version(path, expected_version, context)

    def _get_size_from_cache(self, path: str) -> int | None:
        """Get file size from cache (efficient, no backend call)."""
        return self._cache_service.get_size_from_cache(path)

    # =========================================================================
    # Public-name aliases (used by SyncPipelineService on develop)
    # =========================================================================

    def read_bulk_from_cache(
        self, paths: list[str], original: bool = False
    ) -> dict[str, CacheEntry]:
        """Public alias for _read_bulk_from_cache (SyncPipelineService)."""
        return self._read_bulk_from_cache(paths, original)

    def batch_read_from_backend(
        self,
        paths: list[str],
        contexts: dict[str, OperationContext] | None = None,
    ) -> dict[str, bytes]:
        """Public alias for _batch_read_from_backend (SyncPipelineService)."""
        return self._batch_read_from_backend(paths, contexts)

    def parse_content(
        self, path: str, content: bytes
    ) -> tuple[str | None, str | None, dict | None]:
        """Public alias for _parse_content (SyncPipelineService)."""
        return self._parse_content(path, content)

    def batch_write_to_cache(self, entries: list[dict]) -> list[CacheEntry]:
        """Public alias for _batch_write_to_cache (SyncPipelineService)."""
        return self._batch_write_to_cache(entries)

    def generate_embeddings_for_path(self, path: str) -> None:
        """Public alias for _generate_embeddings (SyncPipelineService)."""
        return self._generate_embeddings(path)

    # =========================================================================
    # Backend I/O — delegated to BackendIOService via CacheService
    # =========================================================================

    def _batch_read_from_backend(
        self,
        paths: list[str],
        contexts: dict[str, OperationContext] | None = None,
    ) -> dict[str, bytes]:
        """Batch read content directly from backend (bypassing cache)."""
        return self._cache_service.backend_io.batch_read_from_backend(paths, contexts)

    def _read_content_from_backend(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> bytes | None:
        """Read content directly from backend (bypassing cache)."""
        return self._cache_service.backend_io.read_content_from_backend(path, context)

    def _parse_content(
        self,
        path: str,
        content: bytes,
    ) -> tuple[str | None, str | None, dict | None]:
        """Parse content using the parser registry."""
        return self._cache_service.backend_io.parse_content(path, content)

    def _generate_embeddings(self, path: str) -> None:
        """Generate embeddings for a file. Override in connectors."""
        pass

    # =========================================================================
    # Connector hooks (stay in mixin — overridden by connectors)
    # =========================================================================

    def _fetch_content(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> bytes:
        """Fetch content from the backend (to be implemented by connectors)."""
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement _fetch_content() "
            "to use read_content_with_cache()"
        )

    def _get_backend_version(
        self,
        context: OperationContext | None = None,
    ) -> str | None:
        """Get the backend version for a path (for cache invalidation)."""
        return None

    def sync_content_to_cache(
        self,
        path: str | None = None,
        mount_point: str | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        max_file_size: int | None = None,
        generate_embeddings: bool = True,
        context: OperationContext | None = None,
    ) -> SyncResult:
        """Sync content from connector backend to cache layer."""
        from nexus.backends.misc.sync_pipeline import SyncPipelineService

        pipeline = SyncPipelineService(self)
        result = pipeline.execute(
            path=path,
            mount_point=mount_point,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            max_file_size=max_file_size,
            generate_embeddings=generate_embeddings,
            context=context,
        )

        if result.files_synced > 0 and self.on_sync_callback is not None:
            self.on_sync_callback(result.files_synced)

        return result
