"""Cache mixin for connectors — thin adapter.

Delegates all cache logic to CacheService and BackendIOService.
Preserves the full public API for backward compatibility with all
7 connectors (GCS, S3, Local, Gmail, Slack, HN, GCalendar).

Part of: #506, #510 (cache layer epic), #1628 (SRP refactor)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.backends.cache_models import (
    IMMUTABLE_VERSION,
    MAX_CACHE_FILE_SIZE,
    MAX_FULL_TEXT_SIZE,
    SUMMARY_SIZE,
    CachedReadResult,
    CacheEntry,
    SyncResult,
)
from nexus.core.permissions import OperationContext

if TYPE_CHECKING:
    from nexus_fast import L1MetadataCache
    from sqlalchemy.orm import Session

    from nexus.backends.cache_service import CacheService as _CacheServiceType

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
        class GCSConnectorBackend(BaseBlobStorageConnector, CacheConnectorMixin):
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

    _l1_cache: L1MetadataCache | None = None
    _l1_max_entries: int = 100_000
    _l1_default_ttl: int = 300

    @classmethod
    def _get_l1_cache(cls) -> L1MetadataCache | None:
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
    def _cache_service(self) -> _CacheServiceType:
        """Lazy-create CacheService on first access."""
        if not hasattr(self, "_cache_service_instance"):
            from nexus.backends.backend_io import BackendIOService
            from nexus.backends.cache_service import CacheService

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

    def _get_db_session(self) -> Session:
        """Get database session."""
        return self._cache_service.get_db_session()

    def _get_path_id(self, path: str, session: Session) -> str | None:
        """Get path_id for a virtual path."""
        return self._cache_service.get_path_id(path, session)

    def _get_path_ids_bulk(self, paths: list[str], session: Session) -> dict[str, str]:
        """Get path_ids for multiple virtual paths in a single query."""
        return self._cache_service.get_path_ids_bulk(paths, session)

    def _read_from_cache(self, path: str, original: bool = False) -> CacheEntry | None:
        """Read content from cache (L1 then L2)."""
        return self._cache_service.read_from_cache(path, original)

    def _read_bulk_from_cache(
        self, paths: list[str], original: bool = False
    ) -> dict[str, CacheEntry]:
<<<<<<< HEAD
        """Read multiple entries from cache in bulk (L1 + L2).

        This is optimized for batch operations like grep where many files
        need to be read. Uses a single DB query for L2 lookups instead of N queries.

        L1 cache now uses Rust-based L1MetadataCache which stores only metadata
        (~100 bytes per entry) and reads content via mmap from disk.

        Args:
            paths: List of virtual file paths
            original: If True, return binary content even for parsed files

        Returns:
            Dict mapping path -> CacheEntry (only for paths that are cached)
        """
        if not paths:
            return {}

        results: dict[str, CacheEntry] = {}
        paths_needing_l2: list[str] = []

        # L1: Check Rust metadata cache first
        l1_cache = self._get_l1_cache()
        for path in paths:
            if l1_cache is None:
                paths_needing_l2.append(path)
                continue

            # get_content returns (content_bytes, content_hash, is_text) or None
            l1_result = l1_cache.get_content(path) if original else l1_cache.get(path)
            if l1_result is not None:
                if original:
                    # get_content returns (content, hash, is_text)
                    content_bytes, content_hash, is_text = l1_result
                    # Create a minimal CacheEntry for L1 hits
                    # Note: We don't have full metadata from L1, just what we need
                    entry = CacheEntry(
                        cache_id="",  # Not available from L1
                        path_id="",  # Not available from L1
                        content_text=None,
                        _content_binary=bytes(content_bytes),
                        content_hash=content_hash,
                        content_type="full",
                        original_size=len(content_bytes),
                        cached_size=len(content_bytes),
                        backend_version=None,
                        synced_at=datetime.now(UTC),
                        stale=False,
                    )
                    results[path] = entry
                    logger.debug(f"[CACHE-BULK] L1 HIT: {path}")
                else:
                    # get() returns (path_id, content_hash, disk_path, original_size, is_text, is_fresh)
                    path_id, content_hash, disk_path, original_size, is_text, is_fresh = l1_result
                    if is_fresh:
                        # Create minimal CacheEntry without content
                        entry = CacheEntry(
                            cache_id="",
                            path_id=path_id,
                            content_text=None,
                            _content_binary=None,
                            content_hash=content_hash,
                            content_type="full",
                            original_size=original_size,
                            cached_size=0,
                            backend_version=None,
                            synced_at=datetime.now(UTC),
                            stale=False,
                        )
                        results[path] = entry
                        logger.debug(f"[CACHE-BULK] L1 HIT (metadata): {path}")
                    else:
                        # Entry expired, need L2
                        paths_needing_l2.append(path)
                continue
            paths_needing_l2.append(path)

        if not paths_needing_l2:
            logger.info(f"[CACHE-BULK] All {len(paths)} paths from L1 memory")
            return results

        # L2: Disk-based lookup for remaining paths (metadata sidecar + content files)
        file_cache = get_file_cache()

        # Determine zone — use connector's zone_id or "root"
        cache_zone = getattr(self, "zone_id", None) or "root"

        # Read metadata sidecars in bulk
        meta_entries = file_cache.read_meta_bulk(cache_zone, paths_needing_l2)

        # Bulk read binary content from disk cache
        disk_contents: dict[str, bytes] = {}
        if original and meta_entries:
            disk_contents = file_cache.read_bulk(cache_zone, list(meta_entries.keys()))

        l2_hits = 0
        for vpath, meta in meta_entries.items():
            content_binary_raw = disk_contents.get(vpath) if original else None

            # Read text content from disk
            content_text = file_cache.read_text(meta.get("zone_id", cache_zone), vpath)

            entry = CacheEntry(
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
            results[vpath] = entry
            l2_hits += 1

            # Populate L1 Rust metadata cache for future reads
            if l1_cache is not None:
                try:
                    disk_path = str(
                        file_cache._get_cache_path(meta.get("zone_id", cache_zone), vpath)
                    )
                    is_text = meta.get("content_type", "full") in ("full", "parsed", "summary")
                    l1_cache.put(
                        key=vpath,
                        path_id=meta.get("path_id", ""),
                        content_hash=meta.get("content_hash", ""),
                        disk_path=disk_path,
                        original_size=meta.get("original_size", 0),
                        ttl_seconds=0,
                        is_text=is_text,
                        zone_id=meta.get("zone_id", cache_zone),
                    )
                except Exception as e:
                    logger.debug("[CACHE] L1 cache populate failed for %s: %s", vpath, e)

        logger.info(
            f"[CACHE-BULK] {len(results) - l2_hits} L1 hits, {l2_hits} L2 hits, "
            f"{len(paths) - len(results)} misses (total {len(paths)} paths)"
        )
        return results
=======
        """Read multiple entries from cache in bulk (L1 + L2)."""
        return self._cache_service.read_bulk_from_cache(paths, original)
>>>>>>> origin/develop

    def read_content_bulk(
        self, paths: list[str], context: OperationContext | None = None
    ) -> dict[str, bytes]:
<<<<<<< HEAD
        """Read multiple files' content in bulk, using cache where available.

        This method is optimized for batch operations like grep. It:
        1. Checks L1 memory cache for all paths
        2. Bulk queries L2 database cache for remaining paths
        3. Falls back to backend for cache misses

        Args:
            paths: List of virtual file paths to read
            context: Operation context

        Returns:
            Dict mapping path -> content bytes (only for successful reads)
        """
        if not paths:
            return {}

        results: dict[str, bytes] = {}

        # Bulk cache lookup (L1 + L2)
        cache_entries = self.read_bulk_from_cache(paths, original=True)

        # Extract content from cache hits
        paths_needing_backend: list[str] = []
        for path in paths:
            entry = cache_entries.get(path)
            if entry and not entry.stale and entry.content_binary:
                results[path] = entry.content_binary
            else:
                paths_needing_backend.append(path)

        if not paths_needing_backend:
            logger.info(f"[CACHE-BULK] All {len(paths)} files served from cache")
            return results

        # Read remaining from backend (one at a time for now)
        # TODO: Could add backend bulk read if supported
        for path in paths_needing_backend:
            try:
                content = self._read_content_from_backend(path, context)
                if content:
                    results[path] = content
            except Exception as e:
                logger.debug("[CACHE-BULK] Backend read failed for %s: %s", path, e)

        logger.info(
            f"[CACHE-BULK] {len(cache_entries)} cache hits, "
            f"{len(paths_needing_backend)} backend reads"
        )
        return results

    def _read_from_cache(
        self,
        path: str,
        original: bool = False,
    ) -> CacheEntry | None:
        """Read content from cache (L1 Rust metadata cache, then L2 database).

        L1 cache now uses Rust-based L1MetadataCache which stores only metadata
        (~100 bytes per entry) and reads content via mmap from disk.

        Args:
            path: Virtual file path
            original: If True, return binary content even for parsed files

        Returns:
            CacheEntry if cached, None otherwise (or if TTL expired)

        Note:
            TTL is now handled by the Rust L1 cache automatically.
            Connector-specific cache_ttl is applied when populating L1.
        """
        # L1: Check Rust metadata cache first
        l1_cache = self._get_l1_cache()
        if l1_cache is not None:
            if original:
                # get_content returns (content_bytes, content_hash, is_text) or None
                l1_result = l1_cache.get_content(path)
                if l1_result is not None:
                    content_bytes, content_hash, is_text = l1_result
                    entry = CacheEntry(
                        cache_id="",
                        path_id="",
                        content_text=None,
                        _content_binary=bytes(content_bytes),
                        content_hash=content_hash,
                        content_type="full",
                        original_size=len(content_bytes),
                        cached_size=len(content_bytes),
                        backend_version=None,
                        synced_at=datetime.now(UTC),
                        stale=False,
                    )
                    logger.info(f"[CACHE] L1 HIT (Rust): {path}")
                    return entry
            else:
                # get() returns (path_id, content_hash, disk_path, original_size, is_text, is_fresh)
                l1_result = l1_cache.get(path)
                if l1_result is not None:
                    path_id, content_hash, disk_path, original_size, is_text, is_fresh = l1_result
                    if is_fresh:
                        entry = CacheEntry(
                            cache_id="",
                            path_id=path_id,
                            content_text=None,
                            _content_binary=None,
                            content_hash=content_hash,
                            content_type="full",
                            original_size=original_size,
                            cached_size=0,
                            backend_version=None,
                            synced_at=datetime.now(UTC),
                            stale=False,
                        )
                        logger.info(f"[CACHE] L1 HIT (Rust metadata): {path}")
                        return entry
                    else:
                        logger.debug(f"[CACHE] L1 EXPIRED: {path}")
        logger.debug(f"[CACHE] L1 MISS: {path}")

        # L2: Check disk cache (skip if l1_only mode)
        if not self._has_l2_caching():
            logger.debug(f"[CACHE] L2 SKIP (l1_only mode): {path}")
            return None

        # Read metadata sidecar from disk
        file_cache = get_file_cache()
        cache_zone = getattr(self, "zone_id", None) or "root"
        meta = file_cache.read_meta(cache_zone, path)

        if not meta:
            logger.debug(f"[CACHE] L2 MISS (disk): {path}")
            return None

        logger.info(f"[CACHE] L2 HIT (disk): {path}")

        # Read binary content from disk
        content_binary_raw = None
        if original:
            meta_zone = meta.get("zone_id", cache_zone)
            content_binary_raw = file_cache.read(meta_zone, path)
            if content_binary_raw:
                logger.debug(f"[CACHE] L2 content from DISK: {path}")

        # Read text content from disk
        content_text = file_cache.read_text(meta.get("zone_id", cache_zone), path)

        entry = CacheEntry(
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

        # Check TTL if connector defines cache_ttl
        if hasattr(self, "cache_ttl") and self.cache_ttl:
            age = (datetime.now(UTC) - entry.synced_at).total_seconds()
            if age > self.cache_ttl:
                logger.info(
                    f"[CACHE] L2 TTL EXPIRED: {path} (age={age:.0f}s > ttl={self.cache_ttl}s)"
                )
                return None

        # Populate L1 Rust metadata cache for future reads
        if l1_cache is not None:
            try:
                meta_zone = meta.get("zone_id", cache_zone)
                disk_path = str(file_cache._get_cache_path(meta_zone, path))
                is_text = meta.get("content_type", "full") in ("full", "parsed", "summary")
                ttl = getattr(self, "cache_ttl", 0) or 0
                l1_cache.put(
                    key=path,
                    path_id=meta.get("path_id", ""),
                    content_hash=meta.get("content_hash", ""),
                    disk_path=disk_path,
                    original_size=meta.get("original_size", 0),
                    ttl_seconds=ttl,
                    is_text=is_text,
                    zone_id=meta_zone,
                )
                logger.debug(f"[CACHE] L1 POPULATED from L2: {path}")
            except Exception as e:
                logger.debug("[CACHE] L1 cache populate failed for %s: %s", path, e)

        return entry
=======
        """Read multiple files' content in bulk, using cache where available."""
        return self._cache_service.read_content_bulk(paths, context)
