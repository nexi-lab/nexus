"""Tier 1 (SYSTEM) boot — degraded-mode on failure + background starter."""

from __future__ import annotations

import logging
import time
from typing import Any

from nexus.factory.adapters import _parse_resiliency_config
from nexus.factory.boot_context import _BootContext

logger = logging.getLogger(__name__)


def _boot_system_services(ctx: _BootContext, kernel: dict[str, Any]) -> dict[str, Any]:
    """Boot Tier 1 (SYSTEM) — degraded-mode on failure.

    Creates AgentRegistry, NamespaceManager, AsyncVFSRouter,
    EventDeliveryWorker, ObservabilitySubsystem, ResiliencyManager.
    On failure: logs WARNING, sets that service to None.

    Returns:
        Dict with 11 system service entries (some may be None).
    """
    t0 = time.perf_counter()

    # --- Agent Registry (Issue #1502) ---
    agent_registry: Any = None
    async_agent_registry: Any = None
    if ctx.record_store is not None:
        try:
            from nexus.services.agents.agent_registry import AgentRegistry
            from nexus.services.agents.async_agent_registry import AsyncAgentRegistry

            agent_registry = AgentRegistry(
                record_store=ctx.record_store,
                entity_registry=kernel["entity_registry"],
                flush_interval=60,
            )
            async_agent_registry = AsyncAgentRegistry(agent_registry)
            logger.debug("[BOOT:SYSTEM] AgentRegistry + AsyncAgentRegistry created")
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] AgentRegistry unavailable: %s", exc)

    # --- Namespace Manager (Issue #1502) ---
    namespace_manager: Any = None
    async_namespace_manager: Any = None
    try:
        from nexus.rebac.async_namespace_manager import AsyncNamespaceManager
        from nexus.rebac.namespace_factory import (
            create_namespace_manager as _create_ns_manager,
        )

        namespace_manager = _create_ns_manager(
            rebac_manager=kernel["rebac_manager"],
            record_store=ctx.record_store,
        )
        async_namespace_manager = AsyncNamespaceManager(namespace_manager)
        logger.debug("[BOOT:SYSTEM] NamespaceManager + AsyncNamespaceManager created")
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] NamespaceManager unavailable: %s", exc)

    # --- Async VFS Router (Issue #1502) ---
    async_vfs_router: Any = None
    try:
        from nexus.services.routing.async_router import AsyncVFSRouter

        async_vfs_router = AsyncVFSRouter(ctx.router)
        logger.debug("[BOOT:SYSTEM] AsyncVFSRouter created")
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] AsyncVFSRouter unavailable: %s", exc)

    # --- Event Delivery Worker (Issue #1241, constructed, NOT started) ---
    delivery_worker = None
    if ctx.db_url.startswith(("postgres", "postgresql")):
        try:
            from nexus.services.event_log.delivery_worker import EventDeliveryWorker

            delivery_worker = EventDeliveryWorker(
                record_store=ctx.record_store,
                poll_interval_ms=200,
                batch_size=50,
            )
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] EventDeliveryWorker unavailable: %s", exc)

    # --- Observability Subsystem (Issue #1301) ---
    observability_subsystem: Any = None
    try:
        from nexus.core.config import ObservabilityConfig
        from nexus.services.subsystems.observability_subsystem import ObservabilitySubsystem

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
    try:
        from nexus.core.resiliency import ResiliencyManager, set_default_manager

        resiliency_config = _parse_resiliency_config(ctx.resiliency_raw)
        resiliency_manager = ResiliencyManager(config=resiliency_config)
        set_default_manager(resiliency_manager)
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] ResiliencyManager unavailable: %s", exc)

    # --- Context Branch Service (Issue #1315) ---
    context_branch_service: Any = None
    try:
        from nexus.services.context_branch import ContextBranchService

        context_branch_service = ContextBranchService(
            workspace_manager=kernel["workspace_manager"],
            record_store=ctx.record_store,
            rebac_manager=kernel["rebac_manager"],
            default_zone_id=ctx.zone_id,
            default_agent_id=ctx.agent_id,
        )
        logger.debug("[BOOT:SYSTEM] ContextBranchService created")
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] ContextBranchService unavailable: %s", exc)

    # --- Hook Engine chain: PluginHooks → AsyncHookEngine → ScopedHookEngine (Issue #1257) ---
    scoped_hook_engine: Any = None
    try:
        from nexus.plugins.async_hooks import AsyncHookEngine
        from nexus.plugins.hooks import PluginHooks
        from nexus.services.hook_engine import ScopedHookEngine

        plugin_hooks = PluginHooks()
        async_hook_engine = AsyncHookEngine(inner=plugin_hooks)
        scoped_hook_engine = ScopedHookEngine(inner=async_hook_engine)
        logger.debug("[BOOT:SYSTEM] ScopedHookEngine created")
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] ScopedHookEngine unavailable: %s", exc)

    # --- Brick Lifecycle Manager (Issue #1704) ---
    brick_lifecycle_manager: Any = None
    try:
        from nexus.services.brick_lifecycle import BrickLifecycleManager

        brick_lifecycle_manager = BrickLifecycleManager(hook_engine=scoped_hook_engine)
        logger.debug(
            "[BOOT:SYSTEM] BrickLifecycleManager created (hook_engine=%s)",
            "enabled" if scoped_hook_engine else "disabled",
        )
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] BrickLifecycleManager unavailable: %s", exc)

    # --- Eviction Manager (Issue #2170) ---
    eviction_manager: Any = None
    if agent_registry is not None:
        try:
            from nexus.services.agents.eviction_manager import EvictionManager
            from nexus.services.agents.eviction_policy import LRUEvictionPolicy
            from nexus.services.agents.resource_monitor import ResourceMonitor

            eviction_tuning = ctx.profile_tuning.eviction
            resource_monitor = ResourceMonitor(tuning=eviction_tuning)
            eviction_policy = LRUEvictionPolicy()
            eviction_manager = EvictionManager(
                registry=agent_registry,
                monitor=resource_monitor,
                policy=eviction_policy,
                tuning=eviction_tuning,
            )
        except Exception as exc:
            logger.debug("[BOOT:SYSTEM] EvictionManager unavailable: %s", exc)

    # --- Brick Reconciler (Issue #2060) ---
    brick_reconciler: Any = None
    if brick_lifecycle_manager is not None:
        try:
            from nexus.services.brick_reconciler import BrickReconciler

            brick_reconciler = BrickReconciler(lifecycle_manager=brick_lifecycle_manager)
            logger.debug("[BOOT:SYSTEM] BrickReconciler created")
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] BrickReconciler unavailable: %s", exc)

    result = {
        "agent_registry": agent_registry,
        "async_agent_registry": async_agent_registry,
        "eviction_manager": eviction_manager,
        "namespace_manager": namespace_manager,
        "async_namespace_manager": async_namespace_manager,
        "async_vfs_router": async_vfs_router,
        "delivery_worker": delivery_worker,
        "observability_subsystem": observability_subsystem,
        "resiliency_manager": resiliency_manager,
        "context_branch_service": context_branch_service,
        "brick_lifecycle_manager": brick_lifecycle_manager,
        "brick_reconciler": brick_reconciler,
        "scoped_hook_engine": scoped_hook_engine,
    }

    elapsed = time.perf_counter() - t0
    active = sum(1 for v in result.values() if v is not None)
    logger.info("[BOOT:SYSTEM] %d/%d services ready (%.3fs)", active, len(result), elapsed)
    return result


def _start_background_services(kernel: dict[str, Any], system: dict[str, Any]) -> None:
    """Start background threads after all tiers are constructed.

    Deferred from tier construction so that all services are wired before
    any background I/O begins.
    """
    # Deferred Permission Buffer (kernel tier)
    dpb = kernel.get("deferred_permission_buffer")
    if dpb is not None and hasattr(dpb, "start"):
        dpb.start()
        logger.debug("[BOOT:BG] DeferredPermissionBuffer started")

    # Write Observer — only BufferedRecordStoreSyncer needs .start()
    wo = kernel.get("write_observer")
    if wo is not None and hasattr(wo, "start"):
        from nexus.storage.record_store_syncer import BufferedRecordStoreWriteObserver

        if isinstance(wo, BufferedRecordStoreWriteObserver):
            wo.start()
            logger.debug("[BOOT:BG] BufferedRecordStoreSyncer started")

    # Event Delivery Worker (system tier)
    dw = system.get("delivery_worker")
    if dw is not None and hasattr(dw, "start"):
        dw.start()
        logger.debug("[BOOT:BG] EventDeliveryWorker started")
