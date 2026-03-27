"""ZoneWriteGuardHook — rejects writes to zones being deprovisioned.

Issue #1790: Replaces _check_zone_writable() in nexus_fs.py.

Fires on ALL write-like pre-intercept phases (write, delete, rename,
mkdir, rmdir) and raises ZoneTerminatingError if the target zone
is being deprovisioned (Issue #2061, Decision #4A).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec
    from nexus.contracts.vfs_hooks import (
        CopyHookContext,
        DeleteHookContext,
        MkdirHookContext,
        RenameHookContext,
        RmdirHookContext,
        WriteBatchHookContext,
        WriteHookContext,
    )

logger = logging.getLogger(__name__)


class ZoneWriteGuardHook:
    """Pre-intercept hook that rejects writes to terminating zones.

    Declares hook_spec() for VFS hooks so it can be enlisted via coordinator.
    Registered for all write-like operations.
    """

    # ── Hook spec (duck-typed) ─────────────────────────────────────────

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(
            write_hooks=(self,),
            write_batch_hooks=(self,),
            delete_hooks=(self,),
            rename_hooks=(self,),
            copy_hooks=(self,),
            mkdir_hooks=(self,),
            rmdir_hooks=(self,),
        )

    # ── Constructor ─────────────────────────────────────────────────────

    def __init__(self, zone_lifecycle: Any) -> None:
        self._zone_lifecycle = zone_lifecycle

    # ── Zone check (shared logic) ───────────────────────────────────────

    def _check(self, context: Any) -> None:
        """Raise ZoneTerminatingError if context's zone is being deprovisioned."""
        if context is None:
            return
        zone_id = getattr(context, "zone_id", None)
        if zone_id and self._zone_lifecycle.is_zone_terminating(zone_id):
            from nexus.contracts.exceptions import ZoneTerminatingError

            raise ZoneTerminatingError(zone_id)

    # ── Pre-intercept hooks (all write-like ops) ────────────────────────

    def on_pre_write(self, ctx: "WriteHookContext") -> None:
        self._check(ctx.context)

    def on_post_write(self, ctx: "WriteHookContext") -> None:
        pass

    def on_pre_write_batch(self, ctx: "WriteBatchHookContext") -> None:
        # WriteBatchHookContext has zone_id directly, not a context object
        zone_id = ctx.zone_id
        if zone_id and self._zone_lifecycle.is_zone_terminating(zone_id):
            from nexus.contracts.exceptions import ZoneTerminatingError

            raise ZoneTerminatingError(zone_id)

    def on_post_write_batch(self, ctx: "WriteBatchHookContext") -> None:
        pass

    def on_pre_delete(self, ctx: "DeleteHookContext") -> None:
        self._check(ctx.context)

    def on_post_delete(self, ctx: "DeleteHookContext") -> None:
        pass

    def on_pre_rename(self, ctx: "RenameHookContext") -> None:
        self._check(ctx.context)

    def on_post_rename(self, ctx: "RenameHookContext") -> None:
        pass

    def on_pre_copy(self, ctx: "CopyHookContext") -> None:
        self._check(ctx.context)

    def on_post_copy(self, ctx: "CopyHookContext") -> None:
        pass

    def on_pre_mkdir(self, ctx: "MkdirHookContext") -> None:
        self._check(ctx.context)

    def on_post_mkdir(self, ctx: "MkdirHookContext") -> None:
        pass

    def on_pre_rmdir(self, ctx: "RmdirHookContext") -> None:
        self._check(ctx.context)

    def on_post_rmdir(self, ctx: "RmdirHookContext") -> None:
        pass
