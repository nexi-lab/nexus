"""Background tasks for Nexus server (v0.5.0).

Provides background cleanup tasks for session management and expired resources.
"""

import asyncio
import logging
from datetime import timedelta
from typing import Any

from nexus.core.sessions import cleanup_expired_sessions, cleanup_inactive_sessions

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
