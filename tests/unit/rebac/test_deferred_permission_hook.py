"""Unit tests for DeferredPermissionHook (Issue #1773)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("pyroaring")


from nexus.bricks.rebac.deferred_permission_hook import DeferredPermissionHook
from nexus.contracts.vfs_hooks import WriteHookContext


@pytest.fixture()
def mock_buf() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def hook(mock_buf: MagicMock) -> DeferredPermissionHook:
    return DeferredPermissionHook(mock_buf)


def _make_context(**overrides: object) -> MagicMock:
    ctx = MagicMock()
    ctx.user_id = overrides.get("user_id", "user-1")
    ctx.is_system = overrides.get("is_system", False)
    ctx.zone_id = overrides.get("zone_id", "zone-a")
    return ctx


# ── HookSpec protocol ────────────────────────────────────────────────


class TestHookSpec:
    def test_hook_spec_declares_write(self, hook: DeferredPermissionHook) -> None:
        spec = hook.hook_spec()
        assert hook in spec.write_hooks
        assert spec.delete_hooks == ()

    def test_name(self, hook: DeferredPermissionHook) -> None:
        assert hook.name == "deferred_permission"


# ── on_post_write ─────────────────────────────────────────────────────


class TestOnPostWrite:
    def test_queues_hierarchy_always(
        self, hook: DeferredPermissionHook, mock_buf: MagicMock
    ) -> None:
        ctx = WriteHookContext(
            path="/dir/file.txt",
            content=b"data",
            context=_make_context(),
            zone_id="zone-a",
            is_new_file=False,
        )
        hook.on_post_write(ctx)

        mock_buf.queue_hierarchy.assert_called_once_with("/dir/file.txt", "zone-a")
        mock_buf.queue_owner_grant.assert_not_called()

    def test_queues_owner_grant_for_new_file(
        self, hook: DeferredPermissionHook, mock_buf: MagicMock
    ) -> None:
        ctx = WriteHookContext(
            path="/new.txt",
            content=b"data",
            context=_make_context(user_id="alice"),
            zone_id="zone-b",
            is_new_file=True,
        )
        hook.on_post_write(ctx)

        mock_buf.queue_hierarchy.assert_called_once_with("/new.txt", "zone-b")
        mock_buf.queue_owner_grant.assert_called_once_with("alice", "/new.txt", "zone-b")

    def test_skips_owner_grant_for_system_user(
        self, hook: DeferredPermissionHook, mock_buf: MagicMock
    ) -> None:
        ctx = WriteHookContext(
            path="/new.txt",
            content=b"data",
            context=_make_context(is_system=True),
            zone_id="zone-a",
            is_new_file=True,
        )
        hook.on_post_write(ctx)

        mock_buf.queue_hierarchy.assert_called_once()
        mock_buf.queue_owner_grant.assert_not_called()

    def test_skips_owner_grant_when_no_user_id(
        self, hook: DeferredPermissionHook, mock_buf: MagicMock
    ) -> None:
        ctx = WriteHookContext(
            path="/new.txt",
            content=b"data",
            context=_make_context(user_id=None),
            zone_id="zone-a",
            is_new_file=True,
        )
        hook.on_post_write(ctx)

        mock_buf.queue_hierarchy.assert_called_once()
        mock_buf.queue_owner_grant.assert_not_called()

    def test_defaults_zone_to_root(self, hook: DeferredPermissionHook, mock_buf: MagicMock) -> None:
        ctx = WriteHookContext(
            path="/file.txt",
            content=b"data",
            context=None,
            zone_id=None,
            is_new_file=False,
        )
        hook.on_post_write(ctx)
        mock_buf.queue_hierarchy.assert_called_once_with("/file.txt", "root")

    def test_exception_becomes_warning(
        self, hook: DeferredPermissionHook, mock_buf: MagicMock
    ) -> None:
        mock_buf.queue_hierarchy.side_effect = RuntimeError("buffer full")
        ctx = WriteHookContext(
            path="/file.txt",
            content=b"data",
            context=None,
            zone_id="z",
            is_new_file=False,
        )
        hook.on_post_write(ctx)

        assert len(ctx.warnings) == 1
        assert ctx.warnings[0].severity == "degraded"
        assert ctx.warnings[0].component == "deferred_permission"
        assert "buffer full" in ctx.warnings[0].message

    def test_skips_owner_grant_when_no_context(
        self, hook: DeferredPermissionHook, mock_buf: MagicMock
    ) -> None:
        ctx = WriteHookContext(
            path="/new.txt",
            content=b"data",
            context=None,
            zone_id="zone-a",
            is_new_file=True,
        )
        hook.on_post_write(ctx)

        mock_buf.queue_hierarchy.assert_called_once()
        mock_buf.queue_owner_grant.assert_not_called()
