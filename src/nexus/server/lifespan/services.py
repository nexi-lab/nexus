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

    from nexus.server.lifespan.services_container import LifespanServices

logger = logging.getLogger(__name__)


async def startup_services(app: FastAPI, svc: LifespanServices) -> list[asyncio.Task]:
    """Initialize application services and return background tasks.

    Covers:
    - AgentRegistry (Issue #1240)
    - KeyService (Issue #1355)
    - SandboxAuthService (Issue #1307)
    - SchedulerService (Issue #1212)
    - Task Queue Engine (Issue #574)
    """
    bg_tasks: list[asyncio.Task] = []

    _startup_agent_registry(app, svc)
    _startup_key_service(app, svc)
    _startup_reputation_delegation_from_bricks(app, svc)
    _startup_governance(app, svc)
    _startup_sandbox_auth(app, svc)
    _startup_transactional_snapshot(app, svc)
    _startup_rlm_service(app, svc)

    # Agent background tasks depend on agent_registry
    agent_tasks = _startup_agent_tasks(app, svc)
    bg_tasks.extend(agent_tasks)

    await _startup_scheduler(app, svc)
    task_runner_task = _startup_task_queue(app, svc)
    if task_runner_task:
        bg_tasks.append(task_runner_task)

    await _startup_workflow_engine(app, svc)

    return bg_tasks


async def shutdown_services(app: FastAPI, svc: LifespanServices) -> None:
    """Shutdown services in reverse order."""
    # Issue #625: Stop workflow dispatch consumer
    wds = app.state.workflow_dispatch
    if wds is not None:
        try:
            await wds.stop()
            logger.info("Workflow dispatch service stopped")
        except Exception as e:
            logger.warning("Error stopping workflow dispatch service: %s", e, exc_info=True)

    # Stop Task Queue runner (Issue #574)
    task_runner = app.state.task_runner
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

    # Cancel agent background tasks and final flush (Issue #1240, #2170)
    heartbeat_task = getattr(app.state, "_heartbeat_task", None)
    stale_detection_task = getattr(app.state, "_stale_detection_task", None)
    eviction_task = getattr(app.state, "_eviction_task", None)
    cleanup_task = getattr(app.state, "_checkpoint_cleanup_task", None)
    for task_ref in (heartbeat_task, stale_detection_task, eviction_task, cleanup_task):
        if task_ref and not task_ref.done():
            task_ref.cancel()
            with suppress(asyncio.CancelledError):
                await task_ref
    app.state._heartbeat_task = None
    app.state._stale_detection_task = None
    app.state._eviction_task = None
    app.state._checkpoint_cleanup_task = None

    if app.state.agent_registry:
        try:
            app.state.agent_registry.flush_heartbeats()
            logger.info("[AGENT-REG] Final heartbeat flush completed")
        except Exception:
            logger.warning("[AGENT-REG] Final heartbeat flush failed", exc_info=True)

    # Shutdown RLM thread pool (Issue #1306)
    rlm_service = app.state.rlm_service
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
    if app.state.directory_grant_expander:
        try:
            app.state.directory_grant_expander.stop()
            logger.info("DirectoryGrantExpander worker stopped")
        except Exception as e:
            logger.warning("Error stopping DirectoryGrantExpander: %s", e, exc_info=True)

    # Cancel pending event tasks in NexusFS (Issue #913)
    if svc.nexus_fs and hasattr(svc.nexus_fs, "_event_tasks"):
        event_tasks = svc.nexus_fs._event_tasks.copy()
        for task in event_tasks:
            task.cancel()
        if event_tasks:
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*event_tasks, return_exceptions=True)
            logger.info("Cancelled %d pending event tasks", len(event_tasks))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _startup_agent_registry(app: FastAPI, svc: LifespanServices) -> None:
    """Initialize AgentRegistry for agent lifecycle tracking (Issue #1240)."""
    if svc.nexus_fs and svc.session_factory:
        try:
            from nexus.services.agents.agent_registry import AgentRegistry

            _bg = getattr(svc.profile_tuning, "background_task", None)
            app.state.agent_registry = AgentRegistry(
                record_store=svc.record_store,
                entity_registry=svc.entity_registry,
                flush_interval=_bg.heartbeat_flush_interval if _bg else 60,
            )
            # Inject into NexusFS for RPC methods
            svc.nexus_fs._agent_registry = app.state.agent_registry

            # Wire into sync PermissionEnforcer
            perm_enforcer = svc.permission_enforcer
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


