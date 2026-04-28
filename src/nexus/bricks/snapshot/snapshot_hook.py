"""VFS write/delete hook for transactional snapshot tracking (Issue #1770).

Wraps ``TransactionalSnapshotService.track_write()`` / ``track_delete()``
as a proper KernelDispatch hook, eliminating direct kernel coupling
to the snapshot service.

Data mapping:
    WriteHookContext.old_metadata  → snapshot_hash (content_id), metadata_snapshot
    WriteHookContext.content_id  → new content hash
    DeleteHookContext.metadata     → pre-delete snapshot_hash, metadata_snapshot
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec
    from nexus.contracts.vfs_hooks import DeleteHookContext, WriteHookContext

logger = logging.getLogger(__name__)


class SnapshotWriteHook:
    """Post-write/delete hook that auto-tracks mutations in active transactions."""

    name = "snapshot_write_tracker"
    __slots__ = ("_svc",)

    # ── Hook spec (duck-typed) (Issue #1770) ──────────────────────────

    def hook_spec(self) -> HookSpec:
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(write_hooks=(self,), delete_hooks=(self,))

    def __init__(self, snapshot_service: Any) -> None:
        self._svc = snapshot_service

    # ── Hook callbacks ─────────────────────────────────────────────────

    def on_post_write(self, ctx: WriteHookContext) -> None:
        txn_id = self._svc.is_tracked(ctx.path)
        if txn_id is None:
            return
        old = ctx.old_metadata
        snapshot_hash = old.content_id if old else None
        metadata_snapshot: dict[str, Any] | None = None
        if old:
            metadata_snapshot = {
                "size": old.size,
                "version": old.version,
                "modified_at": old.modified_at.isoformat() if old.modified_at else None,
            }
        self._svc.track_write(
            txn_id,
            ctx.path,
            snapshot_hash,
            metadata_snapshot,
            ctx.content_id,
        )

    def on_post_delete(self, ctx: DeleteHookContext) -> None:
        txn_id = self._svc.is_tracked(ctx.path)
        if txn_id is None:
            return
        meta = ctx.metadata  # pre-delete state
        if meta is None:
            return
        snapshot_hash = meta.content_id
        # backend_name/physical_path were dropped from FileMetadata; the
        # kernel resolves a file's physical location at read time via the
        # mount/route layer, so the snapshot only needs content_id + path-level
        # facts to restore a file from CAS.
        metadata_snapshot: dict[str, Any] = {
            "size": meta.size,
            "version": meta.version,
            "modified_at": meta.modified_at.isoformat() if meta.modified_at else None,
        }
        self._svc.track_delete(txn_id, ctx.path, snapshot_hash, metadata_snapshot)
