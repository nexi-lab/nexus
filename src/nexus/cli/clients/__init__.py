"""Domain-specific HTTP clients for Nexus CLI commands."""

from nexus.cli.clients.base import BaseServiceClient, NexusAPIError

__all__ = [
    "BaseServiceClient",
    "NexusAPIError",
]
