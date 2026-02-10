"""Background tasks for Nexus server (v0.5.0).

Provides background cleanup tasks for session management and expired resources.
"""

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from nexus.services.sessions import cleanup_expired_sessions, cleanup_inactive_sessions
from nexus.storage.version_gc import VersionGCSettings, VersionHistoryGC

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


async def sandbox_cleanup_task(sandbox_manager: Any, interval_seconds: int = 300) -> None:
    """Background task: Clean up expired sandboxes (Issue #372).

    Runs periodically to stop and destroy sandboxes that have exceeded their TTL.

    Args:
        sandbox_manager: SandboxManager instance
        interval_seconds: How often to run cleanup (default: 300 = 5 minutes)

    Examples:
        >>> # Start cleanup task in server
        >>> from nexus.sandbox import SandboxManager
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


async def session_cleanup_task(session_factory: Any, interval_seconds: int = 3600) -> None:
    """Background task: Clean up expired sessions.

    Runs periodically to delete expired sessions and their resources.

    Args:
        session_factory: SQLAlchemy session factory
        interval_seconds: How often to run cleanup (default: 3600 = 1 hour)

    Examples:
        >>> # Start cleanup task in server
        >>> asyncio.create_task(session_cleanup_task(SessionLocal, 3600))
    """
    logger.info(f"Starting session cleanup task (interval: {interval_seconds}s)")

    while True:
        try:
            with session_factory() as db:
                result = cleanup_expired_sessions(db)
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
    session_factory: Any,
    inactive_threshold: timedelta = timedelta(days=30),
    interval_seconds: int = 86400,  # 24 hours
) -> None:
    """Background task: Clean up inactive sessions.

    Optional: Removes sessions that haven't been used in N days,
    even if they haven't expired.

    Args:
        session_factory: SQLAlchemy session factory
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
                count = cleanup_inactive_sessions(db, inactive_threshold)
                db.commit()

                if count > 0:
                    logger.info(f"Cleaned up {count} inactive sessions")

        except Exception as e:
            logger.error(f"Inactive session cleanup failed: {e}", exc_info=True)

        await asyncio.sleep(interval_seconds)


async def tiger_cache_queue_task(
    nexus_fs: "NexusFS",
    interval_seconds: int = 60,  # Process less frequently since write-through handles new grants
    batch_size: int = 1,  # Process ONE entry at a time to avoid blocking
) -> None:
    """Background task: Process Tiger Cache queue (Issue #935).

    NOTE: With write-through implemented, this queue is mainly for:
    1. Warming cache on startup (legacy entries)
    2. Processing any entries that failed write-through

    New permission grants are handled immediately by persist_single_grant().

    Args:
        nexus_fs: NexusFS instance with ReBAC manager
        interval_seconds: How often to process queue (default: 60 seconds)
        batch_size: Number of queue entries to process per batch (default: 1)

    Examples:
        >>> # Start Tiger Cache queue processor
        >>> asyncio.create_task(tiger_cache_queue_task(nexus_fs, 60, 1))
    """
    import concurrent.futures

    logger.info(
        f"Starting Tiger Cache queue task (interval: {interval_seconds}s, batch: {batch_size})"
    )

    # Wait for server to fully start
    await asyncio.sleep(5)

    # Create a thread pool for blocking queue processing
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="tiger_queue"
    )

    while True:
        try:
            # Access the rebac_manager from nexus_fs
            rebac_manager = getattr(nexus_fs, "_rebac_manager", None)
            if rebac_manager and hasattr(rebac_manager, "tiger_process_queue"):
                # Run blocking queue processing in thread pool to avoid blocking event loop
                loop = asyncio.get_running_loop()

                # Bind rebac_manager to avoid B023 late-binding closure issue
                def process_queue(mgr: Any = rebac_manager) -> int:
                    result: int = mgr.tiger_process_queue(batch_size=batch_size)
                    return result

                processed = await loop.run_in_executor(executor, process_queue)
                if processed > 0:
                    logger.info(f"Tiger Cache: processed {processed} queue entries (background)")
            elif rebac_manager:
                tiger_updater = getattr(rebac_manager, "_tiger_updater", None)
                if tiger_updater is None:
                    logger.debug("Tiger Cache: _tiger_updater is None, queue cannot be processed")
        except Exception as e:
            logger.warning(f"Tiger Cache queue processing error: {e}")

        await asyncio.sleep(interval_seconds)


