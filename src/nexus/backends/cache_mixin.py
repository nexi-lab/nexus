"""Cache mixin for connectors.

Provides caching capabilities for connector backends (GCS, S3, X, Gmail, etc.).
Local backend does not use this mixin - caching is only for external connectors.

See docs/design/cache-layer.md for design details.
Part of: #506, #510 (cache layer epic)
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from nexus.core.exceptions import ConflictError
from nexus.core.hash_fast import hash_content
from nexus.core.permissions import OperationContext
from nexus.storage.file_cache import get_file_cache
from nexus.storage.models import ContentCacheModel, FilePathModel

if TYPE_CHECKING:
    from nexus_fast import L1MetadataCache
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Backend version constant for immutable content (e.g., Gmail emails that never change)
IMMUTABLE_VERSION = "immutable"


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


@dataclass
class CachedReadResult:
    """Result of a cached read operation.

    Contains both the content and metadata needed for HTTP caching (ETag, etc.).
    """

    content: bytes
    content_hash: str  # Can be used as ETag
    from_cache: bool  # True if served from cache, False if fetched from backend
    cache_entry: CacheEntry | None = None  # Full cache entry if available


class CacheConnectorMixin:
    """Mixin that adds cache support to connectors.

    Provides a two-level cache:
    - L1: In-memory LRU cache (fast, per-instance, lost on restart)
    - L2: PostgreSQL content_cache table (slower, shared, persistent)

    Usage:
        class GCSConnectorBackend(BaseBlobStorageConnector, CacheConnectorMixin):
            pass

    The connector must have:
        - self.session_factory: SQLAlchemy session factory (preferred)
        - OR self.db_session: SQLAlchemy session (legacy)
        - self._read_from_backend(): Read content from actual backend
        - self._list_files(): List files from backend

    Optional (for version checking):
        - self.get_version(): Get current backend version for a path

    L1-Only Mode:
        Set l1_only=True to skip L2 (PostgreSQL) caching entirely.
        This is useful for connectors where the source is already local
        (e.g., LocalConnector), so L2 persistence provides no benefit.

        When l1_only=True:
        - No session_factory/db_session required
        - L1 cache uses disk_path pointing to original files
        - Connector must implement get_physical_path() for disk_path
    """

    # L1-only mode: skip L2 (PostgreSQL) caching entirely
    # Useful for LocalConnector where source is already local disk
    l1_only: bool = False

    # Callback for sync completion notifications (e.g., zoekt reindex). Issue #1520.
    on_sync_callback: Any | None = None

    # Maximum file size to cache (default 100MB)
    MAX_CACHE_FILE_SIZE: int = 100 * 1024 * 1024

    # L1 Metadata Cache (Rust) - lock-free, stores only metadata (~100 bytes per entry)
    # Content is read via mmap from disk when needed
    # Using class variable so all connectors share the same cache
    _l1_cache: L1MetadataCache | None = None
    _l1_max_entries: int = 100_000  # Default 100k entries
    _l1_default_ttl: int = 300  # Default 5 minutes TTL

    @classmethod
    def _get_l1_cache(cls) -> L1MetadataCache | None:
        """Get or create the shared L1 metadata cache (Rust-based).

        The L1 cache stores only metadata (~100 bytes per entry) instead of
        full content. Content is read via mmap from disk when needed.

        Performance:
        - Lookup: <1μs (vs ~100μs for Python pickle-based L1)
        - Concurrent access: Lock-free (vs Python threading.Lock)
        - Memory: ~100 bytes per entry (vs megabytes for content)
        """
        if cls._l1_cache is None:
            try:
                from nexus_fast import L1MetadataCache

                cls._l1_cache = L1MetadataCache(
                    max_entries=cls._l1_max_entries,
                    default_ttl=cls._l1_default_ttl,
                )
                logger.info(
                    f"[CACHE] L1 Rust cache initialized: max_entries={cls._l1_max_entries}, "
                    f"default_ttl={cls._l1_default_ttl}s"
                )
            except ImportError:
                logger.warning("[CACHE] nexus_fast not available, L1 cache disabled")
                return None
        return cls._l1_cache

    @classmethod
    def set_l1_cache_config(cls, max_entries: int = 100_000, default_ttl: int = 300) -> None:
        """Configure the L1 cache. Must be called before first use.

        Args:
            max_entries: Maximum number of entries (default: 100000)
            default_ttl: Default TTL in seconds (default: 300 = 5 minutes)
        """
        cls._l1_max_entries = max_entries
        cls._l1_default_ttl = default_ttl
        # Reset cache if already created
        if cls._l1_cache is not None:
            cls._l1_cache = None

    @classmethod
    def get_l1_cache_stats(cls) -> dict[str, Any]:
        """Get L1 cache statistics.

        Returns:
            Dict with entries, hits, misses, hit_rate, max_entries, default_ttl
        """
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

    # Maximum text size to store as 'full' (default 10MB)
    MAX_FULL_TEXT_SIZE: int = 10 * 1024 * 1024

    # Summary size for large files (default 100KB)
    SUMMARY_SIZE: int = 100 * 1024

    def _has_caching(self) -> bool:
        """Check if any caching is enabled (L1 or L1+L2).

        Returns True if:
        - l1_only mode is enabled (L1 cache only), OR
        - session_factory/db_session is available (L1+L2 cache)

        This is the standard implementation. Connectors can override if needed.
        """
        # L1-only mode: caching enabled without database
        if getattr(self, "l1_only", False):
            return True
        # L1+L2 mode: requires database session
        return (
            getattr(self, "session_factory", None) is not None
            or getattr(self, "db_session", None) is not None
            or getattr(self, "_db_session", None) is not None
        )

    def _has_l2_caching(self) -> bool:
        """Check if L2 (PostgreSQL) caching is enabled.

        Returns True only if:
        - l1_only mode is NOT enabled, AND
        - session_factory/db_session is available

        Used to skip L2 operations in L1-only mode.
        """
        if getattr(self, "l1_only", False):
            return False
        return (
            getattr(self, "session_factory", None) is not None
            or getattr(self, "db_session", None) is not None
            or getattr(self, "_db_session", None) is not None
        )

    def _get_cache_path(self, context: OperationContext | None) -> str | None:
        """Get the cache key path from context.

        Prefers virtual_path (full path like /mnt/s3/file.txt) over backend_path.
        This ensures cache keys match the file_paths table entries.

        Args:
            context: Operation context with virtual_path and/or backend_path

        Returns:
            The path to use as cache key, or None if no path available
        """
        if context is None:
            return None
        # Prefer virtual_path (full path with mount prefix)
        if hasattr(context, "virtual_path") and context.virtual_path:
            return context.virtual_path
        # Fall back to backend_path
        if hasattr(context, "backend_path") and context.backend_path:
            return context.backend_path
        return None

    def _get_db_session(self) -> Session:
        """Get database session. Override if session is stored differently.

        Supports multiple patterns:
        1. session_factory (SessionLocal) - creates new session each call
        2. db_session - existing session instance
        3. _db_session - existing session instance (alternate attribute)
        """
        # Prefer session factory pattern (creates session per operation)
        if hasattr(self, "session_factory") and self.session_factory is not None:
            return self.session_factory()  # type: ignore[no-any-return]
        # Fall back to existing session
        if hasattr(self, "db_session") and self.db_session is not None:
            return self.db_session  # type: ignore[no-any-return]
        if hasattr(self, "_db_session") and self._db_session is not None:
            return self._db_session  # type: ignore[no-any-return]
        raise RuntimeError("No database session available for caching")

    def _get_path_id(self, path: str, session: Session) -> str | None:
        """Get path_id for a virtual path."""
        stmt = select(FilePathModel.path_id).where(
            FilePathModel.virtual_path == path,
            FilePathModel.deleted_at.is_(None),
        )
        result = session.execute(stmt)
        row = result.scalar_one_or_none()
        return row

    def _get_path_ids_bulk(self, paths: list[str], session: Session) -> dict[str, str]:
        """Get path_ids for multiple virtual paths in a single query.

        Args:
            paths: List of virtual paths
            session: Database session

        Returns:
            Dict mapping virtual_path -> path_id (only for paths that exist)
        """
        if not paths:
            return {}

        stmt = select(FilePathModel.virtual_path, FilePathModel.path_id).where(
            FilePathModel.virtual_path.in_(paths),
            FilePathModel.deleted_at.is_(None),
        )
        result = session.execute(stmt)
        return {row[0]: row[1] for row in result.fetchall()}

    def _read_bulk_from_cache(
        self,
        paths: list[str],
        original: bool = False,
    ) -> dict[str, CacheEntry]:
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

        # L2: Bulk database lookup for remaining paths
        session = self._get_db_session()

        # Get path_ids in bulk
        path_id_map = self._get_path_ids_bulk(paths_needing_l2, session)
        if not path_id_map:
            logger.info(
                f"[CACHE-BULK] {len(results)} L1 hits, {len(paths_needing_l2)} not in file_paths"
            )
            return results

        # Query cache entries in bulk
        path_ids = list(path_id_map.values())
        stmt = select(ContentCacheModel).where(ContentCacheModel.path_id.in_(path_ids))
        db_result = session.execute(stmt)
        cache_models = {cm.path_id: cm for cm in db_result.scalars().all()}

        # Build reverse map: path_id -> virtual_path
        path_id_to_path = {v: k for k, v in path_id_map.items()}

        # Process cache models
        # Bulk read binary content from disk cache
        file_cache = get_file_cache()
        disk_contents: dict[str, bytes] = {}
        if original:
            # Group paths by zone for bulk read
            zone_paths: dict[str, list[str]] = {}
            for path_id, cache_model in cache_models.items():
                vpath = path_id_to_path.get(path_id)
                if vpath:
                    cache_zone = cache_model.zone_id or "default"
                    zone_paths.setdefault(cache_zone, []).append(vpath)

            # Bulk read from disk for each zone
            for zone_id, vpaths in zone_paths.items():
                disk_contents.update(file_cache.read_bulk(zone_id, vpaths))

        l2_hits = 0
        for path_id, cache_model in cache_models.items():
            vpath = path_id_to_path.get(path_id)
            if not vpath:
                continue

            # Parse metadata if stored
            parse_metadata = None
            if cache_model.parse_metadata:
                with contextlib.suppress(Exception):
                    import json

                    parse_metadata = json.loads(cache_model.parse_metadata)

            # Get binary content: disk (primary) or database (fallback for old data)
            content_binary_raw = None
            if original:
                content_binary_raw = disk_contents.get(vpath)
                if not content_binary_raw and cache_model.content_binary:
                    # Fall back to database for backward compatibility
                    content_binary_raw = cache_model.content_binary

            entry = CacheEntry(
                cache_id=cache_model.cache_id,
                path_id=cache_model.path_id,
                content_text=cache_model.content_text,
                _content_binary=None,  # Will be lazily loaded from _content_binary_raw
                content_hash=cache_model.content_hash,
                content_type=cache_model.content_type,
                original_size=cache_model.original_size_bytes,
                cached_size=cache_model.cached_size_bytes,
                backend_version=cache_model.backend_version,
                synced_at=cache_model.synced_at,
                stale=cache_model.stale,
                parsed_from=cache_model.parsed_from,
                parse_metadata=parse_metadata,
                _content_binary_raw=content_binary_raw,
            )
            results[vpath] = entry
            l2_hits += 1

            # Populate L1 Rust metadata cache for future reads
            # Stores only metadata (~100 bytes), not full content
            if l1_cache is not None:
                with contextlib.suppress(Exception):
                    file_cache = get_file_cache()
                    cache_zone = cache_model.zone_id or "default"
                    disk_path = str(file_cache._get_cache_path(cache_zone, vpath))
                    is_text = cache_model.content_type in ("full", "parsed", "summary")
                    l1_cache.put(
                        key=vpath,
                        path_id=cache_model.path_id,
                        content_hash=cache_model.content_hash,
                        disk_path=disk_path,
                        original_size=cache_model.original_size_bytes,
                        ttl_seconds=0,  # Use default TTL
                        is_text=is_text,
                        zone_id=cache_zone,
                    )

        logger.info(
            f"[CACHE-BULK] {len(results) - l2_hits} L1 hits, {l2_hits} L2 hits, "
            f"{len(paths) - len(results)} misses (total {len(paths)} paths)"
        )
        return results

    def read_content_bulk(
        self,
        paths: list[str],
        context: OperationContext | None = None,
    ) -> dict[str, bytes]:
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
        cache_entries = self._read_bulk_from_cache(paths, original=True)

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
            with contextlib.suppress(Exception):
                content = self._read_content_from_backend(path, context)
                if content:
                    results[path] = content

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

        # L2: Check database cache (skip if l1_only mode)
        if not self._has_l2_caching():
            logger.debug(f"[CACHE] L2 SKIP (l1_only mode): {path}")
            return None

        session = self._get_db_session()

        path_id = self._get_path_id(path, session)
        if not path_id:
            return None

        stmt = select(ContentCacheModel).where(ContentCacheModel.path_id == path_id)
        result = session.execute(stmt)
        cache_model = result.scalar_one_or_none()

        if not cache_model:
            logger.debug(f"[CACHE] L2 MISS (database): {path}")
            return None

        logger.info(f"[CACHE] L2 HIT (database): {path}")

        # Parse metadata if stored
        parse_metadata = None
        if cache_model.parse_metadata:
            with contextlib.suppress(Exception):
                import json

                parse_metadata = json.loads(cache_model.parse_metadata)

        # Read binary content from disk (primary) or database (fallback for old data)
        content_binary_raw = None
        if original:
            # Try disk cache first (new storage)
            file_cache = get_file_cache()
            cache_zone = cache_model.zone_id or "default"
            content_binary_raw = file_cache.read(cache_zone, path)
            if content_binary_raw:
                logger.debug(f"[CACHE] L2 content from DISK: {path}")
            elif cache_model.content_binary:
                # Fall back to database for backward compatibility
                content_binary_raw = cache_model.content_binary
                logger.debug(f"[CACHE] L2 content from DB (legacy): {path}")

        entry = CacheEntry(
            cache_id=cache_model.cache_id,
            path_id=cache_model.path_id,
            content_text=cache_model.content_text,
            _content_binary=None,  # Will be lazily loaded from _content_binary_raw
            content_hash=cache_model.content_hash,
            content_type=cache_model.content_type,
            original_size=cache_model.original_size_bytes,
            cached_size=cache_model.cached_size_bytes,
            backend_version=cache_model.backend_version,
            synced_at=cache_model.synced_at,
            stale=cache_model.stale,
            parsed_from=cache_model.parsed_from,
            parse_metadata=parse_metadata,
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
        # Stores only metadata (~100 bytes), not full content
        if l1_cache is not None:
            with contextlib.suppress(Exception):
                file_cache = get_file_cache()
                cache_zone = cache_model.zone_id or "default"
                disk_path = str(file_cache._get_cache_path(cache_zone, path))
                is_text = cache_model.content_type in ("full", "parsed", "summary")
                # Use connector-specific TTL if defined
                ttl = getattr(self, "cache_ttl", 0) or 0
                l1_cache.put(
                    key=path,
                    path_id=cache_model.path_id,
                    content_hash=cache_model.content_hash,
                    disk_path=disk_path,
                    original_size=cache_model.original_size_bytes,
                    ttl_seconds=ttl,
                    is_text=is_text,
                    zone_id=cache_zone,
                )
                logger.debug(f"[CACHE] L1 POPULATED from L2: {path}")

        return entry

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
        cache_zone = zone_id or "default"

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
            # L1+L2 mode: write to PostgreSQL + FileContentCache
            session = self._get_db_session()
            path_id = self._get_path_id(path, session)
            if not path_id:
                raise ValueError(f"Path not found in file_paths: {path}")

            # Write binary content to disk via FileContentCache
            file_cache = get_file_cache()
            if original_size <= self.MAX_CACHE_FILE_SIZE:
                try:
                    file_cache.write(cache_zone, path, content, text_content=content_text)
                    logger.debug(f"[CACHE] Wrote {original_size} bytes to disk: {path}")
                except Exception as e:
                    logger.warning(f"[CACHE] Failed to write to disk cache: {e}")

            # Serialize parse_metadata
            parse_metadata_json = None
            if parse_metadata:
                import json

                parse_metadata_json = json.dumps(parse_metadata)

            # Check if entry exists in PostgreSQL
            stmt = select(ContentCacheModel).where(ContentCacheModel.path_id == path_id)
            result = session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing:
                # Update existing entry
                existing.content_text = content_text
                existing.content_binary = None
                existing.content_hash = content_hash
                existing.content_type = content_type
                existing.original_size_bytes = original_size
                existing.cached_size_bytes = cached_size
                existing.backend_version = backend_version
                existing.parsed_from = parsed_from
                existing.parser_version = None
                existing.parse_metadata = parse_metadata_json
                existing.synced_at = now
                existing.stale = False
                existing.updated_at = now
                cache_id = existing.cache_id
            else:
                # Create new entry
                cache_id = str(uuid.uuid4())
                cache_model = ContentCacheModel(
                    cache_id=cache_id,
                    path_id=path_id,
                    zone_id=zone_id,
                    content_text=content_text,
                    content_binary=None,
                    content_hash=content_hash,
                    content_type=content_type,
                    original_size_bytes=original_size,
                    cached_size_bytes=cached_size,
                    backend_version=backend_version,
                    parsed_from=parsed_from,
                    parser_version=None,
                    parse_metadata=parse_metadata_json,
                    synced_at=now,
                    stale=False,
                    created_at=now,
                    updated_at=now,
                )
                session.add(cache_model)

            # Update file_paths for consistency
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
            except Exception as e:
                logger.warning(f"[CACHE] Failed to update file_paths for {path}: {e}")

            session.commit()

            # disk_path for L1 points to FileContentCache
            disk_path = str(get_file_cache()._get_cache_path(cache_zone, path))
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
            with contextlib.suppress(Exception):
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

        # === Common logic: return CacheEntry ===
        return CacheEntry(
            cache_id=cache_id,
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

    # =========================================================================
    # Automatic Caching API (for new connectors)
    # =========================================================================

    def read_content_with_cache(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> CachedReadResult:
        """Read content with automatic L1/L2 caching.

        This method provides automatic caching for connector read operations:
        1. Check L1 (memory) and L2 (PostgreSQL) cache
        2. If cache hit and not stale, return cached content
        3. If cache miss or stale, call _fetch_content() to get from backend
        4. Write fetched content to cache
        5. Return content with metadata (content_hash for ETag support)

        New connectors should:
        1. Inherit from CacheConnectorMixin
        2. Implement _fetch_content() to fetch from their backend
        3. Call read_content_with_cache() in their read_content() method

        Example:
            class NewConnector(Backend, CacheConnectorMixin):
                def _fetch_content(self, content_hash, context):
                    # Fetch from your backend (API, storage, etc.)
                    return self._api_client.get(context.backend_path)

                def read_content(self, content_hash, context):
                    result = self.read_content_with_cache(content_hash, context)
                    return result.content

        Args:
            content_hash: Content hash (may be ignored by some connectors)
            context: Operation context with backend_path, zone_id, etc.

        Returns:
            CachedReadResult with content, content_hash (ETag), and cache metadata

        Raises:
            ValueError: If context.backend_path is not set
            BackendError: If fetch fails
        """
        if not context or not context.backend_path:
            raise ValueError("context with backend_path is required")

        path = self._get_cache_path(context) or context.backend_path
        zone_id = getattr(context, "zone_id", None)

        # Step 1: Check cache (L1 then L2)
        if self._has_caching():
            cached = self._read_from_cache(path, original=True)
            if cached and not cached.stale and cached.content_binary:
                logger.debug(f"[CACHE] HIT: {path}")
                return CachedReadResult(
                    content=cached.content_binary,
                    content_hash=cached.content_hash,
                    from_cache=True,
                    cache_entry=cached,
                )

        # Step 2: Fetch from backend
        logger.debug(f"[CACHE] MISS: {path} - fetching from backend")
        content = self._fetch_content(content_hash, context)

        # Step 3: Write to cache
        result_hash = hash_content(content)
        cache_entry = None

        if self._has_caching():
            try:
                cache_entry = self._write_to_cache(
                    path=path,
                    content=content,
                    backend_version=self._get_backend_version(context),
                    zone_id=zone_id,
                )
                result_hash = cache_entry.content_hash
            except Exception as e:
                logger.warning(f"[CACHE] Failed to cache {path}: {e}")

        return CachedReadResult(
            content=content,
            content_hash=result_hash,
            from_cache=False,
            cache_entry=cache_entry,
        )

    def _fetch_content(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> bytes:
        """Fetch content from the backend (to be implemented by connectors).

        New connectors should override this method to implement their
        backend-specific fetch logic. The caching is handled automatically
        by read_content_with_cache().

        Args:
            content_hash: Content hash (may be ignored by some connectors)
            context: Operation context with backend_path

        Returns:
            Content as bytes

        Raises:
            NotImplementedError: If not overridden by connector
            BackendError: If fetch fails
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement _fetch_content() "
            "to use read_content_with_cache()"
        )

    def _get_backend_version(
        self,
        context: OperationContext | None = None,
    ) -> str | None:
        """Get the backend version for a path (for cache invalidation).

        Override this in connectors that support versioning (S3, GCS).
        API connectors (HN, X) typically return None and use TTL-based expiration.

        Args:
            context: Operation context with backend_path

        Returns:
            Backend version string, or None if not supported
        """
        # Default: no versioning (use TTL-based expiration)
        return None

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

    def _batch_write_to_cache(
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

        session = self._get_db_session()
        now = datetime.now(UTC)
        l1_cache = self._get_l1_cache()  # Rust L1 metadata cache
        file_cache = get_file_cache()  # Disk cache for binary content

        # Get all path_ids in bulk
        paths = [e["path"] for e in entries]
        path_id_map = self._get_path_ids_bulk(paths, session)

        # Get existing cache entries in bulk
        existing_stmt = select(ContentCacheModel).where(
            ContentCacheModel.path_id.in_(list(path_id_map.values()))
        )
        existing_result = session.execute(existing_stmt)
        existing_by_path_id = {ce.path_id: ce for ce in existing_result.scalars().all()}

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

                path_id = path_id_map.get(path)
                if not path_id:
                    logger.warning(f"[CACHE] Path not found in file_paths, skipping: {path}")
                    continue

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

                # Write binary content to disk via FileContentCache
                cache_zone = zone_id or "default"
                if original_size <= self.MAX_CACHE_FILE_SIZE:
                    try:
                        file_cache.write(cache_zone, path, content, text_content=content_text)
                    except Exception as e:
                        logger.warning(f"[CACHE] Failed to write to disk cache: {path}: {e}")

                # Serialize parse_metadata
                parse_metadata_json = None
                if parse_metadata:
                    import json

                    parse_metadata_json = json.dumps(parse_metadata)

                # Update or create entry (content_binary=None, content now on disk)
                existing = existing_by_path_id.get(path_id)
                if existing:
                    # Update existing entry
                    existing.content_text = content_text
                    existing.content_binary = None  # Binary stored on disk via FileContentCache
                    existing.content_hash = content_hash
                    existing.content_type = content_type
                    existing.original_size_bytes = original_size
                    existing.cached_size_bytes = cached_size
                    existing.backend_version = backend_version
                    existing.parsed_from = parsed_from
                    existing.parser_version = None
                    existing.parse_metadata = parse_metadata_json
                    existing.synced_at = now
                    existing.stale = False
                    existing.updated_at = now
                    cache_id = existing.cache_id
                else:
                    # Create new entry
                    cache_id = str(uuid.uuid4())
                    cache_model = ContentCacheModel(
                        cache_id=cache_id,
                        path_id=path_id,
                        zone_id=zone_id,
                        content_text=content_text,
                        content_binary=None,  # Binary stored on disk via FileContentCache
                        content_hash=content_hash,
                        content_type=content_type,
                        original_size_bytes=original_size,
                        cached_size_bytes=cached_size,
                        backend_version=backend_version,
                        parsed_from=parsed_from,
                        parser_version=None,
                        parse_metadata=parse_metadata_json,
                        synced_at=now,
                        stale=False,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(cache_model)

                # Create CacheEntry for return value
                cache_entry = CacheEntry(
                    cache_id=cache_id,
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

                # Update L1 Rust metadata cache (stores only metadata, not content)
                if l1_cache is not None:
                    with contextlib.suppress(Exception):
                        disk_path = str(file_cache._get_cache_path(cache_zone, path))
                        is_text = content_type in ("full", "parsed", "summary")
                        # Use connector-specific TTL if defined
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
                logger.error(f"[CACHE] Failed to prepare cache entry for {path}: {e}")

        # Update file_paths for all cached entries (bulk update for efficiency)
        # This keeps file_paths consistent with cache and enables get_etag() for connectors
        try:
            if cache_entries:
                # Build maps for bulk update
                size_updates = {ce.path_id: ce.original_size for ce in cache_entries}
                hash_updates = {ce.path_id: ce.content_hash for ce in cache_entries}

                # Get all FilePathModel entries in bulk
                file_path_stmt = select(FilePathModel).where(
                    FilePathModel.path_id.in_(list(size_updates.keys()))
                )
                file_path_result = session.execute(file_path_stmt)
                file_paths = file_path_result.scalars().all()

                # Update size_bytes and content_hash for each
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
        except Exception as e:
            # Don't fail batch write if file_paths update fails
            logger.warning(f"[CACHE] Failed to update file_paths in batch: {e}")

        # Commit all changes in single transaction
        session.commit()
        logger.info(f"[CACHE] Batch wrote {len(cache_entries)} entries to L1+L2")

        return cache_entries

    def _invalidate_cache(
        self,
        path: str | None = None,
        mount_prefix: str | None = None,
        delete: bool = False,
    ) -> int:
        """Invalidate cache entries (L1 memory, L2 database, and disk).

        Args:
            path: Specific path to invalidate
            mount_prefix: Invalidate all paths under this prefix
            delete: If True, delete entries. If False, mark as stale.

        Returns:
            Number of entries invalidated
        """
        # Invalidate L1 memory cache
        memory_cache = self._get_l1_cache()
        file_cache = get_file_cache()
        if path:
            # Remove specific path from memory cache
            memory_key = f"cache_entry:{path}"
            if memory_cache is not None:
                memory_cache.remove(memory_key)
        elif mount_prefix:
            # For prefix invalidation, clear entire memory cache
            # (More targeted invalidation would require iterating all keys)
            if memory_cache is not None:
                memory_cache.clear()

        # Invalidate L2 database cache
        session = self._get_db_session()

        if path:
            path_id = self._get_path_id(path, session)
            if not path_id:
                return 0

            stmt = select(ContentCacheModel).where(ContentCacheModel.path_id == path_id)
            result = session.execute(stmt)
            entry = result.scalar_one_or_none()

            if not entry:
                return 0

            if delete:
                # Delete from disk cache
                cache_zone = entry.zone_id or "default"
                file_cache.delete(cache_zone, path)
                session.delete(entry)
            else:
                entry.stale = True
                entry.updated_at = datetime.now(UTC)

            session.commit()
            return 1

        elif mount_prefix:
            # Invalidate all entries under mount prefix
            mount_stmt = (
                select(ContentCacheModel, FilePathModel.virtual_path)
                .join(FilePathModel, ContentCacheModel.path_id == FilePathModel.path_id)
                .where(FilePathModel.virtual_path.startswith(mount_prefix))
            )
            result = session.execute(mount_stmt)
            rows = result.all()

            count = 0
            for cache_entry, vpath in rows:
                if delete:
                    # Delete from disk cache
                    cache_zone = cache_entry.zone_id or "default"
                    file_cache.delete(cache_zone, vpath)
                    session.delete(cache_entry)
                else:
                    cache_entry.stale = True
                    cache_entry.updated_at = datetime.now(UTC)
                count += 1

            session.commit()
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
        """Sync content from connector backend to cache layer.

        Delegates to SyncPipelineService which orchestrates a 7-step process
        to efficiently sync content from external storage backends (GCS, S3,
        Gmail, etc.) into a two-level cache (L1 in-memory + L2 PostgreSQL).

        Args:
            path: Specific path to sync (relative to mount), or None for entire mount
            mount_point: Virtual mount point (e.g., "/mnt/gcs")
            include_patterns: Glob patterns to include (e.g., ["*.py", "*.md"])
            exclude_patterns: Glob patterns to exclude (e.g., ["*.pyc", ".git/*"])
            max_file_size: Maximum file size to cache (default: MAX_CACHE_FILE_SIZE)
            generate_embeddings: Generate embeddings for semantic search (default: True)
            context: Operation context with zone_id, user, etc.

        Returns:
            SyncResult with statistics (files_scanned, files_synced, etc.)
        """
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

        # Notify search brick via callback if files were synced (Issue #1520)
        if result.files_synced > 0 and self.on_sync_callback is not None:
            self.on_sync_callback(result.files_synced)

        return result

    # Backward compatibility alias
    def sync(self, *args: Any, **kwargs: Any) -> SyncResult:
        """Deprecated: Use sync_content_to_cache() instead."""
        import warnings

        warnings.warn(
            "CacheConnectorMixin.sync() is deprecated. Use sync_content_to_cache() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.sync_content_to_cache(*args, **kwargs)

    def _batch_read_from_backend(
        self,
        paths: list[str],
        contexts: dict[str, OperationContext] | None = None,
    ) -> dict[str, bytes]:
        """Batch read content directly from backend (bypassing cache).

        Leverages _bulk_download_blobs() for efficient parallel downloads when
        available (BaseBlobStorageConnector subclasses). Falls back to sequential
        reads for other connector types.

        Args:
            paths: List of backend-relative paths
            contexts: Optional dict mapping path -> OperationContext

        Returns:
            Dict mapping path -> content bytes (only successful reads)

        Performance:
            - With _bulk_download_blobs: 10-20x speedup via parallel downloads
              - GCS: ~15x speedup with 10 workers (optimal)
              - S3: ~20x speedup with 20 workers
            - Without: Sequential reads (fallback)
        """
        # Check if this connector has bulk download support
        if hasattr(self, "_bulk_download_blobs") and hasattr(self, "_get_blob_path"):
            # Use optimized bulk download for blob storage connectors
            logger.info(f"[BATCH-READ] Using bulk download for {len(paths)} paths")

            # Convert backend paths to blob paths
            blob_paths = [self._get_blob_path(path) for path in paths]

            # Extract version IDs from contexts if available
            version_ids: dict[str, str] = {}
            if contexts:
                for path in paths:
                    context = contexts.get(path)
                    if context and hasattr(context, "version_id") and context.version_id:
                        blob_path = self._get_blob_path(path)
                        version_ids[blob_path] = context.version_id

            # Bulk download all blobs in parallel
            # Uses connector's default max_workers (GCS: 10, S3: 20, Base: 20)
            blob_results = self._bulk_download_blobs(
                blob_paths,
                version_ids=version_ids if version_ids else None,
            )

            # Map blob paths back to backend paths
            blob_to_backend = {self._get_blob_path(p): p for p in paths}
            results: dict[str, bytes] = {}
            for blob_path, content in blob_results.items():
                backend_path = blob_to_backend.get(blob_path)
                if backend_path:
                    results[backend_path] = content

            logger.info(
                f"[BATCH-READ] Bulk download complete: {len(results)}/{len(paths)} successful"
            )
            return results

        # Check if this connector has custom bulk download support
        if hasattr(self, "_bulk_download_contents"):
            # Use connector-specific bulk download
            logger.info(f"[BATCH-READ] Using connector bulk download for {len(paths)} paths")
            results = self._bulk_download_contents(paths, contexts)
            logger.info(
                f"[BATCH-READ] Bulk download complete: {len(results)}/{len(paths)} successful"
            )
            return results

        # Fallback: sequential reads for non-blob connectors
        logger.info(f"[BATCH-READ] Falling back to sequential reads for {len(paths)} paths")
        results = {}
        for path in paths:
            context = contexts.get(path) if contexts else None
            content = self._read_content_from_backend(path, context)
            if content is not None:
                results[path] = content
        return results

    def _read_content_from_backend(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> bytes | None:
        """Read content directly from backend (bypassing cache).

        Args:
            path: Backend-relative path
            context: Operation context with backend_path set

        Override this if your connector has a different read method.
        """
        # Try direct blob download first (bypasses cache in read_content)
        if hasattr(self, "_download_blob") and hasattr(self, "_get_blob_path"):
            try:
                blob_path = self._get_blob_path(path)
                content: bytes = self._download_blob(blob_path)
                return content
            except Exception:
                pass

        # Fall back to read_content (may use cache)
        if hasattr(self, "read_content"):
            try:
                # read_content expects (content_hash, context) for GCS connector
                # Pass empty content_hash and let it use context.backend_path
                return self.read_content("", context)  # type: ignore
            except Exception:
                return None
        return None

    def _parse_content(
        self,
        path: str,
        content: bytes,
    ) -> tuple[str | None, str | None, dict | None]:
        """Parse content using the parser registry.

        Args:
            path: File path (used to determine file type)
            content: Raw file content

        Returns:
            Tuple of (parsed_text, parsed_from, parse_metadata)
            Returns (None, None, None) if parsing fails or not supported
        """
        try:
            from nexus.parsers.markitdown_parser import MarkItDownParser
        except ImportError:
            return None, None, None

        try:
            # Get file extension
            ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""

            # Check if parser supports this format
            parser = MarkItDownParser()
            if ext not in parser.supported_formats:
                return None, None, None

            # Parse content
            import asyncio

            # Run async parse in sync context
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    parser.parse(content, {"path": path, "filename": path.split("/")[-1]})
                )
            finally:
                loop.close()

            if result and result.text:
                return result.text, ext.lstrip("."), {"chunks": len(result.chunks)}

        except Exception:
            pass

        return None, None, None

    def _generate_embeddings(self, path: str) -> None:
        """Generate embeddings for a file.

        Override this to integrate with semantic search.
        Default implementation is a no-op.
        """
        # TODO: Integrate with SemanticSearch.index_document()
        pass

    def _get_size_from_cache(self, path: str) -> int | None:
        """Get file size from cache (efficient, no backend call).

        This checks both L1 (memory) and L2 (database) cache for the file's
        original size. This is much more efficient than fetching content or
        calling the backend API.

        Args:
            path: Virtual file path

        Returns:
            File size in bytes, or None if not in cache

        Performance:
            - L1 hit: <1ms (memory lookup)
            - L2 hit: ~5-10ms (single DB query)
            - Miss: None returned, caller should fall back to backend
        """
        if not self._has_caching():
            return None

        try:
            # Check cache (L1 + L2)
            entry = self._read_from_cache(path, original=False)
            if entry and not entry.stale:
                logger.debug(f"[CACHE] SIZE HIT: {path} ({entry.original_size} bytes)")
                return entry.original_size
            logger.debug(f"[CACHE] SIZE MISS: {path}")
        except Exception as e:
            logger.debug(f"[CACHE] SIZE ERROR for {path}: {e}")

        return None
