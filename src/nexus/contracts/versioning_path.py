"""Versioning history syscall-path convention (§2.5).

Single source of truth for the path scheme that holds pre-write snapshots
of file content. Used by:

  - The write observer (``nexus.storage.record_store_write_observer``),
    which publishes a metadata entry pointing at the OLD ``content_id``
    after each overwrite/delete.
  - ``TimeTravelService`` / ``OperationUndoService``
    (``nexus.bricks.versioning.*``), which read the snapshot through
    ``sys_read`` at the same path.

Both sides MUST derive the path here so they agree (no shadow SSOT).
"""

import hashlib

# Path namespace for versioning-history snapshots. Each snapshot is keyed by
# (sha256-of-virtual-path, operation_id) — a hash-bucketed flat directory
# layout keyed by the OperationLogModel.operation_id that produced the write.
VERSIONING_PATH_PREFIX = "/__sys__/versioning"


def versioning_snapshot_path(virtual_path: str, operation_id: str) -> str:
    """Canonical syscall path for a pre-write snapshot of ``virtual_path``.

    ``operation_id`` is the OperationLogModel.operation_id of the write that
    overwrote/deleted the content — the same key TimeTravelService uses to
    look up historical bytes.
    """
    path_hash = hashlib.sha256(virtual_path.encode("utf-8")).hexdigest()
    return f"{VERSIONING_PATH_PREFIX}/{path_hash}/{operation_id}.bin"
