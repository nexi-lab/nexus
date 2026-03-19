"""Background tasks for Nexus server (v0.5.0).

Provides background cleanup tasks for session management and expired resources.
"""

import asyncio
import contextlib
import logging
from datetime import timedelta
from typing import Any

from nexus.storage.version_gc import VersionGCSettings, VersionHistoryGC
from nexus.system_services.lifecycle.sessions import (
    cleanup_expired_sessions,
    cleanup_inactive_sessions,
)

logger = logging.getLogger(__name__)


async def sandbox_cleanup_task(sandbox_manager: Any, interval_seconds: int = 300) -> None:
    """Background task: Clean up expired sandboxes (Issue #372).

    Runs periodically to stop and destroy sandboxes that have exceeded their TTL.

    Args:
        sandbox_manager: SandboxManager instance
        interval_seconds: How often to run cleanup (default: 300 = 5 minutes)

    Examples:
        >>> # Start cleanup task in server
        >>> from nexus.bricks.sandbox import SandboxManager
        >>> mgr = SandboxManager(db_session, e2b_api_key="...")
        >>> asyncio.create_task(sandbox_cleanup_task(mgr, 300))
    """
    logger.info(f"Starting sandbox cleanup task (interval: {interval_seconds}s)")

    while True:
        try:
            count = await sandbox_manager.cleanup_expired_sandboxes()

            if count > 0:
                logger.info(f"Cleaned up {count} expired sandboxes")

        except Exception as e:
            logger.error(f"Sandbox cleanup failed: {e}", exc_info=True)

        await asyncio.sleep(interval_seconds)


async def session_cleanup_task(
    cache_session_store: Any,
    session_factory: Any,
    interval_seconds: int = 3600,
) -> None:
    """Background task: Clean up expired sessions.

    Runs periodically to delete expired sessions (CacheStore) and their
    associated resources (PathRegistration, Memory in RecordStore).

    Args:
        cache_session_store: CacheSessionStore instance
        session_factory: SQLAlchemy session factory (for resource cleanup)
        interval_seconds: How often to run cleanup (default: 3600 = 1 hour)
    """
    logger.info(f"Starting session cleanup task (interval: {interval_seconds}s)")

    while True:
        try:
            with session_factory() as db:
                result = await cleanup_expired_sessions(cache_session_store, db)
                db.commit()

                sessions_count = result["sessions"]
                if isinstance(sessions_count, int) and sessions_count > 0:
                    logger.info(
                        f"Cleaned up {sessions_count} expired sessions, "
                        f"{result['resources']} resources"
                    )

        except Exception as e:
            logger.error(f"Session cleanup failed: {e}", exc_info=True)

        await asyncio.sleep(interval_seconds)


async def inactive_session_cleanup_task(
    cache_session_store: Any,
    session_factory: Any,
    inactive_threshold: timedelta = timedelta(days=30),
    interval_seconds: int = 86400,  # 24 hours
) -> None:
    """Background task: Clean up inactive sessions.

    Removes sessions that haven't been used in N days,
    even if they haven't expired.

    Args:
        cache_session_store: CacheSessionStore instance
        session_factory: SQLAlchemy session factory (for resource cleanup)
        inactive_threshold: Inactivity period (default: 30 days)
        interval_seconds: How often to run (default: 86400 = 24 hours)
    """
    logger.info(
        f"Starting inactive session cleanup task "
        f"(threshold: {inactive_threshold.days} days, interval: {interval_seconds}s)"
    )

    while True:
        try:
            with session_factory() as db:
                count = await cleanup_inactive_sessions(cache_session_store, db, inactive_threshold)
                db.commit()

                if count > 0:
                    logger.info(f"Cleaned up {count} inactive sessions")

        except Exception as e:
            logger.error(f"Inactive session cleanup failed: {e}", exc_info=True)

        await asyncio.sleep(interval_seconds)


