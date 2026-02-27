"""Adapter: WriteObserverProtocol → VFS interceptor hooks.

Wraps the audit write observer as standard VFS interceptor hooks,
registered by factory via ``dispatch.register_intercept_*()``.
The kernel dispatch has no knowledge of audit policy or write
observer specifics — error policy is handled here.

Issue #900.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.contracts.vfs_hooks import (
        DeleteHookContext,
        MkdirHookContext,
        RenameHookContext,
        RmdirHookContext,
        WriteBatchHookContext,
        WriteHookContext,
    )
    from nexus.contracts.write_observer import WriteObserverProtocol

logger = logging.getLogger(__name__)


class AuditWriteInterceptor:
    """Wraps WriteObserverProtocol as VFS interceptor hooks.

    Implements all mutation hook protocols so it can be registered via
    the standard ``register_intercept_*()`` API.  The audit error policy
    (abort vs log-and-continue) is observer-level config, not dispatch-level.
    """

    name = "audit_write_observer"

    __slots__ = ("_observer", "_strict_mode")

    def __init__(self, observer: WriteObserverProtocol, *, strict_mode: bool = True) -> None:
        self._observer = observer
        self._strict_mode = strict_mode

    # ── VFSWriteHook ──────────────────────────────────────────────────

    def on_post_write(self, ctx: WriteHookContext) -> None:
        self._call(
            "write",
            ctx.path,
            metadata=ctx.metadata,
            is_new=ctx.is_new_file,
            path=ctx.path,
            old_metadata=ctx.old_metadata,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
        )

    # ── VFSWriteBatchHook ─────────────────────────────────────────────

    def on_post_write_batch(self, ctx: WriteBatchHookContext) -> None:
        self._call(
            "write_batch",
            "<batch>",
            items=ctx.items,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
        )

    # ── VFSDeleteHook ─────────────────────────────────────────────────

    def on_post_delete(self, ctx: DeleteHookContext) -> None:
        self._call(
            "delete",
            ctx.path,
            path=ctx.path,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
            metadata=ctx.metadata,
        )

    # ── VFSRenameHook ─────────────────────────────────────────────────

    def on_post_rename(self, ctx: RenameHookContext) -> None:
        self._call(
            "rename",
            ctx.old_path,
            old_path=ctx.old_path,
            new_path=ctx.new_path,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
            metadata=ctx.metadata,
        )

    # ── VFSMkdirHook ─────────────────────────────────────────────────

    def on_post_mkdir(self, ctx: MkdirHookContext) -> None:
        self._call(
            "mkdir",
            ctx.path,
            path=ctx.path,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
        )

    # ── VFSRmdirHook ─────────────────────────────────────────────────

    def on_post_rmdir(self, ctx: RmdirHookContext) -> None:
        self._call(
            "rmdir",
            ctx.path,
            path=ctx.path,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
            recursive=ctx.recursive,
        )

    # ── Internal ──────────────────────────────────────────────────────

    def _call(self, operation: str, op_path: str, **kwargs: Any) -> None:
        """Dispatch to WriteObserverProtocol with audit error policy."""
        try:
            method = getattr(self._observer, f"on_{operation}")
            method(**kwargs)
        except Exception as e:
            from nexus.contracts.exceptions import AuditLogError

            if self._strict_mode:
                logger.error(
                    "AUDIT LOG FAILURE: %s on '%s' ABORTED. Error: %s. "
                    "Set audit_strict_mode=False to allow writes without audit logs.",
                    operation,
                    op_path,
                    e,
                )
                raise AuditLogError(
                    f"Operation aborted: audit logging failed for {operation}: {e}",
                    path=op_path,
                    original_error=e,
                ) from e
            else:
                logger.critical(
                    "AUDIT LOG FAILURE: %s on '%s' SUCCEEDED but audit log FAILED. "
                    "Error: %s. This creates an audit trail gap!",
                    operation,
                    op_path,
                    e,
                )
