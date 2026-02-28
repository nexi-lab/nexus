"""Domain client modules for the remote Nexus filesystem.

Each domain client encapsulates a group of related RPC methods
(sandbox, OAuth, MCP, share links, memory, admin, LLM).
Both sync and async variants are co-located in the same file.

Issue #1603: Decompose remote/client.py into domain clients.
"""

from nexus.remote.domain.admin import AsyncAdminClient
from nexus.remote.domain.llm import AsyncLLMClient
from nexus.remote.domain.mcp import AsyncMCPClient, MCPClient
from nexus.remote.domain.memory import AsyncMemoryClient, MemoryClient
from nexus.remote.domain.oauth import AsyncOAuthClient, OAuthClient
from nexus.remote.domain.sandbox import AsyncSandboxClient, SandboxClient
from nexus.remote.domain.share_links import AsyncShareLinksClient, ShareLinksClient

__all__ = [
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
    "AsyncAdminClient",
    "AsyncLLMClient",
]
