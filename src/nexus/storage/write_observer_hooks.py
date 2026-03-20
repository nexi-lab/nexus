"""Audit interceptor: VFS mutation hooks → WriteObserver or DT_PIPE.

Two modes:

1. **Observer mode** (sync): wraps a ``WriteObserverProtocol`` (e.g.
   ``RecordStoreWriteObserver``) and calls ``on_write()`` / ``on_delete()``
   etc. directly.  Used for SQLite and any non-buffered backend.

2. **Pipe mode** (async): serializes each mutation event to JSON and
   writes it into a DT_PIPE via ``nx.sys_write(pipe_path, data)``.
   The pipe is consumed by ``PipedRecordStoreWriteObserver`` which
   flushes events to RecordStore in batches.  Used for PostgreSQL
   with write-buffer enabled.

Issue #900, #1772.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec
    from nexus.contracts.vfs_hooks import (
        DeleteHookContext,
        MkdirHookContext,
        RenameHookContext,
        RmdirHookContext,
        WriteBatchHookContext,
        WriteHookContext,
    )
    from nexus.contracts.write_observer import WriteObserverProtocol
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


class AuditWriteInterceptor:
    """VFS interceptor: dispatch mutation events to observer or pipe.

    Registered as an async POST hook via ``register_intercept_*()``.

    Construction:
        - Observer mode: ``AuditWriteInterceptor(observer=obs)``
        - Pipe mode: ``AuditWriteInterceptor(nx, pipe_path)``

    Error policy: ``strict_mode=True`` aborts with AuditLogError on
    failure; ``strict_mode=False`` logs and continues.
    """

    name = "audit_write_observer"

    __slots__ = ("_nx", "_observer", "_pipe_path", "_strict_mode")

    # ── HotSwappable protocol (Issue #1613) ────────────────────────────

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(
            write_hooks=(self,),
            write_batch_hooks=(self,),
            delete_hooks=(self,),
            rename_hooks=(self,),
            mkdir_hooks=(self,),
            rmdir_hooks=(self,),
        )

    async def drain(self) -> None:
        pass

    async def activate(self) -> None:
        pass

    def __init__(
        self,
        nx: "NexusFS | None" = None,
        pipe_path: str | None = None,
        *,
        observer: "WriteObserverProtocol | None" = None,
        strict_mode: bool = True,
    ) -> None:
        self._nx = nx
        self._pipe_path = pipe_path
        self._observer = observer
        self._strict_mode = strict_mode

    # ── VFSWriteHook ──────────────────────────────────────────────────

    async def on_post_write(self, ctx: "WriteHookContext") -> None:
        if self._observer is not None:
            self._call_observer(
                "write",
                ctx.path,
                metadata=ctx.metadata,
                is_new=ctx.is_new_file,
                path=ctx.path,
                old_metadata=ctx.old_metadata,
                zone_id=ctx.zone_id,
                agent_id=ctx.agent_id,
            )
            return
        event = {
            "op": "write",
            "path": ctx.path,
            "is_new": ctx.is_new_file,
            "zone_id": ctx.zone_id,
            "agent_id": ctx.agent_id,
            "snapshot_hash": ctx.old_metadata.etag if ctx.old_metadata else None,
            "metadata_snapshot": ctx.old_metadata.to_dict() if ctx.old_metadata else None,
            "metadata": ctx.metadata.to_dict() if ctx.metadata else None,
        }
        await self._emit(event, "write", ctx.path)

    # ── VFSWriteBatchHook ─────────────────────────────────────────────

    async def on_post_write_batch(self, ctx: "WriteBatchHookContext") -> None:
        if self._observer is not None:
            self._call_observer(
                "write_batch",
                "<batch>",
                items=ctx.items,
                zone_id=ctx.zone_id,
                agent_id=ctx.agent_id,
            )
            return
        for metadata, is_new in ctx.items:
            event = {
                "op": "write",
                "path": metadata.path,
                "is_new": is_new,
                "zone_id": ctx.zone_id,
                "agent_id": ctx.agent_id,
                "snapshot_hash": metadata.etag,
                "metadata": metadata.to_dict(),
            }
            await self._emit(event, "write_batch", metadata.path)

    # ── VFSDeleteHook ─────────────────────────────────────────────────

    async def on_post_delete(self, ctx: "DeleteHookContext") -> None:
        if self._observer is not None:
            self._call_observer(
                "delete",
                ctx.path,
                path=ctx.path,
                zone_id=ctx.zone_id,
                agent_id=ctx.agent_id,
                metadata=ctx.metadata,
            )
            return
        event = {
            "op": "delete",
            "path": ctx.path,
            "zone_id": ctx.zone_id,
            "agent_id": ctx.agent_id,
            "snapshot_hash": ctx.metadata.etag if ctx.metadata else None,
            "metadata_snapshot": ctx.metadata.to_dict() if ctx.metadata else None,
        }
        await self._emit(event, "delete", ctx.path)

    # ── VFSRenameHook ─────────────────────────────────────────────────

    async def on_post_rename(self, ctx: "RenameHookContext") -> None:
        if self._observer is not None:
            self._call_observer(
                "rename",
                ctx.old_path,
                old_path=ctx.old_path,
                new_path=ctx.new_path,
                zone_id=ctx.zone_id,
                agent_id=ctx.agent_id,
                metadata=ctx.metadata,
            )
            return
        event = {
            "op": "rename",
            "path": ctx.old_path,
            "new_path": ctx.new_path,
            "zone_id": ctx.zone_id,
            "agent_id": ctx.agent_id,
            "snapshot_hash": ctx.metadata.etag if ctx.metadata else None,
            "metadata_snapshot": ctx.metadata.to_dict() if ctx.metadata else None,
        }
        await self._emit(event, "rename", ctx.old_path)

    # ── VFSMkdirHook ─────────────────────────────────────────────────

    async def on_post_mkdir(self, ctx: "MkdirHookContext") -> None:
        if self._observer is not None:
            self._call_observer(
                "mkdir",
                ctx.path,
                path=ctx.path,
                zone_id=ctx.zone_id,
                agent_id=ctx.agent_id,
            )
            return
        event = {
            "op": "mkdir",
            "path": ctx.path,
            "zone_id": ctx.zone_id,
            "agent_id": ctx.agent_id,
        }
        await self._emit(event, "mkdir", ctx.path)

    # ── VFSRmdirHook ─────────────────────────────────────────────────

    async def on_post_rmdir(self, ctx: "RmdirHookContext") -> None:
        if self._observer is not None:
            self._call_observer(
                "rmdir",
                ctx.path,
                path=ctx.path,
                zone_id=ctx.zone_id,
                agent_id=ctx.agent_id,
                recursive=ctx.recursive,
            )
            return
        event = {
            "op": "rmdir",
            "path": ctx.path,
            "zone_id": ctx.zone_id,
            "agent_id": ctx.agent_id,
            "recursive": ctx.recursive,
        }
        await self._emit(event, "rmdir", ctx.path)

    # ── Internal — observer mode (sync) ──────────────────────────────

    def _call_observer(self, operation: str, op_path: str, **kwargs: Any) -> None:
        """Dispatch to WriteObserverProtocol with audit error policy."""
        try:
            method = getattr(self._observer, f"on_{operation}")
            method(**kwargs)
        except Exception as e:
            self._handle_error(operation, op_path, e)

    # ── Internal — pipe mode (async) ─────────────────────────────────

    async def _emit(self, event: dict[str, Any], operation: str, op_path: str) -> None:
        """Serialize event to JSON and write to pipe via sys_write."""
        assert self._nx is not None and self._pipe_path is not None  # pipe mode invariant
        try:
            data = json.dumps(event).encode()
            # Use admin context for internal audit pipe writes to bypass permission checks.
            from nexus.contracts.types import OperationContext as _OC

            _admin_ctx = _OC(user_id="system", groups=[], is_admin=True)
            await self._nx.sys_write(self._pipe_path, data, context=_admin_ctx)
        except Exception as e:
            self._handle_error(operation, op_path, e)

    # ── Shared error handling ────────────────────────────────────────

    def _handle_error(self, operation: str, op_path: str, e: Exception) -> None:
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
