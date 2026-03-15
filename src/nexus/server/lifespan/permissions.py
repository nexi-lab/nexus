"""Permissions startup: async ReBAC, cache factory, Tiger Cache.

Extracted from fastapi_server.py (#1602).
"""

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from nexus.server.lifespan.services_container import LifespanServices

logger = logging.getLogger(__name__)


async def startup_permissions(app: "FastAPI", svc: "LifespanServices") -> list[asyncio.Task]:
    """Initialize permission infrastructure and return background tasks.

    Covers:
    - ReBAC manager + NexusFS (Issue #940)
    - Cache factory + Tiger Cache L2 wiring (Issue #1075, #1106)
    - Tiger Cache queue processor + warm-up (Issue #935, #979)
    - DirectoryGrantExpander worker
    - Sparse directory index backfill
    - File cache warmup (Issue #1076)
    - Circuit breaker wiring (Issue #726)
    """
    bg_tasks: list[asyncio.Task] = []

    await _startup_async_rebac(app, svc)
    await _startup_cache_brick(app, svc)
    bg_tasks.extend(await _startup_tiger_cache(app, svc))
    bg_tasks.extend(_startup_backfill(app, svc))
    _startup_cache_warmup(app, svc)
    await _startup_circuit_breaker(app, svc)

    return bg_tasks


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _startup_async_rebac(app: "FastAPI", svc: "LifespanServices") -> None:
    """Initialize async ReBAC manager."""
    if not svc.database_url:
        return

    try:
        from nexus.bricks.rebac.manager import AsyncReBACManager

        # Reuse the sync ReBACManager from NexusFS (avoids creating standalone engine)
        sync_rebac = svc.rebac_manager
        if sync_rebac:
            app.state.async_rebac_manager = AsyncReBACManager(sync_rebac)
            logger.info("Async ReBAC manager initialized (wrapping sync manager)")
        else:
            # Fallback: create fresh sync manager using RecordStore engine
            from nexus.bricks.rebac.manager import ReBACManager
            from nexus.storage.record_store import SQLAlchemyRecordStore

            _store = SQLAlchemyRecordStore(db_url=svc.database_url)
            _sync_mgr = ReBACManager(engine=_store.engine)
            app.state.async_rebac_manager = AsyncReBACManager(_sync_mgr)
            logger.info("Async ReBAC manager initialized (fresh sync manager via RecordStore)")

        # Enlist with coordinator (Q1 — wrapper is the consumer-facing service)
        if app.state.async_rebac_manager is not None:
            coord = svc.service_coordinator
            if coord is not None:
                await coord.enlist("async_rebac_manager", app.state.async_rebac_manager)

    except Exception as e:
        logger.warning("Failed to initialize async ReBAC manager: %s", e, exc_info=True)


async def _startup_cache_brick(app: "FastAPI", svc: "LifespanServices") -> None:
    """Initialize cache for Dragonfly/Redis or NullCacheStore fallback (Issue #1075, #1251, #1524).

    Prefers CacheBrick from BrickServices (injected by factory). Falls back to
    creating a CacheBrick from environment settings if not available.
    """
    try:
        # Prefer CacheBrick already in BrickServices (set by create_nexus_fs → lifespan)
        cache_brick = app.state.cache_brick
        if cache_brick is None:
            brk = svc.brick_services
            cache_brick = getattr(brk, "cache_brick", None) if brk else None

        if cache_brick is None:
            # Fallback: create CacheBrick from env settings (standalone mode)
            from nexus.cache.brick import CacheBrick
            from nexus.cache.settings import CacheSettings

            cache_settings = CacheSettings.from_env()
            record_store = svc.record_store
            cache_store = getattr(svc.nexus_fs, "_cache_store", None)
            cache_brick = CacheBrick(
                cache_store=cache_store,
                settings=cache_settings,
                record_store=record_store,
            )

        coord = svc.service_coordinator
        if coord is not None:
            await coord.enlist("cache_brick", cache_brick)
        else:
            await cache_brick.start()
        app.state.cache_brick = cache_brick
        logger.info("CacheBrick initialized with %s backend", cache_brick.backend_name)

        # Wire up CacheStoreABC L2 cache to TigerCache (Issue #1106)
        if cache_brick.has_cache_store:
            rebac = svc.rebac_manager
            tiger_cache = getattr(rebac, "_tiger_cache", None) if rebac is not None else None
            if tiger_cache:
                dragonfly_tiger = cache_brick.get_tiger_cache()
                tiger_cache.set_dragonfly_cache(dragonfly_tiger)
                logger.info(
                    "[TIGER] Dragonfly L2 cache wired up - "
                    "L1 (memory) -> L2 (CacheBrick) -> L3 (PostgreSQL)"
                )
    except Exception as e:
        logger.warning("Failed to initialize cache: %s", e, exc_info=True)


