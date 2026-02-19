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
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

from nexus.cache.base import (
    EmbeddingCacheProtocol,
    PermissionCacheProtocol,
    ResourceMapCacheProtocol,
    TigerCacheProtocol,
)
from nexus.cache.cache_store import NullCacheStore
from nexus.cache.domain import (
    EmbeddingCache,
    PermissionCache,
    ResourceMapCache,
    TigerCache,
)
from nexus.cache.settings import CacheSettings

if TYPE_CHECKING:
    from nexus.backends.caching_wrapper import CacheWrapperConfig, CachingBackendWrapper

logger = logging.getLogger(__name__)


class CacheBrick:
    """Tier 2 Cache Brick — owns all cache domain services.

    Provides protocol-typed accessors for domain caches and integrates
    with ``BrickLifecycleManager`` for start/stop orchestration.

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
    # Lifecycle — satisfies BrickLifecycleProtocol
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize the cache brick.

        Verifies store connectivity. On failure, logs a warning and
        continues (Tier 2 = silent degradation).
        """
        if self._started:
            return
        try:
            await self._store.health_check()
            self._started = True
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

    # Backward-compat aliases for CacheFactory API
    async def initialize(self) -> None:
        """Alias for start() — backward compat with CacheFactory."""
        await self.start()

    async def shutdown(self) -> None:
        """Alias for stop() — backward compat with CacheFactory."""
        await self.stop()

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
    # Protocol-typed accessors (properties)
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

    # Backward-compat method aliases for CacheFactory API
    def get_permission_cache(self) -> PermissionCacheProtocol:
        """Alias for .permission_cache — backward compat with CacheFactory."""
        return self._permission_cache

    def get_tiger_cache(self) -> TigerCacheProtocol:
        """Alias for .tiger_cache — backward compat with CacheFactory."""
        return self._tiger_cache

    def get_resource_map_cache(self) -> ResourceMapCacheProtocol:
        """Alias for .resource_map_cache — backward compat with CacheFactory."""
        return self._resource_map_cache

    def get_embedding_cache(self) -> EmbeddingCacheProtocol:
        """Alias for .embedding_cache — backward compat with CacheFactory."""
        return self._embedding_cache

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
        config: CacheWrapperConfig | None = None,
        *,
        enable_logging: bool = False,
    ) -> CachingBackendWrapper:
        """Create a CachingBackendWrapper for the given backend.

        Args:
            inner: Backend to wrap with caching.
            config: Optional wrapper configuration.
            enable_logging: If True, insert LoggingBackendWrapper.

        Returns:
            CachingBackendWrapper wrapping the inner backend.
        """
        from nexus.backends.caching_wrapper import CacheWrapperConfig as CWC
        from nexus.backends.caching_wrapper import CachingBackendWrapper

        effective_config = config or CWC()

        wrapped_inner = inner
        if enable_logging:
            from nexus.backends.logging_wrapper import LoggingBackendWrapper

            wrapped_inner = LoggingBackendWrapper(inner=inner)

        cache_store = self._store if self.has_cache_store else None

        return CachingBackendWrapper(
            inner=wrapped_inner,
            config=effective_config,
            cache_store=cache_store,
        )

    # ------------------------------------------------------------------
    # SHA-256 hash utility
    # ------------------------------------------------------------------

    def compute_content_hash(self, data: bytes) -> str:
        """Compute SHA-256 hash of content.

        Args:
            data: Binary content to hash.

        Returns:
            64-character hex digest string.
        """
        return hashlib.sha256(data).hexdigest()

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> CacheBrick:
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        """Async context manager exit."""
        await self.stop()
