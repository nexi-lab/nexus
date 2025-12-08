"""Cache mixin for connectors.

Provides caching capabilities for connector backends (GCS, S3, X, Gmail, etc.).
Local backend does not use this mixin - caching is only for external connectors.

See docs/design/cache-layer.md for design details.
Part of: #506, #510 (cache layer epic)
"""

from __future__ import annotations

import base64
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from nexus.core.exceptions import ConflictError
from nexus.core.hash_fast import hash_content
from nexus.core.permissions import OperationContext
from nexus.storage.models import ContentCacheModel, FilePathModel

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.storage.content_cache import ContentCache

logger = logging.getLogger(__name__)


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
    """A cached content entry."""

    cache_id: str
    path_id: str
    content_text: str | None
    content_binary: bytes | None
    content_hash: str
    content_type: str
    original_size: int
    cached_size: int
    backend_version: str | None
    synced_at: datetime
    stale: bool
    parsed_from: str | None = None
    parse_metadata: dict | None = None


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
    """

    # Maximum file size to cache (default 100MB)
    MAX_CACHE_FILE_SIZE: int = 100 * 1024 * 1024

    # In-memory LRU cache (L1) - shared across all instances of this mixin
    # Using class variable so all connectors share the same cache
    _memory_cache: ContentCache | None = None
    _memory_cache_size_mb: int = 256  # Default 256MB

    @classmethod
    def _get_memory_cache(cls) -> ContentCache:
        """Get or create the shared in-memory cache."""
        if cls._memory_cache is None:
            from nexus.storage.content_cache import ContentCache

            cls._memory_cache = ContentCache(max_size_mb=cls._memory_cache_size_mb)
        return cls._memory_cache

    @classmethod
    def set_memory_cache_size(cls, size_mb: int) -> None:
        """Configure the in-memory cache size. Must be called before first use.

        Args:
            size_mb: Maximum cache size in megabytes
        """
        cls._memory_cache_size_mb = size_mb
        # Reset cache if already created
        if cls._memory_cache is not None:
            cls._memory_cache = None

    @classmethod
    def get_memory_cache_stats(cls) -> dict[str, int]:
        """Get in-memory cache statistics.

        Returns:
            Dict with entries, size_bytes, size_mb, max_size_mb
        """
        if cls._memory_cache is None:
            return {
                "entries": 0,
                "size_bytes": 0,
                "size_mb": 0,
                "max_size_mb": cls._memory_cache_size_mb,
            }
        return cls._memory_cache.get_stats()

    @classmethod
    def clear_memory_cache(cls) -> None:
        """Clear the in-memory cache."""
        if cls._memory_cache is not None:
            cls._memory_cache.clear()

    # Maximum text size to store as 'full' (default 10MB)
    MAX_FULL_TEXT_SIZE: int = 10 * 1024 * 1024

    # Summary size for large files (default 100KB)
    SUMMARY_SIZE: int = 100 * 1024

    def _has_caching(self) -> bool:
        """Check if caching is enabled (session factory or db_session available).

        This is the standard implementation. Connectors can override if needed.
        """
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

        # L1: Check in-memory cache first
        memory_cache = self._get_memory_cache()
        for path in paths:
            memory_key = f"cache_entry:{path}"
            cached_bytes = memory_cache.get(memory_key)
            if cached_bytes is not None:
                with contextlib.suppress(Exception):
                    import pickle

                    entry: CacheEntry = pickle.loads(cached_bytes)
                    results[path] = entry
                    logger.debug(f"[CACHE-BULK] L1 HIT: {path}")
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
        l2_hits = 0
        for path_id, cache_model in cache_models.items():
            vpath = path_id_to_path.get(path_id)
            if not vpath:
                continue

            # Decode binary if stored
            content_binary = None
            if original and cache_model.content_binary:
                with contextlib.suppress(Exception):
                    content_binary = base64.b64decode(cache_model.content_binary)

            # Parse metadata if stored
            parse_metadata = None
            if cache_model.parse_metadata:
                with contextlib.suppress(Exception):
                    import json

                    parse_metadata = json.loads(cache_model.parse_metadata)

            entry = CacheEntry(
                cache_id=cache_model.cache_id,
                path_id=cache_model.path_id,
                content_text=cache_model.content_text,
                content_binary=content_binary,
                content_hash=cache_model.content_hash,
                content_type=cache_model.content_type,
                original_size=cache_model.original_size_bytes,
                cached_size=cache_model.cached_size_bytes,
                backend_version=cache_model.backend_version,
                synced_at=cache_model.synced_at,
                stale=cache_model.stale,
                parsed_from=cache_model.parsed_from,
                parse_metadata=parse_metadata,
            )
            results[vpath] = entry
            l2_hits += 1

            # Populate L1 memory cache for future reads
            with contextlib.suppress(Exception):
                import pickle

                memory_key = f"cache_entry:{vpath}"
                memory_cache.put(memory_key, pickle.dumps(entry))

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
        """Read content from cache (L1 in-memory, then L2 database).

        Args:
            path: Virtual file path
            original: If True, return binary content even for parsed files

        Returns:
            CacheEntry if cached, None otherwise (or if TTL expired)

        Note:
            If the connector defines `cache_ttl` (in seconds), entries older
            than TTL are treated as cache misses. This is used by API connectors
            (HN, etc.) that need time-based expiration instead of version-based.
        """
        # L1: Check in-memory cache first (keyed by path)
        memory_cache = self._get_memory_cache()
        memory_key = f"cache_entry:{path}"
        cached_bytes = memory_cache.get(memory_key)
        if cached_bytes is not None:
            # Deserialize CacheEntry from memory cache
            with contextlib.suppress(Exception):
                import pickle

                entry: CacheEntry = pickle.loads(cached_bytes)

                # Check TTL if connector defines cache_ttl
                if hasattr(self, "cache_ttl") and self.cache_ttl:
                    age = (datetime.now(UTC) - entry.synced_at).total_seconds()
                    if age > self.cache_ttl:
                        logger.info(
                            f"[CACHE] L1 TTL EXPIRED: {path} "
                            f"(age={age:.0f}s > ttl={self.cache_ttl}s)"
                        )
                        memory_cache.remove(memory_key)
                        return None

                logger.info(f"[CACHE] L1 HIT (memory): {path}")
                return entry
        logger.debug(f"[CACHE] L1 MISS (memory): {path}")

        # L2: Check database cache
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

        # Decode binary if stored
        content_binary = None
        if original and cache_model.content_binary:
            with contextlib.suppress(Exception):
                content_binary = base64.b64decode(cache_model.content_binary)

        # Parse metadata if stored
        parse_metadata = None
        if cache_model.parse_metadata:
            with contextlib.suppress(Exception):
                import json

                parse_metadata = json.loads(cache_model.parse_metadata)

        entry = CacheEntry(
            cache_id=cache_model.cache_id,
            path_id=cache_model.path_id,
            content_text=cache_model.content_text,
            content_binary=content_binary,
            content_hash=cache_model.content_hash,
            content_type=cache_model.content_type,
            original_size=cache_model.original_size_bytes,
            cached_size=cache_model.cached_size_bytes,
            backend_version=cache_model.backend_version,
            synced_at=cache_model.synced_at,
            stale=cache_model.stale,
            parsed_from=cache_model.parsed_from,
            parse_metadata=parse_metadata,
        )

        # Check TTL if connector defines cache_ttl
        if hasattr(self, "cache_ttl") and self.cache_ttl:
            age = (datetime.now(UTC) - entry.synced_at).total_seconds()
            if age > self.cache_ttl:
                logger.info(
                    f"[CACHE] L2 TTL EXPIRED: {path} (age={age:.0f}s > ttl={self.cache_ttl}s)"
                )
                return None

        # Populate L1 memory cache for future reads
        with contextlib.suppress(Exception):
            import pickle

            memory_cache.put(memory_key, pickle.dumps(entry))
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
        tenant_id: str | None = None,
    ) -> CacheEntry:
        """Write content to cache.

        Args:
            path: Virtual file path
            content: Original binary content
            content_text: Parsed/extracted text (if None, tries to decode content as UTF-8)
            content_type: 'full', 'parsed', 'summary', or 'reference'
            backend_version: Backend version for optimistic locking
            parsed_from: Parser that extracted text ('pdf', 'xlsx', etc.)
            parse_metadata: Additional metadata from parsing
            tenant_id: Tenant ID for multi-tenant filtering

        Returns:
            CacheEntry for the cached content
        """
        session = self._get_db_session()

        path_id = self._get_path_id(path, session)
        if not path_id:
            raise ValueError(f"Path not found in file_paths: {path}")

        # Compute content hash (BLAKE3, Rust-accelerated)
        content_hash = hash_content(content)

        # Determine text content
        if content_text is None:
            try:
                content_text = content.decode("utf-8")
            except UnicodeDecodeError:
                content_text = None
                content_type = "reference"  # Can't decode, store as reference only

        # Handle large files
        original_size = len(content)
        if content_text and len(content_text) > self.MAX_FULL_TEXT_SIZE:
            content_text = content_text[: self.SUMMARY_SIZE]
            content_type = "summary"

        cached_size = len(content_text) if content_text else 0

        # Encode binary for storage (base64)
        content_binary_b64 = None
        if original_size <= self.MAX_CACHE_FILE_SIZE:
            content_binary_b64 = base64.b64encode(content).decode("ascii")

        # Serialize parse_metadata
        parse_metadata_json = None
        if parse_metadata:
            import json

            parse_metadata_json = json.dumps(parse_metadata)

        now = datetime.now(UTC)

        # Check if entry exists
        stmt = select(ContentCacheModel).where(ContentCacheModel.path_id == path_id)
        result = session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            # Update existing entry
            existing.content_text = content_text
            existing.content_binary = content_binary_b64  # type: ignore[assignment]
            existing.content_hash = content_hash
            existing.content_type = content_type
            existing.original_size_bytes = original_size
            existing.cached_size_bytes = cached_size
            existing.backend_version = backend_version
            existing.parsed_from = parsed_from
            existing.parser_version = None  # TODO: Add parser versioning
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
                tenant_id=tenant_id,
                content_text=content_text,
                content_binary=content_binary_b64,
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

        session.commit()

        entry = CacheEntry(
            cache_id=cache_id,
            path_id=path_id,
            content_text=content_text,
            content_binary=content if content_binary_b64 else None,
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

        # Update L1 memory cache (in-memory, per-process)
        with contextlib.suppress(Exception):
            import pickle

            memory_cache = self._get_memory_cache()
            memory_key = f"cache_entry:{path}"
            memory_cache.put(memory_key, pickle.dumps(entry))
            logger.info(f"[CACHE] WRITE to L1+L2: {path} (size={original_size})")

        return entry

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
                - tenant_id: Optional tenant ID

        Returns:
            List of CacheEntry objects (one per successfully written entry)
        """
        if not entries:
            return []

        session = self._get_db_session()
        now = datetime.now(UTC)
        memory_cache = self._get_memory_cache()

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
                tenant_id = entry_data.get("tenant_id")

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

                # Encode binary for storage
                content_binary_b64 = None
                if original_size <= self.MAX_CACHE_FILE_SIZE:
                    content_binary_b64 = base64.b64encode(content).decode("ascii")

                # Serialize parse_metadata
                parse_metadata_json = None
                if parse_metadata:
                    import json

                    parse_metadata_json = json.dumps(parse_metadata)

                # Update or create entry
                existing = existing_by_path_id.get(path_id)
                if existing:
                    # Update existing entry
                    existing.content_text = content_text
                    existing.content_binary = content_binary_b64  # type: ignore[assignment]
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
                        tenant_id=tenant_id,
                        content_text=content_text,
                        content_binary=content_binary_b64,
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
                    content_binary=content if content_binary_b64 else None,
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

                # Update L1 memory cache
                with contextlib.suppress(Exception):
                    import pickle

                    memory_key = f"cache_entry:{path}"
                    memory_cache.put(memory_key, pickle.dumps(cache_entry))

            except Exception as e:
                logger.error(f"[CACHE] Failed to prepare cache entry for {path}: {e}")

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
        """Invalidate cache entries (both L1 memory and L2 database).

        Args:
            path: Specific path to invalidate
            mount_prefix: Invalidate all paths under this prefix
            delete: If True, delete entries. If False, mark as stale.

        Returns:
            Number of entries invalidated
        """
        # Invalidate L1 memory cache
        memory_cache = self._get_memory_cache()
        if path:
            # Remove specific path from memory cache
            memory_key = f"cache_entry:{path}"
            memory_cache.remove(memory_key)
        elif mount_prefix:
            # For prefix invalidation, clear entire memory cache
            # (More targeted invalidation would require iterating all keys)
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
                session.delete(entry)
            else:
                entry.stale = True
                entry.updated_at = datetime.now(UTC)

            session.commit()
            return 1

        elif mount_prefix:
            # Invalidate all entries under mount prefix
            stmt = (
                select(ContentCacheModel)
                .join(FilePathModel, ContentCacheModel.path_id == FilePathModel.path_id)
                .where(FilePathModel.virtual_path.startswith(mount_prefix))
            )
            result = session.execute(stmt)
            entries = result.scalars().all()

            count = 0
            for entry in entries:
                if delete:
                    session.delete(entry)
                else:
                    entry.stale = True
                    entry.updated_at = datetime.now(UTC)
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

    def sync(
        self,
        path: str | None = None,
        mount_point: str | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        max_file_size: int | None = None,
        generate_embeddings: bool = True,
        context: OperationContext | None = None,
    ) -> SyncResult:
        """Sync content from connector to cache.

        This method reads files from the backend and populates the content_cache
        table for fast grep/search operations.

        Args:
            path: Specific path to sync (relative to mount), or None for entire mount
            mount_point: Virtual mount point (e.g., "/mnt/gcs"). Required for proper
                        path mapping between virtual paths and backend paths.
            include_patterns: Glob patterns to include (e.g., ["*.py", "*.md"])
            exclude_patterns: Glob patterns to exclude (e.g., ["*.pyc", ".git/*"])
            max_file_size: Maximum file size to cache (default: MAX_CACHE_FILE_SIZE)
            generate_embeddings: Generate embeddings for semantic search
            context: Operation context

        Returns:
            SyncResult with statistics

        Examples:
            # Sync entire mount
            connector.sync(mount_point="/mnt/gcs")

            # Sync specific directory
            connector.sync(path="reports/2024", mount_point="/mnt/gcs")

            # Sync single file
            connector.sync(path="data/report.pdf", mount_point="/mnt/gcs")

            # Sync with patterns
            connector.sync(mount_point="/mnt/gcs", include_patterns=["*.py"])
        """
        import fnmatch

        result = SyncResult()
        max_size = max_file_size or self.MAX_CACHE_FILE_SIZE

        # Get files to sync (backend-relative paths)
        try:
            if path:
                # Sync specific path - check if it's a file or directory
                backend_path = path.lstrip("/")
                try:
                    entries = (
                        self.list_dir(backend_path, context) if hasattr(self, "list_dir") else []
                    )
                    if entries:
                        # It's a directory - list recursively
                        files = self._list_files_recursive(backend_path, context)
                    else:
                        # It's a file (or empty dir with extension)
                        import os.path as osp

                        files = [backend_path] if osp.splitext(backend_path)[1] else []
                except Exception:
                    # list_dir failed - assume it's a file
                    files = [backend_path]
            elif hasattr(self, "list_dir"):
                # List all files recursively from root
                files = self._list_files_recursive("", context)
            else:
                result.errors.append("Connector does not support list_dir")
                return result
        except Exception as e:
            result.errors.append(f"Failed to list files: {e}")
            return result

        result.files_scanned = len(files)

        # OPTIMIZATION: Build virtual paths and bulk load cache entries
        virtual_paths: list[str] = []
        backend_to_virtual: dict[str, str] = {}

        for backend_path in files:
            # Construct virtual path from mount_point + backend_path
            if mount_point:
                virtual_path = f"{mount_point.rstrip('/')}/{backend_path.lstrip('/')}"
            else:
                virtual_path = f"/{backend_path.lstrip('/')}"

            # Check include/exclude patterns (match against virtual path)
            if include_patterns and not any(
                fnmatch.fnmatch(virtual_path, p) for p in include_patterns
            ):
                result.files_skipped += 1
                continue

            if exclude_patterns and any(fnmatch.fnmatch(virtual_path, p) for p in exclude_patterns):
                result.files_skipped += 1
                continue

            virtual_paths.append(virtual_path)
            backend_to_virtual[backend_path] = virtual_path

        # STEP 1: Bulk load all cache entries in one query (L1 + L2)
        logger.info(f"[CACHE-SYNC] Step 1: Bulk loading cache for {len(virtual_paths)} paths...")
        cached_entries = self._read_bulk_from_cache(virtual_paths, original=True)
        logger.info(f"[CACHE-SYNC] Found {len(cached_entries)} cached entries")

        # STEP 2: Determine which files need backend reads (collect in batch)
        files_needing_backend: list[str] = []
        file_contexts: dict[str, OperationContext] = {}
        file_metadata: dict[str, dict] = {}  # Store per-file metadata

        # Prepare contexts for all files
        all_contexts: dict[str, OperationContext] = {}
        for backend_path in files:
            vpath = backend_to_virtual.get(backend_path)
            if vpath is None:
                continue
            read_context = self._create_read_context(
                backend_path=backend_path,
                virtual_path=vpath,
                context=context,
            )
            all_contexts[backend_path] = read_context

        # BATCH VERSION CHECK: Get all versions in one call
        # This is 10-25x faster than sequential get_version() calls
        versions: dict[str, str | None] = {}
        paths_needing_version_check = []

        for backend_path in files:
            vpath = backend_to_virtual.get(backend_path)
            if vpath is None:
                continue
            cached = cached_entries.get(vpath)
            # Check version if: no cache OR cache is stale
            needs_version_check = not cached or cached.stale
            if needs_version_check and hasattr(self, "get_version"):
                paths_needing_version_check.append(backend_path)

        if paths_needing_version_check and hasattr(self, "_batch_get_versions"):
            logger.info(
                f"[CACHE-SYNC] Batch fetching versions for {len(paths_needing_version_check)} files..."
            )
            try:
                versions = self._batch_get_versions(paths_needing_version_check, all_contexts)
                logger.info(
                    f"[CACHE-SYNC] Batch version fetch complete: {len(versions)} versions retrieved"
                )
            except Exception as e:
                logger.warning(
                    f"[CACHE-SYNC] Batch version fetch failed: {e}, continuing without versions"
                )
                versions = {}

        # Now process all files with versions already fetched
        for backend_path in files:
            try:
                # Get virtual path (may be None if filtered out by patterns)
                vpath = backend_to_virtual.get(backend_path)
                if vpath is None:
                    # Was filtered out by patterns
                    continue

                # Get context
                if backend_path not in all_contexts:
                    continue
                read_context = all_contexts[backend_path]

                # Get cached entry from bulk-loaded data
                cached = cached_entries.get(vpath)

                # Get version from batch fetch (or None)
                version = versions.get(backend_path)

                # If cache is fresh and not stale, skip without reading content
                if (
                    cached
                    and not cached.stale
                    and hasattr(self, "get_version")
                    and cached.backend_version
                    and cached.backend_version == version
                ):
                    logger.debug(f"[CACHE] SYNC SKIP (cached, version match): {vpath}")
                    result.files_skipped += 1
                    continue

                # This file needs backend read
                files_needing_backend.append(backend_path)
                file_contexts[backend_path] = read_context
                file_metadata[backend_path] = {
                    "virtual_path": vpath,
                    "cached": cached,
                    "version": version,
                }

            except Exception as e:
                result.errors.append(f"Failed to prepare sync for {backend_path}: {e}")

        # STEP 3: Batch read from backend (single batch call)
        logger.info(
            f"[CACHE-SYNC] Step 2: Batch reading {len(files_needing_backend)} files from backend..."
        )
        backend_contents = self._batch_read_from_backend(files_needing_backend, file_contexts)
        logger.info(f"[CACHE-SYNC] Successfully read {len(backend_contents)} files from backend")

        # STEP 4: Process and batch write to cache
        logger.info("[CACHE-SYNC] Step 3: Processing and preparing batch cache write...")
        tenant_id = getattr(context, "tenant_id", None) if context else None

        cache_entries_to_write: list[dict] = []
        files_to_embed: list[str] = []

        for backend_path, content in backend_contents.items():
            try:
                metadata = file_metadata[backend_path]
                vpath = metadata["virtual_path"]
                cached = metadata["cached"]
                version = metadata["version"]

                # Skip if cache is fresh and content matches
                if cached and not cached.stale:
                    if version is not None:
                        # Version already checked above, shouldn't reach here
                        pass
                    else:
                        # No versioning - compare content hashes
                        if cached.content_binary:
                            content_hash = hash_content(content)
                            cached_hash = hash_content(cached.content_binary)
                            if content_hash == cached_hash:
                                logger.debug(f"[CACHE] SYNC SKIP (hash match): {vpath}")
                                result.files_skipped += 1
                                continue

                # Check size
                if len(content) > max_size:
                    result.files_skipped += 1
                    continue

                # Parse content if supported (PDF, Excel, etc.)
                parsed_text, parsed_from, parse_metadata = self._parse_content(vpath, content)

                # Prepare cache entry for batch write
                cache_entries_to_write.append(
                    {
                        "path": vpath,
                        "content": content,
                        "content_text": parsed_text,
                        "content_type": "parsed" if parsed_text else "full",
                        "backend_version": version,
                        "parsed_from": parsed_from,
                        "parse_metadata": parse_metadata,
                        "tenant_id": tenant_id,
                    }
                )

                result.files_synced += 1
                result.bytes_synced += len(content)

                # Track for embedding generation
                if generate_embeddings:
                    files_to_embed.append(vpath)

            except Exception as e:
                result.errors.append(f"Failed to process {backend_path}: {e}")

        # Batch write all cache entries in single transaction
        if cache_entries_to_write:
            logger.info(
                f"[CACHE-SYNC] Step 4: Batch writing {len(cache_entries_to_write)} entries..."
            )
            try:
                self._batch_write_to_cache(cache_entries_to_write)
            except Exception as e:
                result.errors.append(f"Failed to batch write cache entries: {e}")

        # Generate embeddings if requested
        if files_to_embed:
            logger.info(
                f"[CACHE-SYNC] Step 5: Generating embeddings for {len(files_to_embed)} files..."
            )
            for vpath in files_to_embed:
                try:
                    self._generate_embeddings(vpath)
                    result.embeddings_generated += 1
                except Exception as e:
                    result.errors.append(f"Failed to generate embeddings for {vpath}: {e}")

        logger.info(
            f"[CACHE-SYNC] Complete: synced={result.files_synced}, "
            f"skipped={result.files_skipped}, errors={len(result.errors)}"
        )
        return result

    def _create_read_context(
        self,
        backend_path: str,
        virtual_path: str,
        context: OperationContext | None = None,
    ) -> OperationContext:
        """Create a context for reading content with proper backend_path set."""
        if context:
            # Clone context and set backend_path
            new_context = OperationContext(
                user=context.user,
                groups=context.groups,
                backend_path=backend_path,
                tenant_id=getattr(context, "tenant_id", None),
                is_system=True,  # Bypass permissions for sync
            )
        else:
            new_context = OperationContext(
                user="system",
                groups=[],
                backend_path=backend_path,
                is_system=True,
            )
        # Set virtual_path as attribute
        new_context.virtual_path = virtual_path
        return new_context

    def _list_files_recursive(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> list[str]:
        """Recursively list all files under a path.

        Args:
            path: Backend-relative path (e.g., "" for root, "subdir" for subdirectory)
            context: Operation context

        Returns:
            List of backend-relative file paths
        """
        files: list[str] = []

        if not hasattr(self, "list_dir"):
            return files

        try:
            entries = self.list_dir(path, context)
            for entry in entries:
                entry_name = entry.rstrip("/")

                # Build full backend-relative path
                if path == "" or path == "/":
                    full_path = entry_name
                else:
                    full_path = f"{path.rstrip('/')}/{entry_name}"

                if entry.endswith("/"):
                    # Directory - recurse
                    files.extend(self._list_files_recursive(full_path, context))
                else:
                    # File
                    files.append(full_path)
        except Exception:
            pass

        return files

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
