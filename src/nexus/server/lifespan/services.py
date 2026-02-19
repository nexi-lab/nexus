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
    _startup_reputation_service(app)
    _startup_delegation_service(app)
    _startup_sandbox_auth(app)
    _startup_transactional_snapshot(app)
    _startup_rlm_service(app)

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
    # Issue #625: Stop workflow dispatch consumer
    wds = getattr(app.state, "workflow_dispatch", None)
    if wds is not None:
        try:
            await wds.stop()
            logger.info("Workflow dispatch service stopped")
        except Exception as e:
            logger.warning("Error stopping workflow dispatch service: %s", e, exc_info=True)

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

    # Shutdown RLM thread pool (Issue #1306)
    rlm_service = getattr(app.state, "rlm_service", None)
    if rlm_service is not None:
        try:
            rlm_service.shutdown()
            logger.info("[RLM] Thread pool shut down")
        except Exception as e:
            logger.warning("[RLM] Error shutting down thread pool: %s", e, exc_info=True)

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
            from nexus.services.agents.agent_registry import AgentRegistry

            _bg = getattr(getattr(app.state, "profile_tuning", None), "background_task", None)
            app.state.agent_registry = AgentRegistry(
                session_factory=app.state.nexus_fs.SessionLocal,
                entity_registry=getattr(app.state.nexus_fs, "_entity_registry", None),
                flush_interval=_bg.heartbeat_flush_interval if _bg else 60,
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
            from nexus.auth.oauth.crypto import OAuthCrypto
            from nexus.identity.crypto import IdentityCrypto
            from nexus.identity.key_service import KeyService
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


def _startup_reputation_service(app: FastAPI) -> None:
    """Initialize ReputationService singleton for trust routing (#1619)."""
    if not (app.state.nexus_fs and getattr(app.state.nexus_fs, "SessionLocal", None)):
        app.state.reputation_service = None
        return

    try:
        from nexus.services.reputation.reputation_service import ReputationService

        app.state.reputation_service = ReputationService(
            session_factory=app.state.nexus_fs.SessionLocal,
        )
        logger.info("[REPUTATION] ReputationService initialized (singleton)")
    except Exception as e:
        logger.warning("[REPUTATION] Failed to initialize: %s", e, exc_info=True)
        app.state.reputation_service = None


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
        reputation_service = getattr(app.state, "reputation_service", None)

        app.state.delegation_service = DelegationService(
            session_factory=app.state.nexus_fs.SessionLocal,
            rebac_manager=rebac_manager,
            namespace_manager=namespace_manager,
            entity_registry=entity_registry,
            agent_registry=agent_registry,
            reputation_service=reputation_service,
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
        from nexus.bricks.sandbox.auth_service import SandboxAuthService
        from nexus.bricks.sandbox.sandbox_manager import SandboxManager

        session_factory = getattr(app.state.nexus_fs, "SessionLocal", None)
        if not (session_factory and callable(session_factory)):
            return

        # Get AgentEventLog from factory (preferred) or create fallback
        _brk = getattr(app.state.nexus_fs, "_brick_services", None)
        _factory_event_log = getattr(_brk, "agent_event_log", None) if _brk else None
        if _factory_event_log is not None:
            app.state.agent_event_log = _factory_event_log
        else:
            from nexus.bricks.sandbox.events import AgentEventLog

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
                from nexus.rebac.namespace_factory import (
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


def _startup_transactional_snapshot(app: FastAPI) -> None:
    """Expose TransactionalSnapshotService on app.state for REST API (Issue #1752)."""
    svc = getattr(app.state.nexus_fs, "_snapshot_service", None) if app.state.nexus_fs else None
    app.state.transactional_snapshot_service = svc
    if svc is not None:
        logger.info("[SNAPSHOT] TransactionalSnapshotService wired to app.state")
    else:
        logger.debug("[SNAPSHOT] TransactionalSnapshotService not available")


def _startup_rlm_service(app: FastAPI) -> None:
    """Initialize RLM inference service (Issue #1306).

    Requires SandboxAuthService (for SandboxManager) and an LLM provider.
    Falls back gracefully if either is unavailable — the /api/v2/rlm/infer
    endpoint will return 503.
    """
    sandbox_auth = getattr(app.state, "sandbox_auth_service", None)
    if sandbox_auth is None:
        logger.debug("[RLM] SandboxAuthService not available, RLM service skipped")
        return

    sandbox_mgr = getattr(sandbox_auth, "_sandbox_manager", None)
    if sandbox_mgr is None:
        logger.debug("[RLM] SandboxManager not available, RLM service skipped")
        return

    llm_provider = getattr(app.state.nexus_fs, "_llm_provider", None)

    try:
        from nexus.rlm.service import RLMInferenceService

        nexus_api_url = os.environ.get("NEXUS_API_URL", "http://localhost:2026")
        max_concurrent = int(os.environ.get("NEXUS_RLM_MAX_CONCURRENT", "8"))

        app.state.rlm_service = RLMInferenceService(
            sandbox_manager=sandbox_mgr,
            llm_provider=llm_provider,
            nexus_api_url=nexus_api_url,
            max_concurrent=max_concurrent,
        )
        logger.info("[RLM] RLMInferenceService initialized (max_concurrent=%d)", max_concurrent)
    except Exception as e:
        logger.warning("[RLM] Failed to initialize RLMInferenceService: %s", e, exc_info=True)


def _startup_agent_tasks(app: FastAPI) -> list[asyncio.Task]:
    """Start agent heartbeat and stale detection background tasks (Issue #1240)."""
    if not app.state.agent_registry:
        return []

    from nexus.server.background_tasks import (
        heartbeat_flush_task,
        stale_agent_detection_task,
    )

    _bg_tuning = app.state.profile_tuning.background_task
    app.state._heartbeat_task = asyncio.create_task(
        heartbeat_flush_task(
            app.state.agent_registry,
            interval_seconds=_bg_tuning.heartbeat_flush_interval,
        )
    )
    app.state._stale_detection_task = asyncio.create_task(
        stale_agent_detection_task(
            app.state.agent_registry,
            interval_seconds=_bg_tuning.stale_agent_check_interval,
        )
    )
    logger.info("[AGENT-REG] Background heartbeat flush and stale detection tasks started")

    return [app.state._heartbeat_task, app.state._stale_detection_task]


async def _startup_scheduler(app: FastAPI) -> None:
    """Initialize SchedulerService if PostgreSQL database is available (Issue #1212)."""
    if not (app.state.database_url and "postgresql" in app.state.database_url):
        return

    try:
        import asyncpg

        from nexus.bricks.pay.credits import CreditsService
        from nexus.bricks.scheduler.events import AgentStateEmitter
        from nexus.bricks.scheduler.policies.fair_share import FairShareCounter
        from nexus.bricks.scheduler.queue import TaskQueue
        from nexus.bricks.scheduler.service import SchedulerService

        # Convert SQLAlchemy URL to asyncpg DSN
        pg_dsn = app.state.database_url.replace("+asyncpg", "").replace("+psycopg2", "")
        _pool_tuning = app.state.profile_tuning.pool
        pool = await asyncpg.create_pool(
            pg_dsn,
            min_size=_pool_tuning.asyncpg_min_size,
            max_size=_pool_tuning.asyncpg_max_size,
        )
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

        # Wire hook cleanup handler into state emitter (Issue #1257)
        _nx = getattr(app.state, "nexus_fs", None)
        _sys = getattr(_nx, "_system_services", None) if _nx else None
        scoped_hook_engine = getattr(_sys, "scoped_hook_engine", None) if _sys else None
        if scoped_hook_engine is not None:
            from nexus.services.hook_engine import create_agent_cleanup_handler

            state_emitter.add_handler(create_agent_cleanup_handler(scoped_hook_engine))
            logger.debug("Hook cleanup handler registered on AgentStateEmitter")

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

            _task_workers = app.state.profile_tuning.concurrency.task_runner_workers
            runner = AsyncTaskRunner(engine=engine, max_workers=_task_workers)
            service.set_runner(runner)
            app.state.task_runner = runner
            task = asyncio.create_task(runner.run())
            logger.info("Task Queue runner started (%d workers)", _task_workers)
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

    # Issue #625: Start workflow dispatch consumer (DT_PIPE → workflow engine)
    wds = getattr(app.state, "workflow_dispatch", None)
    if wds is not None:
        try:
            await wds.start()
            logger.info("Workflow dispatch service started")
        except Exception as e:
            logger.warning("Workflow dispatch service start failed (non-fatal): %s", e)
