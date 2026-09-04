"""DeferredPermissionHook — admin-bypass owner-grant skip.

Under ``PermissionConfig.allow_admin_bypass`` an admin subject's creator
``direct_owner`` tuple never takes part in a decision, yet since #4739 every
new file written by an admin paid a synchronous ``rebac_write`` + three Tiger
write-throughs for it (measured 130–190 ms per write against a remote
Postgres, plus three tuple rows per file). The orchestrator now constructs the
hook with ``skip_admin_owner_grant=True`` in that configuration; these tests
pin the hook-level contract and the config → orchestrator derivation.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.bricks.rebac.deferred_permission_hook import DeferredPermissionHook
from nexus.contracts.types import OperationContext
from nexus.contracts.vfs_hooks import (
    MkdirHookContext,
    WriteBatchHookContext,
    WriteHookContext,
)
from nexus.core.config import PermissionConfig


def _ctx(user_id: str = "admin", *, is_admin: bool = True) -> OperationContext:
    return OperationContext(user_id=user_id, groups=[], zone_id="root", is_admin=is_admin)


@pytest.fixture
def buf() -> MagicMock:
    return MagicMock(name="deferred_buffer")


@pytest.fixture
def rebac() -> MagicMock:
    return MagicMock(name="rebac_manager")


def _write_ctx(user_ctx: OperationContext, *, is_new: bool = True) -> WriteHookContext:
    return WriteHookContext(
        path="/ws/a.txt", content=b"x", context=user_ctx, zone_id="root", is_new_file=is_new
    )


class TestSkipAdminOwnerGrant:
    def test_admin_writer_gets_no_grant_sync_or_deferred(self, buf, rebac):
        hook = DeferredPermissionHook(buf, rebac_manager=rebac, skip_admin_owner_grant=True)
        ctx = _write_ctx(_ctx())

        hook.on_post_write(ctx)

        rebac.rebac_write.assert_not_called()
        buf.queue_owner_grant.assert_not_called()
        # Hierarchy tuples are unaffected: sharing/inheritance still work.
        buf.queue_hierarchy.assert_called_once_with("/ws/a.txt", "root")
        assert ctx.warnings == []

    def test_non_admin_writer_still_gets_sync_grant(self, buf, rebac):
        hook = DeferredPermissionHook(buf, rebac_manager=rebac, skip_admin_owner_grant=True)
        ctx = _write_ctx(_ctx("alice", is_admin=False))

        hook.on_post_write(ctx)

        rebac.rebac_write.assert_called_once_with(
            subject=("user", "alice"),
            relation="direct_owner",
            object=("file", "/ws/a.txt"),
            zone_id="root",
        )
        buf.queue_owner_grant.assert_not_called()

    def test_default_keeps_admin_grant(self, buf, rebac):
        """Without the flag (no admin bypass) an admin is an ordinary subject."""
        hook = DeferredPermissionHook(buf, rebac_manager=rebac)

        hook.on_post_write(_write_ctx(_ctx()))

        rebac.rebac_write.assert_called_once()
        assert rebac.rebac_write.call_args.kwargs["subject"] == ("user", "admin")

    def test_skip_also_applies_when_grant_would_be_deferred(self, buf, rebac):
        hook = DeferredPermissionHook(
            buf, rebac_manager=rebac, sync_owner_grant=False, skip_admin_owner_grant=True
        )

        hook.on_post_write(_write_ctx(_ctx()))

        rebac.rebac_write.assert_not_called()
        buf.queue_owner_grant.assert_not_called()

    def test_mkdir_skips_admin_grant(self, buf, rebac):
        hook = DeferredPermissionHook(buf, rebac_manager=rebac, skip_admin_owner_grant=True)

        hook.on_post_mkdir(MkdirHookContext(path="/ws/dir", context=_ctx(), zone_id="z1"))

        rebac.rebac_write.assert_not_called()
        buf.queue_owner_grant.assert_not_called()
        buf.queue_hierarchy.assert_called_once_with("/ws/dir", "z1")

    def test_batch_skips_admin_grant_but_keeps_hierarchy(self, buf, rebac):
        hook = DeferredPermissionHook(buf, rebac_manager=rebac, skip_admin_owner_grant=True)
        m1, m2 = MagicMock(path="/ws/n1.txt"), MagicMock(path="/ws/n2.txt")
        ctx = WriteBatchHookContext(items=[(m1, True), (m2, True)], context=_ctx(), zone_id="root")

        hook.on_post_write_batch(ctx)

        rebac.rebac_write_batch.assert_not_called()
        rebac.rebac_write.assert_not_called()
        buf.queue_owner_grant.assert_not_called()
        assert buf.queue_hierarchy.call_count == 2

    def test_batch_non_admin_still_granted(self, buf, rebac):
        hook = DeferredPermissionHook(buf, rebac_manager=rebac, skip_admin_owner_grant=True)
        m1 = MagicMock(path="/ws/n1.txt")
        ctx = WriteBatchHookContext(
            items=[(m1, True)], context=_ctx("bob", is_admin=False), zone_id="root"
        )

        hook.on_post_write_batch(ctx)

        rebac.rebac_write_batch.assert_called_once()
        grants = rebac.rebac_write_batch.call_args.args[0]
        assert grants == [
            {
                "subject": ("user", "bob"),
                "relation": "direct_owner",
                "object": ("file", "/ws/n1.txt"),
                "zone_id": "root",
            }
        ]


class TestPermissionConfigDerivation:
    """The orchestrator derives the flag as ``allow_admin_bypass and not owner_grant_admin_bypass``."""

    @staticmethod
    def _skip(cfg: PermissionConfig) -> bool:
        return bool(cfg.allow_admin_bypass) and not bool(cfg.owner_grant_admin_bypass)

    def test_defaults_do_not_skip(self):
        assert self._skip(PermissionConfig()) is False

    def test_admin_bypass_skips_by_default(self):
        assert self._skip(PermissionConfig(allow_admin_bypass=True)) is True

    def test_opt_in_keeps_admin_grant_under_bypass(self):
        cfg = PermissionConfig(allow_admin_bypass=True, owner_grant_admin_bypass=True)
        assert self._skip(cfg) is False

    def test_flag_is_inert_without_bypass(self):
        cfg = PermissionConfig(allow_admin_bypass=False, owner_grant_admin_bypass=False)
        assert self._skip(cfg) is False