def _startup_key_service(app: FastAPI, svc: LifespanServices) -> None:
    """Initialize KeyService for agent identity (Issue #1355)."""
    if svc.nexus_fs and svc.session_factory:
        try:
            from nexus.auth.oauth.crypto import OAuthCrypto
            from nexus.identity.crypto import IdentityCrypto
            from nexus.identity.key_service import KeyService
            from nexus.storage.models.identity import AgentKeyModel

            # Ensure agent_keys table exists
            _nx_engine = svc.sql_engine
            if _nx_engine is not None:
                from sqlalchemy import Table

                cast(Table, AgentKeyModel.__table__).create(_nx_engine, checkfirst=True)

            # Reuse OAuthCrypto for Fernet encryption of private keys
            _enc_key = os.environ.get("NEXUS_OAUTH_ENCRYPTION_KEY", "").strip() or None
            _identity_record_store = svc.record_store
            _identity_oauth_crypto = OAuthCrypto(
                encryption_key=_enc_key, record_store=_identity_record_store
            )
            _identity_crypto = IdentityCrypto(oauth_crypto=_identity_oauth_crypto)

            app.state.key_service = KeyService(
                record_store=svc.record_store,
                crypto=_identity_crypto,
            )
            # Inject into NexusFS for register_agent integration
            svc.nexus_fs._key_service = app.state.key_service

            logger.info("[KYA] KeyService initialized and wired")
        except Exception as e:
            logger.warning("[KYA] Failed to initialize KeyService: %s", e, exc_info=True)
            app.state.key_service = None
    else:
        app.state.key_service = None


def _startup_reputation_delegation_from_bricks(app: FastAPI, svc: LifespanServices) -> None:
    """Expose ReputationService and DelegationService from factory brick_dict (Issue #2131).

    These services are now created in ``factory._boot_brick_services()`` and
    stored in ``BrickServices``. This function wires them onto ``app.state``
    for backward-compatible access by routers and dependencies.
    """
    if svc.nexus_fs is None:
        app.state.reputation_service = None
        app.state.delegation_service = None
        return

    # Get from BrickServices (created by factory)
    brk = svc.brick_services
    app.state.reputation_service = getattr(brk, "reputation_service", None) if brk else None
    app.state.delegation_service = getattr(brk, "delegation_service", None) if brk else None

    if app.state.reputation_service is not None:
        logger.info("[REPUTATION] ReputationService wired from brick_dict")
    if app.state.delegation_service is not None:
        # Wire system-tier dependencies that weren't available during factory boot
        deleg = app.state.delegation_service
        if getattr(deleg, "_namespace_manager", None) is None:
            deleg._namespace_manager = svc.namespace_manager
        if getattr(deleg, "_agent_registry", None) is None:
            deleg._agent_registry = app.state.agent_registry
        logger.info("[DELEGATION] DelegationService wired from brick_dict")


def _startup_governance(app: FastAPI, svc: LifespanServices) -> None:
    """Expose governance brick services from factory BrickServices (Issue #2129).

    Governance services are created in ``factory._boot_brick_services()`` and
    stored in ``BrickServices``. This function wires them onto ``app.state``
    for backward-compatible access by the governance router.
    """
    if svc.nexus_fs is None:
        return

    brk = svc.brick_services
    app.state.governance_anomaly_service = (
        getattr(brk, "governance_anomaly_service", None) if brk else None
    )
    app.state.governance_collusion_service = (
        getattr(brk, "governance_collusion_service", None) if brk else None
    )
    app.state.governance_graph_service = (
        getattr(brk, "governance_graph_service", None) if brk else None
    )
    app.state.governance_response_service = (
        getattr(brk, "governance_response_service", None) if brk else None
    )

    if app.state.governance_response_service is not None:
        logger.info("[GOV] Governance services wired from brick_dict")


