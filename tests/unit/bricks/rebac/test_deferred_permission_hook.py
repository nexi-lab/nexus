"""DeferredPermissionHook — sync owner grant, deferred hierarchy (Issue #4739).

With ``enable_deferred=True`` the hook used to queue *both* the hierarchy
tuples and the creator's ``direct_owner`` grant, so a non-admin writer's own
``list`` / ``search`` / ``check`` could miss the new file until the buffer
flushed.  The owner grant is now written synchronously through the ReBAC
manager; only the hierarchy tuples stay deferred.
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


def _ctx(user_id: str = "alice", *, is_system: bool = False) -> OperationContext:
    return OperationContext(user_id=user_id, groups=[], zone_id="root", is_system=is_system)


@pytest.fixture
def buf() -> MagicMock:
    return MagicMock(name="deferred_buffer")


@pytest.fixture
def rebac() -> MagicMock:
    return MagicMock(name="rebac_manager")


class TestWriteHook:
    def test_new_file_owner_grant_is_written_synchronously(self, buf, rebac):
        hook = DeferredPermissionHook(buf, rebac_manager=rebac)
        ctx = WriteHookContext(
            path="/ws/a.txt", content=b"x", context=_ctx(), zone_id="root", is_new_file=True
        )

        hook.on_post_write(ctx)

        rebac.rebac_write.assert_called_once_with(
            subject=("user", "alice"),
            relation="direct_owner",
            object=("file", "/ws/a.txt"),
            zone_id="root",
        )
        buf.queue_owner_grant.assert_not_called()
        # Hierarchy tuples stay deferred.
        buf.queue_hierarchy.assert_called_once_with("/ws/a.txt", "root")
        assert ctx.warnings == []

    def test_existing_file_does_not_regrant_ownership(self, buf, rebac):
        hook = DeferredPermissionHook(buf, rebac_manager=rebac)
        ctx = WriteHookContext(
            path="/ws/a.txt", content=b"x", context=_ctx(), zone_id="root", is_new_file=False
        )

        hook.on_post_write(ctx)

        rebac.rebac_write.assert_not_called()
        buf.queue_owner_grant.assert_not_called()
        buf.queue_hierarchy.assert_called_once_with("/ws/a.txt", "root")

    def test_system_context_gets_no_owner_grant(self, buf, rebac):
        hook = DeferredPermissionHook(buf, rebac_manager=rebac)
        ctx = WriteHookContext(
            path="/ws/a.txt",
            content=b"x",
            context=_ctx("system", is_system=True),
            zone_id="root",
            is_new_file=True,
        )

        hook.on_post_write(ctx)

        rebac.rebac_write.assert_not_called()
        buf.queue_owner_grant.assert_not_called()

    def test_sync_failure_falls_back_to_queue_with_degraded_warning(self, buf, rebac):
        rebac.rebac_write.side_effect = RuntimeError("db down")
        hook = DeferredPermissionHook(buf, rebac_manager=rebac)
        ctx = WriteHookContext(
            path="/ws/a.txt", content=b"x", context=_ctx(), zone_id="root", is_new_file=True
        )

        hook.on_post_write(ctx)  # must not raise — the write already landed

        buf.queue_owner_grant.assert_called_once_with("alice", "/ws/a.txt", "root")
        assert len(ctx.warnings) == 1
        assert ctx.warnings[0].severity == "degraded"
        assert ctx.warnings[0].component == "deferred_permission"
        assert "db down" in ctx.warnings[0].message

    def test_opt_out_queues_owner_grant(self, buf, rebac):
        hook = DeferredPermissionHook(buf, rebac_manager=rebac, sync_owner_grant=False)
        ctx = WriteHookContext(
            path="/ws/a.txt", content=b"x", context=_ctx(), zone_id="root", is_new_file=True
        )

        hook.on_post_write(ctx)

        rebac.rebac_write.assert_not_called()
        buf.queue_owner_grant.assert_called_once_with("alice", "/ws/a.txt", "root")

    def test_without_rebac_manager_queues_owner_grant(self, buf):
        hook = DeferredPermissionHook(buf, rebac_manager=None)
        ctx = WriteHookContext(
            path="/ws/a.txt", content=b"x", context=_ctx(), zone_id="root", is_new_file=True
        )

        hook.on_post_write(ctx)

        buf.queue_owner_grant.assert_called_once_with("alice", "/ws/a.txt", "root")

    def test_default_zone_is_root(self, buf, rebac):
        hook = DeferredPermissionHook(buf, rebac_manager=rebac)
        ctx = WriteHookContext(
            path="/ws/a.txt", content=b"x", context=_ctx(), zone_id=None, is_new_file=True
        )

        hook.on_post_write(ctx)

        assert rebac.rebac_write.call_args.kwargs["zone_id"] == "root"
        buf.queue_hierarchy.assert_called_once_with("/ws/a.txt", "root")


class TestMkdirHook:
    def test_mkdir_owner_grant_is_written_synchronously(self, buf, rebac):
        hook = DeferredPermissionHook(buf, rebac_manager=rebac)
        ctx = MkdirHookContext(path="/ws/dir", context=_ctx(), zone_id="z1")

        hook.on_post_mkdir(ctx)

        rebac.rebac_write.assert_called_once_with(
            subject=("user", "alice"),
            relation="direct_owner",
            object=("file", "/ws/dir"),
            zone_id="z1",
        )
        buf.queue_hierarchy.assert_called_once_with("/ws/dir", "z1")
        buf.queue_owner_grant.assert_not_called()


class TestWriteBatchHook:
    @staticmethod
    def _meta(path: str) -> MagicMock:
        meta = MagicMock()
        meta.path = path
        return meta

    def test_batch_uses_rebac_write_batch_for_new_files_only(self, buf, rebac):
        hook = DeferredPermissionHook(buf, rebac_manager=rebac)
        ctx = WriteBatchHookContext(
            items=[(self._meta("/ws/new.txt"), True), (self._meta("/ws/old.txt"), False)],
            context=_ctx(),
            zone_id="root",
        )

        hook.on_post_write_batch(ctx)

        rebac.rebac_write_batch.assert_called_once_with(
            [
                {
                    "subject": ("user", "alice"),
                    "relation": "direct_owner",
                    "object": ("file", "/ws/new.txt"),
                    "zone_id": "root",
                }
            ]
        )
        rebac.rebac_write.assert_not_called()
        buf.queue_owner_grant.assert_not_called()
        assert [c.args for c in buf.queue_hierarchy.call_args_list] == [
            ("/ws/new.txt", "root"),
            ("/ws/old.txt", "root"),
        ]

    def test_batch_failure_falls_back_to_queue(self, buf, rebac):
        rebac.rebac_write_batch.side_effect = RuntimeError("db down")
        hook = DeferredPermissionHook(buf, rebac_manager=rebac)
        ctx = WriteBatchHookContext(
            items=[(self._meta("/ws/a.txt"), True), (self._meta("/ws/b.txt"), True)],
            context=_ctx(),
            zone_id="root",
        )

        hook.on_post_write_batch(ctx)

        assert [c.args for c in buf.queue_owner_grant.call_args_list] == [
            ("alice", "/ws/a.txt", "root"),
            ("alice", "/ws/b.txt", "root"),
        ]
        assert len(ctx.warnings) == 1
        assert ctx.warnings[0].severity == "degraded"

    def test_batch_without_new_files_writes_nothing(self, buf, rebac):
        hook = DeferredPermissionHook(buf, rebac_manager=rebac)
        ctx = WriteBatchHookContext(
            items=[(self._meta("/ws/old.txt"), False)], context=_ctx(), zone_id="root"
        )

        hook.on_post_write_batch(ctx)

        rebac.rebac_write_batch.assert_not_called()
        buf.queue_owner_grant.assert_not_called()
