"""A2A task store implementations.

Three pluggable backends:

- ``InMemoryTaskStore`` — dict-based, for testing and embedded mode
- ``VFSTaskStore`` — file-based via IPCStorageDriver, Lego-compliant
- ``DatabaseTaskStore`` — SQLAlchemy-backed (PostgreSQL/SQLite)
"""

from __future__ import annotations

from nexus.a2a.stores.in_memory import InMemoryTaskStore
from nexus.a2a.stores.vfs import VFSTaskStore

__all__ = [
    "InMemoryTaskStore",
    "VFSTaskStore",
]