>>>>>>> origin/develop

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
<<<<<<< HEAD
        """Write content to cache.

        Supports two modes:
        - L1+L2 mode (default): Write to PostgreSQL + FileContentCache + L1
        - L1-only mode (l1_only=True): Write to L1 only, disk_path points to original file

        Args:
            path: Virtual file path
            content: Original binary content
            content_text: Parsed/extracted text (if None, tries to decode content as UTF-8)
            content_type: 'full', 'parsed', 'summary', or 'reference'
            backend_version: Backend version for optimistic locking
            parsed_from: Parser that extracted text ('pdf', 'xlsx', etc.)
            parse_metadata: Additional metadata from parsing
            zone_id: Zone ID for multi-zone filtering

        Returns:
            CacheEntry for the cached content
        """
        # === Common logic: compute hash, handle text, determine sizes ===
        content_hash = hash_content(content)
        original_size = len(content)
        now = datetime.now(UTC)
        cache_zone = zone_id or "root"

        # Determine text content
        if content_text is None:
            try:
                content_text = content.decode("utf-8")
            except UnicodeDecodeError:
                content_text = None
                content_type = "reference"  # Can't decode, store as reference only

        # Handle large files
        if content_text and len(content_text) > self.MAX_FULL_TEXT_SIZE:
            content_text = content_text[: self.SUMMARY_SIZE]
            content_type = "summary"

        cached_size = len(content_text) if content_text else 0

        # === Branch based on L2 availability ===
        has_l2 = self._has_l2_caching()

        if has_l2:
            # L1+L2 mode: write to disk (FileContentCache) + metadata sidecar
            file_cache = get_file_cache()

            # Resolve path_id from DB (needed for FilePathModel consistency updates)
            path_id = ""
            session = self._get_db_session()
            if session:
                path_id = self._get_path_id(path, session) or ""

            # Write binary + text content to disk
            if original_size <= self.MAX_CACHE_FILE_SIZE:
                try:
                    file_cache.write(cache_zone, path, content, text_content=content_text)
                    logger.debug(f"[CACHE] Wrote {original_size} bytes to disk: {path}")
                except Exception as e:
                    logger.warning(f"[CACHE] Failed to write to disk cache: {e}")

            # Write metadata sidecar (replaces ContentCacheModel DB entry)
            meta = {
                "path_id": path_id,
                "zone_id": cache_zone,
                "content_hash": content_hash,
                "content_type": content_type,
                "original_size": original_size,
                "cached_size": cached_size,
                "backend_version": backend_version,
                "parsed_from": parsed_from,
                "parse_metadata": parse_metadata,
                "synced_at": now.isoformat(),
                "stale": False,
            }
            file_cache.write_meta(cache_zone, path, meta)

            # Update file_paths for consistency (if DB session available)
            if session and path_id:
                try:
                    file_path_stmt = select(FilePathModel).where(FilePathModel.path_id == path_id)
                    file_path_result = session.execute(file_path_stmt)
                    file_path = file_path_result.scalar_one_or_none()
                    if file_path:
                        updated = False
                        if file_path.size_bytes != original_size:
                            file_path.size_bytes = original_size
                            updated = True
                        if file_path.content_hash != content_hash:
                            file_path.content_hash = content_hash
                            updated = True
                        if updated:
                            file_path.updated_at = now
                            logger.debug(
                                f"[CACHE] Updated file_paths: {path} (size={original_size}, hash={content_hash[:8]}...)"
                            )
                    session.commit()
                except Exception as e:
                    logger.warning(f"[CACHE] Failed to update file_paths for {path}: {e}")

            cache_id = ""
            # disk_path for L1 points to FileContentCache
            disk_path = str(file_cache._get_cache_path(cache_zone, path))
        else:
            # L1-only mode: no PostgreSQL, disk_path points to original file
            path_id = path  # Use path as path_id in L1-only mode
            cache_id = ""  # No cache_id in L1-only mode

            # Get physical path from connector (LocalConnector implements this)
            disk_path = ""
            if hasattr(self, "get_physical_path"):
                try:
                    disk_path = str(self.get_physical_path(path))
                except Exception as e:
                    logger.debug(f"[CACHE] Could not get physical path for {path}: {e}")

        # === Common logic: write to L1 Rust metadata cache ===
        l1_cache = self._get_l1_cache()
        if l1_cache is not None and disk_path:
            try:
                is_text = content_type in ("full", "parsed", "summary")
                ttl = getattr(self, "cache_ttl", 0) or 0
                l1_cache.put(
                    key=path,
                    path_id=path_id,
                    content_hash=content_hash,
                    disk_path=disk_path,
                    original_size=original_size,
                    ttl_seconds=ttl,
                    is_text=is_text,
                    zone_id=cache_zone,
                )
                mode = "L1+L2" if has_l2 else "L1 (l1_only)"
                logger.info(f"[CACHE] WRITE to {mode}: {path} (size={original_size})")
            except Exception as e:
                logger.debug("[CACHE] L1 cache populate failed for %s: %s", path, e)

        # === Common logic: return CacheEntry ===
        return CacheEntry(
            cache_id=cache_id,
            path_id=path_id,
=======
        """Write content to cache."""
        return self._cache_service.write_to_cache(
            path=path,
            content=content,
>>>>>>> origin/develop
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
    ) -> int:
        """Invalidate cache entries (L1 memory + L2 disk)."""
        return self._cache_service.invalidate_cache(path, mount_prefix, delete)

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

<<<<<<< HEAD
    def get_content_hash(
        self,
        path: str,
    ) -> str | None:
        """Get the content hash (ETag) for a path from cache without reading content.

        This enables efficient ETag/If-None-Match checks without downloading
        the full content. Useful for 304 Not Modified responses.

        Args:
            path: Virtual file path

        Returns:
            Content hash (ETag) if cached, None otherwise
        """
        if not self._has_caching():
            return None

        cached = self._read_from_cache(path, original=False)
        if cached and not cached.stale:
            return cached.content_hash
        return None

    def batch_write_to_cache(
        self,
        entries: list[dict],
    ) -> list[CacheEntry]:
        """Write multiple entries to cache in a single transaction.

        This is much more efficient than calling _write_to_cache repeatedly,
        as it commits all changes in one database transaction.

        Args:
            entries: List of dicts with keys:
                - path: Virtual file path
                - content: Original binary content
                - content_text: Optional parsed text
                - content_type: 'full', 'parsed', 'summary', or 'reference'
                - backend_version: Optional backend version
                - parsed_from: Optional parser name
                - parse_metadata: Optional parse metadata dict
                - zone_id: Optional zone ID

        Returns:
            List of CacheEntry objects (one per successfully written entry)
        """
        if not entries:
            return []

        now = datetime.now(UTC)
        l1_cache = self._get_l1_cache()
        file_cache = get_file_cache()

        # Get path_ids in bulk if DB session available (for FilePathModel updates)
        session = self._get_db_session()
        path_id_map: dict[str, str] = {}
        if session:
            paths = [e["path"] for e in entries]
            path_id_map = self._get_path_ids_bulk(paths, session)

        cache_entries: list[CacheEntry] = []

        for entry_data in entries:
            try:
                path = entry_data["path"]
                content = entry_data["content"]
                content_text = entry_data.get("content_text")
                content_type = entry_data.get("content_type", "full")
                backend_version = entry_data.get("backend_version")
                parsed_from = entry_data.get("parsed_from")
                parse_metadata = entry_data.get("parse_metadata")
                zone_id = entry_data.get("zone_id")

                path_id = path_id_map.get(path, "")

                # Compute content hash
                content_hash = hash_content(content)

                # Determine text content
                if content_text is None:
                    try:
                        content_text = content.decode("utf-8")
                    except UnicodeDecodeError:
                        content_text = None
                        content_type = "reference"

                # Handle large files
                original_size = len(content)
                if content_text and len(content_text) > self.MAX_FULL_TEXT_SIZE:
                    content_text = content_text[: self.SUMMARY_SIZE]
                    content_type = "summary"

                cached_size = len(content_text) if content_text else 0

                # Write binary + text content to disk via FileContentCache
                cache_zone = zone_id or "root"
                if original_size <= self.MAX_CACHE_FILE_SIZE:
                    try:
                        file_cache.write(cache_zone, path, content, text_content=content_text)
                    except Exception as e:
                        logger.warning(f"[CACHE] Failed to write to disk cache: {path}: {e}")

                # Write metadata sidecar (replaces ContentCacheModel DB entry)
                meta = {
                    "path_id": path_id,
                    "zone_id": cache_zone,
                    "content_hash": content_hash,
                    "content_type": content_type,
                    "original_size": original_size,
                    "cached_size": cached_size,
                    "backend_version": backend_version,
                    "parsed_from": parsed_from,
                    "parse_metadata": parse_metadata,
                    "synced_at": now.isoformat(),
                    "stale": False,
                }
                file_cache.write_meta(cache_zone, path, meta)

                # Create CacheEntry for return value
                cache_entry = CacheEntry(
                    cache_id="",
                    path_id=path_id,
                    content_text=content_text,
                    _content_binary=content if original_size <= self.MAX_CACHE_FILE_SIZE else None,
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
                cache_entries.append(cache_entry)

                # Update L1 Rust metadata cache
                if l1_cache is not None:
                    try:
                        disk_path = str(file_cache._get_cache_path(cache_zone, path))
                        is_text = content_type in ("full", "parsed", "summary")
                        ttl = getattr(self, "cache_ttl", 0) or 0
                        l1_cache.put(
                            key=path,
                            path_id=path_id,
                            content_hash=content_hash,
                            disk_path=disk_path,
                            original_size=original_size,
                            ttl_seconds=ttl,
                            is_text=is_text,
                            zone_id=cache_zone,
                        )
                    except Exception as e:
                        logger.debug("[CACHE] L1 cache populate failed for %s: %s", path, e)

            except Exception as e:
                logger.error(f"[CACHE] Failed to prepare cache entry for {path}: {e}")

        # Update file_paths in DB for consistency (if session available)
        if session and cache_entries:
            try:
                size_updates = {ce.path_id: ce.original_size for ce in cache_entries if ce.path_id}
                hash_updates = {ce.path_id: ce.content_hash for ce in cache_entries if ce.path_id}

                if size_updates:
                    file_path_stmt = select(FilePathModel).where(
                        FilePathModel.path_id.in_(list(size_updates.keys()))
                    )
                    file_path_result = session.execute(file_path_stmt)
                    file_paths = file_path_result.scalars().all()

                    updated_count = 0
                    for file_path in file_paths:
                        updated = False
                        new_size = size_updates.get(file_path.path_id)
                        new_hash = hash_updates.get(file_path.path_id)
                        if new_size and file_path.size_bytes != new_size:
                            file_path.size_bytes = new_size
                            updated = True
                        if new_hash and file_path.content_hash != new_hash:
                            file_path.content_hash = new_hash
                            updated = True
                        if updated:
                            file_path.updated_at = now
                            updated_count += 1

                    if updated_count > 0:
                        logger.info(
                            f"[CACHE] Updated {updated_count} file_paths entries (size + content_hash)"
                        )
                session.commit()
            except Exception as e:
                logger.warning(f"[CACHE] Failed to update file_paths in batch: {e}")

        logger.info(f"[CACHE] Batch wrote {len(cache_entries)} entries to L1+L2 (disk)")

        return cache_entries

    def _invalidate_cache(
        self,
        path: str | None = None,
        mount_prefix: str | None = None,
        delete: bool = False,
    ) -> int:
        """Invalidate cache entries (L1 memory + L2 disk).

        Per data-storage-matrix.md, ContentCacheModel (DB) has been eliminated.
        L2 is now disk-only (FileContentCache). Invalidation always deletes
        from disk since disk cache is ephemeral (Session durability).

        Args:
            path: Specific path to invalidate
            mount_prefix: Invalidate all paths under this prefix
            delete: If True, delete entries. If False, also delete (no stale concept on disk).

        Returns:
            Number of entries invalidated
        """
        # Invalidate L1 memory cache
        memory_cache = self._get_l1_cache()
        file_cache = get_file_cache()
        cache_zone = getattr(self, "zone_id", None) or "root"

        if path:
            # Remove specific path from memory cache
            memory_key = f"cache_entry:{path}"
            if memory_cache is not None:
                memory_cache.remove(memory_key)

            # Delete from disk cache (both content and metadata sidecar)
            file_cache.delete(cache_zone, path)
            return 1

        elif mount_prefix:
            # For prefix invalidation, clear entire memory cache
            # (More targeted invalidation would require iterating all keys)
            if memory_cache is not None:
                memory_cache.clear()

            # Find all paths under mount prefix via FilePathModel, then delete from disk
            session = self._get_db_session()
            mount_stmt = select(FilePathModel.virtual_path).where(
                FilePathModel.virtual_path.startswith(mount_prefix)
            )
            result = session.execute(mount_stmt)
            rows = result.scalars().all()

            count = 0
            for vpath in rows:
                file_cache.delete(cache_zone, vpath)
                count += 1

            return count

        return 0

    def _check_version(
        self,
        path: str,
        expected_version: str,
        context: OperationContext | None = None,
    ) -> bool:
        """Check if backend version matches expected.

        Args:
            path: Virtual file path
            expected_version: Expected backend version
            context: Operation context

        Returns:
            True if versions match, False otherwise

        Raises:
            ConflictError: If versions don't match
        """
        if not hasattr(self, "get_version"):
            return True  # No version support, always succeed

        current_version = self.get_version(path, context)
        if current_version is None:
            return True  # Backend doesn't support versioning

        if current_version != expected_version:
            raise ConflictError(
                path=path,
                expected_etag=expected_version,
                current_etag=current_version,
            )

        return True

=======
>>>>>>> origin/develop
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
        from nexus.backends.sync_pipeline import SyncPipelineService

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
