"""Pluggable storage drivers for IPC message persistence.

The IPC brick delegates all storage to an ``IPCStorageDriver`` implementation.
This decouples delivery logic from storage concerns, allowing messages to be
stored in the VFS (filesystem), PostgreSQL, or any future backend.

Issue: #1243
"""

from nexus.ipc.storage.postgresql_driver import PostgreSQLStorageDriver
from nexus.ipc.storage.protocol import IPCStorageDriver
from nexus.ipc.storage.vfs_driver import VFSStorageDriver

__all__ = ["IPCStorageDriver", "VFSStorageDriver", "PostgreSQLStorageDriver"]
