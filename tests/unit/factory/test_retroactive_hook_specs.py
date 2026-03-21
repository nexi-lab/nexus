"""Unit tests for VFS hook HotSwappable conformance (Issue #1610/#1612/#1613/#1616).

All VFS hooks now implement HotSwappable — they self-describe via hook_spec().
_build_retroactive_hook_specs() has been deleted.  These tests verify that
every hook class satisfies the HotSwappable structural protocol and returns
the correct HookSpec.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.contracts.protocols.service_lifecycle import HotSwappable

# ---------------------------------------------------------------------------
# HotSwappable conformance — isinstance checks
# ---------------------------------------------------------------------------


class TestHotSwappableConformance:
    """Every VFS hook class satisfies HotSwappable protocol."""

    def test_permission_hook(self) -> None:
        from nexus.bricks.rebac.permission_hook import PermissionCheckHook

        hook = PermissionCheckHook(
            checker=MagicMock(),
            metadata_store=MagicMock(),
            default_context=MagicMock(),
        )
        assert isinstance(hook, HotSwappable)

    def test_audit_interceptor(self) -> None:
        from nexus.storage.write_observer_hooks import AuditWriteInterceptor

        hook = AuditWriteInterceptor(nx=MagicMock(), pipe_path="/nexus/pipes/audit")
        assert isinstance(hook, HotSwappable)

    def test_dynamic_viewer_hook(self) -> None:
        from nexus.bricks.rebac.dynamic_viewer_hook import DynamicViewerReadHook

        hook = DynamicViewerReadHook(
            get_subject=MagicMock(),
            get_viewer_config=MagicMock(),
            apply_filter=MagicMock(),
        )
        assert isinstance(hook, HotSwappable)

    def test_tiger_rename_hook(self) -> None:
        from nexus.bricks.rebac.cache.tiger.rename_hook import TigerCacheRenameHook

        hook = TigerCacheRenameHook(tiger_cache=MagicMock())
        assert isinstance(hook, HotSwappable)

    def test_tiger_write_hook(self) -> None:
        from nexus.bricks.rebac.cache.tiger.write_hook import TigerCacheWriteHook

        hook = TigerCacheWriteHook(tiger_cache=MagicMock())
        assert isinstance(hook, HotSwappable)

    def test_virtual_view_resolver(self) -> None:
        from nexus.bricks.parsers.virtual_view_resolver import VirtualViewResolver

        hook = VirtualViewResolver(
            metadata=MagicMock(),
            path_router=MagicMock(),
            permission_checker=MagicMock(),
        )
        assert isinstance(hook, HotSwappable)

    def test_auto_parse_hook(self) -> None:
        from nexus.bricks.parsers.auto_parse_hook import AutoParseWriteHook

        hook = AutoParseWriteHook(
            get_parser=MagicMock(),
            parse_fn=MagicMock(),
        )
        assert isinstance(hook, HotSwappable)

    def test_event_bus_observer(self) -> None:
        from nexus.system_services.event_bus.observer import EventBusObserver

        hook = EventBusObserver()
        assert isinstance(hook, HotSwappable)

    def test_revision_tracking_observer(self) -> None:
        from nexus.system_services.lifecycle.revision_tracking_observer import (
            RevisionTrackingObserver,
        )

        hook = RevisionTrackingObserver(revision_notifier=MagicMock())
        assert isinstance(hook, HotSwappable)

    def test_task_write_hook(self) -> None:
        from nexus.bricks.task_manager.write_hook import TaskWriteHook

        hook = TaskWriteHook()
        assert isinstance(hook, HotSwappable)


# ---------------------------------------------------------------------------
# hook_spec() — correct channels declared
# ---------------------------------------------------------------------------


class TestHookSpecDeclarations:
    """Each hook declares the correct HookSpec channels."""

    def test_permission_6_channels(self) -> None:
        from nexus.bricks.rebac.permission_hook import PermissionCheckHook

        hook = PermissionCheckHook(
            checker=MagicMock(),
            metadata_store=MagicMock(),
            default_context=MagicMock(),
        )
        spec = hook.hook_spec()
        assert spec.read_hooks == (hook,)
        assert spec.write_hooks == (hook,)
        assert spec.delete_hooks == (hook,)
        assert spec.rename_hooks == (hook,)
        assert spec.mkdir_hooks == (hook,)
        assert spec.rmdir_hooks == (hook,)
        assert spec.total_hooks == 6

    def test_audit_6_channels(self) -> None:
        from nexus.storage.write_observer_hooks import AuditWriteInterceptor

        hook = AuditWriteInterceptor(nx=MagicMock(), pipe_path="/nexus/pipes/audit")
        spec = hook.hook_spec()
        assert spec.write_hooks == (hook,)
        assert spec.write_batch_hooks == (hook,)
        assert spec.delete_hooks == (hook,)
        assert spec.rename_hooks == (hook,)
        assert spec.mkdir_hooks == (hook,)
        assert spec.rmdir_hooks == (hook,)
        assert spec.total_hooks == 6

    def test_viewer_1_channel(self) -> None:
        from nexus.bricks.rebac.dynamic_viewer_hook import DynamicViewerReadHook

        hook = DynamicViewerReadHook(
            get_subject=MagicMock(),
            get_viewer_config=MagicMock(),
            apply_filter=MagicMock(),
        )
        spec = hook.hook_spec()
        assert spec.read_hooks == (hook,)
        assert spec.total_hooks == 1

    def test_tiger_rename_1_channel(self) -> None:
        from nexus.bricks.rebac.cache.tiger.rename_hook import TigerCacheRenameHook

        hook = TigerCacheRenameHook(tiger_cache=MagicMock())
        spec = hook.hook_spec()
        assert spec.rename_hooks == (hook,)
        assert spec.total_hooks == 1

    def test_tiger_write_1_channel(self) -> None:
        from nexus.bricks.rebac.cache.tiger.write_hook import TigerCacheWriteHook

        hook = TigerCacheWriteHook(tiger_cache=MagicMock())
        spec = hook.hook_spec()
        assert spec.write_hooks == (hook,)
        assert spec.total_hooks == 1

    def test_virtual_view_1_resolver(self) -> None:
        from nexus.bricks.parsers.virtual_view_resolver import VirtualViewResolver

        hook = VirtualViewResolver(
            metadata=MagicMock(),
            path_router=MagicMock(),
            permission_checker=MagicMock(),
        )
        spec = hook.hook_spec()
        assert spec.resolvers == (hook,)
        assert spec.total_hooks == 1

    def test_auto_parse_1_channel(self) -> None:
        from nexus.bricks.parsers.auto_parse_hook import AutoParseWriteHook

        hook = AutoParseWriteHook(
            get_parser=MagicMock(),
            parse_fn=MagicMock(),
        )
        spec = hook.hook_spec()
        assert spec.write_hooks == (hook,)
        assert spec.total_hooks == 1

    def test_event_bus_observer_1_channel(self) -> None:
        from nexus.system_services.event_bus.observer import EventBusObserver

        hook = EventBusObserver()
        spec = hook.hook_spec()
        assert spec.observers == (hook,)
        assert spec.total_hooks == 1

    def test_revision_observer_1_channel(self) -> None:
        from nexus.system_services.lifecycle.revision_tracking_observer import (
            RevisionTrackingObserver,
        )

        hook = RevisionTrackingObserver(revision_notifier=MagicMock())
        spec = hook.hook_spec()
        assert spec.observers == (hook,)
        assert spec.total_hooks == 1

    def test_task_write_1_channel(self) -> None:
        from nexus.bricks.task_manager.write_hook import TaskWriteHook

        hook = TaskWriteHook()
        spec = hook.hook_spec()
        assert spec.write_hooks == (hook,)
        assert spec.total_hooks == 1


# ---------------------------------------------------------------------------
# drain() / activate() — lifecycle methods
# ---------------------------------------------------------------------------


class TestDrainActivate:
    """drain() and activate() are callable and don't raise."""

    @pytest.mark.asyncio
    async def test_permission_hook_lifecycle(self) -> None:
        from nexus.bricks.rebac.permission_hook import PermissionCheckHook

        hook = PermissionCheckHook(
            checker=MagicMock(),
            metadata_store=MagicMock(),
            default_context=MagicMock(),
        )
        await hook.drain()
        await hook.activate()

    @pytest.mark.asyncio
    async def test_audit_interceptor_lifecycle(self) -> None:
        from nexus.storage.write_observer_hooks import AuditWriteInterceptor

        hook = AuditWriteInterceptor(nx=MagicMock(), pipe_path="/nexus/pipes/audit")
        await hook.drain()
        await hook.activate()

    @pytest.mark.asyncio
    async def test_auto_parse_drain_calls_shutdown(self) -> None:
        from nexus.bricks.parsers.auto_parse_hook import AutoParseWriteHook

        hook = AutoParseWriteHook(
            get_parser=MagicMock(),
            parse_fn=MagicMock(),
        )
        # drain() calls shutdown() which drains threads
        await hook.drain()
        await hook.activate()

    @pytest.mark.asyncio
    async def test_virtual_view_resolver_lifecycle(self) -> None:
        from nexus.bricks.parsers.virtual_view_resolver import VirtualViewResolver

        hook = VirtualViewResolver(
            metadata=MagicMock(),
            path_router=MagicMock(),
            permission_checker=MagicMock(),
        )
        await hook.drain()
        await hook.activate()

    @pytest.mark.asyncio
    async def test_event_bus_observer_lifecycle(self) -> None:
        from nexus.system_services.event_bus.observer import EventBusObserver

        hook = EventBusObserver()
        await hook.drain()
        await hook.activate()

    @pytest.mark.asyncio
    async def test_revision_observer_lifecycle(self) -> None:
        from nexus.system_services.lifecycle.revision_tracking_observer import (
            RevisionTrackingObserver,
        )

        hook = RevisionTrackingObserver(revision_notifier=MagicMock())
        await hook.drain()
        await hook.activate()

    @pytest.mark.asyncio
    async def test_task_write_hook_lifecycle(self) -> None:
        from nexus.bricks.task_manager.write_hook import TaskWriteHook

        hook = TaskWriteHook()
        await hook.drain()
        await hook.activate()


# ---------------------------------------------------------------------------
# _build_retroactive_hook_specs deleted — verify import removed
# ---------------------------------------------------------------------------


class TestRetroactiveHookSpecsDeleted:
    """_build_retroactive_hook_specs() has been deleted (Issue #1616)."""

    def test_function_no_longer_importable(self) -> None:
        """The retroactive function should not exist in orchestrator."""
        from nexus.factory import orchestrator

        assert not hasattr(orchestrator, "_build_retroactive_hook_specs")
