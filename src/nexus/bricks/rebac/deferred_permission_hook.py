"""VFS hooks for deferred permission buffer (Issue #1773, #1682, #4739).

Wraps ``DeferredPermissionBuffer.queue_hierarchy()`` as a proper
KernelDispatch hook, eliminating direct kernel coupling to the deferred
permission buffer.

Issue #4739 — sync owner grant:
    The creating subject's ``direct_owner`` grant is written **synchronously**
    through ``rebac_manager.rebac_write()`` (``rebac_write_batch()`` for
    ``write_batch``) so the writer's own ``list`` / ``search`` / ``check`` see
    the new file immediately.  Only the hierarchy (``parent``) tuples stay
    deferred: they serve sharing and inheritance, which carry no
    read-your-writes expectation.  If the synchronous write fails, the grant
    falls back to the deferred queue and the operation carries a ``degraded``
    warning instead of failing the write.

    ``sync_owner_grant=False`` restores the pre-#4739 behaviour (owner grant
    queued alongside the hierarchy tuples, flushed every
    ``deferred_flush_interval`` seconds).

    ``skip_admin_owner_grant=True`` (the orchestrator sets it when
    ``PermissionConfig.allow_admin_bypass`` is on and
    ``owner_grant_admin_bypass`` is off) writes NO creator grant for an admin
    subject: under admin bypass the tuple never takes part in a decision, but
    writing it costs a ``rebac_write`` plus three Tiger write-throughs and a
    zone-graph invalidation per new file.  Before #4739 the subprocess-kernel
    deployment never wrote these grants at all (``is_new`` was hard-coded
    False), so an admin-key tenant would otherwise see every write slow down
    and its tuple tables grow by three rows per file.

Data mapping:
    ctx.path          → path
    ctx.zone_id       → zone_id (default "root")
    ctx.context       → OperationContext (user_id, is_system)
    ctx.is_new_file   → only grant ownership for new files

For rename, we call ``rebac_manager.update_object_path()`` directly
(not via the deferred buffer) because path updates must be immediate.
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


class DeferredPermissionHook:
    """Post-write/mkdir/write_batch/rename hook: deferred hierarchy + sync owner grant."""

    name = "deferred_permission"
    __slots__ = ("_buf", "_rebac", "_skip_admin_owner_grant", "_sync_owner_grant")

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
        deferred_buffer: Any,
        rebac_manager: Any | None = None,
        *,
        sync_owner_grant: bool = True,
        skip_admin_owner_grant: bool = False,
    ) -> None:
        self._buf = deferred_buffer
        self._rebac = rebac_manager
        # Issue #4739: write the creator's owner grant synchronously.  Only
        # possible when a rebac_manager is wired; otherwise fall back to queue.
        self._sync_owner_grant = sync_owner_grant
        # Admin-bypass deployments: the creator tuple is redundant for an
        # admin subject, so skip it (sync AND deferred) — see module docstring.
        self._skip_admin_owner_grant = skip_admin_owner_grant

    # ── Shared helpers ────────────────────────────────────────────────

    @staticmethod
    def _warn(warnings: list[OperationWarning], message: str) -> None:
        warnings.append(
            OperationWarning(
                severity="degraded",
                component="deferred_permission",
                message=message,
            )
        )

    def _grant_user(self, context: Any) -> str | None:
        """Return the user to grant ownership to, or ``None`` for system/anonymous.

        Also ``None`` for an admin subject when ``skip_admin_owner_grant`` is
        set: the deployment runs with admin bypass, so the tuple would never be
        consulted and only costs write latency + tuple growth.
        """
        if context is None:
            return None
        if self._skip_admin_owner_grant and getattr(context, "is_admin", False):
            return None
        user = getattr(context, "user_id", None)
        if user and not getattr(context, "is_system", False):
            return str(user)
        return None

    def _queue_hierarchy(self, path: str, zone: str, warnings: list[OperationWarning]) -> None:
        try:
            self._buf.queue_hierarchy(path, zone)
        except Exception as e:
            self._warn(warnings, f"queue failed: {e}")

    def _queue_owner_grant(
        self, user: str, path: str, zone: str, warnings: list[OperationWarning]
    ) -> None:
        try:
            self._buf.queue_owner_grant(user, path, zone)
        except Exception as e:
            self._warn(warnings, f"queue failed: {e}")

    def _owner_grant(
        self, user: str, path: str, zone: str, warnings: list[OperationWarning]
    ) -> None:
        """Grant ``direct_owner`` to *user* on *path* — sync first, queue as fallback."""
        if self._sync_owner_grant and self._rebac is not None:
            try:
                self._rebac.rebac_write(
                    subject=("user", user),
                    relation="direct_owner",
                    object=("file", path),
                    zone_id=zone,
                )
                return
            except Exception as e:
                logger.warning("[deferred_permission] sync owner grant failed for %s: %s", path, e)
                self._warn(warnings, f"sync owner grant failed, queued: {e}")
        self._queue_owner_grant(user, path, zone, warnings)

    def _owner_grants_batch(
        self, user: str, paths: list[str], zone: str, warnings: list[OperationWarning]
    ) -> None:
        """Batch variant of :meth:`_owner_grant` for ``write_batch``."""
        if not paths:
            return
        if self._sync_owner_grant and self._rebac is not None:
            grants = [
                {
                    "subject": ("user", user),
                    "relation": "direct_owner",
                    "object": ("file", path),
                    "zone_id": zone,
                }
                for path in paths
            ]
            try:
                if hasattr(self._rebac, "rebac_write_batch"):
                    self._rebac.rebac_write_batch(grants)
                else:
                    for grant in grants:
                        self._rebac.rebac_write(**grant)
                return
            except Exception as e:
                logger.warning(
                    "[deferred_permission] sync owner grant batch failed (%d paths): %s",
                    len(paths),
                    e,
                )
                self._warn(warnings, f"sync owner grant batch failed, queued: {e}")
        for path in paths:
            self._queue_owner_grant(user, path, zone, warnings)

    # ── Hook callbacks ────────────────────────────────────────────────

    def on_post_write(self, ctx: WriteHookContext) -> None:
        zone = ctx.zone_id or ROOT_ZONE_ID
        self._queue_hierarchy(ctx.path, zone, ctx.warnings)
        if ctx.is_new_file:
            user = self._grant_user(ctx.context)
            if user:
                self._owner_grant(user, ctx.path, zone, ctx.warnings)

    def on_post_mkdir(self, ctx: MkdirHookContext) -> None:
        zone = ctx.zone_id or ROOT_ZONE_ID
        self._queue_hierarchy(ctx.path, zone, ctx.warnings)
        user = self._grant_user(ctx.context)
        if user:
            self._owner_grant(user, ctx.path, zone, ctx.warnings)

    def on_post_write_batch(self, ctx: WriteBatchHookContext) -> None:
        zone = ctx.zone_id or ROOT_ZONE_ID
        new_paths: list[str] = []
        for meta, is_new in ctx.items:
            self._queue_hierarchy(meta.path, zone, ctx.warnings)
            if is_new:
                new_paths.append(meta.path)
        user = self._grant_user(ctx.context)
        if user and new_paths:
            self._owner_grants_batch(user, new_paths, zone, ctx.warnings)

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
                    component="deferred_permission",
                    message=f"update_object_path failed: {e}",
                )
            )
