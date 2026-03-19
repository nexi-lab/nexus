"""VFS hook registration — extracted from orchestrator.py.

All hooks are enlisted via coordinator.enlist() — the single entry point
for service registration (Issue #1708/1709).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


async def register_vfs_hooks(
    nx: NexusFS,
    *,
    permission_checker: Any = None,
    auto_parse: bool = True,
    brick_on: Callable[[str], bool] | None = None,
    parse_fn: Any = None,
) -> None:
    """Register hooks + observers via coordinator.enlist() (Issue #900, #1709).

    Kernel creates KernelDispatch with empty callback lists at init.
    This function populates them at boot — modules register into
    kernel-owned infrastructure, kernel never auto-constructs hooks.

    Issue #1708/1709: All hooks enlisted via coordinator.enlist() —
    single entry point, no fallback.  Coordinator is always available
    (created in _do_link for local profiles, _boot_remote_services for REMOTE).
    """
    from nexus.factory._helpers import _make_gate

    _on = _make_gate(brick_on)

    _coordinator = nx._service_coordinator

    async def _enlist(name: str, hook: Any) -> None:
        """Enlist hook via coordinator — the single entry point."""
        await _coordinator.enlist(name, hook)

    # ── Permission pre-intercept hook (Issue #899) ────────────────
    if permission_checker is not None:
        from nexus.bricks.rebac.permission_hook import PermissionCheckHook

        _perm_hook = PermissionCheckHook(
            checker=permission_checker,
            metadata_store=nx.metadata,
            default_context=nx._default_context,
            enforce_permissions=nx._enforce_permissions,
            permission_enforcer=nx._system_services.permission_enforcer
            if nx._system_services
            else None,
            descendant_checker=getattr(nx, "_descendant_checker", None),
        )
        await _enlist("permission", _perm_hook)

    # ── Audit write observer as interceptor (Issue #900) ──────────
    # Registered FIRST so it runs before other hooks (audit before side effects).
    write_observer = (
        getattr(nx._system_services, "write_observer", None) if nx._system_services else None
    )
    if write_observer is not None:
        from nexus.storage.write_observer_hooks import AuditWriteInterceptor

        strict = getattr(write_observer, "_strict_mode", True)
        audit = AuditWriteInterceptor(write_observer, strict_mode=strict)
        await _enlist("audit", audit)

    # DynamicViewerReadHook (post-read: column-level CSV filtering)
    has_viewer = (
        (getattr(nx._system_services, "rebac_manager", None) if nx._system_services else None)
        is not None
        and hasattr(nx, "get_dynamic_viewer_config")
        and hasattr(nx, "apply_dynamic_viewer_filter")
    )
    if has_viewer:
        from nexus.bricks.rebac.dynamic_viewer_hook import DynamicViewerReadHook
        from nexus.lib.context_utils import get_subject_from_context

        _viewer_hook = DynamicViewerReadHook(
            get_subject=get_subject_from_context,
            get_viewer_config=nx.get_dynamic_viewer_config,
            apply_filter=nx.apply_dynamic_viewer_filter,
        )
        await _enlist("viewer", _viewer_hook)

    # ContentParserEngine (on-demand parsed reads — Issue #1383)
    from nexus.bricks.parsers.engine import ContentParserEngine

    ContentParserEngine(
        metadata=nx.metadata,
        provider_registry=nx._brick_services.provider_registry,
    )

    # AutoParseWriteHook (post-write: background parsing + cache invalidation)
    parser_reg = nx._brick_services.parser_registry
    if auto_parse and parser_reg is not None and parse_fn is not None:
        from nexus.bricks.parsers.auto_parse_hook import AutoParseWriteHook

        _auto_parse_hook = AutoParseWriteHook(
            get_parser=parser_reg.get_parser,
            parse_fn=parse_fn,
            metadata=nx.metadata,
        )
        await _enlist("auto_parse", _auto_parse_hook)

    # TigerCacheRenameHook (post-rename: bitmap updates)
    _rebac_mgr = (
        getattr(nx._system_services, "rebac_manager", None) if nx._system_services else None
    )
    tiger_cache = getattr(_rebac_mgr, "_tiger_cache", None) if _rebac_mgr else None
    if tiger_cache is not None:
        from nexus.bricks.rebac.cache.tiger.rename_hook import TigerCacheRenameHook

        def _metadata_list_iter(
            prefix: str,
            recursive: bool = True,
            zone_id: str = "root",  # noqa: ARG001
        ) -> Any:
            return nx.metadata.list(prefix=prefix, recursive=recursive)

        _tiger_rename_hook = TigerCacheRenameHook(
            tiger_cache=tiger_cache,
            metadata_list_iter=_metadata_list_iter,
        )
        await _enlist("tiger_rename", _tiger_rename_hook)

    # TigerCacheWriteHook (post-write: add new files to ancestor directory grants)
    if tiger_cache is not None:
        from nexus.bricks.rebac.cache.tiger.write_hook import TigerCacheWriteHook

        _tiger_write_hook = TigerCacheWriteHook(tiger_cache=tiger_cache)
        await _enlist("tiger_write", _tiger_write_hook)

    # ── PRE-DISPATCH: Virtual view resolver (Issue #332, #889) ────────
    from nexus.bricks.parsers.virtual_view_resolver import VirtualViewResolver

    _vview_resolver = VirtualViewResolver(
        metadata=nx.metadata,
        path_router=nx.router,
        permission_checker=permission_checker,
        parse_fn=parse_fn,
        read_tracker_fn=None,
    )
    await _enlist("virtual_view", _vview_resolver)

    # ── ProcResolver (procfs virtual filesystem for ProcessTable — Issue #1570) ──
    _proc_table = (
        getattr(nx._system_services, "process_table", None) if nx._system_services else None
    )
    if _proc_table is not None:
        try:
            from nexus.system_services.proc.proc_resolver import ProcResolver

            _proc_resolver = ProcResolver(_proc_table)
            await _enlist("proc", _proc_resolver)
        except Exception as exc:
            logger.debug("[BOOT:HOOKS] ProcResolver unavailable: %s", exc)

    # ── TaskWriteHook + TaskDispatchPipeConsumer + TaskAgentResolver ───────────
    if _on("task_manager"):
        try:
            from nexus.bricks.task_manager.service import TaskManagerService
            from nexus.bricks.task_manager.task_agent_resolver import TaskAgentResolver
            from nexus.bricks.task_manager.write_hook import TaskWriteHook

            _task_svc = TaskManagerService(nexus_fs=nx)
            _task_write_hook = TaskWriteHook()

            # Wire consumer from brick_services (created in _bricks.py)
            _task_consumer = getattr(nx._brick_services, "task_dispatch_consumer", None)
            if _task_consumer is not None:
                _task_write_hook.register_handler(_task_consumer)
                _task_consumer.set_task_service(_task_svc)

            await _enlist("task_write", _task_write_hook)
            await _enlist("task_agent_resolver", TaskAgentResolver(_proc_table))
            await _enlist("task_manager", _task_svc)  # Issue #1768: Q1 service via coordinator
        except Exception as exc:
            logger.warning("[BOOT:BRICK] task_manager wiring failed: %s", exc)
    else:
        logger.debug("[BOOT:BRICK] task_manager disabled by profile")

    # ── Snapshot write tracker (Issue #1770) ─────────────────────────
    _snapshot_svc = getattr(nx._brick_services, "snapshot_service", None)
    if _snapshot_svc is not None:
        from nexus.bricks.snapshot.snapshot_hook import SnapshotWriteHook

        await _enlist("snapshot_write", SnapshotWriteHook(_snapshot_svc))

    # ── Deferred permission buffer (Issue #1773) ──────────────────────
    _dpb = (
        getattr(nx._system_services, "deferred_permission_buffer", None)
        if nx._system_services
        else None
    )
    if _dpb is not None:
        from nexus.bricks.rebac.deferred_permission_hook import DeferredPermissionHook

        await _enlist("deferred_permission", DeferredPermissionHook(_dpb))
    else:
        # Sync fallback — same logic, runs as post-write hook instead of inline kernel code
        _hier = (
            getattr(nx._system_services, "hierarchy_manager", None) if nx._system_services else None
        )
        _rebac = getattr(nx, "_rebac_manager", None)
        if _hier is not None or _rebac is not None:
            from nexus.bricks.rebac.sync_permission_hook import SyncPermissionWriteHook

            await _enlist(
                "sync_permission",
                SyncPermissionWriteHook(hierarchy_manager=_hier, rebac_manager=_rebac),
            )

    # ── OBSERVE observers (Issue #900, #922) ──────────────────────────
    # EventBusObserver: forwards FileEvents to distributed EventBus (Redis/NATS).
    # Replaces _publish_file_event() direct calls — single dispatch exit point.
    # Issue #1701: event_bus is now Tier 1 (SystemServices).  Direct injection —
    # no bus_provider late-binding needed.  Tests use swap_service() to replace.
    from nexus.system_services.event_bus.observer import EventBusObserver

    _bus_observer = EventBusObserver(
        event_bus=nx._system_services.event_bus if nx._system_services else None
    )
    await _enlist("event_bus_observer", _bus_observer)

    # EventsService observer: self-registered via HotSwappable.hook_spec()
    # at bootstrap() → activate_hot_swappable_services() (Issue #1611).

    # RevisionTrackingObserver: feeds RevisionNotifier on versioned mutations.
    # Replaces the old kernel-internal _increment_vfs_revision() (Issue #1382).
    from nexus.lib.revision_notifier import RevisionNotifier
    from nexus.system_services.lifecycle.revision_tracking_observer import RevisionTrackingObserver

    _rev_notifier = RevisionNotifier()
    _rev_observer = RevisionTrackingObserver(revision_notifier=_rev_notifier)
    await _enlist("revision_tracking", _rev_observer)

    # ── Test hooks (Issue #2) ────────────────────────────────────────
    # Only registered when NEXUS_TEST_HOOKS=true for E2E hook testing.
    import os

    if os.getenv("NEXUS_TEST_HOOKS") == "true":
        from nexus.core.test_hooks import register_test_hooks

        register_test_hooks(nx._dispatch)
