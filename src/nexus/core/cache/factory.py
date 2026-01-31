"""Cache factory for creating cache instances based on configuration.

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

    Supports multiple backends:
    - Dragonfly (Redis-compatible): Fast, shared across instances
    - PostgreSQL: Fallback using existing tables

    The factory automatically selects the backend based on configuration:
    - If NEXUS_DRAGONFLY_URL is set and NEXUS_CACHE_BACKEND is "auto" or "dragonfly",
      Dragonfly is used
    - Otherwise, PostgreSQL is used

    Example:
        settings = CacheSettings.from_env()
        factory = CacheFactory(settings, db_engine)
        await factory.initialize()

        # Get cache instances
        perm_cache = factory.get_permission_cache()
        tiger_cache = factory.get_tiger_cache()

        # Check health
        health = await factory.health_check()

        # Cleanup
        await factory.shutdown()
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
        self._coordination_client: DragonflyClient | None = None
        self._initialized = False
        self._using_dragonfly = False

    async def initialize(self) -> None:
        """Initialize cache backends.

        Connects to Dragonfly if configured, otherwise uses PostgreSQL.
        Also initializes coordination client if configured separately.
        """
        if self._initialized:
            return

        self._settings.validate()

        if self._settings.should_use_dragonfly_cache():
            try:
                from nexus.core.cache.dragonfly import DragonflyClient

                # Initialize cache client
                self._cache_client = DragonflyClient(
                    url=self._settings.dragonfly_cache_url,  # type: ignore[arg-type]  # allowed
                    pool_size=self._settings.dragonfly_pool_size,
                    timeout=self._settings.dragonfly_timeout,
                    connect_timeout=self._settings.dragonfly_connect_timeout,
                    pool_timeout=self._settings.dragonfly_pool_timeout,
                    socket_keepalive=self._settings.dragonfly_keepalive,
                    retry_on_timeout=self._settings.dragonfly_retry_on_timeout,
                )
                await self._cache_client.connect()
                self._using_dragonfly = True

                # Initialize coordination client
                if self._settings.is_coordination_separate():
                    # Separate coordination instance (recommended for production)
                    self._coordination_client = DragonflyClient(
                        url=self._settings.dragonfly_coordination_url,  # type: ignore[arg-type]  # allowed
                        pool_size=self._settings.dragonfly_pool_size,
                        timeout=self._settings.dragonfly_timeout,
                        connect_timeout=self._settings.dragonfly_connect_timeout,
                        pool_timeout=self._settings.dragonfly_pool_timeout,
                        socket_keepalive=self._settings.dragonfly_keepalive,
                        retry_on_timeout=self._settings.dragonfly_retry_on_timeout,
                    )
                    await self._coordination_client.connect()
                    logger.info(
                        "Cache factory initialized with separate Dragonfly instances "
                        "(cache + coordination)"
                    )
                else:
                    # Single instance mode: reuse cache client for coordination
                    # This is risky because cache eviction may delete locks
                    if not self._settings.allow_single_dragonfly:
                        raise RuntimeError(
                            "Single Dragonfly instance mode is not allowed. "
                            "Either set NEXUS_DRAGONFLY_COORDINATION_URL for a separate "
                            "coordination instance (recommended), or set "
                            "NEXUS_ALLOW_SINGLE_DRAGONFLY=true to use single instance "
                            "(NOT recommended for production - locks may be evicted)."
                        )
                    self._coordination_client = self._cache_client
                    logger.warning(
                        "Cache factory using single Dragonfly instance for both cache and "
                        "coordination. Locks may be evicted unexpectedly. "
                        "Set NEXUS_DRAGONFLY_COORDINATION_URL for production use."
                    )

            except ImportError:
                logger.warning("redis package not installed, falling back to PostgreSQL cache")
                self._using_dragonfly = False
            except Exception as e:
                if self._settings.cache_backend == "dragonfly":
                    # Dragonfly was explicitly required, don't fall back
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
        """Shutdown cache backends and cleanup connections."""
        # Shutdown coordination client first (if separate from cache)
        if self._coordination_client and self._coordination_client is not self._cache_client:
            await self._coordination_client.disconnect()
        self._coordination_client = None

        # Shutdown cache client
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

    def get_coordination_client(self) -> "DragonflyClient":
        """Get the coordination client for locks, events, pub/sub.

        Returns:
            DragonflyClient configured for coordination (noeviction policy recommended)

        Raises:
            RuntimeError: If factory not initialized or coordination not available
        """
        if not self._initialized:
            raise RuntimeError("CacheFactory not initialized. Call initialize() first.")

        if not self._coordination_client:
            raise RuntimeError(
                "Coordination client not available. "
                "Dragonfly must be configured for coordination features (locks, events)."
            )

        return self._coordination_client

    def get_permission_cache(self) -> PermissionCacheProtocol:
        """Get permission cache instance.

        Returns cache implementation based on configuration:
        - DragonflyPermissionCache if Dragonfly is active
        - PostgresPermissionCache otherwise

        Returns:
            Permission cache instance

        Raises:
            RuntimeError: If factory not initialized
        """
        if not self._initialized:
            raise RuntimeError("CacheFactory not initialized. Call initialize() first.")

        if self._using_dragonfly and self._cache_client:
            from nexus.core.cache.dragonfly import DragonflyPermissionCache

            return DragonflyPermissionCache(
                client=self._cache_client,
                ttl=self._settings.permission_ttl,
                denial_ttl=self._settings.permission_denial_ttl,
            )

        # PostgreSQL fallback
        from nexus.core.cache.postgres import PostgresPermissionCache

        if not self._db_engine:
            raise RuntimeError("PostgreSQL cache requires db_engine but none was provided")
        return PostgresPermissionCache(
            engine=self._db_engine,
            ttl=self._settings.permission_ttl,
            denial_ttl=self._settings.permission_denial_ttl,
        )

    def get_tiger_cache(self) -> TigerCacheProtocol:
        """Get Tiger cache instance.

        Returns:
            Tiger cache instance

        Raises:
            RuntimeError: If factory not initialized
        """
        if not self._initialized:
            raise RuntimeError("CacheFactory not initialized. Call initialize() first.")

        if self._using_dragonfly and self._cache_client:
            from nexus.core.cache.dragonfly import DragonflyTigerCache

            return DragonflyTigerCache(
                client=self._cache_client,
                ttl=self._settings.tiger_ttl,
            )

        # PostgreSQL fallback
        from nexus.core.cache.postgres import PostgresTigerCache

        if not self._db_engine:
            raise RuntimeError("PostgreSQL cache requires db_engine but none was provided")
        return PostgresTigerCache(engine=self._db_engine)

    def get_resource_map_cache(self) -> ResourceMapCacheProtocol:
        """Get resource map cache instance.

        Returns:
            Resource map cache instance

        Raises:
            RuntimeError: If factory not initialized
        """
        if not self._initialized:
            raise RuntimeError("CacheFactory not initialized. Call initialize() first.")

        if self._using_dragonfly and self._cache_client:
            from nexus.core.cache.dragonfly import DragonflyResourceMapCache

            return DragonflyResourceMapCache(client=self._cache_client)

        # PostgreSQL fallback
        from nexus.core.cache.postgres import PostgresResourceMapCache

        if not self._db_engine:
            raise RuntimeError("PostgreSQL cache requires db_engine but none was provided")
        return PostgresResourceMapCache(engine=self._db_engine)

    def get_embedding_cache(self) -> DragonflyEmbeddingCache | None:
        """Get embedding cache instance (Issue #950).

        Returns embedding cache if Dragonfly is available, None otherwise.
        Embedding cache is only supported with Dragonfly backend (no PostgreSQL fallback).

        Returns:
            DragonflyEmbeddingCache instance if available, None otherwise

        Raises:
            RuntimeError: If factory not initialized
        """
        if not self._initialized:
            raise RuntimeError("CacheFactory not initialized. Call initialize() first.")

        if self._using_dragonfly and self._cache_client:
            return DragonflyEmbeddingCache(
                client=self._cache_client,
                ttl=self._settings.embedding_ttl,
            )

        # No PostgreSQL fallback for embedding cache - return None
        # Caller should fall back to direct API calls
        logger.debug("Embedding cache not available (requires Dragonfly backend)")
        return None

    async def health_check(self) -> dict:
        """Check health of all cache backends.

        Returns:
            Dict with health status for each component
        """
        result: dict = {
            "backend": self.backend_name,
            "initialized": self._initialized,
        }

        if self._using_dragonfly and self._cache_client:
            result["dragonfly_cache"] = await self._cache_client.health_check()
            result["dragonfly_cache_info"] = await self._cache_client.get_info()

            # Check coordination client (may be same as cache or separate)
            if self._coordination_client:
                is_separate = self._coordination_client is not self._cache_client
                result["dragonfly_coordination"] = await self._coordination_client.health_check()
                result["dragonfly_coordination_separate"] = is_separate
                if is_separate:
                    result[
                        "dragonfly_coordination_info"
                    ] = await self._coordination_client.get_info()
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
    """Initialize the global cache factory.

    Called during application startup.

    Args:
        settings: Cache configuration
        db_engine: SQLAlchemy engine for PostgreSQL backend

    Returns:
        Initialized CacheFactory instance
    """
    global _cache_factory
    _cache_factory = CacheFactory(settings, db_engine)
    await _cache_factory.initialize()
    return _cache_factory


async def shutdown_cache_factory() -> None:
    """Shutdown the global cache factory.

    Called during application shutdown.
    """
    global _cache_factory
    if _cache_factory:
        await _cache_factory.shutdown()
        _cache_factory = None


def get_cache_factory() -> CacheFactory:
    """Get the global cache factory instance.

    For use in FastAPI dependency injection.

    Returns:
        CacheFactory instance

    Raises:
        RuntimeError: If factory not initialized
    """
    if not _cache_factory:
        raise RuntimeError("Cache factory not initialized. Call init_cache_factory() first.")
    return _cache_factory
