"""Context manifest source executors (Issue #1427, #1428)."""

from nexus.bricks.context_manifest.executors.executor_utils import (
    FileGlobSourceProtocol,
    MemoryQuerySourceProtocol,
    resolve_source_template,
)
from nexus.bricks.context_manifest.executors.file_glob import FileGlobExecutor
from nexus.bricks.context_manifest.executors.memory_query import MemoryQueryExecutor

__all__ = [
    "FileGlobExecutor",
    "FileGlobSourceProtocol",
    "MemoryQueryExecutor",
    "MemoryQuerySourceProtocol",
    "resolve_source_template",
]
