"""A2A task store implementations.

Two pluggable backends:

- ``CacheBackedTaskStore`` — CacheStoreABC-backed, for dev/test and embedded mode
- ``VFSTaskStore`` — file-based via VFSOperations, Lego-compliant
"""

from nexus.bricks.a2a.stores.in_memory import CacheBackedTaskStore
from nexus.bricks.a2a.stores.vfs import VFSTaskStore

__all__ = [
    "CacheBackedTaskStore",
    "VFSTaskStore",
]