def _startup_sandbox_auth(app: FastAPI, svc: LifespanServices) -> None:
    """Initialize SandboxAuthService for authenticated sandbox creation (Issue #1307)."""
    if svc.nexus_fs and not app.state.agent_registry:
        logger.info(
            "[SANDBOX-AUTH] AgentRegistry not available, SandboxAuthService will not be initialized"
        )
    if not (svc.nexus_fs and app.state.agent_registry):
        return

    try:
        from nexus.bricks.sandbox.auth_service import SandboxAuthService
        from nexus.bricks.sandbox.sandbox_manager import SandboxManager
        from nexus.storage.repositories.agent_event_log import (
            SQLAlchemyAgentEventLog as AgentEventLog,
        )

        _sandbox_rs = svc.record_store
        session_factory = svc.session_factory
        if not (_sandbox_rs and session_factory and callable(session_factory)):
            return

        # Get AgentEventLog from factory (preferred) or create fallback
        brk = svc.brick_services
        _factory_event_log = getattr(brk, "agent_event_log", None) if brk else None
        if _factory_event_log is not None:
            app.state.agent_event_log = _factory_event_log
        else:
            app.state.agent_event_log = AgentEventLog(record_store=_sandbox_rs)

        # Create SandboxManager
        sandbox_config = svc.nexus_config
        sandbox_mgr = SandboxManager(
            record_store=_sandbox_rs,
            e2b_api_key=os.getenv("E2B_API_KEY"),
            e2b_team_id=os.getenv("E2B_TEAM_ID"),
            e2b_template_id=os.getenv("E2B_TEMPLATE_ID"),
            config=sandbox_config,
        )

        # Attach smart router for Monty -> Docker -> E2B routing (Issue #1317)
        sandbox_mgr.wire_router()

        # Get NamespaceManager if available (best-effort)
        namespace_manager = None
        sync_rebac = svc.rebac_manager
        if sync_rebac:
            try:
                from nexus.bricks.rebac.namespace_factory import (
                    create_namespace_manager,
                )

                ns_record_store = svc.record_store
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


def _startup_transactional_snapshot(app: FastAPI, svc: LifespanServices) -> None:
    """Expose TransactionalSnapshotService on app.state for REST API (Issue #1752)."""
    snap_svc = svc.snapshot_service
    app.state.transactional_snapshot_service = snap_svc
    if snap_svc is not None:
        logger.info("[SNAPSHOT] TransactionalSnapshotService wired to app.state")
    else:
        logger.debug("[SNAPSHOT] TransactionalSnapshotService not available")


def _startup_rlm_service(app: FastAPI, svc: LifespanServices) -> None:
    """Initialize RLM inference service (Issue #1306).

    Requires SandboxAuthService (for SandboxManager) and an LLM provider.
    Falls back gracefully if either is unavailable — the /api/v2/rlm/infer
    endpoint will return 503.
    """
    sandbox_auth = app.state.sandbox_auth_service
    if sandbox_auth is None:
        logger.debug("[RLM] SandboxAuthService not available, RLM service skipped")
        return

    sandbox_mgr = getattr(sandbox_auth, "_sandbox_manager", None)
    if sandbox_mgr is None:
        logger.debug("[RLM] SandboxManager not available, RLM service skipped")
        return

    llm_provider = svc.llm_provider

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


def _startup_agent_tasks(app: FastAPI, svc: LifespanServices) -> list[asyncio.Task]:
    """Start agent heartbeat and stale detection background tasks (Issue #1240)."""
    if not app.state.agent_registry:
        return []

    from nexus.server.background_tasks import (
        heartbeat_flush_task,
        stale_agent_detection_task,
    )

    _bg_tuning = svc.profile_tuning.background_task
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
            threshold_seconds=_bg_tuning.stale_agent_threshold,
        )
    )
    logger.info("[AGENT-REG] Background heartbeat flush and stale detection tasks started")

    tasks = [app.state._heartbeat_task, app.state._stale_detection_task]

    # Agent eviction under resource pressure (Issue #2170)
    # Use factory-constructed EvictionManager (DRY — avoid duplicate construction)
    app.state._eviction_task = None
    app.state._checkpoint_cleanup_task = None
    _factory_em = svc.eviction_manager
    _eviction_tuning = getattr(svc.profile_tuning, "eviction", None)
    if _factory_em is not None and _eviction_tuning is not None:
        try:
            from nexus.server.background_tasks import (
                agent_eviction_task,
                checkpoint_cleanup_task,
            )

            app.state._eviction_task = asyncio.create_task(
                agent_eviction_task(
                    _factory_em,
                    interval_seconds=_eviction_tuning.eviction_poll_interval_seconds,
                )
            )
            tasks.append(app.state._eviction_task)

            # Stale checkpoint cleanup (Issue #2170, #16A)
            app.state._checkpoint_cleanup_task = asyncio.create_task(
                checkpoint_cleanup_task(
                    app.state.agent_registry,
                    interval_seconds=_eviction_tuning.checkpoint_cleanup_interval_seconds,
                    max_age_seconds=_eviction_tuning.checkpoint_max_age_seconds,
                )
            )
            tasks.append(app.state._checkpoint_cleanup_task)
            logger.info("[EVICTION] Background eviction + checkpoint cleanup tasks started")
        except Exception:
            logger.warning("[EVICTION] Failed to start eviction tasks", exc_info=True)
    elif _eviction_tuning is not None:
        # Fallback: construct EvictionManager here if factory didn't create one
        try:
            from nexus.server.background_tasks import agent_eviction_task
            from nexus.services.agents.eviction_manager import EvictionManager
            from nexus.services.agents.eviction_policy import LRUEvictionPolicy
            from nexus.services.agents.resource_monitor import ResourceMonitor

            resource_monitor = ResourceMonitor(tuning=_eviction_tuning)
            eviction_policy = LRUEvictionPolicy()
            app.state.eviction_manager = EvictionManager(
                registry=app.state.agent_registry,
                monitor=resource_monitor,
                policy=eviction_policy,
                tuning=_eviction_tuning,
            )
            app.state._eviction_task = asyncio.create_task(
                agent_eviction_task(
                    app.state.eviction_manager,
                    interval_seconds=_eviction_tuning.eviction_poll_interval_seconds,
                )
            )
            tasks.append(app.state._eviction_task)
            logger.info("[EVICTION] Background agent eviction task started (fallback)")
        except Exception:
            logger.warning("[EVICTION] Failed to initialize eviction manager", exc_info=True)

    return tasks


