"""Service startup/shutdown: AgentRegistry, KeyService, Sandbox, Scheduler, TaskQueue.

Extracted from fastapi_server.py (#1602).
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

# Module-level references for shutdown — set during startup
_scheduler_engine: Any = None
_heartbeat_task: asyncio.Task | None = None
_stale_detection_task: asyncio.Task | None = None


async def startup_services(app: FastAPI) -> list[asyncio.Task]:
    """Initialize application services and return background tasks.

    Covers:
    - AgentRegistry (Issue #1240)
    - KeyService (Issue #1355)
    - SandboxAuthService (Issue #1307)
    - SchedulerService (Issue #1212)
    - Task Queue Engine (Issue #574)
    """
    global _scheduler_engine, _heartbeat_task, _stale_detection_task

    bg_tasks: list[asyncio.Task] = []

    _startup_agent_registry(app)
    _startup_key_service(app)
    _startup_sandbox_auth(app)

    # Agent background tasks depend on agent_registry
    agent_tasks = _startup_agent_tasks(app)
    bg_tasks.extend(agent_tasks)

    await _startup_scheduler(app)
    task_runner_task = _startup_task_queue(app)
    if task_runner_task:
        bg_tasks.append(task_runner_task)

    return bg_tasks


async def shutdown_services(app: FastAPI) -> None:
    """Shutdown services in reverse order."""
    global _scheduler_engine, _heartbeat_task, _stale_detection_task

    # Stop Task Queue runner (Issue #574)
    task_runner = getattr(app.state, "task_runner", None)
    if task_runner:
        try:
            await task_runner.shutdown()
            logger.info("Task Queue runner stopped")
        except Exception as e:
            logger.warning(f"Error shutting down Task Queue runner: {e}")

    # Shutdown scheduler engine (Issue #1212)
    if _scheduler_engine:
        try:
            await _scheduler_engine.dispose()
            logger.info("Scheduler engine disposed")
        except Exception as e:
            logger.warning(f"Error disposing scheduler engine: {e}")
        _scheduler_engine = None

    # Cancel agent background tasks and final flush (Issue #1240)
    for task_ref in (_heartbeat_task, _stale_detection_task):
        if task_ref and not task_ref.done():
            task_ref.cancel()
            with suppress(asyncio.CancelledError):
                await task_ref
    _heartbeat_task = None
    _stale_detection_task = None

    if app.state.agent_registry:
        try:
            app.state.agent_registry.flush_heartbeats()
            logger.info("[AGENT-REG] Final heartbeat flush completed")
        except Exception:
            logger.warning("[AGENT-REG] Final heartbeat flush failed", exc_info=True)

    # SandboxManager cleanup
    if app.state.sandbox_auth_service:
        logger.info(
            "[SANDBOX-AUTH] SandboxAuthService cleaned up (session-per-op, no persistent session)"
        )

    # Shutdown AsyncNexusFS (Issue #940)
    if app.state.async_nexus_fs:
        try:
            await app.state.async_nexus_fs.close()
            logger.info("AsyncNexusFS stopped")
        except Exception as e:
            logger.warning(f"Error shutting down AsyncNexusFS: {e}")

    # Shutdown Search Daemon (Issue #951)
    if app.state.search_daemon:
        try:
            await app.state.search_daemon.shutdown()
            logger.info("Search Daemon stopped")
        except Exception as e:
            logger.warning(f"Error shutting down Search Daemon: {e}")

    # Stop DirectoryGrantExpander worker
    if hasattr(app.state, "directory_grant_expander") and app.state.directory_grant_expander:
        try:
            app.state.directory_grant_expander.stop()
            logger.info("DirectoryGrantExpander worker stopped")
        except Exception as e:
            logger.debug(f"Error stopping DirectoryGrantExpander: {e}")

    # Cancel pending event tasks in NexusFS (Issue #913)
    if app.state.nexus_fs and hasattr(app.state.nexus_fs, "_event_tasks"):
        event_tasks = app.state.nexus_fs._event_tasks.copy()
        for task in event_tasks:
            task.cancel()
        if event_tasks:
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*event_tasks, return_exceptions=True)
            logger.info(f"Cancelled {len(event_tasks)} pending event tasks")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _startup_agent_registry(app: FastAPI) -> None:
    """Initialize AgentRegistry for agent lifecycle tracking (Issue #1240)."""
    if app.state.nexus_fs and getattr(app.state.nexus_fs, "SessionLocal", None):
        try:
            from nexus.core.agent_registry import AgentRegistry

            app.state.agent_registry = AgentRegistry(
                session_factory=app.state.nexus_fs.SessionLocal,
                entity_registry=getattr(app.state.nexus_fs, "_entity_registry", None),
                flush_interval=60,
            )
            # Inject into NexusFS for RPC methods
            app.state.nexus_fs._agent_registry = app.state.agent_registry

            # Wire into sync PermissionEnforcer
            perm_enforcer = getattr(app.state.nexus_fs, "_permission_enforcer", None)
            if perm_enforcer is not None:
                perm_enforcer.agent_registry = app.state.agent_registry

            # Issue #1440: Create async wrapper for protocol conformance
            from nexus.services.agents.async_agent_registry import AsyncAgentRegistry

            app.state.async_agent_registry = AsyncAgentRegistry(app.state.agent_registry)

            logger.info("[AGENT-REG] AgentRegistry initialized and wired")
        except Exception as e:
            logger.warning(f"[AGENT-REG] Failed to initialize AgentRegistry: {e}")
            app.state.agent_registry = None
            app.state.async_agent_registry = None
    else:
        app.state.agent_registry = None
        app.state.async_agent_registry = None


def _startup_key_service(app: FastAPI) -> None:
    """Initialize KeyService for agent identity (Issue #1355)."""
    if app.state.nexus_fs and getattr(app.state.nexus_fs, "SessionLocal", None):
        try:
            from nexus.identity.crypto import IdentityCrypto
            from nexus.identity.key_service import KeyService
            from nexus.server.auth.oauth_crypto import OAuthCrypto

            # Reuse OAuthCrypto for Fernet encryption of private keys
            _db_url = app.state.database_url or "sqlite:///nexus.db"
            _identity_oauth_crypto = OAuthCrypto(db_url=_db_url)
            _identity_crypto = IdentityCrypto(oauth_crypto=_identity_oauth_crypto)

            _record_store = getattr(app.state.nexus_fs, "_record_store", None)
            if _record_store is None:
                logger.warning(
                    "[KYA] RecordStore not available, KeyService will not be initialized"
                )
                app.state.key_service = None
                return

            app.state.key_service = KeyService(
                record_store=_record_store,
                crypto=_identity_crypto,
            )
            # Inject into NexusFS for register_agent integration
            app.state.nexus_fs._key_service = app.state.key_service

            logger.info("[KYA] KeyService initialized and wired")
        except Exception as e:
            logger.warning(f"[KYA] Failed to initialize KeyService: {e}")
            app.state.key_service = None
    else:
        app.state.key_service = None


def _startup_sandbox_auth(app: FastAPI) -> None:
    """Initialize SandboxAuthService for authenticated sandbox creation (Issue #1307)."""
    if app.state.nexus_fs and not app.state.agent_registry:
        logger.info(
            "[SANDBOX-AUTH] AgentRegistry not available, SandboxAuthService will not be initialized"
        )
    if not (app.state.nexus_fs and app.state.agent_registry):
        return

    try:
        from nexus.sandbox.auth_service import SandboxAuthService
        from nexus.sandbox.events import AgentEventLog
        from nexus.sandbox.sandbox_manager import SandboxManager

        session_factory = getattr(app.state.nexus_fs, "SessionLocal", None)
        if not (session_factory and callable(session_factory)):
            return

        # Create AgentEventLog for sandbox lifecycle audit
        app.state.agent_event_log = AgentEventLog(session_factory=session_factory)

        # Create SandboxManager
        sandbox_config = getattr(app.state.nexus_fs, "_config", None)
        sandbox_mgr = SandboxManager(
            session_factory=session_factory,
            e2b_api_key=os.getenv("E2B_API_KEY"),
            e2b_team_id=os.getenv("E2B_TEAM_ID"),
            e2b_template_id=os.getenv("E2B_TEMPLATE_ID"),
            config=sandbox_config,
        )

        # Attach smart router for Monty -> Docker -> E2B routing (Issue #1317)
        sandbox_mgr.wire_router()

        # Get NamespaceManager if available (best-effort)
        namespace_manager = None
        sync_rebac = getattr(app.state.nexus_fs, "_rebac_manager", None)
        if sync_rebac:
            try:
                from nexus.services.permissions.namespace_factory import (
                    create_namespace_manager,
                )

                ns_record_store = getattr(app.state.nexus_fs, "_record_store", None)
                namespace_manager = create_namespace_manager(
                    rebac_manager=sync_rebac,
                    record_store=ns_record_store,
                )
                # Wire event-driven invalidation for sandbox namespace (Issue #1244)
                sync_rebac.register_namespace_invalidator(
                    "sandbox_namespace_dcache",
                    lambda st, sid, _zid: namespace_manager.invalidate((st, sid)),
                )
            except Exception as e:
                logger.info(
                    "[SANDBOX-AUTH] NamespaceManager not available (%s), "
                    "sandbox mount tables will be empty",
                    e,
                )

        app.state.sandbox_auth_service = SandboxAuthService(
            agent_registry=app.state.agent_registry,
            sandbox_manager=sandbox_mgr,
            namespace_manager=namespace_manager,
            event_log=app.state.agent_event_log,
            budget_enforcement=False,
        )
        logger.info("[SANDBOX-AUTH] SandboxAuthService initialized")
    except Exception as e:
        logger.warning(f"[SANDBOX-AUTH] Failed to initialize SandboxAuthService: {e}")


def _startup_agent_tasks(app: FastAPI) -> list[asyncio.Task]:
    """Start agent heartbeat and stale detection background tasks (Issue #1240)."""
    global _heartbeat_task, _stale_detection_task

    if not app.state.agent_registry:
        return []

    from nexus.server.background_tasks import (
        heartbeat_flush_task,
        stale_agent_detection_task,
    )

    _heartbeat_task = asyncio.create_task(
        heartbeat_flush_task(app.state.agent_registry, interval_seconds=60)
    )
    _stale_detection_task = asyncio.create_task(
        stale_agent_detection_task(app.state.agent_registry, interval_seconds=300)
    )
    logger.info("[AGENT-REG] Background heartbeat flush and stale detection tasks started")

    return [_heartbeat_task, _stale_detection_task]


async def _startup_scheduler(app: FastAPI) -> None:
    """Initialize SchedulerService if PostgreSQL database is available (Issue #1212)."""
    global _scheduler_engine

    if not (app.state.database_url and "postgresql" in app.state.database_url):
        return

    try:
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        from nexus.pay.credits import CreditsService
        from nexus.scheduler.queue import TaskQueue
        from nexus.scheduler.service import SchedulerService
        from nexus.storage.models.scheduler import ScheduledTaskModel

        # Ensure the URL has an async driver
        db_url = app.state.database_url
        if "+asyncpg" not in db_url and "+psycopg2" not in db_url:
            db_url = db_url.replace("postgresql://", "postgresql+asyncpg://")
        elif "+psycopg2" in db_url:
            db_url = db_url.replace("+psycopg2", "+asyncpg")

        _scheduler_engine = create_async_engine(db_url, pool_size=5, max_overflow=2)

        # Create table via ORM metadata (replaces runtime DDL)
        async with _scheduler_engine.begin() as conn:
            await conn.run_sync(
                ScheduledTaskModel.metadata.create_all,
                tables=[ScheduledTaskModel.__table__],
            )

        session_factory = async_sessionmaker(
            _scheduler_engine, class_=AsyncSession, expire_on_commit=False
        )

        scheduler_service = SchedulerService(
            queue=TaskQueue(session_factory=session_factory),
            credits_service=CreditsService(enabled=False),
        )
        app.state.scheduler_service = scheduler_service

        # Store async engine for dispatcher LISTEN/NOTIFY
        app.state.scheduler_engine = _scheduler_engine
        logger.info("Scheduler service initialized (SQLAlchemy async ORM)")
    except ImportError as e:
        logger.debug(f"Scheduler service not available: {e}")
    except Exception as e:
        logger.warning(f"Failed to initialize Scheduler service: {e}")


def _startup_task_queue(app: FastAPI) -> asyncio.Task | None:
    """Start Task Queue Engine background worker (Issue #574)."""
    if not app.state.nexus_fs:
        return None

    try:
        from nexus.tasks import is_available

        if is_available():
            service = app.state.nexus_fs.task_queue_service
            engine = service.get_engine()

            from nexus.tasks.runner import AsyncTaskRunner

            runner = AsyncTaskRunner(engine=engine, max_workers=4)
            service.set_runner(runner)
            app.state.task_runner = runner
            task = asyncio.create_task(runner.run())
            logger.info("Task Queue runner started (4 workers)")
            return task
        else:
            logger.debug("Task Queue: nexus_tasks Rust extension not available")
    except Exception as e:
        logger.warning(f"Task Queue runner not started: {e}")

    return None
