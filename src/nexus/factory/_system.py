"""Boot Tier 1 (SYSTEM) — degraded-mode on failure."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from nexus.factory._boot_context import _BootContext

logger = logging.getLogger(__name__)


def _boot_system_services(
    ctx: _BootContext,
    kernel: dict[str, Any],
    brick_on: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Boot Tier 1 (SYSTEM) — degraded-mode on failure.

    Creates AgentRegistry, NamespaceManager, AsyncVFSRouter,
    EventDeliveryWorker, ObservabilitySubsystem, ResiliencyManager.
    On failure: logs WARNING, sets that service to None.

    Args:
        ctx: Boot context with shared dependencies.
        kernel: Kernel services dict from Tier 0.
        brick_on: Callable ``(name: str) -> bool`` for profile-based gating.
            When None, all services are enabled (backward-compatible default).

    Returns:
        Dict with 13 system service entries (some may be None).
    """
    t0 = time.perf_counter()

    def _on(name: str) -> bool:
        if brick_on is None:
            return True
        return brick_on(name)

    # --- Agent Registry (Issue #1502) ---
    agent_registry: Any = None
    async_agent_registry: Any = None
    if _on("agent_registry") and ctx.record_store is not None:
        try:
            from nexus.services.agents.agent_registry import AgentRegistry
            from nexus.services.agents.async_agent_registry import AsyncAgentRegistry

            agent_registry = AgentRegistry(
                record_store=ctx.record_store,
                entity_registry=kernel["entity_registry"],
                flush_interval=ctx.profile_tuning.background_task.heartbeat_flush_interval,
            )
            async_agent_registry = AsyncAgentRegistry(agent_registry)
            logger.debug("[BOOT:SYSTEM] AgentRegistry + AsyncAgentRegistry created")
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] AgentRegistry unavailable: %s", exc)

    if not _on("agent_registry"):
        logger.debug("[BOOT:SYSTEM] AgentRegistry disabled by profile")

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
            logger.debug("[BOOT:SYSTEM] EvictionManager created")
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] EvictionManager unavailable: %s", exc)

    # --- Namespace Manager (Issue #1502) ---
    namespace_manager: Any = None
    async_namespace_manager: Any = None
    if not _on("namespace"):
        logger.debug("[BOOT:SYSTEM] NamespaceManager disabled by profile")
    else:
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
    if not _on("eventlog"):
        logger.debug("[BOOT:SYSTEM] EventDeliveryWorker disabled by profile")
    elif ctx.db_url.startswith(("postgres", "postgresql")):
        try:
            from nexus.services.event_log.delivery_worker import EventDeliveryWorker

            delivery_worker = EventDeliveryWorker(
                record_store=ctx.record_store,
                poll_interval_ms=200,
                batch_size=50,
                use_row_locking=True,
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
    if not _on("resiliency"):
        logger.debug("[BOOT:SYSTEM] ResiliencyManager disabled by profile")
    else:
        try:
            from nexus.core.resiliency import (
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
        "eviction_manager": eviction_manager,
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
