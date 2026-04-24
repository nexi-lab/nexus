"""Unit tests for VFS hook conformance (Issue #1610/#1612/#1613/#1616).

All VFS hooks now self-describe via hook_spec().
_build_retroactive_hook_specs() has been deleted.  These tests verify that
every hook class exposes hook_spec and returns the correct HookSpec.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("pyroaring")


# ---------------------------------------------------------------------------
# hook_spec conformance — hasattr checks
# ---------------------------------------------------------------------------


class TestHookSpecConformance:
    """Every VFS hook class exposes hook_spec."""

    def test_permission_hook(self) -> None:
        from nexus.bricks.rebac.permission_hook import PermissionCheckHook

        hook = PermissionCheckHook(
            checker=MagicMock(),
            metadata_store=MagicMock(),
            default_context=MagicMock(),
        )
        assert hasattr(hook, "hook_spec")

    def test_audit_interceptor(self) -> None:
        from nexus.storage.write_observer_hooks import AuditWriteInterceptor

        hook = AuditWriteInterceptor(nx=MagicMock(), pipe_path="/nexus/pipes/audit")
        assert hasattr(hook, "hook_spec")

    def test_dynamic_viewer_hook(self) -> None:
        from nexus.bricks.rebac.dynamic_viewer_hook import DynamicViewerReadHook

        hook = DynamicViewerReadHook(
            get_subject=MagicMock(),
            get_viewer_config=MagicMock(),
            apply_filter=MagicMock(),
        )
        assert hasattr(hook, "hook_spec")

    def test_tiger_rename_hook(self) -> None:
        from nexus.bricks.rebac.cache.tiger.rename_hook import TigerCacheRenameHook

        hook = TigerCacheRenameHook(tiger_cache=MagicMock())
        assert hasattr(hook, "hook_spec")

    def test_tiger_write_hook(self) -> None:
        from nexus.bricks.rebac.cache.tiger.write_hook import TigerCacheWriteHook

        hook = TigerCacheWriteHook(tiger_cache=MagicMock())
        assert hasattr(hook, "hook_spec")

    def test_virtual_view_resolver(self) -> None:
        from nexus.bricks.parsers.virtual_view_resolver import VirtualViewResolver

        hook = VirtualViewResolver(
            metadata=MagicMock(),
            permission_checker=MagicMock(),
        )
        assert hasattr(hook, "hook_spec")

    def test_auto_parse_hook(self) -> None:
        from nexus.bricks.parsers.auto_parse_hook import AutoParseWriteHook

        hook = AutoParseWriteHook(
            get_parser=MagicMock(),
            parse_fn=MagicMock(),
        )
        assert hasattr(hook, "hook_spec")

    def test_event_bus_observer_no_hook_spec(self) -> None:
        """EventBusObserver no longer has hook_spec — Rust kernel dispatches directly."""
        from nexus.services.event_bus.observer import EventBusObserver

        hook = EventBusObserver()
        assert not hasattr(hook, "hook_spec")

    def test_revision_tracking_deleted(self) -> None:
        """RevisionTrackingObserver deleted (§10 A2) — kernel primitive."""
        pass

    def test_task_write_hook(self) -> None:
        from nexus.bricks.task_manager.write_hook import TaskWriteHook

        hook = TaskWriteHook()
        assert hasattr(hook, "hook_spec")


# ---------------------------------------------------------------------------
# hook_spec() — correct channels declared
# ---------------------------------------------------------------------------


class TestHookSpecDeclarations:
    """Each hook declares the correct HookSpec channels."""

    def test_permission_8_channels(self) -> None:
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
        assert spec.copy_hooks == (hook,)
        assert spec.mkdir_hooks == (hook,)
        assert spec.rmdir_hooks == (hook,)
        assert spec.stat_hooks == (hook,)
        assert spec.access_hooks == (hook,)
        assert spec.total_hooks == 9

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

    def test_event_bus_observer_no_hook_spec(self) -> None:
        """EventBusObserver no longer has hook_spec — Rust kernel dispatches directly."""
        pass

    def test_revision_observer_deleted(self) -> None:
        """RevisionTrackingObserver deleted (§10 A2) — kernel primitive."""
        pass

    def test_task_write_1_channel(self) -> None:
        from nexus.bricks.task_manager.write_hook import TaskWriteHook

        hook = TaskWriteHook()
        spec = hook.hook_spec()
        assert spec.write_hooks == (hook,)
        assert spec.total_hooks == 1
