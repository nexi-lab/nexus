"""Remote Nexus filesystem client.

This module provides remote client implementations of NexusFilesystem
that connect to a Nexus RPC server over HTTP.

Two client implementations are available:
- RemoteNexusFS: Synchronous client using httpx.Client
- AsyncRemoteNexusFS: Asynchronous client using httpx.AsyncClient

Domain-specific operations are organized into domain clients:
- SkillsClient / AsyncSkillsClient
- SandboxClient / AsyncSandboxClient
- OAuthClient / AsyncOAuthClient
- MCPClient / AsyncMCPClient
- ShareLinksClient / AsyncShareLinksClient
- MemoryClient / AsyncMemoryClient
- AsyncAdminClient (async-only)
- AsyncACEClient (async-only)
- AsyncLLMClient (async-only)

Example (sync):
    >>> from nexus.remote import RemoteNexusFS
    >>> nx = RemoteNexusFS("http://localhost:2026", api_key="sk-xxx")
    >>> content = nx.read("/workspace/file.txt")
    >>> nx.skills.create("my-skill", "A skill", template="basic")

Example (async):
    >>> from nexus.remote import AsyncRemoteNexusFS
    >>> async with AsyncRemoteNexusFS("http://localhost:2026", api_key="sk-xxx") as nx:
    ...     content = await nx.read("/workspace/file.txt")
    ...     await nx.skills.create("my-skill", "A skill", template="basic")
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
    RemoteMemory,
    RemoteNexusFS,
    RemoteTimeoutError,
)
from nexus.remote.domain import (
    AsyncACEClient,
    AsyncAdminClient,
    AsyncLLMClient,
    AsyncMCPClient,
    AsyncMemoryClient,
    AsyncOAuthClient,
    AsyncSandboxClient,
    AsyncShareLinksClient,
    AsyncSkillsClient,
    MCPClient,
    MemoryClient,
    OAuthClient,
    SandboxClient,
    ShareLinksClient,
    SkillsClient,
)

__all__ = [
    # Main clients
    "RemoteNexusFS",
    "AsyncRemoteNexusFS",
    # Backwards-compat wrapper classes
    "AsyncRemoteMemory",
    "AsyncAdminAPI",
    "AsyncACE",
    "RemoteMemory",
    # Error classes
    "RemoteFilesystemError",
    "RemoteConnectionError",
    "RemoteTimeoutError",
    # Domain clients (sync + async)
    "SkillsClient",
    "AsyncSkillsClient",
    "SandboxClient",
    "AsyncSandboxClient",
    "OAuthClient",
    "AsyncOAuthClient",
    "MCPClient",
    "AsyncMCPClient",
    "ShareLinksClient",
    "AsyncShareLinksClient",
    "MemoryClient",
    "AsyncMemoryClient",
    # Async-only domain clients
    "AsyncAdminClient",
    "AsyncACEClient",
    "AsyncLLMClient",
]
