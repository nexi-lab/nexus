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

    _startup_agent_lifecycle(app, svc)
    _startup_key_service(app, svc)
    _startup_credential_service(app, svc)
    _startup_task_manager(app, svc)
    _startup_delegation_from_bricks(app, svc)
    _startup_governance(app, svc)
    _startup_sandbox_auth(app, svc)
    _startup_transactional_snapshot(app, svc)
    _startup_access_manifest(app, svc)

    # Agent background tasks depend on agent_registry
    agent_tasks = _startup_agent_tasks(app, svc)
    bg_tasks.extend(agent_tasks)

    await _startup_scheduler(app, svc)
    await _startup_workflow_engine(app, svc)
    await _startup_pipe_consumers(app, svc)

    return bg_tasks


async def shutdown_services(app: "FastAPI", svc: "LifespanServices") -> None:
    """Shutdown services in reverse order."""
    # Issue #809/#810: Stop DT_PIPE consumers
    await _shutdown_pipe_consumers(app)

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

    # Shutdown scheduler (Issue #1212, #2360)
    # Both SchedulerService and InMemoryScheduler implement shutdown().
    scheduler_svc = getattr(app.state, "scheduler_service", None)
    if scheduler_svc is not None:
        try:
            await scheduler_svc.shutdown()
            logger.info("Scheduler service shut down")
        except Exception as e:
            logger.warning("Error shutting down scheduler: %s", e, exc_info=True)

    # Cancel agent eviction task (Issue #2170)
    eviction_task = getattr(app.state, "_eviction_task", None)
    if eviction_task and not eviction_task.done():
        eviction_task.cancel()
        with suppress(asyncio.CancelledError):
            await eviction_task
    app.state._eviction_task = None

    # SandboxManager cleanup
    if app.state.sandbox_auth_service:
        logger.info(
            "[SANDBOX-AUTH] SandboxAuthService cleaned up (session-per-op, no persistent session)"
        )

    # Shutdown Search Daemon (Issue #951)
    if app.state.search_daemon:
        try:
            await app.state.search_daemon.shutdown()
            logger.info("Search Daemon stopped")
        except Exception as e:
            logger.warning("Error shutting down Search Daemon: %s", e, exc_info=True)

    # Dispose loop-local path-context engines created lazily by the router
    # (Issue #3773 review — avoid pooled-connection leak on loop churn).
    try:
        from nexus.server.api.v2.routers.path_contexts import dispose_loop_local_engines

        await dispose_loop_local_engines(app.state)
    except Exception as e:
        logger.debug("dispose_loop_local_engines failed: %s", e)

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


def _startup_agent_lifecycle(app: "FastAPI", svc: "LifespanServices") -> None:
    """Wire AgentRegistry for agent lifecycle tracking."""
    agent_registry = svc.agent_registry
    if agent_registry is None:
        app.state.agent_registry = None
        return

    app.state.agent_registry = agent_registry

    # Wire into sync PermissionEnforcer
    perm_enforcer = svc.permission_enforcer
    if perm_enforcer is not None:
        perm_enforcer.agent_registry = agent_registry

    # Issue #2172: Create AgentWarmupService with step registry
    try:
        from nexus.services.agents.agent_warmup import AgentWarmupService
        from nexus.services.agents.warmup_steps import register_standard_steps

        app.state.agent_warmup_service = AgentWarmupService(
            agent_registry=agent_registry,
            namespace_manager=svc.namespace_manager,
            enabled_bricks=getattr(app.state, "enabled_bricks", frozenset()),
            cache_store=getattr(app.state, "cache_brick", None),
            mcp_config=None,
        )
        register_standard_steps(app.state.agent_warmup_service)
        logger.info("[WARMUP] AgentWarmupService initialized with standard steps")
    except Exception as e:
        logger.warning("[WARMUP] Failed to initialize AgentWarmupService: %s", e, exc_info=True)
        app.state.agent_warmup_service = None

    logger.info("[PROCESS-TABLE] AgentRegistry wired for agent lifecycle")