async def tiger_cache_queue_task(
    rebac_manager: Any,
    interval_seconds: int = 60,  # Process less frequently since write-through handles new grants
    batch_size: int = 1,  # Process ONE entry at a time to avoid blocking
) -> None:
    """Background task: Process Tiger Cache queue (Issue #935).

    NOTE: With write-through implemented, this queue is mainly for:
    1. Warming cache on startup (legacy entries)
    2. Processing any entries that failed write-through

    New permission grants are handled immediately by persist_single_grant().

    Args:
        rebac_manager: ReBAC manager instance (injected via DI)
        interval_seconds: How often to process queue (default: 60 seconds)
        batch_size: Number of queue entries to process per batch (default: 1)

    Examples:
        >>> # Start Tiger Cache queue processor
        >>> asyncio.create_task(tiger_cache_queue_task(rebac_manager, 60, 1))
    """
    logger.info(
        f"Starting Tiger Cache queue task (interval: {interval_seconds}s, batch: {batch_size})"
    )

    # Wait for server to fully start
    await asyncio.sleep(5)

    while True:
        try:
            if hasattr(rebac_manager, "tiger_process_queue"):
                # Run blocking queue processing in thread to avoid blocking event loop
                processed = await asyncio.to_thread(
                    rebac_manager.tiger_process_queue, batch_size=batch_size
                )
                if processed > 0:
                    logger.info(f"Tiger Cache: processed {processed} queue entries (background)")
            else:
                tiger_updater = getattr(rebac_manager, "_tiger_updater", None)
                if tiger_updater is None:
                    logger.debug("Tiger Cache: _tiger_updater is None, queue cannot be processed")
        except Exception as e:
            logger.warning(f"Tiger Cache queue processing error: {e}")

        await asyncio.sleep(interval_seconds)


async def version_gc_task(
    record_store: Any,
    config: VersionGCSettings | None = None,
    *,
    is_postgresql: bool = False,
) -> None:
    """Background task: Garbage collect old version history (Issue #974).

    Runs periodically to clean up old version_history entries based on:
    1. Age-based retention: Delete versions older than retention_days
    2. Count-based retention: Keep only max_versions per resource

    Always preserves the latest version for each resource.

    Args:
        record_store: RecordStoreABC instance for database access.
        config: GC configuration (uses VersionGCSettings.from_env() if None)
        is_postgresql: Whether the database is PostgreSQL (config-time flag).

    Examples:
        >>> # Start version GC task in server
        >>> config = VersionGCSettings(retention_days=30, max_versions_per_resource=100)
        >>> asyncio.create_task(version_gc_task(record_store, config))
    """
    config = config or VersionGCSettings.from_env()
    interval_seconds = config.run_interval_hours * 3600

    logger.info(
        f"Starting version GC task (interval: {config.run_interval_hours}h, "
        f"retention: {config.retention_days}d, max_versions: {config.max_versions_per_resource})"
    )

    # Wait for server to fully start
    await asyncio.sleep(10)

    gc = VersionHistoryGC(record_store, is_postgresql=is_postgresql)

    while True:
        if config.enabled:
            try:
                stats = await gc.run_gc_async(config)

                if stats.total_deleted > 0:
                    logger.info(
                        f"Version GC: deleted {stats.total_deleted} versions "
                        f"({stats.deleted_by_age} by age, {stats.deleted_by_count} by count), "
                        f"reclaimed ~{stats.bytes_reclaimed / 1024 / 1024:.2f}MB, "
                        f"took {stats.duration_seconds:.2f}s"
                    )
                else:
                    logger.debug("Version GC: no versions to clean up")

            except Exception as e:
                logger.error(f"Version GC failed: {e}", exc_info=True)

        await asyncio.sleep(interval_seconds)


