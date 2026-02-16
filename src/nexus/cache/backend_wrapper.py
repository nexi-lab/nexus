"""CachingBackendWrapper — transparent caching decorator for any Backend (#1392).

Wraps an inner Backend and adds two-layer caching:
- L1: In-memory ContentCache (sync, fast, process-local, LZ4 compressed)
- L2: CacheStoreABC/Dragonfly (async background population, distributed)

Follows LEGO Architecture Pattern E (Decorator/Wrapper Composition).
All Backend operations pass through transparently. Only CAS read operations
are cached (read_content, batch_read_content). Writes invalidate or populate
cache based on the configured CacheStrategy.

Cache failures are silently swallowed — inner backend is always the fallback.

Usage:
    from nexus.cache.backend_wrapper import CachingBackendWrapper, CacheWrapperConfig

    wrapper = CachingBackendWrapper(
        inner=local_backend,
        config=CacheWrapperConfig(strategy=CacheStrategy.WRITE_AROUND),
        cache_store=dragonfly_store,  # optional L2
    )
    # Use wrapper exactly like any Backend
    resp = wrapper.read_content(content_hash)
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from nexus.backends.backend import Backend
from nexus.core.response import HandlerResponse
from nexus.storage.content_cache import ContentCache

if TYPE_CHECKING:
    from nexus.core.cache_store import CacheStoreABC
    from nexus.core.permissions import OperationContext
    from nexus.core.permissions_enhanced import EnhancedOperationContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class CacheStrategy(Enum):
    """Cache write strategy.

    WRITE_AROUND: On write, invalidate cache. Populate on read miss. (Default)
        Best for: write-heavy workloads, large content that's rarely re-read.

    WRITE_THROUGH: On write, populate cache immediately.
        Best for: read-heavy workloads, content that's read shortly after write.
    """

    WRITE_AROUND = "write_around"
    WRITE_THROUGH = "write_through"


@dataclass(frozen=True)
class CacheWrapperConfig:
    """Immutable configuration for CachingBackendWrapper.

    Attributes:
        strategy: Cache write strategy (WRITE_AROUND or WRITE_THROUGH).
        l1_max_size_mb: L1 ContentCache memory budget in MB.
        l1_compression_threshold: Minimum content size in bytes for LZ4 compression.
        l2_enabled: Enable L2 (CacheStoreABC) background population.
        l2_ttl_seconds: L2 cache entry TTL in seconds.
        l2_key_prefix: L2 key prefix for namespace isolation.
        metrics_enabled: Enable OTel cache hit/miss/error counters.
    """

    strategy: CacheStrategy = CacheStrategy.WRITE_AROUND
    l1_max_size_mb: int = 128
    l1_compression_threshold: int = 1024
    l2_enabled: bool = True
    l2_ttl_seconds: int = 3600
    l2_key_prefix: str = "cbw"
    metrics_enabled: bool = True


# ---------------------------------------------------------------------------
# CachingBackendWrapper
# ---------------------------------------------------------------------------


class CachingBackendWrapper(Backend):
    """Transparent caching decorator for any Backend implementation.

    Delegates all operations to the inner backend. CAS content reads are
    cached in L1 (in-memory) and optionally L2 (distributed). Writes
    invalidate or populate cache based on the configured strategy.

    Cache failures are silently swallowed — inner backend is always the
    source of truth. OTel counters track hit/miss/error rates.
    """

    def __init__(
        self,
        inner: Backend,
        config: CacheWrapperConfig | None = None,
        cache_store: CacheStoreABC | None = None,
    ) -> None:
        self._inner = inner
        self._config = config or CacheWrapperConfig()
        self._cache_store = cache_store

        # L1: in-memory content cache (owned by this wrapper)
        self._l1_cache = ContentCache(
            max_size_mb=self._config.l1_max_size_mb,
            compression_threshold=self._config.l1_compression_threshold,
        )

        # Hit/miss counters (protected by _stats_lock for thread-safety)
        self._stats_lock = threading.Lock()
        self._l1_hits = 0
        self._l1_misses = 0
        self._l2_hits = 0
        self._l2_misses = 0
        self._cache_errors = 0
        self._invalidations = 0

        # OTel metrics (lazy-initialized on first access)
        self._metrics: dict[str, Any] | None = None
        self._metrics_initialized = False

    # === Name & Properties (explicit delegation) ===

    @property
    def name(self) -> str:
        return f"cached({self._inner.name})"

    @property
    def user_scoped(self) -> bool:
        return self._inner.user_scoped

    @property
    def is_connected(self) -> bool:
        return self._inner.is_connected

    @property
    def thread_safe(self) -> bool:
        return self._inner.thread_safe

    @property
    def supports_rename(self) -> bool:
        return self._inner.supports_rename

    @property
    def has_virtual_filesystem(self) -> bool:
        return self._inner.has_virtual_filesystem

    @property
    def has_root_path(self) -> bool:
        return self._inner.has_root_path

    @property
    def has_token_manager(self) -> bool:
        return self._inner.has_token_manager

    @property
    def has_data_dir(self) -> bool:
        return self._inner.has_data_dir

    @property
    def is_passthrough(self) -> bool:
        return self._inner.is_passthrough

    @property
    def supports_parallel_mmap_read(self) -> bool:
        return self._inner.supports_parallel_mmap_read

    # === Cached Content Operations ===

    def read_content(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[bytes]:
        """Read content with L1 → L2 → inner backend fallback."""
        # L1 check
        try:
            cached = self._l1_cache.get(content_hash)
            if cached is not None:
                self._record_l1_hit()
                return HandlerResponse.ok(data=cached, backend_name=self.name)
            self._record_l1_miss()
        except Exception as e:
            self._record_cache_error("l1_read", e)

        # L2 check (sync bridge — only if event loop is running)
        l2_data = self._l2_get_sync(content_hash)
        if l2_data is not None:
            self._record_l2_hit()
            # Promote to L1
            try:
                self._l1_cache.put(content_hash, l2_data)
            except Exception as e:
                self._record_cache_error("l1_promote", e)
            return HandlerResponse.ok(data=l2_data, backend_name=self.name)

        self._record_l2_miss()

        # Inner backend read
        response = self._inner.read_content(content_hash, context=context)
        if response.success and response.data is not None:
            # Populate L1
            try:
                self._l1_cache.put(content_hash, response.data)
            except Exception as e:
                self._record_cache_error("l1_populate", e)
            # Schedule L2 population (fire-and-forget)
            self._schedule_l2_populate(content_hash, response.data)

        return response

    def write_content(
        self, content: bytes, context: OperationContext | None = None
    ) -> HandlerResponse[str]:
        """Write content to inner backend, then handle cache based on strategy."""
        response = self._inner.write_content(content, context=context)
        if not response.success or response.data is None:
            return response

        content_hash = response.data

        if self._config.strategy == CacheStrategy.WRITE_THROUGH:
            # Populate L1 immediately
            try:
                self._l1_cache.put(content_hash, content)
            except Exception as e:
                self._record_cache_error("l1_write_through", e)
            # Schedule L2 population
            self._schedule_l2_populate(content_hash, content)
        else:
            # WRITE_AROUND: invalidate (content may have changed ref count, etc.)
            self._invalidate(content_hash)

        return response

    def delete_content(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[None]:
        """Delete from inner backend and invalidate caches."""
        response = self._inner.delete_content(content_hash, context=context)
        self._invalidate(content_hash)
        return response

    def content_exists(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[bool]:
        """Always delegate to inner backend — source of truth for existence.

        Unlike read_content, content_exists is not cached because a stale True
        from L1 could cause the caller to skip a necessary write. The inner
        backend check is lightweight (no data transfer).
        """
        return self._inner.content_exists(content_hash, context=context)

    def batch_read_content(
        self, content_hashes: list[str], context: OperationContext | None = None
    ) -> dict[str, bytes | None]:
        """Batch read with L1 cache for already-cached items."""
        result: dict[str, bytes | None] = {}
        uncached_hashes: list[str] = []

        # First pass: check L1 for all hashes
        for content_hash in content_hashes:
            try:
                cached = self._l1_cache.get(content_hash)
                if cached is not None:
                    result[content_hash] = cached
                    self._record_l1_hit()
                else:
                    uncached_hashes.append(content_hash)
                    self._record_l1_miss()
            except Exception as e:
                self._record_cache_error("l1_batch_read", e)
                uncached_hashes.append(content_hash)

        # Second pass: check L2 for uncached items
        if uncached_hashes and self._l2_available:
            still_uncached: list[str] = []
            for content_hash in uncached_hashes:
                l2_data = self._l2_get_sync(content_hash)
                if l2_data is not None:
                    result[content_hash] = l2_data
                    self._record_l2_hit()
                    # Promote to L1
                    try:
                        self._l1_cache.put(content_hash, l2_data)
                    except Exception as e:
                        self._record_cache_error("l1_batch_promote", e)
                else:
                    still_uncached.append(content_hash)
                    self._record_l2_miss()
            uncached_hashes = still_uncached

        # Third pass: read remaining uncached from inner backend
        if uncached_hashes:
            inner_results = self._inner.batch_read_content(uncached_hashes, context=context)
            for content_hash, data in inner_results.items():
                result[content_hash] = data
                # Populate L1 for successful reads
                if data is not None:
                    try:
                        self._l1_cache.put(content_hash, data)
                    except Exception as e:
                        self._record_cache_error("l1_batch_populate", e)
                    # Schedule L2 population
                    self._schedule_l2_populate(content_hash, data)

        return result

    # === Non-cached content operations (explicit delegation) ===

    def get_content_size(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[int]:
        return self._inner.get_content_size(content_hash, context=context)

    def get_ref_count(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[int]:
        return self._inner.get_ref_count(content_hash, context=context)

    # === Directory operations (explicit delegation) ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | EnhancedOperationContext | None = None,
    ) -> HandlerResponse[None]:
        return self._inner.mkdir(path, parents=parents, exist_ok=exist_ok, context=context)

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | EnhancedOperationContext | None = None,
    ) -> HandlerResponse[None]:
        return self._inner.rmdir(path, recursive=recursive, context=context)

    def is_directory(
        self, path: str, context: OperationContext | None = None
    ) -> HandlerResponse[bool]:
        return self._inner.is_directory(path, context=context)

    # === Fallback delegation for remaining methods ===

    def __getattr__(self, name: str) -> Any:
        """Delegate any non-overridden attribute to inner backend.

        Covers: list_dir, connect, disconnect, check_connection,
        stream_content, write_stream, get_file_info, get_object_type,
        get_object_id, and any future Backend methods.
        """
        return getattr(self._inner, name)

    # === Cache management ===

    def get_cache_stats(self) -> dict[str, Any]:
        """Return L1 stats, L2 health, and hit/miss counters."""
        l1_stats = self._l1_cache.get_stats()
        with self._stats_lock:
            return {
                "l1": l1_stats,
                "l1_hits": self._l1_hits,
                "l1_misses": self._l1_misses,
                "l2_hits": self._l2_hits,
                "l2_misses": self._l2_misses,
                "cache_errors": self._cache_errors,
                "invalidations": self._invalidations,
                "strategy": self._config.strategy.value,
                "l2_enabled": self._config.l2_enabled,
                "l2_connected": self._cache_store is not None,
            }

    def clear_cache(self) -> None:
        """Clear L1 cache and reset all stats. L2 cleared via scheduled invalidation."""
        self._l1_cache.clear()
        with self._stats_lock:
            self._l1_hits = 0
            self._l1_misses = 0
            self._l2_hits = 0
            self._l2_misses = 0
            self._cache_errors = 0
            self._invalidations = 0

    # === L2 helpers ===

    @property
    def _l2_available(self) -> bool:
        """Check if L2 caching is both enabled and connected."""
        return self._config.l2_enabled and self._cache_store is not None

    def _l2_key(self, content_hash: str) -> str:
        """Build the L2 cache key for a content hash."""
        return f"{self._config.l2_key_prefix}:{content_hash}"

    # === L2 background population ===

    def _schedule_l2_populate(self, content_hash: str, content: bytes) -> None:
        """Fire-and-forget L2 population in the event loop (if running)."""
        if not self._l2_available:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._l2_populate(content_hash, content))
        except RuntimeError:
            # No running event loop — skip L2 (pure sync context)
            pass

    async def _l2_populate(self, content_hash: str, content: bytes) -> None:
        """Async L2 cache population. Errors logged, never raised."""
        if self._cache_store is None:
            return
        try:
            await self._cache_store.set(
                self._l2_key(content_hash), content, ttl=self._config.l2_ttl_seconds
            )
        except Exception as e:
            self._record_cache_error("l2_populate", e)

    def _l2_get_sync(self, content_hash: str) -> bytes | None:
        """Synchronous L2 read — only called on L1 miss.

        Uses run_coroutine_threadsafe with a timeout to avoid blocking
        indefinitely. Returns None on any error (graceful degradation).

        IMPORTANT: If the current thread IS the event loop thread,
        run_coroutine_threadsafe + .result() would deadlock. In that case
        we skip L2 and let the caller fall through to the inner backend.
        L2 population (fire-and-forget via create_task) still works.
        """
        if not self._l2_available or self._cache_store is None:
            return None
        try:
            loop = asyncio.get_running_loop()
            # Deadlock guard: if we're on the event loop thread, we cannot
            # block with .result() — the loop would never execute the coroutine.
            if loop.is_running() and threading.current_thread() is threading.main_thread():
                return None
            key = self._l2_key(content_hash)
            store = self._cache_store
            future = asyncio.run_coroutine_threadsafe(store.get(key), loop)
            result = future.result(timeout=0.1)  # 100ms timeout
            return result if isinstance(result, bytes) else None
        except Exception:
            return None

    def _l2_invalidate(self, content_hash: str) -> None:
        """Schedule L2 cache invalidation (fire-and-forget)."""
        if not self._l2_available:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._l2_delete(self._l2_key(content_hash)))
        except RuntimeError:
            pass

    async def _l2_delete(self, key: str) -> None:
        """Async L2 cache deletion. Errors logged, never raised."""
        if self._cache_store is None:
            return
        try:
            await self._cache_store.delete(key)
        except Exception as e:
            self._record_cache_error("l2_delete", e)

    # === Cache invalidation ===

    def _invalidate(self, content_hash: str) -> None:
        """Invalidate content from both L1 and L2."""
        try:
            self._l1_cache.remove(content_hash)
        except Exception as e:
            self._record_cache_error("l1_invalidate", e)

        self._l2_invalidate(content_hash)
        self._record_invalidation()

    # === Metrics ===

    def _record_l1_hit(self) -> None:
        with self._stats_lock:
            self._l1_hits += 1
        self._emit_metric("l1_hits")

    def _record_l1_miss(self) -> None:
        with self._stats_lock:
            self._l1_misses += 1
        self._emit_metric("l1_misses")

    def _record_l2_hit(self) -> None:
        with self._stats_lock:
            self._l2_hits += 1
        self._emit_metric("l2_hits")

    def _record_l2_miss(self) -> None:
        with self._stats_lock:
            self._l2_misses += 1
        self._emit_metric("l2_misses")

    def _record_invalidation(self) -> None:
        with self._stats_lock:
            self._invalidations += 1
        self._emit_metric("invalidations")

    def _record_cache_error(self, operation: str, error: Exception) -> None:
        with self._stats_lock:
            self._cache_errors += 1
        self._emit_metric("cache_errors")
        logger.debug("Cache error in %s: %s", operation, error)

    def _emit_metric(self, name: str) -> None:
        """Emit OTel counter increment (lazy-initialized, no-op if disabled)."""
        if not self._config.metrics_enabled:
            return
        metrics = self._get_metrics()
        if metrics is not None and name in metrics:
            metrics[name].add(1)

    def _get_metrics(self) -> dict[str, Any] | None:
        """Lazy-init OTel counters. Returns None if OTel disabled."""
        if self._metrics_initialized:
            return self._metrics
        self._metrics_initialized = True

        try:
            import os

            if os.environ.get("OTEL_ENABLED", "false").lower() not in ("true", "1", "yes"):
                return None

            from opentelemetry import metrics

            meter = metrics.get_meter("nexus.cache.backend_wrapper")
            self._metrics = {
                "l1_hits": meter.create_counter("cache.backend_wrapper.l1.hits"),
                "l1_misses": meter.create_counter("cache.backend_wrapper.l1.misses"),
                "l2_hits": meter.create_counter("cache.backend_wrapper.l2.hits"),
                "l2_misses": meter.create_counter("cache.backend_wrapper.l2.misses"),
                "cache_errors": meter.create_counter("cache.backend_wrapper.errors"),
                "invalidations": meter.create_counter("cache.backend_wrapper.invalidations"),
            }
            return self._metrics
        except Exception:
            return None
