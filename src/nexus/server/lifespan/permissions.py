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
    from nexus.storage.models import ZoneModel

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

    _seed_root_zone(svc)  # Issue #3897 — satisfy api_key_zones FK on first key insert
    await _startup_async_rebac(app, svc)
    await _startup_cache_brick(app, svc)
    await _startup_durable_invalidation(app, svc)  # Issue #3396
    bg_tasks.extend(await _startup_tiger_cache(app, svc))
    bg_tasks.extend(_startup_backfill(app, svc))
    _startup_cache_warmup(app, svc)
    await _startup_circuit_breaker(app, svc)

    return bg_tasks


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _seed_root_zone(svc: "LifespanServices") -> None:
    """Ensure ``zones.root`` exists so api_key_zones FK inserts succeed.

    Defense in depth: the alembic migration for ``api_key_zones`` already
    seeds the row. This re-asserts the invariant on installs that bypass
    Alembic (``Base.metadata.create_all`` only) and on schemas that may
    have lost the row through manual intervention.

    Fails closed: any inability to confirm the row is present is fatal,
    because every later ``create_api_key`` call will otherwise hit
    FK ``api_key_zones_zone_id_fkey``. Concurrent-startup races (two
    processes inserting the row at once) are tolerated — the conflicting
    insert is treated as success once the row is observable on re-read.
    """
    session_factory = svc.session_factory
    if session_factory is None:
        return

    from datetime import UTC, datetime

    from sqlalchemy.exc import IntegrityError

    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.storage.models import ZoneModel

    with session_factory() as session:
        existing = session.get(ZoneModel, ROOT_ZONE_ID)
        if existing is not None:
            _assert_root_zone_active(existing)
            return
        session.add(
            ZoneModel(
                zone_id=ROOT_ZONE_ID,
                name="Root",
                phase="Active",
                finalizers="[]",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        try:
            session.commit()
            logger.info("Seeded default zone %r", ROOT_ZONE_ID)
            return
        except IntegrityError:
            session.rollback()

    # Concurrent insert raced us. Confirm the row is present and Active in
    # a fresh session — if it isn't, the original error wasn't a benign
    # race and we must fail closed.
    with session_factory() as session:
        racer = session.get(ZoneModel, ROOT_ZONE_ID)
        if racer is None:
            raise RuntimeError(
                f"failed to seed default zone {ROOT_ZONE_ID!r}: "
                "IntegrityError on insert and row not visible on re-read"
            )
        _assert_root_zone_active(racer)


def _assert_root_zone_active(zone: "ZoneModel") -> None:
    """Refuse to start when zones.root exists but isn't Active.

    Auth rejects keys whose zone is not Active (or is soft-deleted), so
    accepting a Terminating/Terminated/deleted root row would let startup
    look healthy while every default agent registration / root-token call
    still failed at request time. Fail closed with an actionable error.
    """
    if zone.phase != "Active" or zone.deleted_at is not None:
        raise RuntimeError(
            f"default zone {zone.zone_id!r} is not usable: "
            f"phase={zone.phase!r} deleted_at={zone.deleted_at!r}. "
            "Restore it to Active (and clear deleted_at) before starting."
        )


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
            nx = svc
            if hasattr(nx, "sys_setattr"):
                nx.sys_setattr(
                    "/__sys__/services/async_rebac_manager",
                    service=app.state.async_rebac_manager,
                )

    except Exception as e:
        logger.warning("Failed to initialize async ReBAC manager: %s", e, exc_info=True)


async def _startup_cache_brick(app: "FastAPI", svc: "LifespanServices") -> None:
    """Initialize cache for Dragonfly/Redis or NullCacheStore fallback (Issue #1075, #1251, #1524).

    Prefers CacheBrick from ServiceRegistry (injected by factory). Falls back to
    creating a CacheBrick from environment settings if not available.
    """
    try:
        cache_brick = app.state.cache_brick
        if cache_brick is None and svc.nexus_fs is not None:
            _svc_fn = getattr(svc.nexus_fs, "service", None)
            cache_brick = _svc_fn("cache_brick") if _svc_fn else None

        from nexus.cache.settings import CacheSettings

        cache_settings = CacheSettings.from_env()
        needs_env_cache_brick = cache_brick is None
        if cache_brick is not None and not cache_brick.has_cache_store:
            needs_env_cache_brick = cache_settings.should_use_dragonfly()

        if needs_env_cache_brick:
            # Fallback: create CacheBrick from env settings (standalone mode)
            from nexus.cache.brick import CacheBrick
            from nexus.cache.dragonfly import DragonflyCacheStore, DragonflyClient

            record_store = svc.record_store
            cache_store = getattr(svc.nexus_fs, "_cache_store", None)
            if cache_settings.should_use_dragonfly() and cache_settings.dragonfly_url:
                client = DragonflyClient(
                    url=cache_settings.dragonfly_url,
                    pool_size=cache_settings.dragonfly_pool_size,
                    timeout=cache_settings.dragonfly_timeout,
                    connect_timeout=cache_settings.dragonfly_connect_timeout,
                    pool_timeout=cache_settings.dragonfly_pool_timeout,
                    socket_keepalive=cache_settings.dragonfly_keepalive,
                    retry_on_timeout=cache_settings.dragonfly_retry_on_timeout,
                )
                await client.connect()
                cache_store = DragonflyCacheStore(client)
            cache_brick = CacheBrick(
                cache_store=cache_store,
                settings=cache_settings,
                record_store=record_store,
            )

        # If NexusFS is bootstrapped, kernel handles start; otherwise start manually
        if not getattr(svc, "_bootstrapped", False):
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


async def _startup_durable_invalidation(app: "FastAPI", svc: "LifespanServices") -> None:
    """Initialize cross-zone durable invalidation stream and read fence (Issue #3396).

    Creates DurableInvalidationStream + ReadFence backed by the CacheBrick's
    Dragonfly client, wires them into the ReBACManager's CacheCoordinator,
    and registers a consumer-side handler that triggers local cache invalidation
    when cross-zone events arrive.

    Must be called AFTER _startup_cache_brick (needs Dragonfly client) and
    AFTER _startup_async_rebac (needs ReBACManager).
    """
    try:
        cache_brick = getattr(app.state, "cache_brick", None)
        if cache_brick is None or not cache_brick.has_cache_store:
            logger.info("[DurableStream] No cache store — durable invalidation disabled")
            return

        # Try multiple paths to find the ReBACManager.
        # In some profiles (cluster), it lives inside a ReBACService wrapper
        # registered via the factory, not as a direct named service.
        rebac = svc.rebac_manager
        if rebac is None and svc.nexus_fs is not None:
            # Direct attribute on NexusFS
            rebac = getattr(svc.nexus_fs, "_rebac_manager", None)
        if rebac is None and svc.nexus_fs is not None:
            # Inside ReBACService wrapper (registered as "rebac" in cluster profile).
            # service_lookup returns raw instance — attribute access is direct.
            _svc_fn = getattr(svc.nexus_fs, "service", None)
            if _svc_fn:
                for svc_name in ("rebac", "rebac_service", "rebac_manager"):
                    rebac_svc = _svc_fn(svc_name)
                    if rebac_svc is not None:
                        rebac = getattr(rebac_svc, "_rebac_manager", None)
                        if rebac is not None:
                            logger.debug(
                                "[DurableStream] Found ReBACManager via service '%s'", svc_name
                            )
                            break
        if rebac is None:
            # From AsyncReBACManager on app.state
            async_rebac = getattr(app.state, "async_rebac_manager", None)
            if async_rebac is not None:
                rebac = getattr(async_rebac, "_sync_manager", None)
        if rebac is None:
            logger.info("[DurableStream] No ReBACManager — durable invalidation disabled")
            return

        # Get a raw redis.asyncio.Redis client for Streams API.
        # CacheBrick wraps Dragonfly in a DragonflyClient that exposes only
        # high-level methods (get/set/publish).  We need the raw Redis client
        # for XADD/XREADGROUP/XACK.  Create one from the existing connection pool.
        cache_store = cache_brick.cache_store
        dragonfly_client = getattr(cache_store, "_client", None)
        connection_pool = getattr(dragonfly_client, "_pool", None)
        if connection_pool is None:
            logger.debug("[DurableStream] No connection pool on cache store — skipping")
            return

        import redis.asyncio as aioredis
        from redis.asyncio.retry import Retry
        from redis.backoff import ExponentialBackoff
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import TimeoutError as RedisTimeoutError

        # Mirror the retry/backoff policy the sibling DragonflyClient configures
        # on its own wrapped client (src/nexus/cache/dragonfly.py).  Without this
        # a transient startup-race timeout against dragonfly bubbles up as a
        # fatal error during server lifespan init instead of retrying.
        redis_client = aioredis.Redis(
            connection_pool=connection_pool,
            retry=Retry(ExponentialBackoff(), retries=3),
            retry_on_error=[RedisConnectionError, RedisTimeoutError],
        )

        from nexus.bricks.rebac.cache.durable_stream import DurableInvalidationStream
        from nexus.bricks.rebac.cache.read_fence import ReadFence
        from nexus.contracts.constants import ROOT_ZONE_ID

        zone_id = getattr(svc.nexus_fs, "_zone_id", ROOT_ZONE_ID) if svc.nexus_fs else ROOT_ZONE_ID

        read_fence = ReadFence()
        durable_stream = DurableInvalidationStream(
            redis_client=redis_client,
            zone_id=zone_id,
            read_fence=read_fence,
        )

        # Register consumer-side handler: incoming cross-zone events trigger
        # local cache invalidation via the CacheCoordinator.
        coordinator = getattr(rebac, "_cache_coordinator", None)
        if coordinator is not None:

            async def _on_cross_zone_invalidation(source_zone: str, payload: dict) -> None:
                """Handle incoming durable stream event by invalidating local caches.

                Uses local_only=True to prevent re-broadcasting: the invalidation
                was already published by the originating zone — this zone only needs
                to invalidate its own caches, not relay to other zones.
                """
                coordinator.invalidate_for_write(
                    zone_id=source_zone,
                    subject=(payload.get("subject_type", ""), payload.get("subject_id", "")),
                    relation=payload.get("relation", ""),
                    object=(payload.get("object_type", ""), payload.get("object_id", "")),
                    local_only=True,
                )

            durable_stream.register_handler("local-cache-invalidate", _on_cross_zone_invalidation)

            # Wire into coordinator
            coordinator.set_durable_stream(durable_stream)
            coordinator.set_read_fence(read_fence)

            # Wire read fence into L1 cache (for staleness detection on read path)
            l1_cache = getattr(rebac, "_l1_cache", None)
            if l1_cache is not None:
                l1_cache._read_fence = read_fence

        # Wire DistributedLeaseManager for cross-zone lease coordination (Issue #3396)
        try:
            from nexus.lib.distributed_lease import DistributedLeaseManager

            distributed_lease_mgr = DistributedLeaseManager(
                redis_client=redis_client,
                zone_id=zone_id,
            )

            # Register as a lease invalidator: on permission mutation,
            # force-revoke all distributed leases for the affected zone.
            if coordinator is not None:
                _dlm = distributed_lease_mgr  # capture for closure

                def _distributed_lease_invalidate(affected_zone_id: str) -> None:
                    """Zone-wide distributed lease revocation on permission change.

                    This runs in sync context (from invalidate_for_write).
                    Schedules the async revocation as fire-and-forget on the
                    running event loop. If no loop is running (e.g. during
                    tests), the revocation is skipped — TTL expiry is the
                    safety net.
                    """
                    import asyncio

                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        return  # No event loop — skip (TTL fallback)

                    async def _revoke() -> None:
                        try:
                            # Scan and revoke all leases in this zone
                            pattern = f"lease:{affected_zone_id}:*"
                            async for key in _dlm._client.scan_iter(match=pattern, count=100):
                                await _dlm._client.delete(key)
                        except Exception:
                            logger.debug(
                                "[DistributedLease] Zone-wide revoke failed for %s",
                                affected_zone_id,
                                exc_info=True,
                            )

                    loop.create_task(_revoke(), name=f"dlm-revoke-{affected_zone_id}")

                coordinator.register_lease_invalidator(
                    "distributed_lease", _distributed_lease_invalidate
                )

            app.state.distributed_lease_manager = distributed_lease_mgr
        except Exception as e:
            logger.debug("[DurableStream] DistributedLeaseManager init skipped: %s", e)

        # Start background drain + consumer tasks
        await durable_stream.start()

        # Store on app.state for health checks and shutdown
        app.state.durable_stream = durable_stream
        app.state.read_fence = read_fence

        logger.info(
            "[DurableStream] Cross-zone durable invalidation started for zone %s",
            zone_id,
        )
    except Exception as e:
        logger.warning(
            "[DurableStream] Failed to initialize durable invalidation: %s",
            e,
            exc_info=True,
        )


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

                    # Q3 BackgroundService — kernel auto-calls start()
                    nx = svc.nexus_fs if hasattr(svc, "nexus_fs") else svc
                    if hasattr(nx, "sys_setattr"):
                        nx.sys_setattr(
                            "/__sys__/services/directory_grant_expander",
                            service=expander,
                        )
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
    """Wire circuit breaker and manifest resolver onto app.state from ServiceRegistry."""
    if svc.nexus_fs:
        _svc_fn = getattr(svc.nexus_fs, "service", None)
        app.state.rebac_circuit_breaker = _svc_fn("rebac_circuit_breaker") if _svc_fn else None
        app.state.manifest_resolver = _svc_fn("manifest_resolver") if _svc_fn else None
