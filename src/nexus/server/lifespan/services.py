"""Service startup/shutdown: AgentRegistry, KeyService, Sandbox, Scheduler, TaskQueue.

Extracted from fastapi_server.py (#1602).
"""

import asyncio
import logging
import os
from contextlib import suppress
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from fastapi import FastAPI

    from nexus.server.lifespan.services_container import LifespanServices

logger = logging.getLogger(__name__)


async def startup_services(app: "FastAPI", svc: "LifespanServices") -> list[asyncio.Task]:
    """Initialize application services and return background tasks.

    Covers:
    - AgentRegistry (Issue #1240)
    - KeyService (Issue #1355)
    - SandboxAuthService (Issue #1307)
    - SchedulerService (Issue #1212)
    - Task Queue Engine (Issue #574)
    """
    bg_tasks: list[asyncio.Task] = []

    await _startup_agent_registry(app, svc)
    await _startup_key_service(app, svc)
    await _startup_credential_service(app, svc)
    await _startup_reputation_delegation_from_bricks(app, svc)
    await _startup_governance(app, svc)
    await _startup_sandbox_auth(app, svc)
    await _startup_transactional_snapshot(app, svc)
    # Agent background tasks depend on agent_registry
    agent_tasks = await _startup_agent_tasks(app, svc)
    bg_tasks.extend(agent_tasks)

    _startup_task_manager(app, svc)

    await _startup_scheduler(app, svc)
    await _startup_workflow_engine(app, svc)
    await _startup_pipe_consumers(app, svc)

    return bg_tasks


async def shutdown_services(app: "FastAPI", svc: "LifespanServices") -> None:
    """Shutdown services in reverse order.

    Q3 PersistentService instances are stopped by coordinator via
    aclose() → stop_persistent_services():
    - task_runner, scheduler_service (#1598-#1601)
    - directory_grant_expander, workflow_dispatch, write_observer
    - zoekt_pipe_consumer, search_daemon

    Only non-PersistentService cleanup remains here.
    """
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

    # SandboxManager cleanup
    if app.state.sandbox_auth_service:
        logger.info(
            "[SANDBOX-AUTH] SandboxAuthService cleaned up (session-per-op, no persistent session)"
        )

    # search_daemon, workflow_dispatch, directory_grant_expander (Q3)
    # — stopped by coordinator via aclose() → stop_persistent_services()

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


async def _startup_agent_registry(app: "FastAPI", svc: "LifespanServices") -> None:
    """Initialize AgentRegistry for agent lifecycle tracking (Issue #1240)."""
    if svc.nexus_fs and svc.session_factory:
        try:
            from nexus.system_services.agents.agent_registry import AgentRegistry

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
            from nexus.system_services.agents.agent_registry import AsyncAgentRegistry

            app.state.async_agent_registry = AsyncAgentRegistry(app.state.agent_registry)

            # Issue #2172: Create AgentWarmupService with step registry
            try:
                from nexus.system_services.agents.agent_warmup import AgentWarmupService
                from nexus.system_services.agents.warmup_steps import register_standard_steps

                app.state.agent_warmup_service = AgentWarmupService(
                    agent_registry=app.state.agent_registry,
                    namespace_manager=svc.namespace_manager,
                    enabled_bricks=getattr(app.state, "enabled_bricks", frozenset()),
                    cache_store=getattr(app.state, "cache_brick", None),
                    mcp_config=None,
                )
                register_standard_steps(app.state.agent_warmup_service)
                logger.info("[WARMUP] AgentWarmupService initialized with standard steps")
            except Exception as e:
                logger.warning(
                    "[WARMUP] Failed to initialize AgentWarmupService: %s", e, exc_info=True
                )
                app.state.agent_warmup_service = None

            # Enlist with coordinator (Q1 — no lifecycle)
            coord = svc.service_coordinator
            if coord is not None:
                await coord.enlist("agent_registry", app.state.agent_registry)
                if app.state.agent_warmup_service is not None:
                    await coord.enlist("agent_warmup_service", app.state.agent_warmup_service)

            logger.info("[AGENT-REG] AgentRegistry initialized and wired")
        except Exception as e:
            logger.warning("[AGENT-REG] Failed to initialize AgentRegistry: %s", e, exc_info=True)
            app.state.agent_registry = None
            app.state.async_agent_registry = None
    else:
        app.state.agent_registry = None
        app.state.async_agent_registry = None


