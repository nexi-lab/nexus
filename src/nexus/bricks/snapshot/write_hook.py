"""SnapshotWriteInterceptor — records write/delete ops into active snapshot transactions.

Issue #1770: Replaces direct snapshot_service calls in nexus_fs.sys_write and
sys_unlink. Kernel no longer needs to know about snapshot_service.

VFSWriteHook on_post_write → captures pre-write state (old_metadata) and
post-write hash (content_hash) from WriteHookContext.

VFSDeleteHook on_pre_delete → captures pre-delete metadata from
DeleteHookContext (enriched by sys_unlink with metadata=meta).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec
    from nexus.contracts.vfs_hooks import DeleteHookContext, WriteHookContext


class SnapshotWriteInterceptor:
    """Record write/delete operations into active snapshot transactions.

    Implements HotSwappable so it can be enlisted via coordinator and
    registered into KernelDispatch hook chains.
    """

    # ── HotSwappable protocol ───────────────────────────────────────────

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(write_hooks=(self,), delete_hooks=(self,))

    async def drain(self) -> None:
        pass

    async def activate(self) -> None:
        pass

    # ── Constructor ─────────────────────────────────────────────────────

    def __init__(self, snapshot_service: Any) -> None:
        self._snapshot_service = snapshot_service

    # ── VFSWriteHook ────────────────────────────────────────────────────

    def on_post_write(self, ctx: "WriteHookContext") -> None:
        """Record write into active snapshot transaction.

        Fires after the physical write completes so ctx.content_hash
        (the new CAS hash) is available alongside ctx.old_metadata
        (the pre-write state needed for rollback).
        """
        txn_id = self._snapshot_service.is_tracked(ctx.path)
        if txn_id is None:
            return
        old_meta = ctx.old_metadata
        snapshot_hash = old_meta.etag if old_meta is not None else None
        metadata_snapshot = None
        if old_meta is not None:
            metadata_snapshot = {
                "size": old_meta.size,
                "version": old_meta.version,
                "modified_at": old_meta.modified_at.isoformat() if old_meta.modified_at else None,
            }
        self._snapshot_service.track_write(
            txn_id, ctx.path, snapshot_hash, metadata_snapshot, ctx.content_hash
        )

    # ── VFSDeleteHook ───────────────────────────────────────────────────

    def on_post_delete(self, ctx: "DeleteHookContext") -> None:
        """No-op: snapshot tracking is done in on_pre_delete (before actual deletion)."""

    def on_pre_delete(self, ctx: "DeleteHookContext") -> None:
        """Record delete into active snapshot transaction.

        Must fire BEFORE the physical delete so the file's metadata is
        still accessible. Requires sys_unlink to pass metadata=meta
        when constructing DeleteHookContext (Issue #1770).
        """
        txn_id = self._snapshot_service.is_tracked(ctx.path)
        if txn_id is None:
            return
        meta = ctx.metadata
        if meta is None:
            return
        snapshot_hash = meta.etag
        metadata_snapshot = {
            "size": meta.size,
            "version": meta.version,
            "modified_at": meta.modified_at.isoformat() if meta.modified_at else None,
            "backend_name": meta.backend_name,
            "physical_path": meta.physical_path,
        }
        self._snapshot_service.track_delete(txn_id, ctx.path, snapshot_hash, metadata_snapshot)
