"""OperationType StrEnum — canonical operation types for operation_log.

Replaces hardcoded string lists in OperationLogModel.validate() and
EventDeliveryWorker._OP_TO_EVENT_TYPE.

Issue #1138, #1139: Event Stream Export + Event Replay foundation.
"""

from enum import StrEnum


class OperationType(StrEnum):
    """Filesystem operation types recorded in operation_log."""

    WRITE = "write"
    DELETE = "delete"
    RENAME = "rename"
    MKDIR = "mkdir"
    RMDIR = "rmdir"
    RMDIR_RECURSIVE = "rmdir_recursive"
    CHMOD = "chmod"
    CHOWN = "chown"
    CHGRP = "chgrp"
    SETFACL = "setfacl"

    # Aspect-level mutations (Issue #2929): logged when AspectService.put_aspect()
    # or delete_aspect() is called directly (e.g., CatalogService.extract_schema()).
    # These carry MCL columns (entity_urn, aspect_name, change_type) for replay.
    ASPECT_UPSERT = "aspect_upsert"
    ASPECT_DELETE = "aspect_delete"
