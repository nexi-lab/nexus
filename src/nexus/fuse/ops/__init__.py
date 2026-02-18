"""FUSE operation handler subpackage.

Re-exports all handler classes and shared context for clean imports.
"""

from nexus.fuse.ops._events import FUSEEventDispatcher
from nexus.fuse.ops._shared import (
    FUSESharedContext,
    MetadataObj,
    fuse_operation,
)
from nexus.fuse.ops.attr_handler import AttrHandler
from nexus.fuse.ops.io_handler import IOHandler
from nexus.fuse.ops.metadata_handler import MetadataHandler
from nexus.fuse.ops.mutation_handler import MutationHandler

__all__ = [
    "AttrHandler",
    "FUSEEventDispatcher",
    "FUSESharedContext",
    "IOHandler",
    "MetadataHandler",
    "MetadataObj",
    "MutationHandler",
    "fuse_operation",
]
