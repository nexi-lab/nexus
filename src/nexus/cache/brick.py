"""CacheBrick — Tier 2 cache brick facade (Issue #1524).

Single entry point for all cache domain services. Follows the exemplary
brick pattern (like ``ParsersBrick``):

- Zero runtime imports from ``nexus.core``
- Constructor injection for CacheStoreABC + CacheSettings
- NullCacheStore fallback (Tier 2 = silent degradation)
- Full lifecycle: start() / stop() / health_check()
- Satisfies ``BrickLifecycleProtocol`` via structural subtyping

Architecture::

    CacheBrick
    ├── CacheStoreABC (injected — Dragonfly, InMemory, or Null)
    ├── PermissionCache (driver-agnostic domain cache)
    ├── TigerCache
    ├── ResourceMapCache
    ├── EmbeddingCache
    └── CachingBackendWrapper factory

NOTE: L2 async write-behind is a follow-up (Decision #14).
"""

import logging
from typing import TYPE_CHECKING, Any

from nexus.cache.base import (
    EmbeddingCacheProtocol,
    PermissionCacheProtocol,
    ResourceMapCacheProtocol,
    TigerCacheProtocol,
)
from nexus.cache.domain import (
    EmbeddingCache,
    PermissionCache,
    ResourceMapCache,
    TigerCache,
)
from nexus.cache.settings import CacheSettings
from nexus.contracts.cache_store import NullCacheStore

if TYPE_CHECKING:
    from nexus.backends.wrappers.caching import CacheWrapperConfig, CachingBackendWrapper

logger = logging.getLogger(__name__)


