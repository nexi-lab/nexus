"""Cache factory for creating cache instances based on configuration.

NOTE: This is the SYSTEMD LAYER (service manager), NOT kernel code.
    The kernel (NexusFS) only knows CacheStoreABC + NullCacheStore.
    This factory creates service-level domain caches on top of CacheStoreABC.

Architecture Note:
    CacheFactory owns a CacheStoreABC (the Fourth Pillar driver) and builds
    driver-agnostic domain caches on top of it.

    CacheStoreABC is used for hot caching (permission, embedding, tiger cache).
    All SSOT data (metadata, locks) is in Raft state machine (sled).

The factory handles:
- CacheStoreABC lifecycle (create/inject, connect, shutdown)
- Domain cache creation (PermissionCache, TigerCache, ResourceMapCache, EmbeddingCache)
- Health checks

Usage:
    # Option A: Auto-create from settings
    factory = CacheFactory(settings)
    await factory.initialize()

    # Option B: Inject pre-built CacheStoreABC
    factory = CacheFactory(settings, cache_store=my_store)
    await factory.initialize()

    permission_cache = factory.get_permission_cache()
    tiger_cache = factory.get_tiger_cache()

    # On shutdown
    await factory.shutdown()
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

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
from nexus.core.cache_store import CacheStoreABC, NullCacheStore

if TYPE_CHECKING:
    from nexus.cache.dragonfly import DragonflyClient
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


class CacheFactory:
    """Factory for creating cache instances based on configuration.

    Owns a CacheStoreABC driver and builds domain caches on top.
    When no driver is injected, creates one from settings or falls back to NullCacheStore.

    Note: This factory provides HOT CACHING only.
    SSOT (metadata, locks) is handled by Raft state machine, not this factory.
    """

    def __init__(
        self,
        settings: CacheSettings,
        cache_store: CacheStoreABC | None = None,
        record_store: RecordStoreABC | None = None,
    ):
        """Initialize cache factory.

        Args:
            settings: Cache configuration
            cache_store: Pre-built CacheStoreABC driver. If None, created from settings.
            record_store: RecordStoreABC for PostgreSQL cache fallback.
                If provided and Dragonfly is not available, the factory uses
                record_store.engine for PostgreSQL-backed caches instead of NullCacheStore.
        """
        self._settings = settings
        self._cache_store: CacheStoreABC = cache_store or NullCacheStore()
        self._cache_client: DragonflyClient | None = None  # kept for embedding cache
        self._record_store: RecordStoreABC | None = record_store
        self._initialized = False
        self._has_cache_store = False
        self._using_postgres = False

    async def initialize(self) -> None:
        """Initialize cache backend.

        If a CacheStoreABC was injected, uses it directly.
        Otherwise, creates DragonflyCacheStore from settings if configured.
        """
        if self._initialized:
            return

        self._settings.validate()

        # If caller injected a real (non-Null) CacheStoreABC, use it as-is
        if not isinstance(self._cache_store, NullCacheStore):
            self._has_cache_store = True
            self._initialized = True
            logger.info(
                f"Cache factory initialized with injected {type(self._cache_store).__name__}"
            )
            return

        # Auto-create from settings
        if self._settings.should_use_dragonfly():
            try:
                from nexus.cache.dragonfly import DragonflyCacheStore, DragonflyClient

                assert self._settings.dragonfly_url is not None
                self._cache_client = DragonflyClient(
                    url=self._settings.dragonfly_url,
                    pool_size=self._settings.dragonfly_pool_size,
                    timeout=self._settings.dragonfly_timeout,
                    connect_timeout=self._settings.dragonfly_connect_timeout,
                    pool_timeout=self._settings.dragonfly_pool_timeout,
                    socket_keepalive=self._settings.dragonfly_keepalive,
                    retry_on_timeout=self._settings.dragonfly_retry_on_timeout,
                )
                await self._cache_client.connect()
                self._cache_store = DragonflyCacheStore(self._cache_client)
                self._has_cache_store = True
                logger.info("Cache factory initialized with Dragonfly backend (hot cache only)")

            except ImportError:
                logger.warning("redis package not installed, falling back to NullCacheStore")
                self._has_cache_store = False
            except Exception as e:
                if self._settings.cache_backend == "dragonfly":
                    raise
                logger.warning(
                    f"Failed to connect to Dragonfly ({e}), falling back to NullCacheStore"
                )
                self._has_cache_store = False
        elif self._record_store is not None:
            self._using_postgres = True
            logger.info("Cache factory initialized with PostgreSQL cache backend (fallback)")
        else:
            logger.info("Cache factory initialized with NullCacheStore (no Dragonfly configured)")
            self._has_cache_store = False

        self._initialized = True

    async def shutdown(self) -> None:
        """Shutdown cache backend and cleanup connections."""
        await self._cache_store.close()
        self._cache_client = None
        self._cache_store = NullCacheStore()
        self._record_store = None
        self._initialized = False
        self._has_cache_store = False
        self._using_postgres = False
        logger.info("Cache factory shutdown complete")

    @property
    def cache_store(self) -> CacheStoreABC:
        """Get the underlying CacheStoreABC driver.

        Available for kernel/NexusFS to use directly if needed.
        """
        return self._cache_store

    @property
    def has_cache_store(self) -> bool:
        """Check if a real CacheStoreABC driver is active (not NullCacheStore)."""
        return self._has_cache_store

    @property
    def is_using_postgres(self) -> bool:
        """Check if PostgreSQL cache backend is active."""
        return self._using_postgres

    @property
    def backend_name(self) -> str:
        """Get the name of the active backend."""
        if self._using_postgres:
            return "PostgreSQL"
        return type(self._cache_store).__name__

    def get_permission_cache(self) -> PermissionCacheProtocol:
        """Get permission cache instance.

        Returns RecordStore-backed (SQL) cache when record_store is available and
        no CacheStoreABC driver is configured. Otherwise returns CacheStoreABC-backed cache.
        """
        if not self._initialized:
            raise RuntimeError("CacheFactory not initialized. Call initialize() first.")

        if self._using_postgres and self._record_store is not None:
            from nexus.cache.postgres import PostgresPermissionCache

            return PostgresPermissionCache(
                engine=self._record_store.engine,
                ttl=self._settings.permission_ttl,
                denial_ttl=self._settings.permission_denial_ttl,
            )

        return PermissionCache(
            store=self._cache_store,
            ttl=self._settings.permission_ttl,
            denial_ttl=self._settings.permission_denial_ttl,
        )

    def get_tiger_cache(self) -> TigerCacheProtocol:
        """Get Tiger cache instance.

        Returns RecordStore-backed (SQL) cache when record_store is available and
        no CacheStoreABC driver is configured. Otherwise returns CacheStoreABC-backed cache.
        """
        if not self._initialized:
            raise RuntimeError("CacheFactory not initialized. Call initialize() first.")

        if self._using_postgres and self._record_store is not None:
            from nexus.cache.postgres import PostgresTigerCache

            return PostgresTigerCache(engine=self._record_store.engine)

        return TigerCache(
            store=self._cache_store,
            ttl=self._settings.tiger_ttl,
        )

    def get_resource_map_cache(self) -> ResourceMapCacheProtocol:
        """Get resource map cache instance.

        Returns RecordStore-backed (SQL) cache when record_store is available and
        no CacheStoreABC driver is configured. Otherwise returns CacheStoreABC-backed cache.
        """
        if not self._initialized:
            raise RuntimeError("CacheFactory not initialized. Call initialize() first.")

        if self._using_postgres and self._record_store is not None:
            from nexus.cache.postgres import PostgresResourceMapCache

            return PostgresResourceMapCache(engine=self._record_store.engine)

        return ResourceMapCache(store=self._cache_store)

    def get_embedding_cache(self) -> EmbeddingCacheProtocol:
        """Get embedding cache instance (driver-agnostic, built on CacheStoreABC)."""
        if not self._initialized:
            raise RuntimeError("CacheFactory not initialized. Call initialize() first.")

        return EmbeddingCache(
            store=self._cache_store,
            ttl=self._settings.embedding_ttl,
        )

    async def health_check(self) -> dict:
        """Check health of cache backend."""
        if self._using_postgres and self._record_store is not None:
            from nexus.cache.postgres import PostgresPermissionCache

            pg_cache = PostgresPermissionCache(engine=self._record_store.engine)
            healthy = await pg_cache.health_check()
            return {
                "backend": self.backend_name,
                "initialized": self._initialized,
                "healthy": healthy,
            }

        result: dict = {
            "backend": self.backend_name,
            "initialized": self._initialized,
            "healthy": await self._cache_store.health_check(),
        }

        if self._has_cache_store and self._cache_client:
            result["dragonfly_info"] = await self._cache_client.get_info()

        return result

    async def __aenter__(self) -> CacheFactory:
        """Async context manager entry."""
        await self.initialize()
        return self

    async def __aexit__(self, *args: object) -> None:
        """Async context manager exit."""
        await self.shutdown()


# Global factory instance for dependency injection
_cache_factory: CacheFactory | None = None


async def init_cache_factory(
    settings: CacheSettings,
    cache_store: CacheStoreABC | None = None,
    record_store: RecordStoreABC | None = None,
) -> CacheFactory:
    """Initialize the global cache factory.

    Args:
        settings: Cache configuration
        cache_store: Pre-built CacheStoreABC driver
        record_store: RecordStoreABC for SQL-backed cache fallback
    """
    global _cache_factory
    _cache_factory = CacheFactory(settings, cache_store=cache_store, record_store=record_store)
    await _cache_factory.initialize()
    return _cache_factory


async def shutdown_cache_factory() -> None:
    """Shutdown the global cache factory."""
    global _cache_factory
    if _cache_factory:
        await _cache_factory.shutdown()
        _cache_factory = None


def get_cache_factory() -> CacheFactory:
    """Get the global cache factory instance."""
    if not _cache_factory:
        raise RuntimeError("Cache factory not initialized. Call init_cache_factory() first.")
    return _cache_factory
