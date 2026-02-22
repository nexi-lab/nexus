"""Context manifest source executors (Issue #1427, #1428)."""

from nexus.services.context_manifest.executors.executor_utils import (
    FileGlobSourceProtocol,
    MemoryQuerySourceProtocol,
    WorkspaceSnapshotSourceProtocol,
    resolve_source_template,
)
from nexus.services.context_manifest.executors.file_glob import FileGlobExecutor
from nexus.services.context_manifest.executors.memory_query import MemoryQueryExecutor
from nexus.services.context_manifest.executors.workspace_snapshot import (
    WorkspaceSnapshotExecutor,
)

__all__ = [
    "FileGlobExecutor",
    "FileGlobSourceProtocol",
    "MemoryQueryExecutor",
    "MemoryQuerySourceProtocol",
    "WorkspaceSnapshotExecutor",
    "WorkspaceSnapshotSourceProtocol",
    "resolve_source_template",
]
