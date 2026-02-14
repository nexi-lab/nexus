"""Protocol definition for pluggable IPC storage drivers.

Every storage driver implements this protocol. The IPC delivery layer
(MessageSender, MessageProcessor, etc.) depends only on this interface,
never on a concrete implementation.

Implementations:
- ``VFSStorageDriver`` — delegates to VFSOperations (filesystem-backed)
- ``PostgreSQLStorageDriver`` — stores messages in PostgreSQL via RecordStoreABC
- ``InMemoryStorageDriver`` — in-memory fake for testing
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class IPCStorageDriver(Protocol):
    """Pluggable storage backend for IPC messages.

    A superset of the operations needed by MessageSender, MessageProcessor,
    AgentProvisioner, AgentDiscovery, and TTLSweeper.

    All methods accept a ``zone_id`` for multi-tenant isolation.
    """

    async def read(self, path: str, zone_id: str) -> bytes:
        """Read file contents at the given path.

        Raises:
            FileNotFoundError: If the path does not exist.
        """
        ...

    async def write(self, path: str, data: bytes, zone_id: str) -> None:
        """Write data to the given path (create or overwrite)."""
        ...

    async def list_dir(self, path: str, zone_id: str) -> list[str]:
        """List filenames in a directory (not full paths).

        Raises:
            FileNotFoundError: If the directory does not exist.
        """
        ...

    async def count_dir(self, path: str, zone_id: str) -> int:
        """Count entries in a directory without listing them.

        More efficient than ``len(await self.list_dir(...))``.

        Raises:
            FileNotFoundError: If the directory does not exist.
        """
        ...

    async def rename(self, src: str, dst: str, zone_id: str) -> None:
        """Atomically rename/move a file from src to dst.

        Raises:
            FileNotFoundError: If src does not exist.
        """
        ...

    async def mkdir(self, path: str, zone_id: str) -> None:
        """Create a directory (including parents if needed).

        Idempotent — does not raise if directory already exists.
        """
        ...

    async def exists(self, path: str, zone_id: str) -> bool:
        """Check if a path (file or directory) exists."""
        ...