def _startup_key_service(app: "FastAPI", svc: "LifespanServices") -> None:
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
            _identity_settings_store = None
            try:
                from nexus.storage.auth_stores.metastore_settings_store import (
                    MetastoreSettingsStore,
                )

                _identity_settings_store = MetastoreSettingsStore(svc.nexus_fs.metadata)
            except Exception:
                pass
            _identity_oauth_crypto = OAuthCrypto(
                encryption_key=_enc_key, settings_store=_identity_settings_store
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


def _startup_credential_service(app: "FastAPI", svc: "LifespanServices") -> None:
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
        _kernel_agent_id = "__nexus_runtime__"
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
        logger.info(
            "[VC] CredentialService initialized (kernel DID=%s)",
            kernel_key_record.did,
        )
    except Exception as e:
        logger.warning("[VC] Failed to initialize CredentialService: %s", e, exc_info=True)
        app.state.credential_service = None


def _startup_delegation_from_bricks(app: "FastAPI", svc: "LifespanServices") -> None:
    """Expose DelegationService from ServiceRegistry (Issue #2131)."""
    if svc.nexus_fs is None:
        app.state.delegation_service = None
        return

    _svc_fn = getattr(svc.nexus_fs, "service", None)
    app.state.delegation_service = _svc_fn("delegation_service") if _svc_fn else None

    if app.state.delegation_service is not None:
        # Wire system-tier dependencies that weren't available during factory boot
        deleg = app.state.delegation_service
        if getattr(deleg, "_namespace_manager", None) is None:
            deleg._namespace_manager = svc.namespace_manager
        if getattr(deleg, "_agent_registry", None) is None:
            deleg._agent_registry = app.state.agent_registry
        logger.info("[DELEGATION] DelegationService wired from brick_dict")


def _startup_governance(app: "FastAPI", svc: "LifespanServices") -> None:
    """Expose governance services from ServiceRegistry (Issue #2129)."""
    if svc.nexus_fs is None:
        return

    _svc_fn = getattr(svc.nexus_fs, "service", None)
    if _svc_fn is None:
        return
    app.state.governance_anomaly_service = _svc_fn("governance_anomaly_service")
    app.state.governance_collusion_service = _svc_fn("governance_collusion_service")
    app.state.governance_graph_service = _svc_fn("governance_graph_service")
    app.state.governance_response_service = _svc_fn("governance_response_service")

    if app.state.governance_response_service is not None:
        logger.info("[GOV] Governance services wired from brick_dict")


def _startup_sandbox_auth(app: "FastAPI", svc: "LifespanServices") -> None:
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

        # Get AgentEventLog from ServiceRegistry (preferred) or create fallback
        _svc_fn = getattr(svc.nexus_fs, "service", None) if svc.nexus_fs else None
        _factory_event_log = _svc_fn("agent_event_log") if _svc_fn else None
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


def _startup_task_manager(app: "FastAPI", svc: "LifespanServices") -> None:
    """Wire TaskManagerService from factory to app.state (PR #3124)."""
    if svc.nexus_fs is None:
        return
    task_svc = svc.nexus_fs.service("task_manager")  # Issue #1768: via coordinator
    app.state.task_manager_service = task_svc
    if task_svc is not None:
        logger.info("[TASK-MGR] TaskManagerService wired to app.state")


def _startup_transactional_snapshot(app: "FastAPI", svc: "LifespanServices") -> None:
    """Expose TransactionalSnapshotService on app.state for REST API (Issue #1752)."""
    snap_svc = svc.snapshot_service
    app.state.transactional_snapshot_service = snap_svc
    if snap_svc is not None:
        logger.info("[SNAPSHOT] TransactionalSnapshotService wired to app.state")
    else:
        logger.debug("[SNAPSHOT] TransactionalSnapshotService not available")


def _startup_access_manifest(app: "FastAPI", svc: "LifespanServices") -> None:
    """Wire AccessManifestService to app.state for REST API."""
    if not svc.nexus_fs:
        return
    record_store = getattr(svc.nexus_fs, "_record_store", None) or getattr(
        svc, "record_store", None
    )
    rebac_mgr = svc.rebac_manager
    if record_store is None or rebac_mgr is None:
        logger.debug("[ACCESS-MANIFEST] Skipped — record_store or rebac_manager not available")
        return
    try:
        from nexus.bricks.access_manifest.service import AccessManifestService

        svc_instance = AccessManifestService(record_store=record_store, rebac_manager=rebac_mgr)
        app.state.access_manifest_service = svc_instance
        logger.info("[ACCESS-MANIFEST] AccessManifestService wired to app.state")
    except Exception as e:
        logger.warning("[ACCESS-MANIFEST] Failed to initialize: %s", e)


def _startup_agent_tasks(app: "FastAPI", svc: "LifespanServices") -> list[asyncio.Task]:
    """Start agent eviction background task."""
    # AgentRegistry handles heartbeats directly (no buffer), so no flush task needed.
    # Stale detection handled by AgentRegistry's external_info.last_heartbeat field.
    # Checkpoint cleanup handled by VFS when process is reaped.
    app.state._heartbeat_task = None
    app.state._stale_detection_task = None
    app.state._eviction_task = None
    app.state._checkpoint_cleanup_task = None

    tasks: list[asyncio.Task] = []

    # Agent eviction under resource pressure (Issue #2170)
    _factory_em = svc.eviction_manager
    _eviction_tuning = getattr(svc.profile_tuning, "eviction", None)
    if _factory_em is not None and _eviction_tuning is not None:
        try:
            from nexus.server.background_tasks import agent_eviction_task

            app.state._eviction_task = asyncio.create_task(
                agent_eviction_task(
                    _factory_em,
                    interval_seconds=_eviction_tuning.eviction_poll_interval_seconds,
                )
            )
            tasks.append(app.state._eviction_task)
            logger.info("[EVICTION] Background eviction task started")
        except Exception:
            logger.warning("[EVICTION] Failed to start eviction tasks", exc_info=True)
    elif _eviction_tuning is not None and app.state.agent_registry is not None:
        # Fallback: construct EvictionManager here if factory didn't create one
        try:
            from nexus.server.background_tasks import agent_eviction_task
            from nexus.services.agents.eviction_manager import EvictionManager
            from nexus.services.agents.eviction_policy import LRUEvictionPolicy
            from nexus.services.agents.resource_monitor import ResourceMonitor

            resource_monitor = ResourceMonitor(tuning=_eviction_tuning)
            eviction_policy = LRUEvictionPolicy()
            app.state.eviction_manager = EvictionManager(
                agent_registry=app.state.agent_registry,
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

    # InMemoryScheduler doesn't need PostgreSQL init
    from nexus.services.scheduler.in_memory import InMemoryScheduler

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
            await wds.start()
            logger.info("Workflow dispatch service started")
        except Exception as e:
            logger.warning("Workflow dispatch service start failed (non-fatal): %s", e)


async def _startup_pipe_consumers(app: "FastAPI", svc: "LifespanServices") -> None:
    """Start DT_PIPE consumers + register OBSERVE-phase observers (Issue #809, #810).

    RecordStoreWriteObserver (OBSERVE-phase) is registered via hook_spec
    at factory enlist time — no start/stop lifecycle needed here.
    We just expose it on app.state for shutdown cleanup.

    ZoektWriteObserver (Issue #810) is also OBSERVE-phase — no lifecycle.
    """
    nx = svc.nexus_fs

    # Issue #809: RecordStoreWriteObserver (OBSERVE-phase)
    # No bind_fs/start needed — registered via hook_spec at factory enlist time.
    wo = svc.write_observer
    if wo is not None:
        app.state.write_observer = wo
        logger.info("[OBSERVE] RecordStoreWriteObserver registered (OBSERVE-phase)")

    # Issue #3193: EventDeliveryWorker is started by the Rust kernel
    # (BackgroundService auto-start). We only expose it + event_signal on
    # app.state so the API layer (EventReplayService) can access the signal.
    if svc.delivery_worker is not None:
        app.state.delivery_worker = svc.delivery_worker
    if svc.event_signal is not None:
        app.state.event_signal = svc.event_signal

    # Issue #810: ZoektWriteObserver — registered as OBSERVE-phase observer
    # via hook_spec at factory enlist time. No start/stop lifecycle needed.
    # Legacy callbacks (notify_write/notify_sync_complete) still work for
    # CASLocalBackend fallback path.

    # TaskDispatchPipeConsumer (task lifecycle signals)
    tdc = svc.task_dispatch_consumer
    # Fallback: create consumer if not provided by factory (e.g. no record_store)
    if tdc is None and nx is not None:
        try:
            from nexus.task_manager.dispatch_consumer import TaskDispatchPipeConsumer

            _task_svc = getattr(nx, "_task_manager_service", None)
            # AcpService lives on AcpRPCService (created in _boot_wired_services)
            _acp_svc = None
            _acp_rpc_ref = nx.service("acp_rpc") if hasattr(nx, "service") else None
            if _acp_rpc_ref is not None:
                _acp_rpc_obj = getattr(_acp_rpc_ref, "_service", _acp_rpc_ref)
                _acp_svc = getattr(_acp_rpc_obj, "_acp", None)
            _proc_tbl = app.state.agent_registry
            if _task_svc is not None:
                tdc = TaskDispatchPipeConsumer(
                    acp_service=_acp_svc,
                    agent_registry=_proc_tbl,
                )
                tdc.set_task_service(_task_svc)
                # Wire into existing TaskWriteHook
                _twh = getattr(nx, "_task_write_hook", None)
                if _twh is not None:
                    _twh.register_handler(tdc)
                logger.info("[PIPE] TaskDispatchPipeConsumer created (lifespan fallback)")
        except Exception as e:
            logger.warning("[PIPE] TaskDispatchPipeConsumer fallback failed: %s", e)
    if tdc is not None and nx is not None:
        try:
            tdc.set_nx(nx)
            # Inject server base URL so enriched worker prompts can reference the API
            if hasattr(tdc, "set_server_info"):
                _port = os.environ.get("NEXUS_PORT", "2026")
                _host = os.environ.get("NEXUS_HOST", "127.0.0.1")
                tdc.set_server_info(f"http://{_host}:{_port}", "")
            await tdc.start()
            app.state.task_dispatch_consumer = tdc
            logger.info("[PIPE] TaskDispatchPipeConsumer started")
        except Exception as e:
            logger.warning("[PIPE] TaskDispatchPipeConsumer start failed: %s", e, exc_info=True)

    # Issue #3725: SkeletonPipeConsumer — created in startup_search, started here
    # so the Nexus kernel pipe registry is fully ready (startup_services runs after
    # startup_search in the lifespan sequence).
    skpc = getattr(app.state, "skeleton_pipe_consumer", None)
    if skpc is not None and nx is not None:
        try:
            skpc.bind_fs(nx)
            await skpc.start()
            logger.info("[PIPE] SkeletonPipeConsumer started")
        except Exception as e:
            logger.warning("[PIPE] SkeletonPipeConsumer start failed: %s", e, exc_info=True)


async def _shutdown_pipe_consumers(app: "FastAPI") -> None:
    """Stop DT_PIPE consumers (Issue #809, #810).

    Note: EventDeliveryWorker (Issue #3193) is stopped by
    the Rust kernel's service_stop_all() — no
    explicit stop here to avoid double-stop.
    """
    # Issue #809: RecordStoreWriteObserver (OBSERVE-phase)
    wo = getattr(app.state, "write_observer", None)
    if wo is not None:
        try:
            if hasattr(wo, "flush_sync"):
                wo.flush_sync()
            if hasattr(wo, "cancel"):
                wo.cancel()
            logger.info("[OBSERVE] RecordStoreWriteObserver stopped")
        except Exception as e:
            logger.warning(
                "[OBSERVE] Error stopping RecordStoreWriteObserver: %s", e, exc_info=True
            )

    # Issue #810: ZoektWriteObserver — no async stop needed (OBSERVE phase).
    # Cancel any pending debounce timer for clean shutdown.
    _zwo = getattr(app.state, "zoekt_write_observer", None)
    if _zwo is not None and hasattr(_zwo, "cancel"):
        try:
            _zwo.cancel()
            logger.info("[OBSERVE] ZoektWriteObserver cancelled")
        except Exception as e:
            logger.warning("[OBSERVE] Error cancelling ZoektWriteObserver: %s", e, exc_info=True)

    # TaskDispatchPipeConsumer
    tdc = getattr(app.state, "task_dispatch_consumer", None)
    if tdc is not None and hasattr(tdc, "stop"):
        try:
            await tdc.stop()
            logger.info("[PIPE] TaskDispatchPipeConsumer stopped")
        except Exception as e:
            logger.warning("[PIPE] Error stopping TaskDispatchPipeConsumer: %s", e, exc_info=True)

    # Issue #3725: SkeletonPipeConsumer (created in startup_search, stopped here)
    skpc = getattr(app.state, "skeleton_pipe_consumer", None)
    if skpc is not None and hasattr(skpc, "stop"):
        try:
            await skpc.stop()
            logger.info("[PIPE] SkeletonPipeConsumer stopped")
        except Exception as e:
            logger.warning("[PIPE] Error stopping SkeletonPipeConsumer: %s", e, exc_info=True)
