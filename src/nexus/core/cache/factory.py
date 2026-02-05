"""Cache factory for creating cache instances based on configuration.

Architecture Note:
    Dragonfly is used ONLY for hot caching (permission, embedding, tiger cache).
    All SSOT data (metadata, locks) is in Raft state machine (sled).
    dragonfly-coordination has been REMOVED - locks are now linearizable via Raft.

The factory handles:
- Backend selection (Dragonfly vs PostgreSQL)
- Connection lifecycle management
- Health checks
- Graceful fallback when Dragonfly is unavailable

Usage:
    settings = CacheSettings.from_env()
    factory = CacheFactory(settings)
    await factory.initialize()

    permission_cache = factory.get_permission_cache()
    tiger_cache = factory.get_tiger_cache()

    # On shutdown
    await factory.shutdown()
"""

import logging
from typing import TYPE_CHECKING, Optional

from nexus.core.cache.base import (
    PermissionCacheProtocol,
    ResourceMapCacheProtocol,
    TigerCacheProtocol,
)
from nexus.core.cache.dragonfly import DragonflyEmbeddingCache
from nexus.core.cache.settings import CacheSettings

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from nexus.core.cache.dragonfly import DragonflyClient

logger = logging.getLogger(__name__)


class CacheFactory:
    """Factory for creating cache instances based on configuration.

    Note: This factory provides HOT CACHING only.
    SSOT (metadata, locks) is handled by Raft state machine, not this factory.

    Supports multiple backends:
    - Dragonfly (Redis-compatible): Fast, shared across instances
    - PostgreSQL: Fallback using existing tables
    """

    def __init__(
        self,
        settings: CacheSettings,
        db_engine: Optional["Engine"] = None,
    ):
        """Initialize cache factory.

        Args:
            settings: Cache configuration
            db_engine: SQLAlchemy engine for PostgreSQL backend (optional if using Dragonfly only)
        """
        self._settings = settings
        self._db_engine = db_engine
        self._cache_client: DragonflyClient | None = None
        self._initialized = False
        self._using_dragonfly = False

    async def initialize(self) -> None:
        """Initialize cache backend (Dragonfly or PostgreSQL)."""
        if self._initialized:
            return

        self._settings.validate()

        if self._settings.should_use_dragonfly():
            try:
                from nexus.core.cache.dragonfly import DragonflyClient

                self._cache_client = DragonflyClient(
                    url=self._settings.dragonfly_url,  # type: ignore[arg-type]
                    pool_size=self._settings.dragonfly_pool_size,
                    timeout=self._settings.dragonfly_timeout,
                    connect_timeout=self._settings.dragonfly_connect_timeout,
                    pool_timeout=self._settings.dragonfly_pool_timeout,
                    socket_keepalive=self._settings.dragonfly_keepalive,
                    retry_on_timeout=self._settings.dragonfly_retry_on_timeout,
                )
                await self._cache_client.connect()
                self._using_dragonfly = True
                logger.info("Cache factory initialized with Dragonfly backend (hot cache only)")

            except ImportError:
                logger.warning("redis package not installed, falling back to PostgreSQL cache")
                self._using_dragonfly = False
            except Exception as e:
                if self._settings.cache_backend == "dragonfly":
                    raise
                logger.warning(
                    f"Failed to connect to Dragonfly ({e}), falling back to PostgreSQL cache"
                )
                self._using_dragonfly = False
        else:
            logger.info("Cache factory initialized with PostgreSQL backend")
            self._using_dragonfly = False

        self._initialized = True

    async def shutdown(self) -> None:
        """Shutdown cache backend and cleanup connections."""
        if self._cache_client:
            await self._cache_client.disconnect()
        self._cache_client = None
        self._initialized = False
        self._using_dragonfly = False
        logger.info("Cache factory shutdown complete")

    @property
    def is_using_dragonfly(self) -> bool:
        """Check if Dragonfly backend is active."""
        return self._using_dragonfly

    @property
    def backend_name(self) -> str:
        """Get the name of the active backend."""
        return "dragonfly" if self._using_dragonfly else "postgres"

    def get_permission_cache(self) -> PermissionCacheProtocol:
        """Get permission cache instance."""
        if not self._initialized:
            raise RuntimeError("CacheFactory not initialized. Call initialize() first.")

        if self._using_dragonfly and self._cache_client:
            from nexus.core.cache.dragonfly import DragonflyPermissionCache

            return DragonflyPermissionCache(
                client=self._cache_client,
                ttl=self._settings.permission_ttl,
                denial_ttl=self._settings.permission_denial_ttl,
            )

        from nexus.core.cache.postgres import PostgresPermissionCache

        if not self._db_engine:
            raise RuntimeError("PostgreSQL cache requires db_engine but none was provided")
        return PostgresPermissionCache(
            engine=self._db_engine,
            ttl=self._settings.permission_ttl,
            denial_ttl=self._settings.permission_denial_ttl,
        )

    def get_tiger_cache(self) -> TigerCacheProtocol:
        """Get Tiger cache instance."""
        if not self._initialized:
            raise RuntimeError("CacheFactory not initialized. Call initialize() first.")

        if self._using_dragonfly and self._cache_client:
            from nexus.core.cache.dragonfly import DragonflyTigerCache

            return DragonflyTigerCache(
                client=self._cache_client,
                ttl=self._settings.tiger_ttl,
            )

        from nexus.core.cache.postgres import PostgresTigerCache

        if not self._db_engine:
            raise RuntimeError("PostgreSQL cache requires db_engine but none was provided")
        return PostgresTigerCache(engine=self._db_engine)

    def get_resource_map_cache(self) -> ResourceMapCacheProtocol:
        """Get resource map cache instance."""
        if not self._initialized:
            raise RuntimeError("CacheFactory not initialized. Call initialize() first.")

        if self._using_dragonfly and self._cache_client:
            from nexus.core.cache.dragonfly import DragonflyResourceMapCache

            return DragonflyResourceMapCache(client=self._cache_client)

        from nexus.core.cache.postgres import PostgresResourceMapCache

        if not self._db_engine:
            raise RuntimeError("PostgreSQL cache requires db_engine but none was provided")
        return PostgresResourceMapCache(engine=self._db_engine)

    def get_embedding_cache(self) -> DragonflyEmbeddingCache | None:
        """Get embedding cache instance (Dragonfly only, no PostgreSQL fallback)."""
        if not self._initialized:
            raise RuntimeError("CacheFactory not initialized. Call initialize() first.")

        if self._using_dragonfly and self._cache_client:
            return DragonflyEmbeddingCache(
                client=self._cache_client,
                ttl=self._settings.embedding_ttl,
            )

        logger.debug("Embedding cache not available (requires Dragonfly backend)")
        return None

    async def health_check(self) -> dict:
        """Check health of cache backend."""
        result: dict = {
            "backend": self.backend_name,
            "initialized": self._initialized,
        }

        if self._using_dragonfly and self._cache_client:
            result["dragonfly"] = await self._cache_client.health_check()
            result["dragonfly_info"] = await self._cache_client.get_info()
        else:
            result["postgres"] = self._db_engine is not None

        return result

    async def __aenter__(self) -> "CacheFactory":
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
    db_engine: Optional["Engine"] = None,
) -> CacheFactory:
    """Initialize the global cache factory."""
    global _cache_factory
    _cache_factory = CacheFactory(settings, db_engine)
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