class CacheBrick:
    """Tier 2 Cache Brick — owns all cache domain services.

    Provides protocol-typed accessors for domain caches with
    start/stop lifecycle (enlisted as BackgroundService via ServiceRegistry).

    Example::

        brick = CacheBrick(cache_store=dragonfly_store, settings=settings)
        await brick.start()

        perm = brick.permission_cache
        result = await perm.get("user", "alice", "read", "file", "/a", "z1")

        await brick.stop()
    """

    def __init__(
        self,
        cache_store: Any | None = None,
        settings: CacheSettings | None = None,
        record_store: Any | None = None,
    ) -> None:
        """Initialize the CacheBrick.

        Args:
            cache_store: CacheStoreABC driver. If None, NullCacheStore is used
                (Tier 2 = silent degradation).
            settings: Cache configuration. If None, defaults are used.
            record_store: Optional RecordStoreABC for PostgreSQL cache fallback.
        """
        self._store = cache_store if cache_store is not None else NullCacheStore()
        self._settings = settings or CacheSettings(dragonfly_url=None)
        self._record_store = record_store
        self._started = False

        # Domain caches (driver-agnostic, built on CacheStoreABC primitives)
        self._permission_cache = PermissionCache(
            store=self._store,
            ttl=self._settings.permission_ttl,
            denial_ttl=self._settings.permission_denial_ttl,
        )
        self._tiger_cache = TigerCache(
            store=self._store,
            ttl=self._settings.tiger_ttl,
        )
        self._resource_map_cache = ResourceMapCache(store=self._store)
        self._embedding_cache = EmbeddingCache(
            store=self._store,
            ttl=self._settings.embedding_ttl,
        )

    # ------------------------------------------------------------------
    # Lifecycle (Decision #4) — satisfies BrickLifecycleProtocol
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize the cache brick.

        Verifies store connectivity. On failure, logs a warning and
        continues (Tier 2 = silent degradation).
        """
        if self._started:
            return
        try:
            healthy = await self._store.health_check()
            self._started = True
            if not healthy:
                logger.warning(
                    "[CacheBrick] started but health check returned unhealthy (backend=%s)",
                    self.backend_name,
                )
            else:
                logger.info("[CacheBrick] started (backend=%s)", self.backend_name)
        except Exception as exc:
            logger.warning("[CacheBrick] start health check failed: %s", exc)
            self._started = True  # Still mark as started — silent degradation

    async def stop(self) -> None:
        """Shut down the cache brick and close the store connection."""
        if not self._started:
            return
        try:
            await self._store.close()
        except Exception as exc:
            logger.warning("[CacheBrick] stop failed: %s", exc)
        self._started = False
        logger.info("[CacheBrick] stopped")

    async def health_check(self) -> bool:
        """Check if the cache backend is healthy.

        Returns:
            True if healthy, False otherwise.
        """
        try:
            return await self._store.health_check()
        except Exception:
            return False

    # ------------------------------------------------------------------
    # CacheFactory-compatible API (Issue #1524 / #3A absorption)
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """CacheFactory-compatible alias for ``start()``.

        Allows CacheBrick to be used as a drop-in replacement for CacheFactory.
        """
        await self.start()

    async def shutdown(self) -> None:
        """CacheFactory-compatible alias for ``stop()``.

        Allows CacheBrick to be used as a drop-in replacement for CacheFactory.
        """
        await self.stop()

    def get_permission_cache(self) -> PermissionCacheProtocol:
        """CacheFactory-compatible accessor for permission cache."""
        return self._permission_cache

    def get_tiger_cache(self) -> TigerCacheProtocol:
        """CacheFactory-compatible accessor for tiger cache."""
        return self._tiger_cache

    def get_resource_map_cache(self) -> ResourceMapCacheProtocol:
        """CacheFactory-compatible accessor for resource map cache."""
        return self._resource_map_cache

    def get_embedding_cache(self) -> EmbeddingCacheProtocol:
        """CacheFactory-compatible accessor for embedding cache."""
        return self._embedding_cache

    # ------------------------------------------------------------------
    # Protocol-typed accessors
    # ------------------------------------------------------------------

    @property
    def permission_cache(self) -> PermissionCacheProtocol:
        """Permission cache (PermissionCacheProtocol)."""
        return self._permission_cache

    @property
    def tiger_cache(self) -> TigerCacheProtocol:
        """Tiger bitmap cache (TigerCacheProtocol)."""
        return self._tiger_cache

    @property
    def resource_map_cache(self) -> ResourceMapCacheProtocol:
        """Resource map cache (ResourceMapCacheProtocol)."""
        return self._resource_map_cache

    @property
    def embedding_cache(self) -> EmbeddingCacheProtocol:
        """Embedding vector cache (EmbeddingCacheProtocol)."""
        return self._embedding_cache

    @property
    def cache_store(self) -> Any:
        """Underlying CacheStoreABC driver."""
        return self._store

    @property
    def settings(self) -> CacheSettings:
        """Cache configuration."""
        return self._settings

    # ------------------------------------------------------------------
    # Status / reporting
    # ------------------------------------------------------------------

    @property
    def backend_name(self) -> str:
        """Name of the active backend for health/status reporting."""
        return type(self._store).__name__

    @property
    def has_cache_store(self) -> bool:
        """Whether a real (non-Null) CacheStoreABC driver is active."""
        return not isinstance(self._store, NullCacheStore)

    # ------------------------------------------------------------------
    # CachingBackendWrapper factory
    # ------------------------------------------------------------------

    def create_caching_wrapper(
        self,
        inner: Any,
        config: "CacheWrapperConfig | None" = None,
        *,
        enable_logging: bool = False,
    ) -> "CachingBackendWrapper":
        """Create a CachingBackendWrapper for the given backend.

        Args:
            inner: Backend to wrap with caching.
            config: Optional wrapper configuration.
            enable_logging: If True, insert LoggingBackendWrapper.

        Returns:
            CachingBackendWrapper wrapping the inner backend.
        """
        from nexus.backends.wrappers.caching import CacheWrapperConfig as CWC
        from nexus.backends.wrappers.caching import CachingBackendWrapper

        effective_config = config or CWC()

        wrapped_inner = inner
        if enable_logging:
            from nexus.backends.wrappers.logging import LoggingBackendWrapper

            wrapped_inner = LoggingBackendWrapper(inner=inner)

        cache_store = self._store if self.has_cache_store else None

        return CachingBackendWrapper(
            inner=wrapped_inner,
            config=effective_config,
            cache_store=cache_store,
        )
