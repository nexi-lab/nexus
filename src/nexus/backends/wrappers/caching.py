"""CachingBackendWrapper — transparent caching decorator for any Backend (#1392).

Wraps an inner Backend and adds two-layer caching:
- L1: In-memory ContentCache (sync, fast, process-local, LZ4 compressed)
- L2: CacheStoreABC/Dragonfly (async background population, distributed)

Follows LEGO Architecture PART 16 (Recursive Wrapping, Mechanism 2).
All Backend operations pass through transparently. Only CAS read operations
are cached (read_content, batch_read_content). Writes invalidate or populate
cache based on the configured CacheStrategy.

L2 is write-populate-only: content is populated into L2 on read misses
and writes (fire-and-forget), but L2 is never read synchronously. This
avoids deadlock risks in the FastAPI server path and keeps the hot path
fast. Other instances (or restarts) benefit from L2 population.

Cache failures are silently swallowed — inner backend is always the fallback.

Usage:
    from nexus.backends.wrappers.caching import CachingBackendWrapper, CacheWrapperConfig

    wrapper = CachingBackendWrapper(
        inner=local_backend,
        config=CacheWrapperConfig(strategy=CacheStrategy.WRITE_AROUND),
        cache_store=dragonfly_store,  # optional L2
    )
    # Use wrapper exactly like any Backend
    resp = wrapper.read_content(content_hash)
"""

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from nexus.backends.base.backend import Backend
from nexus.backends.storage.delegating import DelegatingBackend
from nexus.backends.wrappers.metrics import WrapperMetrics
from nexus.core.object_store import WriteResult
from nexus.storage.content_cache import ContentCache

if TYPE_CHECKING:
    from nexus.contracts.cache_store import CacheStoreABC
    from nexus.contracts.types import OperationContext

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