async def _startup_key_service(app: "FastAPI", svc: "LifespanServices") -> None:
    """Initialize KeyService for agent identity (Issue #1355)."""
    if svc.nexus_fs and svc.session_factory:
        try:
            from nexus.bricks.auth.oauth.crypto import OAuthCrypto
            from nexus.bricks.identity.crypto import IdentityCrypto
            from nexus.bricks.identity.key_service import KeyService
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

            # Enlist with coordinator (Q1 — no lifecycle)
            coord = svc.service_coordinator
            if coord is not None:
                await coord.enlist("key_service", app.state.key_service)

            logger.info("[KYA] KeyService initialized and wired")
        except Exception as e:
            logger.warning("[KYA] Failed to initialize KeyService: %s", e, exc_info=True)
            app.state.key_service = None
    else:
        app.state.key_service = None


async def _startup_credential_service(app: "FastAPI", svc: "LifespanServices") -> None:
    """Initialize CredentialService for agent capability attestation (Issue #1753).

    Depends on KeyService being initialized first. Pre-decrypts the kernel's
    signing key and creates the CapabilityIssuer, CapabilityVerifier, and
    CredentialService.
    """
    key_service = getattr(app.state, "key_service", None)
    if key_service is None or svc.record_store is None:
        app.state.credential_service = None
        return

    try:
        from sqlalchemy import Table

        from nexus.bricks.identity.credential_service import CredentialService
        from nexus.bricks.identity.credentials import CapabilityIssuer, CapabilityVerifier
        from nexus.bricks.identity.crypto import IdentityCrypto
        from nexus.storage.models.access_manifest import AccessManifestModel
        from nexus.storage.models.identity import AgentCredentialModel

        # Ensure agent_credentials and access_manifests tables exist
        _nx_engine = svc.sql_engine
        if _nx_engine is not None:
            cast(Table, AgentCredentialModel.__table__).create(_nx_engine, checkfirst=True)
            cast(Table, AccessManifestModel.__table__).create(_nx_engine, checkfirst=True)

        # Get or create a kernel identity for credential signing.
        # The kernel uses a well-known agent_id for its signing key.
        _kernel_agent_id = "__nexus_kernel__"
        kernel_key_record = key_service.ensure_keypair(_kernel_agent_id)

        # Pre-decrypt the signing key for fast issuance
        kernel_private_key = key_service.decrypt_private_key(kernel_key_record.key_id)
        kernel_public_key = IdentityCrypto.public_key_from_bytes(kernel_key_record.public_key_bytes)

        # Create issuer and verifier
        issuer = CapabilityIssuer(
            issuer_did=kernel_key_record.did,
            signing_key=kernel_private_key,
            key_id=kernel_key_record.key_id,
        )
        verifier = CapabilityVerifier()
        verifier.trust_issuer(kernel_key_record.did, kernel_public_key)

        # Create the credential service
        credential_service = CredentialService(
            record_store=svc.record_store,
            issuer=issuer,
            verifier=verifier,
            revocation_cache_ttl=30.0,
        )

        app.state.credential_service = credential_service

        # Enlist with coordinator (Q1 — no lifecycle)
        coord = svc.service_coordinator
        if coord is not None:
            await coord.enlist("credential_service", credential_service)

        logger.info(
            "[VC] CredentialService initialized (kernel DID=%s)",
            kernel_key_record.did,
        )
    except Exception as e:
        logger.warning("[VC] Failed to initialize CredentialService: %s", e, exc_info=True)
        app.state.credential_service = None


async def _startup_reputation_delegation_from_bricks(
    app: "FastAPI", svc: "LifespanServices"
) -> None:
    """Expose ReputationService and DelegationService from factory brick_dict (Issue #2131).

    The DelegationService is created in ``factory._boot_brick_services()`` and
    stored in ``BrickServices``. This function wires it onto ``app.state``
    for backward-compatible access by routers and dependencies.
    """
    if svc.nexus_fs is None:
        app.state.delegation_service = None
        return

    # Get from BrickServices (created by factory)
    brk = svc.brick_services
    app.state.reputation_service = getattr(brk, "reputation_service", None) if brk else None
    app.state.delegation_service = getattr(brk, "delegation_service", None) if brk else None

    if app.state.delegation_service is not None:
        # Wire system-tier dependencies that weren't available during factory boot
        deleg = app.state.delegation_service
        if getattr(deleg, "_namespace_manager", None) is None:
            deleg._namespace_manager = svc.namespace_manager
        if getattr(deleg, "_agent_registry", None) is None:
            deleg._agent_registry = app.state.agent_registry
        logger.info("[DELEGATION] DelegationService wired from brick_dict")

    # Enlist with coordinator (Q1 — no lifecycle)
    coord = svc.service_coordinator
    if coord is not None:
        if app.state.reputation_service is not None:
            await coord.enlist("reputation_service", app.state.reputation_service)
        if app.state.delegation_service is not None:
            await coord.enlist("delegation_service", app.state.delegation_service)


