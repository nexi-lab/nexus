"""Boot Tier 1 (SYSTEM) — critical + degradable services.

Issue #2193: Absorbs 11 former-kernel services per Liedtke's test.

Two severity classes:

**Critical** (single try/except → BootError):
    rebac_manager, audit_store, entity_registry, permission_enforcer,
    write_observer — the "Trusted Computing Base outside the kernel".

**Degradable** (per-service try/except → WARNING + None):
    dir_visibility_cache, hierarchy_manager, deferred_permission_buffer,
    workspace_registry, mount_manager, workspace_manager, plus all
    original system services (agent registry, namespace, etc.).
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.factory._boot_context import _BootContext
from nexus.factory._helpers import _make_gate


@dataclass(frozen=True, slots=True)
class AgentRuntimePlaceholder:
    """Two-phase wiring placeholder for agent runtime.

    Created during system-service boot (Phase 1) with partial dependencies.
    Fully wired by orchestrator.py (Phase 2) once NexusFS + LLM are available.
    """

    factory_class: type
    agent_registry: Any
    sandbox: Any
    scheduler: Any


logger = logging.getLogger(__name__)


def _boot_system_services(
    ctx: _BootContext,
    brick_on: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Boot Tier 1 (SYSTEM) — critical + degradable services.

    1. **Critical section** — creates ReBAC, permissions, audit, entity
       registry, and write observer.  A single try/except raises
       ``BootError`` if any critical service fails.

    2. **Degradable former-kernel section** — creates dir visibility
       cache, hierarchy manager, deferred permission buffer, workspace
       services.  Per-service try/except logs WARNING and sets None.

    3. **Original system services** — agent registry, namespace,
       observability, resiliency, lifecycle management.  Same degraded
       pattern as before.

    Args:
        ctx: Boot context with shared dependencies.
        brick_on: Callable ``(name: str) -> bool`` for profile-based gating.
            When None, all services are enabled (backward-compatible default).

    Returns:
        Dict with all system service entries (some degradable ones may be None).
    """
    t0 = time.perf_counter()
    _on = _make_gate(brick_on)

    # =====================================================================
    # CRITICAL SECTION (BootError on failure) — Issue #2193
    # Gated by "permissions" brick — MINIMAL/EMBEDDED profiles skip this.
    # =====================================================================
    from nexus.contracts.exceptions import BootError

    rebac_manager: Any = None
    audit_store: Any = None
    entity_registry: Any = None
    permission_enforcer: Any = None
    write_observer: Any = None

    if not _on("permissions"):
        logger.debug(
            "[BOOT:SYSTEM] Permissions brick disabled by profile — skipping critical section"
        )
    else:
        try:
            # Config-time dialect flag (KERNEL-ARCHITECTURE §7)
            _is_pg = not ctx.db_url.startswith("sqlite")

            # --- ReBAC Manager ---
            from nexus.bricks.rebac.manager import ReBACManager

            rebac_manager = ReBACManager(
                engine=ctx.engine,
                cache_ttl_seconds=ctx.cache_ttl_seconds or 300,
                max_depth=10,
                enforce_zone_isolation=ctx.perm.enforce_zone_isolation,
                enable_graph_limits=True,
                enable_tiger_cache=ctx.perm.enable_tiger_cache,
                read_engine=ctx.read_engine,
                is_postgresql=_is_pg,
            )

            # --- Audit Store ---
            from nexus.bricks.rebac.permissions_enhanced import AuditStore

            audit_store = AuditStore(engine=ctx.engine, is_postgresql=_is_pg)

            # --- Entity Registry ---
            from nexus.bricks.rebac.entity_registry import EntityRegistry

            entity_registry = EntityRegistry(ctx.record_store)

            # --- Permission Enforcer ---
            from nexus.bricks.rebac.enforcer import PermissionEnforcer

            permission_enforcer = PermissionEnforcer(
                metadata_store=ctx.metadata_store,
                rebac_manager=rebac_manager,
                allow_admin_bypass=ctx.perm.allow_admin_bypass,
                allow_system_bypass=True,
                audit_store=audit_store,
                admin_bypass_paths=[],
                router=ctx.router,
                entity_registry=entity_registry,
            )

            # --- RecordStore Syncer (constructed, NOT started) ---
            import os

            use_buffer = ctx.enable_write_buffer
            if use_buffer is None:
                env_val = os.environ.get("NEXUS_ENABLE_WRITE_BUFFER", "").lower()
                if env_val in ("true", "1", "yes"):
                    use_buffer = True
                elif env_val in ("false", "0", "no"):
                    use_buffer = False
                else:
                    use_buffer = ctx.db_url.startswith(("postgres", "postgresql"))

            if use_buffer:
                from nexus.storage.piped_record_store_write_observer import (
                    PipedRecordStoreWriteObserver,
                )

                write_observer = PipedRecordStoreWriteObserver(
                    ctx.record_store,
                    strict_mode=ctx.audit.strict_mode,
                )
            else:
                from nexus.storage.record_store_write_observer import RecordStoreWriteObserver

                write_observer = RecordStoreWriteObserver(
                    ctx.record_store,
                    strict_mode=ctx.audit.strict_mode,
                )

            logger.debug(
                "[BOOT:SYSTEM] Critical services created: rebac_manager, audit_store, "
                "entity_registry, permission_enforcer, write_observer"
            )

        except BootError:
            raise
        except Exception as exc:
            logger.critical("[BOOT:SYSTEM] Critical service failure: %s", exc)
            raise BootError(str(exc), tier="system-critical") from exc

    # =====================================================================
    # DEGRADABLE FORMER-KERNEL SECTION (WARNING + None) — Issue #2193
    # =====================================================================

    # --- Directory Visibility Cache ---
    dir_visibility_cache: Any = None
    try:
        from nexus.bricks.rebac.cache.visibility import DirectoryVisibilityCache

        dir_visibility_cache = DirectoryVisibilityCache(
            tiger_cache=getattr(rebac_manager, "_tiger_cache", None),
            ttl=ctx.cache_ttl_seconds or 300,
            max_entries=10000,
        )

        # Wire: rebac invalidation -> dir visibility cache
        rebac_manager.register_dir_visibility_invalidator(
            "nexusfs",
            lambda zone_id, path: dir_visibility_cache.invalidate_for_resource(path, zone_id),
        )
        logger.debug("[BOOT:SYSTEM] DirectoryVisibilityCache created")
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] DirectoryVisibilityCache unavailable: %s", exc)

    # --- Hierarchy Manager ---
    hierarchy_manager: Any = None
    try:
        from nexus.bricks.rebac.hierarchy_manager import HierarchyManager

        hierarchy_manager = HierarchyManager(
            rebac_manager=rebac_manager,
            enable_inheritance=ctx.perm.inherit,
        )
        logger.debug("[BOOT:SYSTEM] HierarchyManager created")
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] HierarchyManager unavailable: %s", exc)

    # --- Deferred Permission Buffer (constructed, NOT started) ---
    deferred_permission_buffer: Any = None
    if ctx.perm.enable_deferred:
        try:
            from nexus.bricks.rebac.deferred_permission_buffer import DeferredPermissionBuffer

            deferred_permission_buffer = DeferredPermissionBuffer(
                rebac_manager=rebac_manager,
                hierarchy_manager=hierarchy_manager,
                flush_interval_sec=ctx.perm.deferred_flush_interval,
            )
            logger.debug("[BOOT:SYSTEM] DeferredPermissionBuffer created")
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] DeferredPermissionBuffer unavailable: %s", exc)

    # --- Workspace Registry ---
    workspace_registry: Any = None
    try:
        from nexus.bricks.workspace.workspace_registry import WorkspaceRegistry

        workspace_registry = WorkspaceRegistry(
            metadata=ctx.metadata_store,
            rebac_manager=rebac_manager,
            record_store=ctx.record_store,
        )
        logger.debug("[BOOT:SYSTEM] WorkspaceRegistry created")
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] WorkspaceRegistry unavailable: %s", exc)

    # --- Mount Manager ---
    mount_manager: Any = None
    try:
        from nexus.bricks.mount.mount_manager import MountManager

        mount_manager = MountManager(ctx.record_store)
        logger.debug("[BOOT:SYSTEM] MountManager created")
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] MountManager unavailable: %s", exc)

    # --- Workspace Manager ---
    workspace_manager: Any = None
    try:
        from nexus.contracts.protocols.rebac import ReBACBrickProtocol
        from nexus.system_services.workspace.workspace_manager import WorkspaceManager

        workspace_manager = WorkspaceManager(
            metadata=ctx.metadata_store,
            backend=ctx.backend,
            rebac_manager=cast(ReBACBrickProtocol, rebac_manager),
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
            record_store=ctx.record_store,
        )
        logger.debug("[BOOT:SYSTEM] WorkspaceManager created")
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] WorkspaceManager unavailable: %s", exc)

    # =====================================================================
    # ORIGINAL SYSTEM SERVICES (all degradable)
    # =====================================================================

    # --- Agent Registry (Issue #1502) ---
    agent_registry: Any = None
    async_agent_registry: Any = None
    if _on("agent_registry") and ctx.record_store is not None:
        try:
            from nexus.system_services.agents.agent_registry import (
                AgentRegistry,
                AsyncAgentRegistry,
            )

            agent_registry = AgentRegistry(
                record_store=ctx.record_store,
                entity_registry=entity_registry,
                flush_interval=ctx.profile_tuning.background_task.heartbeat_flush_interval,
            )
            async_agent_registry = AsyncAgentRegistry(agent_registry)
            logger.debug("[BOOT:SYSTEM] AgentRegistry + AsyncAgentRegistry created")
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] AgentRegistry unavailable: %s", exc)

    if not _on("agent_registry"):
        logger.debug("[BOOT:SYSTEM] AgentRegistry disabled by profile")

    # --- Eviction Manager (Issues #2170, #2171) ---
    eviction_manager: Any = None
    if agent_registry is not None:
        try:
            from nexus.system_services.agents.eviction_manager import EvictionManager
            from nexus.system_services.agents.eviction_policy import QoSEvictionPolicy
            from nexus.system_services.agents.resource_monitor import ResourceMonitor

            eviction_tuning = ctx.profile_tuning.eviction
            resource_monitor = ResourceMonitor(tuning=eviction_tuning)
            eviction_policy = QoSEvictionPolicy()
            eviction_manager = EvictionManager(
                registry=agent_registry,
                monitor=resource_monitor,
                policy=eviction_policy,
                tuning=eviction_tuning,
            )
            logger.debug("[BOOT:SYSTEM] EvictionManager created (QoS-aware)")
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] EvictionManager unavailable: %s", exc)

    # --- Namespace Manager (Issue #1502) ---
    namespace_manager: Any = None
    async_namespace_manager: Any = None
    if not _on("namespace"):
        logger.debug("[BOOT:SYSTEM] NamespaceManager disabled by profile")
    else:
        try:
            from nexus.bricks.rebac.namespace_factory import (
                create_namespace_manager as _create_ns_manager,
            )
            from nexus.bricks.rebac.namespace_manager import AsyncNamespaceManager

            namespace_manager = _create_ns_manager(
                rebac_manager=rebac_manager,
                record_store=ctx.record_store,
            )
            async_namespace_manager = AsyncNamespaceManager(namespace_manager)
            logger.debug("[BOOT:SYSTEM] NamespaceManager + AsyncNamespaceManager created")
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] NamespaceManager unavailable: %s", exc)

    # --- Event Delivery Worker (Issue #1241, constructed, NOT started) ---
    delivery_worker = None
    if not _on("eventlog"):
        logger.debug("[BOOT:SYSTEM] EventDeliveryWorker disabled by profile")
    elif ctx.db_url.startswith(("postgres", "postgresql")):
        try:
            from nexus.system_services.event_subsystem.log.delivery import EventDeliveryWorker

            delivery_worker = EventDeliveryWorker(
                record_store=ctx.record_store,
                poll_interval_ms=200,
                batch_size=50,
            )
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] EventDeliveryWorker unavailable: %s", exc)

    # --- Observability Subsystem (Issue #1301) ---
    observability_subsystem: Any = None
    if not _on("observability"):
        logger.debug("[BOOT:SYSTEM] ObservabilitySubsystem disabled by profile")
    else:
        try:
            from nexus.core.config import ObservabilityConfig
            from nexus.server.observability.observability_subsystem import ObservabilitySubsystem

            # Instrument both primary and replica pools (Issue #725)
            obs_engines = [ctx.engine]
            if ctx.record_store.has_read_replica:
                obs_engines.append(ctx.read_engine)
            observability_subsystem = ObservabilitySubsystem(
                config=ObservabilityConfig(),
                engines=obs_engines,
            )
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] ObservabilitySubsystem unavailable: %s", exc)

    # --- Resiliency Subsystem (Issue #1366) ---
    resiliency_manager: Any = None
    if not _on("resiliency"):
        logger.debug("[BOOT:SYSTEM] ResiliencyManager disabled by profile")
    else:
        try:
            from nexus.lib.resiliency import (
                ResiliencyConfig,
                ResiliencyManager,
                set_default_manager,
            )

            resiliency_config = ResiliencyConfig.from_dict(ctx.resiliency_raw)
            resiliency_manager = ResiliencyManager(config=resiliency_config)
            set_default_manager(resiliency_manager)
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] ResiliencyManager unavailable: %s", exc)

    # --- Context Branch Service (Issue #1315) ---
    context_branch_service: Any = None
    try:
        from nexus.contracts.protocols.rebac import ReBACBrickProtocol
        from nexus.system_services.workspace.context_branch import ContextBranchService

        context_branch_service = ContextBranchService(
            workspace_manager=workspace_manager,
            record_store=ctx.record_store,
            rebac_manager=cast(ReBACBrickProtocol, rebac_manager),
            default_zone_id=ctx.zone_id,
            default_agent_id=ctx.agent_id,
        )
        logger.debug("[BOOT:SYSTEM] ContextBranchService created")
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] ContextBranchService unavailable: %s", exc)

    # --- Brick Lifecycle Manager (Issue #1704) ---
    brick_lifecycle_manager: Any = None
    try:
        from nexus.system_services.lifecycle.brick_lifecycle import BrickLifecycleManager

        brick_lifecycle_manager = BrickLifecycleManager()
        logger.debug("[BOOT:SYSTEM] BrickLifecycleManager created")
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] BrickLifecycleManager unavailable: %s", exc)

    # --- Brick Reconciler (Issue #2060) ---
    brick_reconciler: Any = None
    if brick_lifecycle_manager is not None:
        try:
            from nexus.system_services.lifecycle.brick_reconciler import BrickReconciler

            brick_reconciler = BrickReconciler(lifecycle_manager=brick_lifecycle_manager)
            logger.debug("[BOOT:SYSTEM] BrickReconciler created")
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] BrickReconciler unavailable: %s", exc)

    # --- Tiger Cache Manager (Issue #2133: injected via factory) ---
    tiger_cache_manager: Any = None
    try:
        from nexus.bricks.rebac.tiger_cache_manager import TigerCacheManager

        tiger_cache_manager = TigerCacheManager(
            rebac_manager=rebac_manager,
            metadata_store=ctx.metadata_store,
            default_zone_id=ctx.zone_id or ROOT_ZONE_ID,
        )
        tiger_cache_manager.initialize()
        logger.debug("[BOOT:SYSTEM] TigerCacheManager created")
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] TigerCacheManager unavailable: %s", exc)

    # --- Zone Lifecycle Service (Issue #2061) ---
    zone_lifecycle: Any = None
    session_factory = getattr(ctx.record_store, "session_factory", None)
    if session_factory is not None:
        try:
            from nexus.system_services.lifecycle.zone_lifecycle import ZoneLifecycleService

            zone_lifecycle = ZoneLifecycleService(session_factory=session_factory)

            # Register session-based finalizers (available at boot).
            # Cache + Mount finalizers are registered later in service_wiring
            # when their dependencies (file_cache, mount_service) exist.
            try:
                from nexus.system_services.lifecycle.zone_finalizers import (
                    ReBACZoneFinalizer,
                    SearchZoneFinalizer,
                )

                zone_lifecycle.register_finalizer(SearchZoneFinalizer(session_factory))
                # ReBAC finalizer (MUST be last — Decision #13A)
                zone_lifecycle.register_finalizer(ReBACZoneFinalizer(session_factory))
            except Exception as exc:
                logger.warning("[BOOT:SYSTEM] Zone finalizer registration failed: %s", exc)

            logger.debug("[BOOT:SYSTEM] ZoneLifecycleService created")
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] ZoneLifecycleService unavailable: %s", exc)

    # --- Scheduler Service (Issue #2195, #2360) ---
    scheduler_service: Any = None
    if not _on("scheduler"):
        logger.debug("[BOOT:SYSTEM] SchedulerService disabled by profile")
    else:
        try:
            if ctx.db_url.startswith(("postgres", "postgresql")):
                from nexus.system_services.scheduler.service import SchedulerService

                scheduler_service = SchedulerService(db_pool=None)
                logger.debug("[BOOT:SYSTEM] SchedulerService created (two-phase, pool=None)")
            else:
                from nexus.system_services.scheduler.in_memory import InMemoryScheduler

                scheduler_service = InMemoryScheduler()
                logger.debug("[BOOT:SYSTEM] InMemoryScheduler created (no PostgreSQL)")
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] SchedulerService unavailable: %s", exc)

    # --- PipeManager (Issue #809: DT_PIPE kernel IPC for write observer + zoekt) ---
    # self_address enables federated DT_PIPE: remote nodes can proxy pipe
    # I/O to the origin via gRPC Call RPC (Issue #1576).
    pipe_manager: Any = None
    try:
        import os

        from nexus.core.pipe_manager import PipeManager

        _pipe_self_addr = os.environ.get("NEXUS_ADVERTISE_ADDR")
        pipe_manager = PipeManager(
            ctx.metadata_store,
            zone_id=ctx.zone_id or ROOT_ZONE_ID,
            self_address=_pipe_self_addr,
        )
        logger.debug(
            "[BOOT:SYSTEM] PipeManager created (self_address=%s)",
            _pipe_self_addr or "none/single-node",
        )
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] PipeManager unavailable: %s", exc)

    # --- ProcessTable (Issue #1509: kernel process lifecycle) ---
    process_table: Any = None
    try:
        from nexus.core.process_table import ProcessTable

        process_table = ProcessTable(ctx.metadata_store, zone_id=ctx.zone_id or ROOT_ZONE_ID)
        recovered = process_table.recover()
        if recovered > 0:
            logger.info("[BOOT:SYSTEM] ProcessTable recovered %d processes", recovered)
        else:
            logger.debug("[BOOT:SYSTEM] ProcessTable created")
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] ProcessTable unavailable: %s", exc)

    # --- Agent Runtime (Agent Process Engine, AGENT-PROCESS-ARCHITECTURE) ---
    agent_runtime: Any = None
    if _on("agent_runtime") and agent_registry is not None:
        try:
            from nexus.system_services.agent_runtime.process_manager import (
                ProcessManager as _AgentProcessManager,
            )

            # LLM provider is wired later (in _boot_wired_services) since
            # it needs NexusFS.  For now, agent_runtime is constructed lazily
            # at first use via the two-phase pattern.  Store the class + deps
            # so orchestrator.py can finish wiring after NexusFS is created.
            agent_runtime = AgentRuntimePlaceholder(
                factory_class=_AgentProcessManager,
                agent_registry=async_agent_registry,
                sandbox=None,  # wired later
                scheduler=scheduler_service,
            )
            logger.debug("[BOOT:SYSTEM] AgentRuntime placeholder created (two-phase)")
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] AgentRuntime unavailable: %s", exc)

    # =====================================================================
    # Assemble result
    # =====================================================================

    result = {
        # Former-kernel critical
        "rebac_manager": rebac_manager,
        "audit_store": audit_store,
        "entity_registry": entity_registry,
        "permission_enforcer": permission_enforcer,
        "write_observer": write_observer,
        "pipe_manager": pipe_manager,
        "process_table": process_table,
        # Former-kernel degradable
        "dir_visibility_cache": dir_visibility_cache,
        "hierarchy_manager": hierarchy_manager,
        "deferred_permission_buffer": deferred_permission_buffer,
        "workspace_registry": workspace_registry,
        "mount_manager": mount_manager,
        "workspace_manager": workspace_manager,
        # Original system services
        "agent_registry": agent_registry,
        "async_agent_registry": async_agent_registry,
        "namespace_manager": namespace_manager,
        "async_namespace_manager": async_namespace_manager,
        "delivery_worker": delivery_worker,
        "observability_subsystem": observability_subsystem,
        "resiliency_manager": resiliency_manager,
        "context_branch_service": context_branch_service,
        "brick_lifecycle_manager": brick_lifecycle_manager,
        "brick_reconciler": brick_reconciler,
        "eviction_manager": eviction_manager,
        "zone_lifecycle": zone_lifecycle,
        "scheduler_service": scheduler_service,
        "agent_runtime": agent_runtime,
    }

    elapsed = time.perf_counter() - t0
    active = sum(1 for v in result.values() if v is not None)
    logger.info(
        "[BOOT:SYSTEM] %d/%d services ready (%.3fs, profile gating=%s)",
        active,
        len(result),
        elapsed,
        "active" if brick_on is not None else "off",
    )
    return result
