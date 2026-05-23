"""Operation undo service — extracted from CLI layer (S24).

Orchestrates reversal of filesystem operations logged by OperationLogger.
Uses kernel + DLC for CAS content retrieval and kernel primitives for state mutation.

References:
    - docs/architecture/ops-scenario-matrix.md  (S24: Operations Undo)
    - services/protocols/operation_log.py        (OperationLogProtocol)
"""

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class UndoResult:
    """Result of an undo operation."""

    success: bool
    message: str
    operation_type: str
    path: str


class OperationUndoService:
    """Undo filesystem operations recorded in the operation log.

    Extracted from ``cli/commands/operations.py::_undo_operation`` so that the
    CLI remains a thin presentation layer (KERNEL-ARCHITECTURE.md).

    Dependencies are injected as callables to avoid coupling to a concrete
    NexusFS class.
    """

    def __init__(
        self,
        *,
        dlc: Any = None,
        write_fn: Any = None,
        delete_fn: Any = None,
        rename_fn: Any = None,
        exists_fn: Any = None,
    ) -> None:
        """Initialise the undo service.

        Args:
            dlc: DriverLifecycleCoordinator for routing + backend refs.
            write_fn: ``(path, content) -> None`` kernel write primitive.
            delete_fn: ``(path) -> None`` kernel delete primitive.
            rename_fn: ``(old, new) -> None`` kernel rename primitive.
            exists_fn: ``(path) -> bool`` kernel existence check.
        """
        self._dlc = dlc
        self._write = write_fn
        self._delete = delete_fn
        self._rename = rename_fn
        self._exists = exists_fn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def undo_operation(self, operation: Any) -> UndoResult:
        """Undo a single operation log entry.

        Args:
            operation: ``OperationLogModel`` to undo.

        Returns:
            ``UndoResult`` describing whether the undo succeeded and what
            action was taken.
        """
        op_type = operation.operation_type

        if op_type == "write":
            return self._undo_write(operation)
        if op_type == "delete":
            return self._undo_delete(operation)
        if op_type == "rename":
            return self._undo_rename(operation)

        return UndoResult(
            success=False,
            message=f"Undo not implemented for {op_type}",
            operation_type=op_type,
            path=operation.path,
        )

    # ------------------------------------------------------------------
    # Per-type undo strategies
    # ------------------------------------------------------------------

    def _undo_write(self, operation: Any) -> UndoResult:
        """Undo a write: restore previous content or delete new file."""
        if operation.snapshot_hash:
            old_content = self._read_snapshot(operation)
            self._write(operation.path, old_content)
            return UndoResult(
                success=True,
                message=f"Restored previous version of {operation.path}",
                operation_type="write",
                path=operation.path,
            )

        # File did not exist before — remove it.
        self._delete(operation.path)
        return UndoResult(
            success=True,
            message=f"Deleted {operation.path} (was newly created)",
            operation_type="write",
            path=operation.path,
        )

    def _undo_delete(self, operation: Any) -> UndoResult:
        """Undo a delete: restore content from CAS snapshot."""
        if not operation.snapshot_hash:
            return UndoResult(
                success=False,
                message=f"Cannot restore {operation.path} (no snapshot)",
                operation_type="delete",
                path=operation.path,
            )

        content = self._read_snapshot(operation)
        self._write(operation.path, content)

        # NOTE: metadata restoration (chown/chgrp/chmod) is intentionally
        # omitted.  The original CLI code called nx.chown/chgrp/chmod which
        # do not exist on the NexusFS kernel — only on the FUSE layer.
        # A future task should add metadata restoration via MetastoreABC.

        return UndoResult(
            success=True,
            message=f"Restored deleted file: {operation.path}",
            operation_type="delete",
            path=operation.path,
        )

    def _undo_rename(self, operation: Any) -> UndoResult:
        """Undo a rename: move file back to original path."""
        if not operation.new_path:
            return UndoResult(
                success=False,
                message="Cannot undo rename - missing new_path",
                operation_type="rename",
                path=operation.path,
            )

        if not self._exists(operation.new_path):
            return UndoResult(
                success=False,
                message=(f"Cannot undo rename - {operation.new_path} no longer exists"),
                operation_type="rename",
                path=operation.path,
            )

        self._rename(operation.new_path, operation.path)
        return UndoResult(
            success=True,
            message=f"Renamed {operation.new_path} back to {operation.path}",
            operation_type="rename",
            path=operation.path,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_snapshot(self, operation: Any) -> bytes:
        """Read the pre-write snapshot bytes for ``operation`` via the syscall.

        Snapshots live at ``/__sys__/versioning/{sha256(path)}/{op_id}.bin`` —
        published by the write observer's OBSERVE-stage hook when the write
        that produced ``operation`` was committed. Both sides derive the path
        from ``nexus.contracts.versioning_path`` so the convention is SSOT.

        Reads through ``sys_read_raw`` (§2.5 — service tier never touches CAS
        by hash). The snapshot entry is metadata-only and points back at the
        original ``content_id``, so this round-trip costs one path resolve plus
        one CAS fetch — no byte copy at write time.

        Raises ``RuntimeError`` if no kernel is wired; propagates the kernel's
        ``NexusFileNotFoundError`` if the snapshot was never published (legacy
        operation log entries from before the observer was wired, or paths the
        observer skips).
        """
        from nexus.contracts.versioning_path import versioning_snapshot_path

        _kernel = getattr(self._dlc, "_kernel", None) if self._dlc else None
        if _kernel is None:
            raise RuntimeError(f"No kernel available for snapshot read: {operation.path}")
        snap_path = versioning_snapshot_path(operation.path, operation.operation_id)
        result: bytes = _kernel.sys_read_raw(snap_path, "root")
        return result