async def _startup_governance(app: "FastAPI", svc: "LifespanServices") -> None:
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

    # Enlist with coordinator (Q1 — no lifecycle)
    coord = svc.service_coordinator
    if coord is not None:
        for _name, _attr in (
            ("governance_anomaly", app.state.governance_anomaly_service),
            ("governance_collusion", app.state.governance_collusion_service),
            ("governance_graph", app.state.governance_graph_service),
            ("governance_response", app.state.governance_response_service),
        ):
            if _attr is not None:
                await coord.enlist(_name, _attr)


async def _startup_sandbox_auth(app: "FastAPI", svc: "LifespanServices") -> None:
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

        # Enlist agent_event_log with coordinator (Q1 — no lifecycle)
        coord = svc.service_coordinator
        if coord is not None:
            await coord.enlist("agent_event_log", app.state.agent_event_log)

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

        # Enlist with coordinator (Q1 — no lifecycle)
        coord = svc.service_coordinator
        if coord is not None:
            await coord.enlist("sandbox_auth", app.state.sandbox_auth_service)

        logger.info("[SANDBOX-AUTH] SandboxAuthService initialized")
    except Exception as e:
        logger.warning(
            "[SANDBOX-AUTH] Failed to initialize SandboxAuthService: %s", e, exc_info=True
        )


async def _startup_transactional_snapshot(app: "FastAPI", svc: "LifespanServices") -> None:
    """Expose TransactionalSnapshotService on app.state for REST API (Issue #1752)."""
    snap_svc = svc.snapshot_service
    app.state.transactional_snapshot_service = snap_svc
    if snap_svc is not None:
        # Enlist with coordinator (Q1 — no lifecycle)
        coord = svc.service_coordinator
        if coord is not None:
            await coord.enlist("transactional_snapshot", snap_svc)
        logger.info("[SNAPSHOT] TransactionalSnapshotService wired to app.state")
    else:
        logger.debug("[SNAPSHOT] TransactionalSnapshotService not available")


async def _startup_agent_tasks(app: "FastAPI", svc: "LifespanServices") -> list[asyncio.Task]:
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
            from nexus.system_services.agents.eviction_manager import EvictionManager
            from nexus.system_services.agents.eviction_policy import LRUEvictionPolicy
            from nexus.system_services.agents.resource_monitor import ResourceMonitor

            resource_monitor = ResourceMonitor(tuning=_eviction_tuning)
            eviction_policy = LRUEvictionPolicy()
            app.state.eviction_manager = EvictionManager(
                registry=app.state.agent_registry,
                monitor=resource_monitor,
                policy=eviction_policy,
                tuning=_eviction_tuning,
            )
            # Enlist fallback eviction_manager (Q1 — no lifecycle)
            coord = svc.service_coordinator
            if coord is not None:
                await coord.enlist("eviction_manager", app.state.eviction_manager)
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


def _startup_task_manager(app: "FastAPI", svc: "LifespanServices") -> None:
    """Initialize TaskManagerService backed by NexusFS."""
    if svc.nexus_fs is None:
        app.state.task_manager_service = None
        return

    try:
        from nexus.bricks.task_manager.service import TaskManagerService

        task_svc = TaskManagerService(nexus_fs=svc.nexus_fs)
        app.state.task_manager_service = task_svc
        logger.info("[TASK-MGR] TaskManagerService initialized")

        task_write_hook = getattr(svc.nexus_fs, "_task_write_hook", None)
        if task_write_hook is not None:
            app.state.task_write_hook = task_write_hook

            # Wire up DT_PIPE dispatch consumer for task signals
            from nexus.bricks.task_manager.dispatch_consumer import TaskDispatchPipeConsumer

            dispatch_consumer = TaskDispatchPipeConsumer()
            dispatch_consumer.set_task_service(task_svc)
            task_write_hook.register_handler(dispatch_consumer)
            app.state.task_dispatch_consumer = dispatch_consumer

        # Set up DT_STREAM for SSE notifications
        stream_manager = getattr(svc.nexus_fs, "_stream_manager", None)
        if stream_manager is not None:
            from nexus.core.stream import StreamError

            _SSE_STREAM_PATH = "/nexus/streams/task-events"
            try:
                stream_manager.create(_SSE_STREAM_PATH, capacity=65_536, owner_id="kernel")
            except StreamError:
                stream_manager.open(_SSE_STREAM_PATH, capacity=65_536)
            app.state.task_stream_manager = stream_manager
            logger.info("[TASK-MGR] DT_STREAM for SSE initialized at %s", _SSE_STREAM_PATH)
    except Exception as e:
        logger.warning("[TASK-MGR] Failed to initialize TaskManagerService: %s", e, exc_info=True)
        app.state.task_manager_service = None


