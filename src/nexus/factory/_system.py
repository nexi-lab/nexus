"""Boot Tier 1 — critical + degradable services (pre-kernel).

Issue #2193: Absorbs 11 former-kernel services per Liedtke's test.
Renamed ``_boot_system_services`` → ``_boot_services`` → ``_boot_pre_kernel_services``
(PR #3350, PR #3371 Phase 2).

Two severity classes:

**Critical** (single try/except → BootError):
    rebac_manager, audit_store, entity_registry, permission_enforcer,
    write_observer — the "Trusted Computing Base outside the kernel".

**Degradable** (per-service try/except → WARNING + None):
    dir_visibility_cache, hierarchy_manager, deferred_permission_buffer,
    workspace_registry, mount_manager, workspace_manager, plus all
    original services (namespace, etc.).
"""

import logging
import time
from collections.abc import Callable
from typing import Any, cast

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.factory._boot_context import _BootContext
from nexus.factory._helpers import _make_gate

logger = logging.getLogger(__name__)


def _boot_pre_kernel_services(
    ctx: _BootContext,
    svc_on: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Boot Tier 1 — critical + degradable services.

    1. **Critical section** — creates ReBAC, permissions, audit, entity
       registry, and write observer.  A single try/except raises
       ``BootError`` if any critical service fails.

    2. **Degradable former-kernel section** — creates dir visibility
       cache, hierarchy manager, deferred permission buffer, workspace
       services.  Per-service try/except logs WARNING and sets None.

    3. **Original services** — namespace, observability, resiliency,
       lifecycle management.  Same degraded pattern as before.

    Args:
        ctx: Boot context with shared dependencies.
        svc_on: Callable ``(name: str) -> bool`` for profile-based gating.
            When None, all services are enabled (backward-compatible default).

    Returns:
        Dict with all system service entries (some degradable ones may be None).
    """
    t0 = time.perf_counter()
    _on = _make_gate(svc_on)

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
            from nexus.bricks.rebac.consistency.metastore_namespace_store import (
                MetastoreNamespaceStore,
            )
            from nexus.bricks.rebac.consistency.metastore_version_store import (
                MetastoreVersionStore,
            )
            from nexus.bricks.rebac.manager import ReBACManager

            _version_store = MetastoreVersionStore(ctx.metadata_store)
            _namespace_store = MetastoreNamespaceStore(ctx.metadata_store)

            rebac_manager = ReBACManager(
                engine=ctx.engine,
                cache_ttl_seconds=ctx.cache_ttl_seconds or 300,
                max_depth=10,
                enforce_zone_isolation=ctx.perm.enforce_zone_isolation,
                enable_graph_limits=True,
                enable_tiger_cache=ctx.perm.enable_tiger_cache,
                read_engine=ctx.read_engine,
                is_postgresql=_is_pg,
                version_store=_version_store,
                namespace_store=_namespace_store,
                enable_inheritance=ctx.perm.inherit,
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
                dlc=ctx.dlc,
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
                    # Issue #3399: default to piped (async) observer for all profiles.
                    # DFUSE principle: defer non-consistency-critical work from I/O path.
                    # Set NEXUS_ENABLE_WRITE_BUFFER=false for strict audit compliance.
                    use_buffer = True

            if use_buffer:
                from nexus.storage.piped_record_store_write_observer import (
                    RecordStoreWriteObserver as ObserverWriteObserver,
                )

                write_observer = ObserverWriteObserver(
                    ctx.record_store,
                    strict_mode=ctx.audit.strict_mode,
                    event_signal=ctx.event_signal,
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

    # --- Async-on-write extraction hook (Issue #2978) ---
    # Degradable: extraction is best-effort; failures do not block writes.
    if hasattr(write_observer, "register_post_flush_hook"):
        try:
            from nexus.factory._extraction_hook import make_extraction_hook

            extraction_hook = make_extraction_hook(
                session_factory=ctx.record_store.session_factory,
                backend=ctx.backend,
                metastore=ctx.metadata_store,
                max_extract_bytes=100 * 1024 * 1024,  # 100MB
            )
            write_observer.register_post_flush_hook(extraction_hook)
            logger.debug("[BOOT:SYSTEM] Async-on-write extraction hook registered")
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] Extraction hook unavailable: %s", exc)

    # --- Agent lineage hook (Issue #3417) ---
    # Records which files an agent read to produce each output file.
    # Degradable: lineage is best-effort; failures do not block writes.
    if hasattr(write_observer, "register_post_flush_hook"):
        try:
            from nexus.factory._lineage_hook import make_lineage_hook

            lineage_hook = make_lineage_hook(
                session_factory=ctx.record_store.session_factory,
            )
            write_observer.register_post_flush_hook(lineage_hook)
            logger.debug("[BOOT:SYSTEM] Agent lineage hook registered")
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] Lineage hook unavailable: %s", exc)

    # =====================================================================
    # DEGRADABLE FORMER-KERNEL SECTION (WARNING + None) — Issue #2193
    # =====================================================================

    # --- Directory Visibility Cache + Hierarchy Manager ---
    # Now internalized into ReBACManager (constructed in its __init__).
    # Access via rebac_manager.dir_visibility_cache / rebac_manager.hierarchy_manager.
    dir_visibility_cache: Any = (
        getattr(rebac_manager, "dir_visibility_cache", None) if rebac_manager else None
    )
    hierarchy_manager: Any = (
        getattr(rebac_manager, "hierarchy_manager", None) if rebac_manager else None
    )
    if dir_visibility_cache is not None:
        logger.debug("[BOOT:SYSTEM] DirectoryVisibilityCache (rebac-internal)")
    if hierarchy_manager is not None:
        logger.debug("[BOOT:SYSTEM] HierarchyManager (rebac-internal)")

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
            # Issue #3192: Wire Pub/Sub for cross-zone flush coordination
            if hasattr(rebac_manager, "_cache_coordinator"):
                _coord = rebac_manager._cache_coordinator
                _pubsub = getattr(_coord, "_pubsub", None)
                if _pubsub is not None:
                    deferred_permission_buffer.set_pubsub(_pubsub)
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
    # Deferred to post-kernel tier (factory/_wired.py) — the VFS-backed
    # MountStore needs a live NexusFS handle, which isn't constructed yet.
    mount_manager: Any = None

    # --- Workspace Manager ---
    workspace_manager: Any = None
    try:
        from nexus.contracts.protocols.rebac import ReBACBrickProtocol
        from nexus.services.workspace.workspace_manager import WorkspaceManager

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

    # --- Namespace Manager (Issue #1502) ---
    # Now created via rebac_manager.create_namespace_manager() (rebac-internal).
    namespace_manager: Any = None
    async_namespace_manager: Any = None
    if not _on("namespace"):
        logger.debug("[BOOT:SYSTEM] NamespaceManager disabled by profile")
    elif rebac_manager is not None:
        try:
            from nexus.bricks.rebac.namespace_manager import AsyncNamespaceManager

            namespace_manager = rebac_manager.create_namespace_manager(
                record_store=ctx.record_store,
            )
            async_namespace_manager = AsyncNamespaceManager(namespace_manager)
            logger.debug(
                "[BOOT:SYSTEM] NamespaceManager + AsyncNamespaceManager created (rebac-internal)"
            )
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] NamespaceManager unavailable: %s", exc)

    # --- Event Delivery Worker (Issue #1241, constructed, NOT started) ---
    delivery_worker = None
    if not _on("eventlog"):
        logger.debug("[BOOT:SYSTEM] EventDeliveryWorker disabled by profile")
    elif ctx.db_url.startswith(("postgres", "postgresql")):
        try:
            from nexus.services.event_log.delivery import EventDeliveryWorker

            delivery_worker = EventDeliveryWorker(
                record_store=ctx.record_store,
                event_signal=ctx.event_signal,
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
        from nexus.services.workspace.context_branch import ContextBranchService

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
            from nexus.services.lifecycle.zone_lifecycle import ZoneLifecycleService

            zone_lifecycle = ZoneLifecycleService(session_factory=session_factory)

            # Register session-based finalizers (available at boot).
            # Cache + Mount finalizers are registered later in service_wiring
            # when their dependencies (file_cache, mount_service) exist.
            try:
                from nexus.services.lifecycle.zone_finalizers import (
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
                from nexus.services.scheduler.service import SchedulerService

                scheduler_service = SchedulerService(db_pool=None)
                logger.debug("[BOOT:SYSTEM] SchedulerService created (two-phase, pool=None)")
            else:
                from nexus.services.scheduler.in_memory import InMemoryScheduler

                scheduler_service = InMemoryScheduler()
                logger.debug("[BOOT:SYSTEM] InMemoryScheduler created (no PostgreSQL)")
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] SchedulerService unavailable: %s", exc)

    # (Federation is wired at link time in _lifecycle.py via the federation parameter.)

    # (IPC primitives are kernel-owned: Rust DashMap + metastore/dcache integration.
    # AgentRegistry is lazy-constructed by the first consumer via sys_setattr.
    # EvictionManager + AcpService are deferred to _do_link().  See Issue #1792.)

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
        # Former-kernel degradable
        "deferred_permission_buffer": deferred_permission_buffer,
        "workspace_registry": workspace_registry,
        "mount_manager": mount_manager,
        "workspace_manager": workspace_manager,
        # Original services
        "async_namespace_manager": async_namespace_manager,
        "delivery_worker": delivery_worker,
        "event_signal": ctx.event_signal,
        "observability_subsystem": observability_subsystem,
        "resiliency_manager": resiliency_manager,
        "context_branch_service": context_branch_service,
        "zone_lifecycle": zone_lifecycle,
        "scheduler_service": scheduler_service,
    }

    elapsed = time.perf_counter() - t0
    active = sum(1 for v in result.values() if v is not None)
    logger.info(
        "[BOOT:SYSTEM] %d/%d services ready (%.3fs, profile gating=%s)",
        active,
        len(result),
        elapsed,
        "active" if svc_on is not None else "off",
    )
    return result


# Backward compatibility alias
_boot_services = _boot_pre_kernel_services
