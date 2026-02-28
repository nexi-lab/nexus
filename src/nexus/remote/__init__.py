"""Remote Nexus filesystem transport layer.

This module provides the transport and proxy infrastructure for REMOTE
deployment profile. Use ``nexus.connect(config={"mode": "remote", ...})``
to create a remote NexusFS instance.

Domain-specific operations are organized into domain clients:
- SandboxClient / AsyncSandboxClient
- OAuthClient / AsyncOAuthClient
- MCPClient / AsyncMCPClient
- ShareLinksClient / AsyncShareLinksClient
- MemoryClient / AsyncMemoryClient
- AsyncAdminClient (async-only)
- AsyncLLMClient (async-only)

Example:
    >>> import nexus
    >>> nx = nexus.connect(config={"mode": "remote", "url": "http://localhost:2026", "api_key": "sk-xxx"})
    >>> content = nx.sys_read("/workspace/file.txt")
"""

from nexus.contracts.exceptions import (
    RemoteConnectionError,
    RemoteFilesystemError,
    RemoteTimeoutError,
)
from nexus.remote.domain import (
    AsyncAdminClient,
    AsyncLLMClient,
    AsyncMCPClient,
    AsyncMemoryClient,
    AsyncOAuthClient,
    AsyncSandboxClient,
    AsyncShareLinksClient,
    MCPClient,
    MemoryClient,
    OAuthClient,
    SandboxClient,
    ShareLinksClient,
)

__all__ = [
    # Error classes
    "RemoteFilesystemError",
    "RemoteConnectionError",
    "RemoteTimeoutError",
    # Domain clients (sync + async)
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
    "AsyncLLMClient",
]
