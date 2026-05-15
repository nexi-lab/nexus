"""Unit tests for SyncPermissionWriteHook (Issue #1773)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("pyroaring")


from nexus.bricks.rebac.sync_permission_hook import SyncPermissionWriteHook
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.vfs_hooks import WriteHookContext


@pytest.fixture()
def mock_hier() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def mock_rebac() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def hook(mock_hier: MagicMock, mock_rebac: MagicMock) -> SyncPermissionWriteHook:
    return SyncPermissionWriteHook(hierarchy_manager=mock_hier, rebac_manager=mock_rebac)


def _make_context(**overrides: object) -> MagicMock:
    ctx = MagicMock()
    ctx.user_id = overrides.get("user_id", "user-1")
    ctx.is_system = overrides.get("is_system", False)
    return ctx


# ── HookSpec ──────────────────────────────────────────────────────────


class TestHookSpec:
    def test_hook_spec_declares_write(self, hook: SyncPermissionWriteHook) -> None:
        spec = hook.hook_spec()
        assert hook in spec.write_hooks

    def test_name(self, hook: SyncPermissionWriteHook) -> None:
        assert hook.name == "sync_permission"


# ── on_post_write ─────────────────────────────────────────────────────


class TestOnPostWrite:
    def test_calls_ensure_parent_tuples(
        self, hook: SyncPermissionWriteHook, mock_hier: MagicMock, mock_rebac: MagicMock
    ) -> None:
        ctx = WriteHookContext(
            path="/dir/file.txt",
            content=b"data",
            context=_make_context(),
            zone_id="zone-a",
            is_new_file=False,
        )
        hook.on_post_write(ctx)

        mock_hier.ensure_parent_tuples.assert_called_once_with("/dir/file.txt", zone_id="zone-a")
        mock_rebac.rebac_write.assert_not_called()

    def test_grants_owner_for_new_file(
        self, hook: SyncPermissionWriteHook, mock_hier: MagicMock, mock_rebac: MagicMock
    ) -> None:
        ctx = WriteHookContext(
            path="/new.txt",
            content=b"data",
            context=_make_context(user_id="alice"),
            zone_id="zone-b",
            is_new_file=True,
        )
        hook.on_post_write(ctx)

        mock_hier.ensure_parent_tuples.assert_called_once()
        mock_rebac.rebac_write.assert_called_once_with(
            subject=("user", "alice"),
            relation="direct_owner",
            object=("file", "/new.txt"),
            zone_id="zone-b",
        )

    def test_skips_owner_grant_for_system(
        self, hook: SyncPermissionWriteHook, mock_rebac: MagicMock
    ) -> None:
        ctx = WriteHookContext(
            path="/new.txt",
            content=b"data",
            context=_make_context(is_system=True),
            zone_id="z",
            is_new_file=True,
        )
        hook.on_post_write(ctx)
        mock_rebac.rebac_write.assert_not_called()

    def test_defaults_zone_to_root(
        self, hook: SyncPermissionWriteHook, mock_hier: MagicMock
    ) -> None:
        ctx = WriteHookContext(
            path="/file.txt",
            content=b"x",
            context=None,
            zone_id=None,
            is_new_file=False,
        )
        hook.on_post_write(ctx)
        mock_hier.ensure_parent_tuples.assert_called_once_with("/file.txt", zone_id=ROOT_ZONE_ID)

    def test_hierarchy_error_becomes_warning(
        self, hook: SyncPermissionWriteHook, mock_hier: MagicMock
    ) -> None:
        mock_hier.ensure_parent_tuples.side_effect = RuntimeError("db down")
        ctx = WriteHookContext(
            path="/f.txt",
            content=b"x",
            context=None,
            zone_id="z",
            is_new_file=False,
        )
        hook.on_post_write(ctx)

        assert len(ctx.warnings) == 1
        assert ctx.warnings[0].component == "sync_permission"
        assert "ensure_parent_tuples" in ctx.warnings[0].message

    def test_owner_grant_error_becomes_warning(
        self, hook: SyncPermissionWriteHook, mock_rebac: MagicMock
    ) -> None:
        mock_rebac.rebac_write.side_effect = RuntimeError("fail")
        ctx = WriteHookContext(
            path="/new.txt",
            content=b"x",
            context=_make_context(user_id="bob"),
            zone_id="z",
            is_new_file=True,
        )
        hook.on_post_write(ctx)

        assert len(ctx.warnings) == 1
        assert "owner grant" in ctx.warnings[0].message

    def test_works_with_none_managers(self) -> None:
        hook = SyncPermissionWriteHook(hierarchy_manager=None, rebac_manager=None)
        ctx = WriteHookContext(
            path="/f.txt",
            content=b"x",
            context=_make_context(),
            zone_id="z",
            is_new_file=True,
        )
        hook.on_post_write(ctx)
        assert len(ctx.warnings) == 0
