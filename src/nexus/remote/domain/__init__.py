"""Domain client modules for the remote Nexus filesystem.

Each domain client encapsulates a group of related RPC methods
(skills, sandbox, OAuth, MCP, share links, memory, admin, ACE, LLM).
Both sync and async variants are co-located in the same file.

Issue #1603: Decompose remote/client.py into domain clients.
"""

from nexus.remote.domain.ace import AsyncACEClient
from nexus.remote.domain.admin import AsyncAdminClient
from nexus.remote.domain.llm import AsyncLLMClient
from nexus.remote.domain.mcp import AsyncMCPClient, MCPClient
from nexus.remote.domain.memory import AsyncMemoryClient, MemoryClient
from nexus.remote.domain.oauth import AsyncOAuthClient, OAuthClient
from nexus.remote.domain.sandbox import AsyncSandboxClient, SandboxClient
from nexus.remote.domain.share_links import AsyncShareLinksClient, ShareLinksClient
from nexus.remote.domain.skills import AsyncSkillsClient, SkillsClient

__all__ = [
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
    "AsyncAdminClient",
    "AsyncACEClient",
    "AsyncLLMClient",
]
