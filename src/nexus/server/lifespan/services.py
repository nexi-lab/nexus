"""Service startup/shutdown: AgentRegistry, KeyService, Sandbox, Scheduler, TaskQueue.

Extracted from fastapi_server.py (#1602).
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def startup_services(app: FastAPI) -> list[asyncio.Task]:
    """Initialize application services and return background tasks.

    Covers:
    - AgentRegistry (Issue #1240)
    - KeyService (Issue #1355)
    - SandboxAuthService (Issue #1307)
    - SchedulerService (Issue #1212)
    - Task Queue Engine (Issue #574)
    """
    bg_tasks: list[asyncio.Task] = []

    _startup_agent_registry(app)
    _startup_key_service(app)
    _startup_delegation_service(app)
    _startup_sandbox_auth(app)

    # Agent background tasks depend on agent_registry
    agent_tasks = _startup_agent_tasks(app)
    bg_tasks.extend(agent_tasks)

    await _startup_scheduler(app)
    task_runner_task = _startup_task_queue(app)
    if task_runner_task:
        bg_tasks.append(task_runner_task)

    await _startup_workflow_engine(app)

    return bg_tasks


async def shutdown_services(app: FastAPI) -> None:
    """Shutdown services in reverse order."""
    # Stop Task Queue runner (Issue #574)
    task_runner = getattr(app.state, "task_runner", None)
    if task_runner:
        try:
            await task_runner.shutdown()
            logger.info("Task Queue runner stopped")
        except Exception as e:
            logger.warning("Error shutting down Task Queue runner: %s", e, exc_info=True)

    # Shutdown scheduler pool (Issue #1212)
    scheduler_pool = getattr(app.state, "_scheduler_pool", None)
    if scheduler_pool:
        try:
            await scheduler_pool.close()
            logger.info("Scheduler pool closed")
        except Exception as e:
            logger.warning("Error closing scheduler pool: %s", e, exc_info=True)
        app.state._scheduler_pool = None

    # Cancel agent background tasks and final flush (Issue #1240)
    heartbeat_task = getattr(app.state, "_heartbeat_task", None)
    stale_detection_task = getattr(app.state, "_stale_detection_task", None)
    for task_ref in (heartbeat_task, stale_detection_task):
        if task_ref and not task_ref.done():
            task_ref.cancel()
            with suppress(asyncio.CancelledError):
                await task_ref
    app.state._heartbeat_task = None
    app.state._stale_detection_task = None

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
            logger.warning("Error shutting down AsyncNexusFS: %s", e, exc_info=True)

    # Shutdown Search Daemon (Issue #951)
    if app.state.search_daemon:
        try:
            await app.state.search_daemon.shutdown()
            logger.info("Search Daemon stopped")
        except Exception as e:
            logger.warning("Error shutting down Search Daemon: %s", e, exc_info=True)

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
            logger.warning("[AGENT-REG] Failed to initialize AgentRegistry: %s", e, exc_info=True)
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
            from nexus.storage.models.identity import AgentKeyModel

            # Ensure agent_keys table exists
            _nx_engine = getattr(app.state.nexus_fs, "_sql_engine", None)
            if _nx_engine is not None:
                from sqlalchemy import Table

                cast(Table, AgentKeyModel.__table__).create(_nx_engine, checkfirst=True)

            # Reuse OAuthCrypto for Fernet encryption of private keys
            _enc_key = os.environ.get("NEXUS_OAUTH_ENCRYPTION_KEY", "").strip() or None
            _session_factory = getattr(app.state.nexus_fs, "SessionLocal", None)
            _identity_oauth_crypto = OAuthCrypto(
                encryption_key=_enc_key, session_factory=_session_factory
            )
            _identity_crypto = IdentityCrypto(oauth_crypto=_identity_oauth_crypto)

            app.state.key_service = KeyService(
                record_store=app.state.nexus_fs._record_store,
                crypto=_identity_crypto,
            )
            # Inject into NexusFS for register_agent integration
            app.state.nexus_fs._key_service = app.state.key_service

            logger.info("[KYA] KeyService initialized and wired")
        except Exception as e:
            logger.warning("[KYA] Failed to initialize KeyService: %s", e, exc_info=True)
            app.state.key_service = None
    else:
        app.state.key_service = None


def _startup_delegation_service(app: FastAPI) -> None:
    """Initialize DelegationService for agent delegation (Issue #1618)."""
    if not (app.state.nexus_fs and getattr(app.state.nexus_fs, "SessionLocal", None)):
        app.state.delegation_service = None
        return

    rebac_manager = getattr(app.state.nexus_fs, "_rebac_manager", None)
    if rebac_manager is None:
        app.state.delegation_service = None
        return

    try:
        from nexus.services.delegation.service import DelegationService

        namespace_manager = getattr(app.state.nexus_fs, "_namespace_manager", None)
        entity_registry = getattr(app.state.nexus_fs, "_entity_registry", None)
        agent_registry = getattr(app.state, "agent_registry", None)

        app.state.delegation_service = DelegationService(
            session_factory=app.state.nexus_fs.SessionLocal,
            rebac_manager=rebac_manager,
            namespace_manager=namespace_manager,
            entity_registry=entity_registry,
            agent_registry=agent_registry,
        )
        logger.info("[DELEGATION] DelegationService initialized and wired")
    except Exception as e:
        logger.warning("[DELEGATION] Failed to initialize DelegationService: %s", e, exc_info=True)
        app.state.delegation_service = None


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
        sandbox_config = getattr(app.state.nexus_fs, "config", None)
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
        logger.warning(
            "[SANDBOX-AUTH] Failed to initialize SandboxAuthService: %s", e, exc_info=True
        )


def _startup_agent_tasks(app: FastAPI) -> list[asyncio.Task]:
    """Start agent heartbeat and stale detection background tasks (Issue #1240)."""
    if not app.state.agent_registry:
        return []

    from nexus.server.background_tasks import (
        heartbeat_flush_task,
        stale_agent_detection_task,
    )

    app.state._heartbeat_task = asyncio.create_task(
        heartbeat_flush_task(app.state.agent_registry, interval_seconds=60)
    )
    app.state._stale_detection_task = asyncio.create_task(
        stale_agent_detection_task(app.state.agent_registry, interval_seconds=300)
    )
    logger.info("[AGENT-REG] Background heartbeat flush and stale detection tasks started")

    return [app.state._heartbeat_task, app.state._stale_detection_task]


async def _startup_scheduler(app: FastAPI) -> None:
    """Initialize SchedulerService if PostgreSQL database is available (Issue #1212)."""
    if not (app.state.database_url and "postgresql" in app.state.database_url):
        return

    try:
        import asyncpg

        from nexus.pay.credits import CreditsService
        from nexus.scheduler.events import AgentStateEmitter
        from nexus.scheduler.policies.fair_share import FairShareCounter
        from nexus.scheduler.queue import TaskQueue
        from nexus.scheduler.service import SchedulerService

        # Convert SQLAlchemy URL to asyncpg DSN
        pg_dsn = app.state.database_url.replace("+asyncpg", "").replace("+psycopg2", "")
        pool = await asyncpg.create_pool(pg_dsn, min_size=2, max_size=5)
        app.state._scheduler_pool = pool

        # Create scheduled_tasks table if it doesn't exist
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    agent_id TEXT NOT NULL,
                    executor_id TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    payload JSONB NOT NULL DEFAULT '{}',
                    priority_tier SMALLINT NOT NULL DEFAULT 2,
                    deadline TIMESTAMPTZ,
                    boost_amount NUMERIC(12,6) NOT NULL DEFAULT 0,
                    boost_tiers SMALLINT NOT NULL DEFAULT 0,
                    effective_tier SMALLINT NOT NULL DEFAULT 2,
                    enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    started_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ,
                    status TEXT NOT NULL DEFAULT 'queued',
                    boost_reservation_id TEXT,
                    idempotency_key TEXT UNIQUE,
                    zone_id TEXT NOT NULL DEFAULT 'default',
                    error_message TEXT
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_dequeue
                ON scheduled_tasks (effective_tier, enqueued_at)
                WHERE status = 'queued'
            """)

            # Astraea columns (Issue #1274) — idempotent ALTER TABLE
            for col_sql in (
                "ALTER TABLE scheduled_tasks ADD COLUMN IF NOT EXISTS request_state TEXT NOT NULL DEFAULT 'pending'",
                "ALTER TABLE scheduled_tasks ADD COLUMN IF NOT EXISTS priority_class TEXT NOT NULL DEFAULT 'batch'",
                "ALTER TABLE scheduled_tasks ADD COLUMN IF NOT EXISTS executor_state TEXT NOT NULL DEFAULT 'UNKNOWN'",
                "ALTER TABLE scheduled_tasks ADD COLUMN IF NOT EXISTS estimated_service_time REAL NOT NULL DEFAULT 30.0",
            ):
                await conn.execute(col_sql)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sched_astraea_dequeue
                ON scheduled_tasks (priority_class, enqueued_at)
                WHERE status = 'queued'
            """)

        # Astraea: state emitter + fair-share (Issue #1274)
        state_emitter = AgentStateEmitter()
        fair_share = FairShareCounter()

        scheduler_service = SchedulerService(
            queue=TaskQueue(),
            db_pool=pool,
            credits_service=CreditsService(enabled=False),
            state_emitter=state_emitter,
            fair_share=fair_share,
            use_hrrn=True,
        )
        app.state.scheduler_service = scheduler_service

        # Wire emitter into AsyncAgentRegistry if available
        async_reg = getattr(app.state, "async_agent_registry", None)
        if async_reg is not None:
            async_reg._state_emitter = state_emitter

        # Initialize fair-share counters from DB
        await scheduler_service.sync_fair_share()

        logger.info("Scheduler service initialized with Astraea (PostgreSQL)")
    except ImportError as e:
        logger.debug(f"Scheduler service not available: {e}")
    except Exception as e:
        logger.warning("Failed to initialize Scheduler service: %s", e, exc_info=True)


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
        logger.warning("Task Queue runner not started: %s", e, exc_info=True)

    return None


async def _startup_workflow_engine(app: FastAPI) -> None:
    """Load workflows from persistent storage (Issue #1522)."""
    if not app.state.nexus_fs:
        return

    engine = getattr(app.state.nexus_fs, "workflow_engine", None)
    if engine and hasattr(engine, "startup"):
        try:
            await engine.startup()
            logger.info("Workflow engine started — loaded workflows from storage")
        except Exception as e:
            logger.warning(f"Workflow engine startup failed (non-fatal): {e}")

    # Expose on app.state so routers can access without reaching into NexusFS
    app.state.workflow_engine = engine
