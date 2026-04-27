"""Audit interceptor: serialize VFS mutations → DT_PIPE via sys_write.

Sync VFS interceptor hook that serializes each mutation event to JSON
and writes it into the audit DT_PIPE via ``nx.sys_write()``
(Rust kernel routes DT_PIPE writes through dcache ring buffer, ~0.5μs).
The pipe is consumed by ``RecordStoreWriteObserver`` which flushes
events to RecordStore in batches.

The interceptor uses the public NexusFS pipe API rather than reaching
into kernel internals, decoupling it from the underlying transport
while still benefiting from the IPC fast-path.

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
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)

_INTERNAL_PIPE_PREFIX = "/nexus/pipes/"


class SyncAuditWriteInterceptor:
    """Sync VFS interceptor: call RecordStoreWriteObserver methods directly.

    Used when the write observer is the synchronous RecordStoreWriteObserver
    (SQLite mode, ``enable_write_buffer=False``).  Unlike the async
    ``AuditWriteInterceptor`` which serializes events to a DT_PIPE for the
    piped consumer, this hook calls ``on_write`` / ``on_delete`` /
    ``on_rename`` / ``on_mkdir`` / ``on_rmdir`` inline, matching the
    pre-#1772 kernel behaviour.

    Registered as a **sync** POST hook because all ``on_post_*`` methods are
    plain ``def`` (not ``async def``).  The Rust HookRegistry dispatches them
    serially before async hooks.
    """

    name = "audit_write_observer"

    __slots__ = ("_observer", "_strict_mode")

    # ── Hook spec (duck-typed) ────────────────────────────────────────

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

    def __init__(self, observer: Any, *, strict_mode: bool = True) -> None:
        self._observer = observer
        self._strict_mode = strict_mode

    # ── Sync POST hooks (called by KernelDispatch serial path) ─────────

    def on_post_write(self, ctx: "WriteHookContext") -> None:
        if ctx.path.startswith(_INTERNAL_PIPE_PREFIX):
            return
        self._observer.on_write(
            ctx.metadata,
            is_new=ctx.is_new_file,
            path=ctx.path,
            old_metadata=ctx.old_metadata,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
        )

    def on_post_write_batch(self, ctx: "WriteBatchHookContext") -> None:
        filtered_items = [
            (metadata, is_new)
            for metadata, is_new in ctx.items
            if not metadata.path.startswith(_INTERNAL_PIPE_PREFIX)
        ]
        if not filtered_items:
            return
        self._observer.on_write_batch(
            filtered_items,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
        )

    def on_post_delete(self, ctx: "DeleteHookContext") -> None:
        if ctx.path.startswith(_INTERNAL_PIPE_PREFIX):
            return
        self._observer.on_delete(
            path=ctx.path,
            metadata=ctx.metadata,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
        )

    def on_post_rename(self, ctx: "RenameHookContext") -> None:
        if ctx.old_path.startswith(_INTERNAL_PIPE_PREFIX) or ctx.new_path.startswith(
            _INTERNAL_PIPE_PREFIX
        ):
            return
        self._observer.on_rename(
            old_path=ctx.old_path,
            new_path=ctx.new_path,
            metadata=ctx.metadata,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
        )

    def on_post_mkdir(self, ctx: "MkdirHookContext") -> None:
        if ctx.path.startswith(_INTERNAL_PIPE_PREFIX):
            return
        self._observer.on_mkdir(
            path=ctx.path,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
        )

    def on_post_rmdir(self, ctx: "RmdirHookContext") -> None:
        if ctx.path.startswith(_INTERNAL_PIPE_PREFIX):
            return
        self._observer.on_rmdir(
            path=ctx.path,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
            recursive=ctx.recursive,
        )


class AuditWriteInterceptor:
    """Sync VFS interceptor: serialize mutation events → write_nowait to pipe.

    Registered as a sync POST hook (all ``on_post_*`` are plain ``def``).
    The fast path writes directly to the pipe ring buffer (~0.5μs).

    Error policy: ``strict_mode=True`` aborts with AuditLogError on
    pipe write failure; ``strict_mode=False`` logs and continues.
    """

    name = "audit_write_observer"

    __slots__ = ("_nx", "_pipe_path", "_strict_mode")

    # ── Hook spec (duck-typed) (Issue #1613) ──────────────────────────

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

    def __init__(self, nx: "NexusFS", pipe_path: str, *, strict_mode: bool = True) -> None:
        self._nx = nx
        self._pipe_path = pipe_path
        self._strict_mode = strict_mode

    # ── VFSWriteHook ──────────────────────────────────────────────────

    def on_post_write(self, ctx: "WriteHookContext") -> None:
        if ctx.path.startswith(_INTERNAL_PIPE_PREFIX):
            return
        event = {
            "op": "write",
            "path": ctx.path,
            "is_new": ctx.is_new_file,
            "zone_id": ctx.zone_id,
            "agent_id": ctx.agent_id,
            "snapshot_hash": ctx.old_metadata.content_id if ctx.old_metadata else None,
            "metadata_snapshot": ctx.old_metadata.to_dict() if ctx.old_metadata else None,
            "metadata": ctx.metadata.to_dict() if ctx.metadata else None,
        }
        self._emit(event, "write", ctx.path)

    # ── VFSWriteBatchHook ─────────────────────────────────────────────

    def on_post_write_batch(self, ctx: "WriteBatchHookContext") -> None:
        for metadata, is_new in ctx.items:
            if metadata.path.startswith(_INTERNAL_PIPE_PREFIX):
                continue
            event = {
                "op": "write",
                "path": metadata.path,
                "is_new": is_new,
                "zone_id": ctx.zone_id,
                "agent_id": ctx.agent_id,
                "snapshot_hash": metadata.content_id,
                "metadata": metadata.to_dict(),
            }
            self._emit(event, "write_batch", metadata.path)

    # ── VFSDeleteHook ─────────────────────────────────────────────────

    def on_post_delete(self, ctx: "DeleteHookContext") -> None:
        if ctx.path.startswith(_INTERNAL_PIPE_PREFIX):
            return
        event = {
            "op": "delete",
            "path": ctx.path,
            "zone_id": ctx.zone_id,
            "agent_id": ctx.agent_id,
            "snapshot_hash": ctx.metadata.content_id if ctx.metadata else None,
            "metadata_snapshot": ctx.metadata.to_dict() if ctx.metadata else None,
        }
        self._emit(event, "delete", ctx.path)

    # ── VFSRenameHook ─────────────────────────────────────────────────

    def on_post_rename(self, ctx: "RenameHookContext") -> None:
        if ctx.old_path.startswith(_INTERNAL_PIPE_PREFIX) or ctx.new_path.startswith(
            _INTERNAL_PIPE_PREFIX
        ):
            return
        event = {
            "op": "rename",
            "path": ctx.old_path,
            "new_path": ctx.new_path,
            "zone_id": ctx.zone_id,
            "agent_id": ctx.agent_id,
            "snapshot_hash": ctx.metadata.content_id if ctx.metadata else None,
            "metadata_snapshot": ctx.metadata.to_dict() if ctx.metadata else None,
        }
        self._emit(event, "rename", ctx.old_path)

    # ── VFSMkdirHook ─────────────────────────────────────────────────

    def on_post_mkdir(self, ctx: "MkdirHookContext") -> None:
        if ctx.path.startswith(_INTERNAL_PIPE_PREFIX):
            return
        event = {
            "op": "mkdir",
            "path": ctx.path,
            "zone_id": ctx.zone_id,
            "agent_id": ctx.agent_id,
        }
        self._emit(event, "mkdir", ctx.path)

    # ── VFSRmdirHook ─────────────────────────────────────────────────

    def on_post_rmdir(self, ctx: "RmdirHookContext") -> None:
        if ctx.path.startswith(_INTERNAL_PIPE_PREFIX):
            return
        event = {
            "op": "rmdir",
            "path": ctx.path,
            "zone_id": ctx.zone_id,
            "agent_id": ctx.agent_id,
            "recursive": ctx.recursive,
        }
        self._emit(event, "rmdir", ctx.path)

    # ── Internal ──────────────────────────────────────────────────────

    def _emit(self, event: dict[str, Any], operation: str, op_path: str) -> None:
        """Serialize event to JSON and write directly to the Rust pipe buffer (~0.5μs).

        Uses ``sys_write`` which routes to the Rust kernel ring buffer for
        DT_PIPE entries. Rust skips observer dispatch for DT_PIPE, so this
        hook does not re-trigger post-write hooks (no recursion).

        If the pipe buffer is closed/missing (startup race or kernel
        teardown), the event is dropped with a warning.
        """
        try:
            data = json.dumps(event).encode()

            # Fast path: kernel ring buffer write via VFS (~0.5μs, no GIL re-entry).
            try:
                self._nx.sys_write(self._pipe_path, data)
                return
            except Exception:
                # Pipe not ready (startup race) or closed — fall through to drop.
                pass

            logger.warning(
                "Audit pipe not ready, dropping %s event for '%s'",
                operation,
                op_path,
            )
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
