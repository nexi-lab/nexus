"""Background tasks for Nexus server (v0.5.0).

Provides background cleanup tasks for session management and expired resources.
"""

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from nexus.core.sessions import cleanup_expired_sessions, cleanup_inactive_sessions

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
        >>> from nexus.core.sandbox_manager import SandboxManager
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
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="tiger_queue")

    while True:
        try:
            # Access the rebac_manager from nexus_fs
            rebac_manager = getattr(nexus_fs, "_rebac_manager", None)
            if rebac_manager and hasattr(rebac_manager, "tiger_process_queue"):
                # Run blocking queue processing in thread pool to avoid blocking event loop
                loop = asyncio.get_event_loop()
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


def start_background_tasks(session_factory: Any, sandbox_manager: Any | None = None) -> list:
    """Start all background tasks.

    Args:
        session_factory: SQLAlchemy session factory
        sandbox_manager: Optional SandboxManager for sandbox cleanup (Issue #372)

    Returns:
        List of asyncio tasks

    Examples:
        >>> # In server startup
        >>> from nexus.server.background_tasks import start_background_tasks
        >>> tasks = start_background_tasks(SessionLocal, sandbox_mgr)
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

    logger.info(f"Started {len(tasks)} background tasks")
    return tasks
