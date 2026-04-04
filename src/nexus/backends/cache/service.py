"""Cache service for connector backends.

Extracted from CacheConnectorMixin (#1628). Contains all L1/L2 cache
read/write/invalidate logic as a standalone service with constructor injection.

Bug fixes applied:
    - invalidate_cache(): L1 removal uses bare `path` key instead of
      `f"cache_entry:{path}"` which never matched stored keys.
    - contextlib.suppress(Exception) replaced with logged warnings
      for narrower, observable error handling.
    - datetime.now(UTC) cached before bulk loops to avoid repeated syscalls.

Part of: #1628 (Split CacheConnectorMixin into focused units)
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select

from nexus.backends.cache.models import (
    MAX_CACHE_FILE_SIZE,
    MAX_FULL_TEXT_SIZE,
    SUMMARY_SIZE,
    CachedReadResult,
    CacheEntry,
)
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import ConflictError
from nexus.contracts.types import OperationContext
from nexus.core.hash_fast import hash_content
from nexus.storage.file_cache import FileContentCache
from nexus.storage.models import FilePathModel

# RUST_FALLBACK: L1MetadataCache
if TYPE_CHECKING:
    from nexus_kernel import L1MetadataCache
    from sqlalchemy.orm import Session

    from nexus.backends.misc.backend_io import BackendIOService

logger = logging.getLogger(__name__)


class CacheService:
    """Core cache logic for connector backends.

    Provides a two-level cache:
    - L1: In-memory Rust metadata cache (fast, per-instance, lost on restart)
    - L2: Disk-based content + metadata sidecar (FileContentCache)

    Uses constructor injection (same pattern as SyncPipelineService).

    Args:
        connector: The connector instance (provides session_factory, zone_id, etc.)
        l1_cache: Optional L1 metadata cache instance
        backend_io: Optional BackendIOService for content I/O
    """

    def __init__(
        self,
        connector: Any,
        l1_cache: "L1MetadataCache | None" = None,
        backend_io: "BackendIOService | None" = None,
        file_cache: FileContentCache | None = None,
    ) -> None:
        self._connector = connector
        self._l1_cache = l1_cache
        self._backend_io = backend_io
        self._file_cache = file_cache

    @property
    def file_cache(self) -> FileContentCache:
        """Get the injected FileContentCache (lazy-create if not provided)."""
        if self._file_cache is None:
            import os

            self._file_cache = FileContentCache(os.getenv("NEXUS_DATA_DIR", "./nexus-data"))
        return self._file_cache

    @property
    def backend_io(self) -> "BackendIOService":
        """Lazy-initialize BackendIOService."""
        if self._backend_io is None:
            from nexus.backends.misc.backend_io import BackendIOService

            self._backend_io = BackendIOService(self._connector)
        return self._backend_io

    # =========================================================================
    # Helpers (consolidate duplicated logic)
    # =========================================================================

    def _get_cache_zone(self) -> str:
        """Get cache zone from connector (consolidates 3 duplicated getattr calls)."""
        return getattr(self._connector, "zone_id", None) or ROOT_ZONE_ID

    def _get_cache_ttl(self) -> int:
        """Get cache TTL from connector (consolidates 3 duplicated getattr calls)."""
        return getattr(self._connector, "cache_ttl", 0) or 0

    def _populate_l1(
        self,
        key: str,
        path_id: str,
        content_hash: str,
        disk_path: str,
        original_size: int,
        zone_id: str,
    ) -> None:
        """Populate L1 cache entry (consolidates 4 duplicated l1_cache.put calls).

        Logs warnings instead of silently swallowing exceptions.
        """
        if self._l1_cache is None or not disk_path:
            return
        try:
            is_text = True  # Default; caller can adjust if needed
            ttl = self._get_cache_ttl()
            self._l1_cache.put(
                key=key,
                path_id=path_id,
                content_hash=content_hash,
                disk_path=disk_path,
                original_size=original_size,
                ttl_seconds=ttl,
                is_text=is_text,
                zone_id=zone_id,
            )
        except Exception:
            logger.warning("[CACHE] Failed to populate L1 for %s", key, exc_info=True)

    def _populate_l1_with_meta(
        self,
        key: str,
        meta: dict,
        cache_zone: str,
        file_cache: Any,
    ) -> None:
        """Populate L1 from disk meta sidecar (used in read paths)."""
        if self._l1_cache is None:
            return
        try:
            meta_zone = meta.get("zone_id", cache_zone)
            disk_path = str(file_cache._get_cache_path(meta_zone, key))
            is_text = meta.get("content_type", "full") in ("full", "parsed", "summary")
            ttl = self._get_cache_ttl()
            self._l1_cache.put(
                key=key,
                path_id=meta.get("path_id", ""),
                content_hash=meta.get("content_hash", ""),
                disk_path=disk_path,
                original_size=meta.get("original_size", 0),
                ttl_seconds=ttl,
                is_text=is_text,
                zone_id=meta_zone,
            )
            logger.debug("[CACHE] L1 POPULATED from L2: %s", key)
        except Exception:
            logger.warning("[CACHE] Failed to populate L1 from meta for %s", key, exc_info=True)

    # =========================================================================
    # Caching availability checks
    # =========================================================================

    def has_caching(self) -> bool:
        """Check if any caching is enabled (L1 or L1+L2)."""
        if getattr(self._connector, "l1_only", False):
            return True
        return (
            getattr(self._connector, "session_factory", None) is not None
            or getattr(self._connector, "db_session", None) is not None
            or getattr(self._connector, "_db_session", None) is not None
        )

    def has_l2_caching(self) -> bool:
        """Check if L2 (disk) caching is enabled."""
        if getattr(self._connector, "l1_only", False):
            return False
        return (
            getattr(self._connector, "session_factory", None) is not None
            or getattr(self._connector, "db_session", None) is not None
            or getattr(self._connector, "_db_session", None) is not None
        )

    # =========================================================================
    # Path resolution
    # =========================================================================

    def get_cache_path(self, context: OperationContext | None) -> str | None:
        """Get the cache key path from context."""
        if context is None:
            return None
        if hasattr(context, "virtual_path") and context.virtual_path:
            return context.virtual_path
        if hasattr(context, "backend_path") and context.backend_path:
            return context.backend_path
        return None

    def get_db_session(self) -> "Session":
        """Get database session from connector."""
        connector = self._connector
        if hasattr(connector, "session_factory") and connector.session_factory is not None:
            return cast("Session", connector.session_factory())
        if hasattr(connector, "db_session") and connector.db_session is not None:
            return cast("Session", connector.db_session)
        if hasattr(connector, "_db_session") and connector._db_session is not None:
            return cast("Session", connector._db_session)
        raise RuntimeError("No database session available for caching")

    def get_path_id(self, path: str, session: "Session") -> str | None:
        """Get path_id for a virtual path."""
        stmt = select(FilePathModel.path_id).where(
            FilePathModel.virtual_path == path,
            FilePathModel.deleted_at.is_(None),
        )
        result = session.execute(stmt)
        row: str | None = result.scalar_one_or_none()
        return row

    def get_path_ids_bulk(self, paths: list[str], session: "Session") -> dict[str, str]:
        """Get path_ids for multiple virtual paths in a single query."""
        if not paths:
            return {}
        stmt = select(FilePathModel.virtual_path, FilePathModel.path_id).where(
            FilePathModel.virtual_path.in_(paths),
            FilePathModel.deleted_at.is_(None),
        )
        result = session.execute(stmt)
        return {row[0]: row[1] for row in result.fetchall()}

    # =========================================================================
    # Cache reads
    # =========================================================================

    def read_from_cache(
        self,
        path: str,
        original: bool = False,
        zone_id: str | None = None,
    ) -> CacheEntry | None:
        """Read content from cache (L1 Rust metadata cache, then L2 disk).

        Args:
            path: Cache key path.
            original: If True, also read binary content from disk.
            zone_id: Explicit zone override. When provided, L2 disk reads use
                     this zone instead of the connector's default zone. This
                     ensures multi-zone isolation on shared connectors.
        """
        l1_cache = self._l1_cache

        # L1: Check Rust metadata cache first
        if l1_cache is not None:
            if original:
                content_result = l1_cache.get_content(path)
                if content_result is not None:
                    content_bytes, content_hash, _is_text = content_result
                    entry = CacheEntry.from_l1_content(bytes(content_bytes), content_hash)
                    logger.info("[CACHE] L1 HIT (Rust): %s", path)
                    return entry
            else:
                meta_result = l1_cache.get(path)
                if meta_result is not None:
                    path_id, content_hash, _disk_path, original_size, _is_text, is_fresh = (
                        meta_result
                    )
                    if is_fresh:
                        entry = CacheEntry.from_l1_metadata(path_id, content_hash, original_size)
                        logger.info("[CACHE] L1 HIT (Rust metadata): %s", path)
                        return entry
                    else:
                        logger.debug("[CACHE] L1 EXPIRED: %s", path)
        logger.debug("[CACHE] L1 MISS: %s", path)

        # L2: Check disk cache (skip if l1_only mode)
        if not self.has_l2_caching():
            logger.debug("[CACHE] L2 SKIP (l1_only mode): %s", path)
            return None

        file_cache = self.file_cache
        cache_zone = zone_id or self._get_cache_zone()
        meta = file_cache.read_meta(cache_zone, path)

        if not meta:
            logger.debug("[CACHE] L2 MISS (disk): %s", path)
            return None

        logger.info("[CACHE] L2 HIT (disk): %s", path)

        # Read binary content from disk
        content_binary_raw = None
        if original:
            meta_zone = meta.get("zone_id", cache_zone)
            content_binary_raw = file_cache.read(meta_zone, path)
            if content_binary_raw:
                logger.debug("[CACHE] L2 content from DISK: %s", path)

        # Read text content from disk
        content_text = file_cache.read_text(meta.get("zone_id", cache_zone), path)

        entry = CacheEntry.from_disk_meta(meta, content_text, content_binary_raw)

        # Check TTL if connector defines cache_ttl
        ttl = self._get_cache_ttl()
        if ttl:
            age = (datetime.now(UTC) - entry.synced_at).total_seconds()
            if age > ttl:
                logger.info("[CACHE] L2 TTL EXPIRED: %s (age=%.0fs > ttl=%ds)", path, age, ttl)
                return None

        # Populate L1 for future reads
        self._populate_l1_with_meta(path, meta, cache_zone, file_cache)

        return entry

    def read_bulk_from_cache(
        self,
        paths: list[str],
        original: bool = False,
        zone_id: str | None = None,
    ) -> dict[str, CacheEntry]:
        """Read multiple entries from cache in bulk (L1 + L2).

        Args:
            paths: List of cache key paths.
            original: If True, also read binary content from disk.
            zone_id: Explicit zone override for L2 disk reads. Ensures
                     multi-zone isolation on shared connectors.
        """
        if not paths:
            return {}

        results: dict[str, CacheEntry] = {}
        paths_needing_l2: list[str] = []
        now = datetime.now(UTC)  # Cache timestamp for the loop

        # L1: Check Rust metadata cache first
        l1_cache = self._l1_cache
        for path in paths:
            if l1_cache is None:
                paths_needing_l2.append(path)
                continue

            if original:
                content_result = l1_cache.get_content(path)
                if content_result is not None:
                    content_bytes, content_hash, _is_text = content_result
                    entry = CacheEntry.from_l1_content(bytes(content_bytes), content_hash, now)
                    results[path] = entry
                    logger.debug("[CACHE-BULK] L1 HIT: %s", path)
                    continue
            else:
                meta_result = l1_cache.get(path)
                if meta_result is not None:
                    path_id, content_hash, _disk_path, original_size, _is_text, is_fresh = (
                        meta_result
                    )
                    if is_fresh:
                        entry = CacheEntry.from_l1_metadata(
                            path_id, content_hash, original_size, now
                        )
                        results[path] = entry
                        logger.debug("[CACHE-BULK] L1 HIT (metadata): %s", path)
                        continue
                    else:
                        paths_needing_l2.append(path)
                        continue
            paths_needing_l2.append(path)

        if not paths_needing_l2:
            logger.info("[CACHE-BULK] All %d paths from L1 memory", len(paths))
            return results

        # L2: Disk-based lookup for remaining paths
        file_cache = self.file_cache
        cache_zone = zone_id or self._get_cache_zone()

        meta_entries = file_cache.read_meta_bulk(cache_zone, paths_needing_l2)

        disk_contents: dict[str, bytes] = {}
        if original and meta_entries:
            disk_contents = file_cache.read_bulk(cache_zone, list(meta_entries.keys()))

        l2_hits = 0
        for vpath, meta in meta_entries.items():
            content_binary_raw = disk_contents.get(vpath) if original else None
            content_text = file_cache.read_text(meta.get("zone_id", cache_zone), vpath)

            entry = CacheEntry.from_disk_meta(meta, content_text, content_binary_raw)
            results[vpath] = entry
            l2_hits += 1

            # Populate L1 for future reads
            self._populate_l1_with_meta(vpath, meta, cache_zone, file_cache)

        logger.info(
            "[CACHE-BULK] %d L1 hits, %d L2 hits, %d misses (total %d paths)",
            len(results) - l2_hits,
            l2_hits,
            len(paths) - len(results),
            len(paths),
        )
        return results

    def read_content_bulk(
        self,
        paths: list[str],
        context: OperationContext | None = None,
    ) -> dict[str, bytes]:
        """Read multiple files' content in bulk, using cache where available."""
        if not paths:
            return {}

        results: dict[str, bytes] = {}

        cache_entries = self.read_bulk_from_cache(paths, original=True)

        paths_needing_backend: list[str] = []
        for path in paths:
            entry = cache_entries.get(path)
            if entry and not entry.stale and entry.content_binary:
                results[path] = entry.content_binary
            else:
                paths_needing_backend.append(path)

        if not paths_needing_backend:
            logger.info("[CACHE-BULK] All %d files served from cache", len(paths))
            return results

        # Read remaining from backend
        for path in paths_needing_backend:
            try:
                content = self.backend_io.read_content_from_backend(path, context)
                if content:
                    results[path] = content
            except Exception:
                logger.warning("[CACHE-BULK] Backend read failed for %s", path, exc_info=True)

        logger.info(
            "[CACHE-BULK] %d cache hits, %d backend reads",
            len(cache_entries),
            len(paths_needing_backend),
        )
        return results

    # =========================================================================
    # Cache writes
    # =========================================================================

    def write_to_cache(
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
        """Write content to cache (L1 + L2 or L1-only)."""
        content_hash = hash_content(content)
        original_size = len(content)
        now = datetime.now(UTC)
        cache_zone = zone_id or ROOT_ZONE_ID

        # Determine text content
        if content_text is None:
            try:
                content_text = content.decode("utf-8")
            except UnicodeDecodeError:
                content_text = None
                content_type = "reference"

        # Handle large files
        if content_text and len(content_text) > MAX_FULL_TEXT_SIZE:
            content_text = content_text[:SUMMARY_SIZE]
            content_type = "summary"

        cached_size = len(content_text) if content_text else 0

        has_l2 = self.has_l2_caching()

        if has_l2:
            file_cache = self.file_cache

            path_id = ""
            session = self.get_db_session()
            if session:
                path_id = self.get_path_id(path, session) or ""

            if original_size <= MAX_CACHE_FILE_SIZE:
                try:
                    file_cache.write(cache_zone, path, content, text_content=content_text)
                    logger.debug("[CACHE] Wrote %d bytes to disk: %s", original_size, path)
                except Exception as e:
                    logger.warning("[CACHE] Failed to write to disk cache: %s", e)

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
                                "[CACHE] Updated file_paths: %s (size=%d, hash=%s...)",
                                path,
                                original_size,
                                content_hash[:8],
                            )
                    session.commit()
                except Exception as e:
                    logger.warning("[CACHE] Failed to update file_paths for %s: %s", path, e)

            cache_id = ""
            disk_path = str(file_cache._get_cache_path(cache_zone, path))
        else:
            # L1-only mode
            path_id = path
            cache_id = ""

            disk_path = ""
            if hasattr(self._connector, "get_physical_path"):
                try:
                    disk_path = str(self._connector.get_physical_path(path))
                except Exception as e:
                    logger.debug("[CACHE] Could not get physical path for %s: %s", path, e)

        # Write to L1
        if disk_path:
            self._populate_l1(
                key=path,
                path_id=path_id,
                content_hash=content_hash,
                disk_path=disk_path,
                original_size=original_size,
                zone_id=cache_zone,
            )
            mode = "L1+L2" if has_l2 else "L1 (l1_only)"
            logger.info("[CACHE] WRITE to %s: %s (size=%d)", mode, path, original_size)

        return CacheEntry.from_write(
            path_id=path_id,
            content=content,
            content_hash=content_hash,
            content_text=content_text,
            content_type=content_type,
            original_size=original_size,
            cached_size=cached_size,
            backend_version=backend_version,
            parsed_from=parsed_from,
            parse_metadata=parse_metadata,
            cache_id=cache_id,
            max_cache_file_size=MAX_CACHE_FILE_SIZE,
            now=now,
        )

    def bulk_write_to_cache(
        self,
        entries: list[dict],
    ) -> list[CacheEntry]:
        """Write multiple entries to cache in a single transaction."""
        if not entries:
            return []

        now = datetime.now(UTC)
        l1_cache = self._l1_cache
        file_cache = self.file_cache

        session = self.get_db_session()
        path_id_map: dict[str, str] = {}
        if session:
            entry_paths = [e["path"] for e in entries]
            path_id_map = self.get_path_ids_bulk(entry_paths, session)

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
                entry_zone_id = entry_data.get("zone_id")

                path_id = path_id_map.get(path, "")
                content_hash = hash_content(content)

                if content_text is None:
                    try:
                        content_text = content.decode("utf-8")
                    except UnicodeDecodeError:
                        content_text = None
                        content_type = "reference"

                original_size = len(content)
                if content_text and len(content_text) > MAX_FULL_TEXT_SIZE:
                    content_text = content_text[:SUMMARY_SIZE]
                    content_type = "summary"

                cached_size = len(content_text) if content_text else 0

                cache_zone = entry_zone_id or ROOT_ZONE_ID
                if original_size <= MAX_CACHE_FILE_SIZE:
                    try:
                        file_cache.write(cache_zone, path, content, text_content=content_text)
                    except Exception as e:
                        logger.warning("[CACHE] Failed to write to disk cache: %s: %s", path, e)

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

                cache_entry = CacheEntry.from_write(
                    path_id=path_id,
                    content=content,
                    content_hash=content_hash,
                    content_text=content_text,
                    content_type=content_type,
                    original_size=original_size,
                    cached_size=cached_size,
                    backend_version=backend_version,
                    parsed_from=parsed_from,
                    parse_metadata=parse_metadata,
                    max_cache_file_size=MAX_CACHE_FILE_SIZE,
                    now=now,
                )
                cache_entries.append(cache_entry)

                # Update L1
                if l1_cache is not None:
                    disk_path = str(file_cache._get_cache_path(cache_zone, path))
                    self._populate_l1(
                        key=path,
                        path_id=path_id,
                        content_hash=content_hash,
                        disk_path=disk_path,
                        original_size=original_size,
                        zone_id=cache_zone,
                    )

            except Exception as e:
                entry_path = entry_data.get("path", "<unknown>")
                logger.error("[CACHE] Failed to prepare cache entry for %s: %s", entry_path, e)

        # Update file_paths in DB
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
                            "[CACHE] Updated %d file_paths entries (size + content_hash)",
                            updated_count,
                        )
                session.commit()
            except Exception as e:
                logger.warning("[CACHE] Failed to update file_paths in batch: %s", e)

        logger.info("[CACHE] Batch wrote %d entries to L1+L2 (disk)", len(cache_entries))
        return cache_entries

    # =========================================================================
    # Cache invalidation
    # =========================================================================

    def invalidate_cache(
        self,
        path: str | None = None,
        mount_prefix: str | None = None,
        delete: bool = False,
        zone_id: str | None = None,
    ) -> int:
        """Invalidate cache entries (L1 memory + L2 disk).

        Args:
            path: Specific path to invalidate.
            mount_prefix: Invalidate all paths under this mount prefix.
            delete: Whether this is a delete operation.
            zone_id: Explicit zone override. Ensures invalidation targets the
                     correct zone on shared connectors instead of defaulting
                     to the connector's zone_id.

        Bug fix: L1 removal now uses bare `path` key (matches stored key)
        instead of `f"cache_entry:{path}"` which never matched.
        """
        memory_cache = self._l1_cache
        file_cache = self.file_cache
        cache_zone = zone_id or self._get_cache_zone()

        if path:
            # FIX: Use bare path (matches the key used in l1_cache.put)
            if memory_cache is not None:
                memory_cache.remove(path)

            file_cache.delete(cache_zone, path)
            return 1

        elif mount_prefix:
            if memory_cache is not None:
                memory_cache.clear()

            session = self.get_db_session()
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

    # =========================================================================
    # Version checking
    # =========================================================================

    def check_version(
        self,
        path: str,
        expected_version: str,
        context: OperationContext | None = None,
    ) -> bool:
        """Check if backend version matches expected."""
        connector = self._connector
        if not hasattr(connector, "get_version"):
            return True

        current_version = connector.get_version(path, context)
        if current_version is None:
            return True

        if current_version != expected_version:
            raise ConflictError(
                path=path,
                expected_etag=expected_version,
                current_etag=current_version,
            )

        return True

    # =========================================================================
    # Content hash / size lookups
    # =========================================================================

    def get_content_hash(self, path: str) -> str | None:
        """Get the content hash (ETag) for a path from cache."""
        if not self.has_caching():
            return None

        cached = self.read_from_cache(path, original=False)
        if cached and not cached.stale:
            return cached.content_hash
        return None

    def get_size_from_cache(self, path: str) -> int | None:
        """Get file size from cache (efficient, no backend call)."""
        if not self.has_caching():
            return None

        try:
            entry = self.read_from_cache(path, original=False)
            if entry and not entry.stale:
                logger.debug("[CACHE] SIZE HIT: %s (%d bytes)", path, entry.original_size)
                return entry.original_size
            logger.debug("[CACHE] SIZE MISS: %s", path)
        except Exception as e:
            logger.debug("[CACHE] SIZE ERROR for %s: %s", path, e)

        return None

    # =========================================================================
    # Automatic caching API
    # =========================================================================

    def read_content_with_cache(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> CachedReadResult:
        """Read content with automatic L1/L2 caching."""
        if not context or not context.backend_path:
            raise ValueError("context with backend_path is required")

        path = self.get_cache_path(context) or context.backend_path
        zone_id = getattr(context, "zone_id", None)

        # Step 1: Check cache (pass zone_id for multi-zone isolation)
        if self.has_caching():
            cached = self.read_from_cache(path, original=True, zone_id=zone_id)
            if cached and not cached.stale and cached.content_binary:
                logger.debug("[CACHE] HIT: %s", path)
                return CachedReadResult(
                    content=cached.content_binary,
                    content_hash=cached.content_hash,
                    from_cache=True,
                    cache_entry=cached,
                )

        # Step 2: Fetch from backend
        logger.debug("[CACHE] MISS: %s - fetching from backend", path)
        content = self._connector._fetch_content(content_hash, context)

        # Step 3: Write to cache
        result_hash = hash_content(content)
        cache_entry = None

        if self.has_caching():
            try:
                cache_entry = self.write_to_cache(
                    path=path,
                    content=content,
                    backend_version=self._connector._get_backend_version(context),
                    zone_id=zone_id,
                )
                result_hash = cache_entry.content_hash
            except Exception as e:
                logger.warning("[CACHE] Failed to cache %s: %s", path, e)

        return CachedReadResult(
            content=content,
            content_hash=result_hash,
            from_cache=False,
            cache_entry=cache_entry,
        )
