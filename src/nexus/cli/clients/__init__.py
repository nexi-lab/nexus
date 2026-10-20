"""Domain-specific HTTP clients for Nexus CLI commands."""

from nexus.cli.clients.agent_ext import AgentExtClient
from nexus.cli.clients.base import BaseServiceClient, NexusAPIError

__all__ = [
    "AgentExtClient",
    "BaseServiceClient",
    "NexusAPIError",
]
