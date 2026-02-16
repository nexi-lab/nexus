"""Permissions startup: async ReBAC, AsyncNexusFS, cache factory, Tiger Cache.

Extracted from fastapi_server.py (#1602).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def startup_permissions(app: FastAPI) -> list[asyncio.Task]:
    """Initialize permission infrastructure and return background tasks.

    Covers:
    - Async ReBAC manager + AsyncNexusFS (Issue #940)
    - Cache factory + Tiger Cache L2 wiring (Issue #1075, #1106)
    - Tiger Cache queue processor + warm-up (Issue #935, #979)
    - DirectoryGrantExpander worker
    - Sparse directory index backfill
    - File cache warmup (Issue #1076)
    - Circuit breaker wiring (Issue #726)
    """
    bg_tasks: list[asyncio.Task] = []

    await _startup_async_rebac(app)
    await _startup_cache_factory(app)
    bg_tasks.extend(_startup_tiger_cache(app))
    bg_tasks.extend(_startup_backfill(app))
    _startup_cache_warmup(app)
    _startup_circuit_breaker(app)

    return bg_tasks


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _startup_async_rebac(app: FastAPI) -> None:
    """Initialize async ReBAC manager and AsyncNexusFS."""
    if not app.state.database_url:
        return

    try:
        from nexus.services.permissions.async_rebac_manager import (
            AsyncReBACManager,
            create_async_engine_from_url,
        )

        engine = create_async_engine_from_url(app.state.database_url)
        app.state.async_rebac_manager = AsyncReBACManager(engine)
        logger.info("Async ReBAC manager initialized")

        # Issue #940: Initialize AsyncNexusFS with permission enforcement
        try:
            from nexus.core.async_nexus_fs import AsyncNexusFS
            from nexus.services.permissions.async_permissions import AsyncPermissionEnforcer

            backend_root = os.getenv("NEXUS_BACKEND_ROOT", ".nexus-data/backend")
            tenant_id = os.getenv("NEXUS_TENANT_ID", "default")
            enforce_permissions = os.getenv("NEXUS_ENFORCE_PERMISSIONS", "true").lower() in (
                "true",
                "1",
                "yes",
            )

            # Issue #1239: Create namespace manager for per-subject visibility
            # Issue #1265: Factory function handles L3 persistent store wiring
            namespace_manager = None
            if enforce_permissions and hasattr(app.state, "nexus_fs"):
                sync_rebac = getattr(app.state.nexus_fs, "_rebac_manager", None)
                if sync_rebac:
                    from nexus.services.permissions.namespace_factory import (
                        create_namespace_manager,
                    )

                    ns_record_store = getattr(app.state.nexus_fs, "_record_store", None)
                    namespace_manager = create_namespace_manager(
                        rebac_manager=sync_rebac,
                        record_store=ns_record_store,
                    )
                    # Wire event-driven invalidation: rebac_write → namespace cache (Issue #1244)
                    sync_rebac.register_namespace_invalidator(
                        "namespace_dcache",
                        lambda st, sid, _zid: namespace_manager.invalidate((st, sid)),
                    )
                    logger.info(
                        "[NAMESPACE] NamespaceManager initialized for AsyncPermissionEnforcer "
                        "(using sync rebac_manager, L3=%s, event-driven invalidation=enabled)",
                        "enabled" if ns_record_store else "disabled",
                    )

            # Create permission enforcer with async ReBAC
            permission_enforcer = AsyncPermissionEnforcer(
                rebac_manager=app.state.async_rebac_manager,
                namespace_manager=namespace_manager,
                agent_registry=getattr(app.state, "agent_registry", None),
            )

            # Create AsyncNexusFS using the same RaftMetadataStore as sync NexusFS
            app.state.async_nexus_fs = AsyncNexusFS(
                backend_root=backend_root,
                metadata_store=app.state.nexus_fs.metadata,
                tenant_id=tenant_id,
                enforce_permissions=enforce_permissions,
                permission_enforcer=permission_enforcer,
            )
            await app.state.async_nexus_fs.initialize()
            logger.info(
                f"AsyncNexusFS initialized (backend={backend_root}, "
                f"tenant={tenant_id}, enforce_permissions={enforce_permissions})"
            )
        except Exception as e:
            logger.warning(f"Failed to initialize AsyncNexusFS: {e}")

    except Exception as e:
        logger.warning(f"Failed to initialize async ReBAC manager: {e}")


async def _startup_cache_factory(app: FastAPI) -> None:
    """Initialize cache factory for Dragonfly/Redis or PostgreSQL fallback (Issue #1075, #1251)."""
    try:
        from nexus.cache.factory import init_cache_factory
        from nexus.cache.settings import CacheSettings

        cache_settings = CacheSettings.from_env()

        # Pass RecordStore for SQL-backed cache fallback
        record_store = getattr(app.state.nexus_fs, "_record_store", None)

        app.state.cache_factory = await init_cache_factory(
            cache_settings, record_store=record_store
        )
        logger.info(
            f"Cache factory initialized with {app.state.cache_factory.backend_name} backend"
        )

        # Wire up CacheStoreABC L2 cache to TigerCache (Issue #1106)
        if app.state.cache_factory.has_cache_store:
            tiger_cache = getattr(
                getattr(app.state.nexus_fs, "_rebac_manager", None),
                "_tiger_cache",
                None,
            )
            if tiger_cache:
                dragonfly_tiger = app.state.cache_factory.get_tiger_cache()
                tiger_cache.set_dragonfly_cache(dragonfly_tiger)
                logger.info(
                    "[TIGER] Dragonfly L2 cache wired up - "
                    "L1 (memory) -> L2 (Dragonfly) -> L3 (PostgreSQL)"
                )
    except Exception as e:
        logger.warning(f"Failed to initialize cache factory: {e}")


def _startup_tiger_cache(app: FastAPI) -> list[asyncio.Task]:
    """Start Tiger Cache worker, warm-up, and DirectoryGrantExpander."""
    bg_tasks: list[asyncio.Task] = []

    # Tiger Cache queue processor (Issue #935)
    if app.state.nexus_fs and os.getenv("NEXUS_ENABLE_TIGER_WORKER", "false").lower() in (
        "true",
        "1",
        "yes",
    ):
        try:
            from nexus.server.background_tasks import tiger_cache_queue_task

            task = asyncio.create_task(
                tiger_cache_queue_task(app.state.nexus_fs, interval_seconds=60, batch_size=1)
            )
            bg_tasks.append(task)
            logger.info("Tiger Cache queue processor started (explicit enable)")
        except Exception as e:
            logger.warning(f"Failed to start Tiger Cache queue processor: {e}")
    else:
        logger.debug("Tiger Cache queue processor disabled (write-through handles grants)")

    # Tiger Cache warm-up on startup (Issue #979)
    if app.state.nexus_fs:
        try:
            tiger_cache = getattr(app.state.nexus_fs._rebac_manager, "_tiger_cache", None)
            if tiger_cache:
                warm_limit = int(os.getenv("NEXUS_TIGER_CACHE_WARM_LIMIT", "500"))

                async def _warm_tiger_cache() -> None:
                    loaded = await asyncio.to_thread(tiger_cache.warm_from_db, warm_limit)
                    logger.info(f"Tiger Cache warmed with {loaded} entries from database")

                warm_task = asyncio.create_task(_warm_tiger_cache())
                bg_tasks.append(warm_task)
                logger.debug(f"Tiger Cache warm-up started (limit={warm_limit})")

                # Start DirectoryGrantExpander worker
                try:
                    from nexus.services.permissions.tiger_cache import DirectoryGrantExpander

                    expander = DirectoryGrantExpander(
                        engine=app.state.nexus_fs._rebac_manager.engine,
                        tiger_cache=tiger_cache,
                        metadata_store=app.state.nexus_fs.metadata,
                    )
                    app.state.directory_grant_expander = expander

                    async def _run_grant_expander() -> None:
                        await expander.run_worker()

                    bg_tasks.append(asyncio.create_task(_run_grant_expander()))
                    logger.info("DirectoryGrantExpander worker started for large folder grants")
                except Exception as e:
                    logger.debug(f"DirectoryGrantExpander startup skipped: {e}")

        except Exception as e:
            logger.debug(f"Tiger Cache warm-up skipped: {e}")

    return bg_tasks


def _startup_backfill(app: FastAPI) -> list[asyncio.Task]:
    """Auto-backfill sparse directory index for system paths (Issue #perf19)."""
    bg_tasks: list[asyncio.Task] = []

    if app.state.nexus_fs and hasattr(app.state.nexus_fs, "metadata"):
        try:
            _nexus_fs = app.state.nexus_fs  # Capture for closure

            async def _backfill_system_paths() -> None:
                for prefix in ["/skills", "/sessions"]:
                    try:
                        created = await asyncio.to_thread(
                            _nexus_fs.metadata.backfill_directory_index,
                            prefix=prefix,
                            zone_id=None,
                        )
                        if created > 0:
                            logger.info(f"Sparse index backfill: {created} entries for {prefix}")
                    except Exception as e:
                        logger.debug(f"Sparse index backfill skipped for {prefix}: {e}")

            bg_tasks.append(asyncio.create_task(_backfill_system_paths()))
            logger.info("Sparse directory index backfill started for system paths")
        except Exception as e:
            logger.warning(f"Sparse index backfill skipped: {e}")

    return bg_tasks


def _startup_cache_warmup(app: FastAPI) -> None:
    """File cache warmup on server startup (Issue #1076)."""
    if not app.state.nexus_fs:
        return

    try:
        warmup_max_files = int(os.getenv("NEXUS_CACHE_WARMUP_MAX_FILES", "1000"))
        warmup_depth = int(os.getenv("NEXUS_CACHE_WARMUP_DEPTH", "2"))
        _nexus_fs_warmup = app.state.nexus_fs  # Capture for closure

        async def _warmup_file_cache() -> None:
            from nexus.cache.warmer import CacheWarmer, WarmupConfig

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
                f"[WARMUP] Server startup warmup complete: "
                f"{stats.files_warmed} files, {stats.metadata_warmed} metadata entries"
            )

        asyncio.create_task(_warmup_file_cache())
        logger.info(
            f"[WARMUP] Server startup warmup started "
            f"(max_files={warmup_max_files}, depth={warmup_depth})"
        )
    except Exception as e:
        logger.debug(f"[WARMUP] Server startup warmup skipped: {e}")


def _startup_circuit_breaker(app: FastAPI) -> None:
    """Wire circuit breaker from factory for health endpoint access (Issue #726)."""
    if app.state.nexus_fs:
        app.state.rebac_circuit_breaker = app.state.nexus_fs._service_extras.get(
            "rebac_circuit_breaker"
        )
