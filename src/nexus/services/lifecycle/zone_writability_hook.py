"""Zone writability hook — gate all writes during zone deprovision (Issue #2061).

Migrated from NexusFS._check_zone_writable() (Issue #1371):
zone lifecycle management is a federation service concern (kernel owns zone_id
as namespace partition, federation owns zone provisioning/deprovisioning).
Injected via KernelDispatch PRE hooks.

When a zone is terminating (being deprovisioned), all write operations
to that zone are blocked with ZoneTerminatingError. This prevents data
corruption during the multi-step zone finalization process.

Same pattern as PermissionEnforcerHook (#1706) and SnapshotWriteHook (#1774).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.protocols.service_hooks import HookSpec

if TYPE_CHECKING:
    from nexus.contracts.vfs_hooks import (
        DeleteHookContext,
        MkdirHookContext,
        RenameHookContext,
        RmdirHookContext,
        WriteHookContext,
    )

logger = logging.getLogger(__name__)


class ZoneWritabilityHook:
    """PRE hook: block writes to zones being deprovisioned.

    Registered on write, delete, rename, mkdir, rmdir — all mutating ops.
    Raises ZoneTerminatingError if the zone is in finalization state.

    No-op when zone_lifecycle service is not available (single-node deploy).
    """

    name = "zone_writability"
    __slots__ = ("_zone_lifecycle",)

    def __init__(self, zone_lifecycle: Any) -> None:
        self._zone_lifecycle = zone_lifecycle

    # --- Hook spec (duck-typed) ---

    def hook_spec(self) -> HookSpec:
        return HookSpec(
            write_hooks=(self,),
            delete_hooks=(self,),
            rename_hooks=(self,),
            mkdir_hooks=(self,),
            rmdir_hooks=(self,),
        )

    # --- PRE hooks (raise to abort) ---

    def _check(self, zone_id: str | None) -> None:
        if zone_id and self._zone_lifecycle.is_zone_terminating(zone_id):
            from nexus.contracts.exceptions import ZoneTerminatingError

            raise ZoneTerminatingError(zone_id)

    def on_pre_write(self, ctx: WriteHookContext) -> None:
        self._check(ctx.zone_id)

    def on_pre_delete(self, ctx: DeleteHookContext) -> None:
        self._check(ctx.zone_id)

    def on_pre_rename(self, ctx: RenameHookContext) -> None:
        self._check(ctx.zone_id)

    def on_pre_mkdir(self, ctx: MkdirHookContext) -> None:
        self._check(ctx.zone_id)

    def on_pre_rmdir(self, ctx: RmdirHookContext) -> None:
        self._check(ctx.zone_id)
