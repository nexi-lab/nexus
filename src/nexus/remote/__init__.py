"""Remote Nexus filesystem client.

This module provides remote client implementations of NexusFilesystem
that connect to a Nexus RPC server over HTTP.

Two client implementations are available:
- RemoteNexusFS: Synchronous client using httpx.Client
- AsyncRemoteNexusFS: Asynchronous client using httpx.AsyncClient

Example (sync):
    >>> from nexus.remote import RemoteNexusFS
    >>> nx = RemoteNexusFS("http://localhost:8080", api_key="sk-xxx")
    >>> content = nx.read("/workspace/file.txt")

Example (async):
    >>> from nexus.remote import AsyncRemoteNexusFS, AsyncRemoteMemory
    >>> async with AsyncRemoteNexusFS("http://localhost:8080", api_key="sk-xxx") as nx:
    ...     content = await nx.read("/workspace/file.txt")
    ...     # Parallel reads
    ...     contents = await asyncio.gather(*[nx.read(p) for p in paths])
    ...     # Memory operations
    ...     memory = AsyncRemoteMemory(nx)
    ...     mem_id = await memory.store("User prefers dark mode")
"""

from nexus.remote.async_client import (
    AsyncACE,
    AsyncAdminAPI,
    AsyncRemoteMemory,
    AsyncRemoteNexusFS,
)
from nexus.remote.client import (
    RemoteConnectionError,
    RemoteFilesystemError,
    RemoteNexusFS,
    RemoteTimeoutError,
)

__all__ = [
    "RemoteNexusFS",
    "AsyncRemoteNexusFS",
    "AsyncRemoteMemory",
    "AsyncAdminAPI",
    "AsyncACE",
    "RemoteFilesystemError",
    "RemoteConnectionError",
    "RemoteTimeoutError",
]
