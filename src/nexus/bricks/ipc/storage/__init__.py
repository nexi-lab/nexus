"""Pluggable storage drivers for IPC message persistence.

The IPC brick delegates all storage to an ``IPCStorageDriver`` implementation.
This decouples delivery logic from storage concerns, allowing messages to be
stored in the VFS (filesystem), PostgreSQL, or any future backend.

Issue: #1243
"""

from nexus.bricks.ipc.storage.cross_zone_driver import CrossZoneStorageDriver
from nexus.bricks.ipc.storage.protocol import IPCStorageDriver
from nexus.bricks.ipc.storage.recordstore_driver import RecordStoreStorageDriver
from nexus.bricks.ipc.storage.vfs_driver import VFSStorageDriver

__all__ = [
    "IPCStorageDriver",
    "VFSStorageDriver",
    "RecordStoreStorageDriver",
    "CrossZoneStorageDriver",
]
