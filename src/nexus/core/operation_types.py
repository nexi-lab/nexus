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
    CHMOD = "chmod"
    CHOWN = "chown"
    CHGRP = "chgrp"
    SETFACL = "setfacl"