async def version_gc_task(
    session_factory: Any,
    config: VersionGCSettings | None = None,
) -> None:
    """Background task: Garbage collect old version history (Issue #974).

    Runs periodically to clean up old version_history entries based on:
    1. Age-based retention: Delete versions older than retention_days
    2. Count-based retention: Keep only max_versions per resource

    Always preserves the latest version for each resource.

    Args:
        session_factory: SQLAlchemy session factory
        config: GC configuration (uses VersionGCSettings.from_env() if None)

    Examples:
        >>> # Start version GC task in server
        >>> config = VersionGCSettings(retention_days=30, max_versions_per_resource=100)
        >>> asyncio.create_task(version_gc_task(SessionLocal, config))
    """
    config = config or VersionGCSettings.from_env()
    interval_seconds = config.run_interval_hours * 3600

    logger.info(
        f"Starting version GC task (interval: {config.run_interval_hours}h, "
        f"retention: {config.retention_days}d, max_versions: {config.max_versions_per_resource})"
    )

    # Wait for server to fully start
    await asyncio.sleep(10)

    gc = VersionHistoryGC(session_factory)

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
    nexus_fs: "NexusFS",
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
        nexus_fs: NexusFS instance with permission enforcer
        interval_seconds: How often to check for prefetch candidates (default: 10s)
        cleanup_interval_seconds: How often to cleanup stale entries (default: 60s)

    Examples:
        >>> # Start hotspot prefetch task
        >>> asyncio.create_task(hotspot_prefetch_task(nexus_fs, 10, 60))
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
            # Get hotspot detector from permission enforcer
            permission_enforcer = getattr(nexus_fs, "_permission_enforcer", None)
            hotspot_detector = None

            if permission_enforcer:
                hotspot_detector = getattr(permission_enforcer, "_hotspot_detector", None)

            if not hotspot_detector:
                # Hotspot tracking not enabled, skip
                await asyncio.sleep(interval_seconds)
                continue

            # Get rebac manager for Tiger Cache access
            rebac_manager = getattr(nexus_fs, "_rebac_manager", None)
            if not rebac_manager:
                await asyncio.sleep(interval_seconds)
                continue

            tiger_cache = getattr(rebac_manager, "_tiger_cache", None)
            tiger_updater = getattr(rebac_manager, "_tiger_updater", None)

            if not tiger_cache or not tiger_updater:
                await asyncio.sleep(interval_seconds)
                continue

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


async def heartbeat_flush_task(agent_registry: Any, interval_seconds: int = 60) -> None:
    """Periodically flush agent heartbeat buffer to database (Issue #1240).

    Args:
        agent_registry: AgentRegistry instance with flush_heartbeats() method
        interval_seconds: Flush interval in seconds (default: 60)
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            flushed = agent_registry.flush_heartbeats()
            if flushed > 0:
                logger.info(f"[HEARTBEAT] Flushed {flushed} agent heartbeats to database")
        except Exception:
            logger.exception("[HEARTBEAT] Failed to flush heartbeat buffer")


async def stale_agent_detection_task(
    agent_registry: Any, interval_seconds: int = 300, threshold_seconds: int = 300
) -> None:
    """Periodically detect agents with stale heartbeats (Issue #1240).

    Args:
        agent_registry: AgentRegistry instance with detect_stale() method
        interval_seconds: Detection interval in seconds (default: 300)
        threshold_seconds: Heartbeat age threshold for staleness (default: 300)
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            stale = agent_registry.detect_stale(threshold_seconds=threshold_seconds)
            if stale:
                stale_ids = [a.agent_id for a in stale]
                logger.warning(f"[HEARTBEAT] {len(stale)} stale agents detected: {stale_ids[:10]}")
        except Exception:
            logger.exception("[HEARTBEAT] Failed to detect stale agents")


def start_background_tasks(
    session_factory: Any,
    sandbox_manager: Any | None = None,
    agent_registry: Any | None = None,
) -> list:
    """Start all background tasks.

    Args:
        session_factory: SQLAlchemy session factory
        sandbox_manager: Optional SandboxManager for sandbox cleanup (Issue #372)
        agent_registry: Optional AgentRegistry for heartbeat flush (Issue #1240)

    Returns:
        List of asyncio tasks

    Examples:
        >>> # In server startup
        >>> from nexus.server.background_tasks import start_background_tasks
        >>> tasks = start_background_tasks(SessionLocal, sandbox_mgr, agent_registry)
        >>> # Tasks run in background
    """
    tasks = [
        asyncio.create_task(session_cleanup_task(session_factory)),
        # Uncomment to enable inactive session cleanup:
        # asyncio.create_task(inactive_session_cleanup_task(session_factory)),
    ]

    # Add sandbox cleanup if manager provided (Issue #372)
    if sandbox_manager is not None:
        tasks.append(asyncio.create_task(sandbox_cleanup_task(sandbox_manager)))

    # Add version history GC (Issue #974)
    gc_config = VersionGCSettings.from_env()
    if gc_config.enabled:
        tasks.append(asyncio.create_task(version_gc_task(session_factory, gc_config)))

    # Add agent heartbeat flush and stale detection (Issue #1240)
    if agent_registry is not None:
        tasks.append(asyncio.create_task(heartbeat_flush_task(agent_registry)))
        tasks.append(asyncio.create_task(stale_agent_detection_task(agent_registry)))

    logger.info(f"Started {len(tasks)} background tasks")
    return tasks
