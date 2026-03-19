"""VFS write hook for synchronous permission hierarchy + owner grant (Issue #1773).

This is the sync fallback for when ``DeferredPermissionBuffer`` is not
available (``enable_deferred=False``).  Wraps the same
``hierarchy_manager.ensure_parent_tuples()`` and
``rebac_manager.rebac_write()`` calls that previously lived inline in
``NexusFS._write_internal()``.

Exactly one of ``DeferredPermissionHook`` or ``SyncPermissionWriteHook``
is enlisted at boot — never both.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.operation_result import OperationWarning

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec
    from nexus.contracts.vfs_hooks import WriteHookContext

logger = logging.getLogger(__name__)


class SyncPermissionWriteHook:
    """Post-write hook: sync hierarchy tuples + owner grant."""

    name = "sync_permission"
    __slots__ = ("_hierarchy", "_rebac")

    # ── HotSwappable protocol (Issue #1773) ────────────────────────────

    def hook_spec(self) -> HookSpec:
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(write_hooks=(self,))

    async def drain(self) -> None:
        pass

    async def activate(self) -> None:
        pass

    def __init__(
        self,
        hierarchy_manager: Any | None = None,
        rebac_manager: Any | None = None,
    ) -> None:
        self._hierarchy = hierarchy_manager
        self._rebac = rebac_manager

    # ── Hook callback ──────────────────────────────────────────────────

    def on_post_write(self, ctx: WriteHookContext) -> None:
        zone = ctx.zone_id or "root"

        # Hierarchy tuples — enables permission inheritance from parents
        if self._hierarchy is not None:
            try:
                self._hierarchy.ensure_parent_tuples(ctx.path, zone_id=zone)
            except Exception as e:
                ctx.warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component="sync_permission",
                        message=f"ensure_parent_tuples failed: {e}",
                    )
                )

        # Owner grant — only for new files by non-system users
        if ctx.is_new_file and self._rebac is not None and ctx.context:
            user = ctx.context.user_id
            if user and not ctx.context.is_system:
                try:
                    self._rebac.rebac_write(
                        subject=("user", user),
                        relation="direct_owner",
                        object=("file", ctx.path),
                        zone_id=zone,
                    )
                except Exception as e:
                    ctx.warnings.append(
                        OperationWarning(
                            severity="degraded",
                            component="sync_permission",
                            message=f"owner grant failed: {e}",
                        )
                    )