class CachingBackendWrapper(DelegatingBackend):
    """Transparent caching decorator for any Backend implementation.

    Inherits property delegation and ``__getattr__`` from ``DelegatingBackend``.
    Overrides CAS content operations to add two-layer caching. Writes
    invalidate or populate cache based on the configured strategy.

    Cache failures are silently swallowed — inner backend is always the
    source of truth. OTel counters track hit/miss/error rates.

    L2 is write-populate-only (Decision 13C): content is pushed to L2 on
    read misses and writes for cross-instance warming, but L2 is never read
    synchronously. This eliminates deadlock risks on the FastAPI event loop
    thread and keeps the hot path fast.
    """

    def __init__(
        self,
        inner: Backend,
        config: CacheWrapperConfig | None = None,
        cache_store: "CacheStoreABC | None" = None,
    ) -> None:
        super().__init__(inner)
        self._config = config or CacheWrapperConfig()
        self._cache_store = cache_store

        # L1: in-memory content cache (owned by this wrapper)
        self._l1_cache = ContentCache(
            max_size_mb=self._config.l1_max_size_mb,
            compression_threshold=self._config.l1_compression_threshold,
        )

        # Shared OTel + in-memory metrics via WrapperMetrics
        self._metrics = WrapperMetrics(
            meter_name="nexus.cache.backend_wrapper",
            counter_names=[
                "l1_hits",
                "l1_misses",
                "cache_errors",
                "invalidations",
            ],
            enabled=self._config.metrics_enabled,
        )

        # Note: get_cache_stats() returns a best-effort snapshot. L1 stats and
        # WrapperMetrics counters are individually thread-safe but the combined
        # snapshot is not atomic (acceptable for diagnostic/observability use).

    # === Name & Chain Introspection ===

    @property
    def name(self) -> str:
        return f"cached({self._inner.name})"

    def describe(self) -> str:
        """Return chain description: ``"cache → {inner.describe()}"``."""
        return f"cache → {self._inner.describe()}"

    # === Cached Content Operations ===

    def _read_content_raw(
        self, content_id: str, context: "OperationContext | None" = None
    ) -> bytes:
        """Read content with L1 → inner backend fallback.

        L2 is write-populate-only; reads never check L2.
        """
        # L1 check
        try:
            cached = self._l1_cache.get(content_id)
            if cached is not None:
                self._metrics.increment("l1_hits")
                return cached
            self._metrics.increment("l1_misses")
        except Exception as e:
            self._record_cache_error("l1_read", e)

        # Inner backend read (raises on failure — no response wrapping)
        content = self._inner.read_content(content_id, context=context)

        # Populate L1
        try:
            self._l1_cache.put(content_id, content)
        except Exception as e:
            self._record_cache_error("l1_populate", e)
        # Schedule L2 population (fire-and-forget)
        self._schedule_l2_populate(content_id, content)

        return content

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        """Write content to inner backend, then handle cache based on strategy."""
        result = self._inner.write_content(content, content_id, offset=offset, context=context)

        # Offset writes always invalidate (content changed partially)
        if offset > 0:
            self._invalidate(result.content_id)
            return result

        content_hash = result.content_id

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

        return result

    def delete_content(self, content_id: str, context: "OperationContext | None" = None) -> None:
        """Delete from inner backend and invalidate caches."""
        self._inner.delete_content(content_id, context=context)
        self._invalidate(content_id)

    def content_exists(self, content_id: str, context: "OperationContext | None" = None) -> bool:
        """Always delegate to inner backend — source of truth for existence."""
        return self._inner.content_exists(content_id, context=context)

    def batch_read_content(
        self,
        content_hashes: list[str],
        context: "OperationContext | None" = None,
        *,
        contexts: "dict[str, OperationContext] | None" = None,
    ) -> dict[str, bytes | None]:
        """Batch read with L1 cache for already-cached items.

        L2 is write-populate-only; batch reads never check L2.
        """
        result: dict[str, bytes | None] = {}
        uncached_hashes: list[str] = []

        # First pass: check L1 for all hashes
        for content_hash in content_hashes:
            try:
                cached = self._l1_cache.get(content_hash)
                if cached is not None:
                    result[content_hash] = cached
                    self._metrics.increment("l1_hits")
                else:
                    uncached_hashes.append(content_hash)
                    self._metrics.increment("l1_misses")
            except Exception as e:
                self._record_cache_error("l1_batch_read", e)
                uncached_hashes.append(content_hash)

        # Second pass: read remaining uncached from inner backend
        if uncached_hashes:
            inner_results = self._inner.batch_read_content(
                uncached_hashes, context=context, contexts=contexts
            )
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

    # === Cache management (CachingConnectorContract) ===

    def get_cache_stats(self) -> dict[str, Any]:
        """Return L1 stats, L2 health, and hit/miss counters.

        Note: Stats are best-effort snapshots. L1 stats and counter values
        are individually thread-safe but the combined snapshot is not atomic.
        """
        l1_stats = self._l1_cache.get_stats()
        counters = self._metrics.get_stats()
        return {
            "l1": l1_stats,
            "l1_hits": counters.get("l1_hits", 0),
            "l1_misses": counters.get("l1_misses", 0),
            "cache_errors": counters.get("cache_errors", 0),
            "invalidations": counters.get("invalidations", 0),
            "strategy": self._config.strategy.value,
            "l2_enabled": self._config.l2_enabled,
            "l2_connected": self._cache_store is not None,
            "l2_mode": "write-populate-only",
        }

    def clear_cache(self) -> None:
        """Clear L1 cache and reset all stats. L2 cleared via scheduled invalidation."""
        self._l1_cache.clear()
        self._metrics.reset()

    # === L2 helpers (write-populate-only) ===

    @property
    def _l2_available(self) -> bool:
        """Check if L2 caching is both enabled and connected."""
        return self._config.l2_enabled and self._cache_store is not None

    def _l2_key(self, content_hash: str) -> str:
        """Build the L2 cache key for a content hash."""
        return f"{self._config.l2_key_prefix}:{content_hash}"

    def _schedule_l2_populate(self, content_hash: str, content: bytes) -> None:
        """Fire-and-forget L2 population in the event loop (if running)."""
        if not self._l2_available:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._l2_populate(content_hash, content))
        except RuntimeError:
            # No running event loop — skip L2 (pure sync context)
            logger.debug(
                "L2 populate skipped: no running event loop for hash=%s", content_hash[:12]
            )

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
        self._metrics.increment("invalidations")

    # === Error recording ===

    def _record_cache_error(self, operation: str, error: Exception) -> None:
        self._metrics.increment("cache_errors")
        logger.debug("Cache error in %s: %s", operation, error)
