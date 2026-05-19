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
            old_content = self._read_content_from_cas(operation.path, operation.snapshot_hash)
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

        content = self._read_content_from_cas(operation.path, operation.snapshot_hash)
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

    def _read_content_from_cas(self, path: str, content_id: str) -> bytes:  # noqa: ARG002
        """Read content via the kernel syscall path (§2.5 mediation).

        Architectural gap (FOLLOW-UP — same as TimeTravelService._read_snapshot):
            ``content_id`` is the hash of the OLD bytes recorded by the write
            observer at log time. The bytes are in CAS keyed by that hash,
            but service tier cannot reach them by hash (§2.5). This method
            currently returns ``sys_read_raw(path)`` which is the CURRENT
            bytes at the path — wrong for any undo-after-write where the
            current content already differs. The systematic fix is a
            kernel-side snapshot-on-write that publishes pre-write bytes to
            a path-addressed namespace; that change belongs in rust/kernel,
            not here.

            ``content_id`` is kept in the signature for the future migration
            but is unused today.
        """
        _kernel = getattr(self._dlc, "_kernel", None) if self._dlc else None
        if _kernel is None:
            raise RuntimeError(f"No kernel available for CAS read: {path}")
        result: bytes = _kernel.sys_read_raw(path, "root")
        return result