async def _startup_tiger_cache(app: "FastAPI", svc: "LifespanServices") -> list[asyncio.Task]:
    """Start Tiger Cache worker, warm-up, and DirectoryGrantExpander."""
    bg_tasks: list[asyncio.Task] = []

    # Tiger Cache queue processor (Issue #935)
    if svc.nexus_fs and os.getenv("NEXUS_ENABLE_TIGER_WORKER", "false").lower() in (
        "true",
        "1",
        "yes",
    ):
        try:
            from nexus.server.background_tasks import tiger_cache_queue_task

            _rebac_mgr = svc.rebac_manager
            if _rebac_mgr is not None:
                task = asyncio.create_task(
                    tiger_cache_queue_task(_rebac_mgr, interval_seconds=60, batch_size=1)
                )
                bg_tasks.append(task)
                logger.info("Tiger Cache queue processor started (explicit enable)")
            else:
                logger.debug("Tiger Cache queue processor skipped: no rebac_manager")
        except Exception as e:
            logger.warning("Failed to start Tiger Cache queue processor: %s", e, exc_info=True)
    else:
        logger.debug("Tiger Cache queue processor disabled (write-through handles grants)")

    # Tiger Cache warm-up on startup (Issue #979)
    if svc.nexus_fs:
        try:
            _rebac = svc.rebac_manager
            tiger_cache = getattr(_rebac, "_tiger_cache", None) if _rebac is not None else None
            if tiger_cache:
                warm_limit = int(os.getenv("NEXUS_TIGER_CACHE_WARM_LIMIT", "500"))

                async def _warm_tiger_cache() -> None:
                    loaded = await asyncio.to_thread(tiger_cache.warm_from_db, warm_limit)
                    logger.info("Tiger Cache warmed with %d entries from database", loaded)

                warm_task = asyncio.create_task(_warm_tiger_cache())
                bg_tasks.append(warm_task)
                logger.debug("Tiger Cache warm-up started (limit=%d)", warm_limit)

                # Start DirectoryGrantExpander worker
                try:
                    from typing import cast

                    from sqlalchemy.engine import Engine

                    from nexus.bricks.rebac.cache.tiger.expander import (
                        DirectoryGrantExpander,
                    )

                    _rebac_engine = cast(Engine, getattr(_rebac, "engine", None))
                    expander = DirectoryGrantExpander(
                        engine=_rebac_engine,
                        tiger_cache=tiger_cache,
                        metadata_store=svc.nexus_fs.metadata,
                    )
                    app.state.directory_grant_expander = expander

                    # Q3 PersistentService — coordinator auto-calls start()
                    coord = svc.service_coordinator
                    if coord is not None:
                        await coord.enlist("directory_grant_expander", expander)
                    else:
                        await expander.start()
                    logger.info("DirectoryGrantExpander worker started for large folder grants")
                except Exception as e:
                    logger.debug("DirectoryGrantExpander startup skipped: %s", e)

        except Exception as e:
            logger.debug("Tiger Cache warm-up skipped: %s", e)

    return bg_tasks


def _startup_backfill(_app: "FastAPI", svc: "LifespanServices") -> list[asyncio.Task]:
    """Auto-backfill sparse directory index for system paths (Issue #perf19)."""
    bg_tasks: list[asyncio.Task] = []

    if svc.nexus_fs and hasattr(svc.nexus_fs, "metadata"):
        try:
            _nexus_fs = svc.nexus_fs  # Capture for closure

            async def _backfill_system_paths() -> None:
                for prefix in ["/sessions"]:
                    try:
                        created = await asyncio.to_thread(
                            _nexus_fs.metadata.backfill_directory_index,
                            prefix=prefix,
                            zone_id=None,
                        )
                        if created > 0:
                            logger.info("Sparse index backfill: %d entries for %s", created, prefix)
                    except Exception as e:
                        logger.debug("Sparse index backfill skipped for %s: %s", prefix, e)

            bg_tasks.append(asyncio.create_task(_backfill_system_paths()))
            logger.info("Sparse directory index backfill started for system paths")
        except Exception as e:
            logger.warning("Sparse index backfill skipped: %s", e)

    return bg_tasks


def _startup_cache_warmup(_app: "FastAPI", svc: "LifespanServices") -> None:
    """File cache warmup on server startup (Issue #1076)."""
    if not svc.nexus_fs:
        return

    try:
        warmup_max_files = int(os.getenv("NEXUS_CACHE_WARMUP_MAX_FILES", "1000"))
        warmup_depth = int(os.getenv("NEXUS_CACHE_WARMUP_DEPTH", "2"))
        _nexus_fs_warmup = svc.nexus_fs  # Capture for closure

        async def _warmup_file_cache() -> None:
            from nexus.server.cache_warmer import CacheWarmer, WarmupConfig

            config = WarmupConfig(
                max_files=warmup_max_files,
                depth=warmup_depth,
                include_content=False,
            )
            warmer = CacheWarmer(nexus_fs=_nexus_fs_warmup, config=config)
            stats = await warmer.warmup_directory(
                path="/",
                depth=warmup_depth,
                include_content=False,
                max_files=warmup_max_files,
            )
            logger.info(
                "[WARMUP] Server startup warmup complete: %d files, %d metadata entries",
                stats.files_warmed,
                stats.metadata_warmed,
            )

        asyncio.create_task(_warmup_file_cache())
        logger.info(
            "[WARMUP] Server startup warmup started (max_files=%d, depth=%d)",
            warmup_max_files,
            warmup_depth,
        )
    except Exception as e:
        logger.debug("[WARMUP] Server startup warmup skipped: %s", e)


async def _startup_circuit_breaker(app: "FastAPI", svc: "LifespanServices") -> None:
    """Wire circuit breaker and manifest resolver from factory (Issue #726, #2130)."""
    if svc.nexus_fs:
        brk = svc.brick_services
        app.state.rebac_circuit_breaker = getattr(brk, "rebac_circuit_breaker", None)
        app.state.manifest_resolver = getattr(brk, "manifest_resolver", None) if brk else None
        # Enlist Q1 — static, no lifecycle
        coord = svc.service_coordinator
        if coord is not None:
            if app.state.rebac_circuit_breaker is not None:
                await coord.enlist("rebac_circuit_breaker", app.state.rebac_circuit_breaker)
            if app.state.manifest_resolver is not None:
                await coord.enlist("manifest_resolver", app.state.manifest_resolver)