async def hotspot_prefetch_task(
    hotspot_detector: Any,
    tiger_cache: Any,
    tiger_updater: Any,
    interval_seconds: int = 10,
    cleanup_interval_seconds: int = 60,
) -> None:
    """Background task: Hotspot prefetching for Tiger Cache (Issue #921).

    Monitors access patterns and proactively warms hot cache entries before
    TTL expiry to prevent latency spikes on frequently accessed permission paths.

    Inspired by:
    - Google Zanzibar Section 3.2.5: Hot spot handling
    - SpiceDB: Consistent hash routing for cache locality
    - JuiceFS: --prefetch for read patterns

    Args:
        hotspot_detector: HotspotDetector instance (injected via DI)
        tiger_cache: TigerBitmapCache instance (injected via DI)
        tiger_updater: TigerCacheUpdater instance (injected via DI)
        interval_seconds: How often to check for prefetch candidates (default: 10s)
        cleanup_interval_seconds: How often to cleanup stale entries (default: 60s)

    Examples:
        >>> # Start hotspot prefetch task
        >>> asyncio.create_task(hotspot_prefetch_task(detector, cache, updater, 10, 60))
    """
    logger.info(
        f"Starting hotspot prefetch task (interval: {interval_seconds}s, "
        f"cleanup: {cleanup_interval_seconds}s)"
    )

    # Wait for server to fully start
    await asyncio.sleep(5)

    cleanup_counter = 0
    cleanup_cycles = max(1, cleanup_interval_seconds // interval_seconds)

    while True:
        try:
            # Get prefetch candidates
            cache_ttl = getattr(tiger_cache, "_cache_ttl", 300)
            candidates = hotspot_detector.get_prefetch_candidates(tiger_cache, cache_ttl=cache_ttl)

            if candidates:
                prefetched = 0
                for entry in candidates:
                    try:
                        tiger_updater.queue_update(
                            subject_type=entry.subject_type,
                            subject_id=entry.subject_id,
                            permission=entry.permission,
                            resource_type=entry.resource_type,
                            zone_id=entry.zone_id,
                            priority=1,  # High priority for hot entries
                        )
                        prefetched += 1
                    except Exception as e:
                        logger.warning(
                            f"Hotspot prefetch queue error for {entry.subject_type}:{entry.subject_id}: {e}"
                        )

                if prefetched > 0:
                    logger.info(f"Hotspot: queued {prefetched} entries for prefetch")

            # Periodic cleanup of stale entries
            cleanup_counter += 1
            if cleanup_counter >= cleanup_cycles:
                removed = hotspot_detector.cleanup_stale_entries()
                if removed > 0:
                    logger.debug(f"Hotspot: cleaned up {removed} stale entries")
                cleanup_counter = 0

        except Exception as e:
            logger.warning(f"Hotspot prefetch error: {e}")

        await asyncio.sleep(interval_seconds)


async def agent_eviction_task(
    eviction_manager: Any,
    interval_seconds: int = 300,
) -> None:
    """Periodically run eviction cycle under resource pressure (Issues #2170, #2171).

    Uses event-driven wakeup: sleeps for interval_seconds OR wakes immediately
    when eviction_manager.trigger_immediate_cycle() is called (e.g. for
    premium agent preemption). Matches BrickReconciler pattern.

    Args:
        eviction_manager: EvictionManager instance with run_cycle() + urgent_event
        interval_seconds: How often to check for eviction (default: 300)
    """
    while True:
        # Wait for interval OR immediate trigger (Issue #2171)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                eviction_manager.urgent_event.wait(),
                timeout=interval_seconds,
            )

        try:
            result = await eviction_manager.run_cycle()
            if result.evicted > 0:
                logger.info(
                    "[EVICTION] Evicted %d agents | reason=%s | post_pressure=%s | skipped=%d",
                    result.evicted,
                    result.reason,
                    result.post_pressure,
                    result.skipped,
                )
        except Exception:
            logger.exception("[EVICTION] Eviction cycle failed")


def start_background_tasks(
    record_store: Any,
    sandbox_manager: Any | None = None,
    *,
    is_postgresql: bool = False,
    cache_session_store: Any | None = None,
) -> list:
    """Start all background tasks.

    Args:
        record_store: RecordStoreABC instance for database access.
        sandbox_manager: Optional SandboxManager for sandbox cleanup (Issue #372)
        is_postgresql: Whether the database is PostgreSQL (config-time flag).
        cache_session_store: Optional CacheSessionStore for session cleanup.

    Returns:
        List of asyncio tasks
    """
    tasks = []

    if cache_session_store is not None:
        tasks.append(
            asyncio.create_task(
                session_cleanup_task(cache_session_store, record_store.session_factory)
            )
        )

    # Add sandbox cleanup if manager provided (Issue #372)
    if sandbox_manager is not None:
        tasks.append(asyncio.create_task(sandbox_cleanup_task(sandbox_manager)))

    # Add version history GC (Issue #974)
    gc_config = VersionGCSettings.from_env()
    if gc_config.enabled:
        tasks.append(
            asyncio.create_task(
                version_gc_task(record_store, gc_config, is_postgresql=is_postgresql)
            )
        )

    logger.info(f"Started {len(tasks)} background tasks")
    return tasks
