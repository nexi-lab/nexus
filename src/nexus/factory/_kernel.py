"""Boot Tier 0 (KERNEL) — mandatory services that are fatal on failure."""

from __future__ import annotations

import logging
import time
from typing import Any, cast

from nexus.factory._boot_context import _BootContext

logger = logging.getLogger(__name__)


def _boot_kernel_services(ctx: _BootContext) -> dict[str, Any]:
    """Boot Tier 0 (KERNEL) — mandatory services that are fatal on failure.

    Creates ReBAC, permissions, workspace, and write-sync services.
    On failure: raises ``BootError`` and logs at CRITICAL.
    Does NOT call ``.start()`` on background threads — that is deferred to
    ``_start_background_services()``.

    Issue #2034: version_service and rebac_circuit_breaker moved to
    ``_boot_brick_services()`` (Tier 2) — they are optional features.

    Returns:
        Dict with 11 kernel service entries.
    """
    from nexus.contracts.exceptions import BootError

    t0 = time.perf_counter()
    try:
        # Config-time dialect flag (KERNEL-ARCHITECTURE §7)
        _is_pg = not ctx.db_url.startswith("sqlite")

        # --- ReBAC Manager ---
        from nexus.rebac.manager import EnhancedReBACManager

        rebac_manager = EnhancedReBACManager(
            engine=ctx.engine,
            cache_ttl_seconds=ctx.cache_ttl_seconds or 300,
            max_depth=10,
            enforce_zone_isolation=ctx.perm.enforce_zone_isolation,
            enable_graph_limits=True,
            enable_tiger_cache=ctx.perm.enable_tiger_cache,
            read_engine=ctx.read_engine,
            is_postgresql=_is_pg,
        )

        # --- Directory Visibility Cache ---
        from nexus.rebac.cache.visibility import DirectoryVisibilityCache

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

        # --- Audit Store ---
        from nexus.rebac.permissions_enhanced import AuditStore

        audit_store = AuditStore(engine=ctx.engine, is_postgresql=_is_pg)

        # --- Entity Registry ---
        from nexus.rebac.entity_registry import EntityRegistry

        entity_registry = EntityRegistry(ctx.record_store)

        # --- Permission Enforcer ---
        from nexus.rebac.enforcer import PermissionEnforcer

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

        # --- Hierarchy Manager ---
        from nexus.rebac.hierarchy_manager import HierarchyManager

        hierarchy_manager = HierarchyManager(
            rebac_manager=rebac_manager,
            enable_inheritance=ctx.perm.inherit,
        )

        # --- Deferred Permission Buffer (constructed, NOT started) ---
        from nexus.rebac.deferred_permission_buffer import DeferredPermissionBuffer

        deferred_permission_buffer = None
        if ctx.perm.enable_deferred:
            deferred_permission_buffer = DeferredPermissionBuffer(
                rebac_manager=rebac_manager,
                hierarchy_manager=hierarchy_manager,
                flush_interval_sec=ctx.perm.deferred_flush_interval,
            )

        # --- Workspace Registry ---
        from nexus.services.workspace.workspace_registry import WorkspaceRegistry

        workspace_registry = WorkspaceRegistry(
            metadata=ctx.metadata_store,
            rebac_manager=rebac_manager,
            record_store=ctx.record_store,
        )

        # --- Mount Manager ---
        from nexus.services.mount_manager import MountManager

        mount_manager = MountManager(ctx.record_store)

        # --- Workspace Manager ---
        from nexus.services.protocols.rebac import ReBACBrickProtocol
        from nexus.services.workspace_manager import WorkspaceManager

        workspace_manager = WorkspaceManager(
            metadata=ctx.metadata_store,
            backend=ctx.backend,
            rebac_manager=cast(ReBACBrickProtocol, rebac_manager),
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
            record_store=ctx.record_store,
        )

        # --- RecordStore Syncer (constructed, NOT started) ---
        import os

        write_observer: Any = None
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
            from nexus.storage.record_store_syncer import BufferedRecordStoreWriteObserver

            _st = ctx.profile_tuning.storage
            write_observer = BufferedRecordStoreWriteObserver(
                ctx.record_store,
                strict_mode=ctx.perm.audit_strict_mode,
                flush_interval_ms=_st.write_buffer_flush_ms,
                max_buffer_size=_st.write_buffer_max_size,
            )
        else:
            from nexus.storage.record_store_syncer import RecordStoreWriteObserver

            write_observer = RecordStoreWriteObserver(
                ctx.record_store,
                strict_mode=ctx.perm.audit_strict_mode,
            )

        result = {
            "rebac_manager": rebac_manager,
            "dir_visibility_cache": dir_visibility_cache,
            "audit_store": audit_store,
            "entity_registry": entity_registry,
            "permission_enforcer": permission_enforcer,
            "hierarchy_manager": hierarchy_manager,
            "deferred_permission_buffer": deferred_permission_buffer,
            "workspace_registry": workspace_registry,
            "mount_manager": mount_manager,
            "workspace_manager": workspace_manager,
            "write_observer": write_observer,
        }

        elapsed = time.perf_counter() - t0
        logger.info("[BOOT:KERNEL] %d services ready (%.3fs)", len(result), elapsed)
        return result

    except Exception as exc:
        logger.critical("[BOOT:KERNEL] Fatal: %s", exc)
        raise BootError(str(exc), tier="kernel") from exc
