"""Audit interceptor: serialize VFS mutations → DT_PIPE via sys_write.

Async VFS interceptor hook that serializes each mutation event to JSON
and writes it into a DT_PIPE via ``nx.sys_write(pipe_path, data)``.
The pipe is consumed by ``PipedRecordStoreWriteObserver`` which flushes
events to RecordStore in batches.

By using ``sys_write`` instead of ``PipeManager`` directly, the
interceptor is decoupled from kernel internals and benefits from the
IPC fast-path (~1μs).

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

    # ── HotSwappable protocol ──────────────────────────────────────────

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

    def __init__(self, observer: Any, *, strict_mode: bool = True) -> None:
        self._observer = observer
        self._strict_mode = strict_mode

    # ── Sync POST hooks (called by KernelDispatch serial path) ─────────

    def on_post_write(self, ctx: "WriteHookContext") -> None:
        self._observer.on_write(
            ctx.metadata,
            is_new=ctx.is_new_file,
            path=ctx.path,
            old_metadata=ctx.old_metadata,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
        )

    def on_post_write_batch(self, ctx: "WriteBatchHookContext") -> None:
        self._observer.on_write_batch(
            ctx.items,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
        )

    def on_post_delete(self, ctx: "DeleteHookContext") -> None:
        self._observer.on_delete(
            path=ctx.path,
            metadata=ctx.metadata,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
        )

    def on_post_rename(self, ctx: "RenameHookContext") -> None:
        self._observer.on_rename(
            old_path=ctx.old_path,
            new_path=ctx.new_path,
            metadata=ctx.metadata,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
        )

    def on_post_mkdir(self, ctx: "MkdirHookContext") -> None:
        self._observer.on_mkdir(
            path=ctx.path,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
        )

    def on_post_rmdir(self, ctx: "RmdirHookContext") -> None:
        self._observer.on_rmdir(
            path=ctx.path,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
            recursive=ctx.recursive,
        )


class AuditWriteInterceptor:
    """Async VFS interceptor: serialize mutation events → sys_write to pipe.

    Registered as an async POST hook via ``register_intercept_*()``.
    The Rust HookRegistry auto-classifies it as async because
    ``on_post_write`` is ``async def``.

    Error policy: ``strict_mode=True`` aborts with AuditLogError on
    pipe write failure; ``strict_mode=False`` logs and continues.
    """

    name = "audit_write_observer"

    __slots__ = ("_nx", "_pipe_path", "_strict_mode")

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

    def __init__(self, nx: "NexusFS", pipe_path: str, *, strict_mode: bool = True) -> None:
        self._nx = nx
        self._pipe_path = pipe_path
        self._strict_mode = strict_mode

    # ── VFSWriteHook ──────────────────────────────────────────────────

    async def on_post_write(self, ctx: "WriteHookContext") -> None:
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
        event = {
            "op": "mkdir",
            "path": ctx.path,
            "zone_id": ctx.zone_id,
            "agent_id": ctx.agent_id,
        }
        await self._emit(event, "mkdir", ctx.path)

    # ── VFSRmdirHook ─────────────────────────────────────────────────

    async def on_post_rmdir(self, ctx: "RmdirHookContext") -> None:
        event = {
            "op": "rmdir",
            "path": ctx.path,
            "zone_id": ctx.zone_id,
            "agent_id": ctx.agent_id,
            "recursive": ctx.recursive,
        }
        await self._emit(event, "rmdir", ctx.path)

    # ── Internal ──────────────────────────────────────────────────────

    async def _emit(self, event: dict[str, Any], operation: str, op_path: str) -> None:
        """Serialize event to JSON and write to pipe via sys_write."""
        try:
            data = json.dumps(event).encode()
            await self._nx.sys_write(self._pipe_path, data)
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