async def _startup_scheduler(app: "FastAPI", svc: "LifespanServices") -> None:
    """Initialize factory-created SchedulerService with async pool (Issue #2195, #2360).

    The SchedulerService is constructed by ``factory._boot_system_services()``
    with ``db_pool=None`` (sync). This lifespan function completes the
    two-phase init: creates the asyncpg pool and calls ``scheduler.initialize(pool)``.

    If the factory fell back to InMemoryScheduler (no PostgreSQL), the
    scheduler is still registered on app.state for API access.
    """
    scheduler = svc.scheduler_service

    if scheduler is None:
        return

    # Always expose the scheduler on app.state (Issue #2360)
    app.state.scheduler_service = scheduler

    # Q3 PersistentService — coordinator auto-calls start() (no-op for scheduler)
    coord = svc.service_coordinator
    if coord is not None:
        await coord.enlist("scheduler_service", scheduler)

    # InMemoryScheduler doesn't need PostgreSQL init
    from nexus.system_services.scheduler.in_memory import InMemoryScheduler

    if isinstance(scheduler, InMemoryScheduler):
        logger.info("Scheduler service started (InMemoryScheduler fallback)")
        return

    if not svc.database_url:
        logger.debug("Scheduler skipped: no database_url")
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
        # Design decision: scheduler uses its own asyncpg pool for isolation.
        # Scheduler queries don't compete with application queries.
        # 2-5 extra connections is negligible cost for the isolation benefit.
        pool = await asyncpg.create_pool(pg_dsn, min_size=_min_size, max_size=_max_size)
        app.state._scheduler_pool = pool

        # Both SchedulerService and InMemoryScheduler implement initialize()
        # as part of the SchedulerProtocol contract.
        await scheduler.initialize(pool)

        logger.info("Scheduler service initialized with Astraea (two-phase, PostgreSQL)")
    except ImportError as e:
        logger.debug("Scheduler async init not available: %s", e)
    except Exception as e:
        logger.warning("Failed to initialize Scheduler: %s", e, exc_info=True)

    return None


async def _startup_workflow_engine(app: "FastAPI", svc: "LifespanServices") -> None:
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
            coord = svc.service_coordinator
            if coord is not None:
                await coord.enlist("workflow_dispatch", wds)
            else:
                await wds.start()
            logger.info("Workflow dispatch service started")
        except Exception as e:
            logger.warning("Workflow dispatch service start failed (non-fatal): %s", e)


async def _startup_pipe_consumers(app: "FastAPI", svc: "LifespanServices") -> None:
    """Inject PipeManager and start DT_PIPE consumers (Issue #809, #810).

    Two consumers are started here:
    1. PipedRecordStoreWriteObserver — async RecordStore sync via kernel IPC
    2. ZoektPipeConsumer — async Zoekt index notifications via kernel IPC

    Both follow the deferred-injection pattern: created in factory without
    PipeManager, then PipeManager is injected here and start() spawns the
    background consumer task.
    """
    pipe_manager = svc.pipe_manager
    if pipe_manager is None:
        return

    coord = svc.service_coordinator

    # Issue #809: PipedRecordStoreWriteObserver
    wo = svc.write_observer
    if wo is not None and hasattr(wo, "set_pipe_manager"):
        try:
            wo.set_pipe_manager(pipe_manager)
            if coord is not None:
                await coord.enlist("write_observer", wo)
            else:
                await wo.start()
            app.state.write_observer = wo
            logger.info("[PIPE] PipedRecordStoreWriteObserver started")
        except Exception as e:
            logger.warning(
                "[PIPE] PipedRecordStoreWriteObserver start failed: %s", e, exc_info=True
            )

    # Issue #810: ZoektPipeConsumer
    zpc = svc.zoekt_pipe_consumer
    if zpc is not None and hasattr(zpc, "set_pipe_manager"):
        try:
            zpc.set_pipe_manager(pipe_manager)
            if coord is not None:
                await coord.enlist("zoekt_pipe_consumer", zpc)
            else:
                await zpc.start()
            app.state.zoekt_pipe_consumer = zpc
            logger.info("[PIPE] ZoektPipeConsumer started")
        except Exception as e:
            logger.warning("[PIPE] ZoektPipeConsumer start failed: %s", e, exc_info=True)

    # TaskDispatchPipeConsumer
    tdc = getattr(app.state, "task_dispatch_consumer", None)
    if tdc is not None and hasattr(tdc, "set_pipe_manager"):
        try:
            tdc.set_pipe_manager(pipe_manager)
            await tdc.start()
            logger.info("[PIPE] TaskDispatchPipeConsumer started")
        except Exception as e:
            logger.warning("[PIPE] TaskDispatchPipeConsumer start failed: %s", e, exc_info=True)

    # write_observer (Q3), zoekt_pipe_consumer (Q3) — stopped by coordinator via aclose()