async def _startup_scheduler(app: FastAPI, svc: LifespanServices) -> None:
    """Initialize factory-created SchedulerService with async pool (Issue #2195).

    The SchedulerService is constructed by ``factory._boot_system_services()``
    with ``db_pool=None`` (sync). This lifespan function completes the
    two-phase init: creates the asyncpg pool and calls ``scheduler.initialize(pool)``.
    """
    scheduler = svc.scheduler_service

    if scheduler is None or not svc.database_url:
        return

    try:
        import asyncpg

        from nexus.core.db_utils import sqlalchemy_url_to_asyncpg_dsn

        pg_dsn = sqlalchemy_url_to_asyncpg_dsn(svc.database_url)
        try:
            _min_size = svc.profile_tuning.pool.asyncpg_min_size
            _max_size = svc.profile_tuning.pool.asyncpg_max_size
        except AttributeError:
            _min_size, _max_size = 2, 5
            logger.warning(
                "Pool tuning config missing asyncpg size attrs, "
                "falling back to defaults (min=%d, max=%d)",
                _min_size,
                _max_size,
            )
        pool = await asyncpg.create_pool(pg_dsn, min_size=_min_size, max_size=_max_size)
        app.state._scheduler_pool = pool

        await scheduler.initialize(pool)
        app.state.scheduler_service = scheduler

        # Wire emitter into AsyncAgentRegistry if available
        state_emitter = getattr(scheduler, "_state_emitter", None)
        if state_emitter is not None:
            async_reg = getattr(app.state, "async_agent_registry", None)
            if async_reg is not None:
                async_reg._state_emitter = state_emitter

            # Wire hook cleanup handler into state emitter (Issue #1257)
            scoped_hook_engine = svc.scoped_hook_engine
            if scoped_hook_engine is not None:
                from nexus.system_services.lifecycle.hook_engine import create_agent_cleanup_handler

                state_emitter.add_handler(create_agent_cleanup_handler(scoped_hook_engine))
                logger.debug("Hook cleanup handler registered on AgentStateEmitter")

        logger.info("Scheduler service initialized with Astraea (two-phase, PostgreSQL)")
    except ImportError as e:
        logger.debug("Scheduler async init not available: %s", e)
    except Exception as e:
        logger.warning("Failed to initialize Scheduler: %s", e, exc_info=True)


def _startup_task_queue(app: FastAPI, svc: LifespanServices) -> asyncio.Task | None:
    """Start Task Queue Engine background worker (Issue #574)."""
    if not svc.nexus_fs:
        return None

    try:
        from nexus.tasks import is_available

        if is_available():
            service = svc.nexus_fs.task_queue_service
            engine = service.get_engine()

            from nexus.tasks.runner import AsyncTaskRunner

            _task_workers = svc.profile_tuning.concurrency.task_runner_workers
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


async def _startup_workflow_engine(app: FastAPI, svc: LifespanServices) -> None:
    """Load workflows from persistent storage (Issue #1522)."""
    if not svc.nexus_fs:
        return

    engine = svc.workflow_engine
    if engine and hasattr(engine, "startup"):
        try:
            await engine.startup()
            logger.info("Workflow engine started — loaded workflows from storage")
        except Exception as e:
            logger.warning("Workflow engine startup failed (non-fatal): %s", e)

    # Expose on app.state so routers can access without reaching into NexusFS
    app.state.workflow_engine = engine

    # Issue #625: Start workflow dispatch consumer (DT_PIPE → workflow engine)
    wds = app.state.workflow_dispatch
    if wds is not None:
        try:
            await wds.start()
            logger.info("Workflow dispatch service started")
        except Exception as e:
            logger.warning("Workflow dispatch service start failed (non-fatal): %s", e)
