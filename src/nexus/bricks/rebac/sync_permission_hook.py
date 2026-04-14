"""VFS hooks for synchronous permission hierarchy + owner grant (Issue #1773, #1682).

This is the sync fallback for when ``DeferredPermissionBuffer`` is not
available (``enable_deferred=False``).  Wraps the same
``hierarchy_manager.ensure_parent_tuples()`` and
``rebac_manager.rebac_write()`` calls that previously lived inline in
``NexusFS.write()``, ``mkdir()``, ``write_batch()``, and
``sys_rename()``.

Exactly one of ``DeferredPermissionHook`` or ``SyncPermissionWriteHook``
is enlisted at boot — never both.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.operation_result import OperationWarning

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec
    from nexus.contracts.vfs_hooks import (
        MkdirHookContext,
        RenameHookContext,
        WriteBatchHookContext,
        WriteHookContext,
    )

logger = logging.getLogger(__name__)


class SyncPermissionWriteHook:
    """Post-write/mkdir/write_batch/rename hook: sync hierarchy tuples + owner grant."""

    name = "sync_permission"
    __slots__ = ("_hierarchy", "_rebac")

    # ── Hook spec (duck-typed) (Issue #1773) ──────────────────────────

    def hook_spec(self) -> HookSpec:
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(
            write_hooks=(self,),
            mkdir_hooks=(self,),
            write_batch_hooks=(self,),
            rename_hooks=(self,),
        )

    def __init__(
        self,
        hierarchy_manager: Any | None = None,
        rebac_manager: Any | None = None,
    ) -> None:
        self._hierarchy = hierarchy_manager
        self._rebac = rebac_manager

    # ── Shared helpers ────────────────────────────────────────────────

    def _do_hierarchy(self, path: str, zone: str, ctx_warnings: list[OperationWarning]) -> None:
        if self._hierarchy is not None:
            try:
                self._hierarchy.ensure_parent_tuples(path, zone_id=zone)
            except Exception as e:
                ctx_warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component="sync_permission",
                        message=f"ensure_parent_tuples failed: {e}",
                    )
                )

    def _do_owner_grant(
        self, user_id: str, path: str, zone: str, ctx_warnings: list[OperationWarning]
    ) -> None:
        if self._rebac is not None:
            try:
                self._rebac.rebac_write(
                    subject=("user", user_id),
                    relation="direct_owner",
                    object=("file", path),
                    zone_id=zone,
                )
            except Exception as e:
                ctx_warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component="sync_permission",
                        message=f"owner grant failed: {e}",
                    )
                )

    # ── Hook callbacks ────────────────────────────────────────────────

    def on_post_write(self, ctx: WriteHookContext) -> None:
        zone = ctx.zone_id or ROOT_ZONE_ID
        self._do_hierarchy(ctx.path, zone, ctx.warnings)

        if ctx.is_new_file and ctx.context:
            user = ctx.context.user_id
            if user and not ctx.context.is_system:
                self._do_owner_grant(user, ctx.path, zone, ctx.warnings)

    def on_post_mkdir(self, ctx: MkdirHookContext) -> None:
        zone = ctx.zone_id or ROOT_ZONE_ID
        self._do_hierarchy(ctx.path, zone, ctx.warnings)

        if ctx.context:
            user = ctx.context.user_id
            if user and not ctx.context.is_system:
                self._do_owner_grant(user, ctx.path, zone, ctx.warnings)

    def on_post_write_batch(self, ctx: WriteBatchHookContext) -> None:
        zone = ctx.zone_id or ROOT_ZONE_ID

        # Batch hierarchy tuples
        if self._hierarchy is not None:
            paths = [meta.path for meta, _is_new in ctx.items]
            if hasattr(self._hierarchy, "ensure_parent_tuples_batch"):
                try:
                    self._hierarchy.ensure_parent_tuples_batch(paths, zone_id=zone)
                except Exception as e:
                    logger.warning("write_batch hierarchy batch failed, falling back: %s", e)
                    for p in paths:
                        self._do_hierarchy(p, zone, ctx.warnings)
            else:
                for p in paths:
                    self._do_hierarchy(p, zone, ctx.warnings)

        # Batch owner grants
        if self._rebac is not None and ctx.context:
            user = ctx.context.user_id
            if user and not ctx.context.is_system:
                owner_grants = [
                    {
                        "subject": ("user", user),
                        "relation": "direct_owner",
                        "object": ("file", meta.path),
                        "zone_id": zone,
                    }
                    for meta, is_new in ctx.items
                    if is_new
                ]
                if owner_grants and hasattr(self._rebac, "rebac_write_batch"):
                    try:
                        self._rebac.rebac_write_batch(owner_grants)
                    except Exception as e:
                        logger.warning("write_batch rebac batch failed, falling back: %s", e)
                        for grant in owner_grants:
                            self._do_owner_grant(user, grant["object"][1], zone, ctx.warnings)
                elif owner_grants:
                    for grant in owner_grants:
                        self._do_owner_grant(user, grant["object"][1], zone, ctx.warnings)

    def on_post_rename(self, ctx: RenameHookContext) -> None:
        if self._rebac is None:
            return
        try:
            self._rebac.update_object_path(
                old_path=ctx.old_path,
                new_path=ctx.new_path,
                object_type="file",
                is_directory=ctx.is_directory,
            )
        except Exception as e:
            ctx.warnings.append(
                OperationWarning(
                    severity="degraded",
                    component="sync_permission",
                    message=f"update_object_path failed: {e}",
                )
            )
