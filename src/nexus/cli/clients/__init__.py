"""Domain-specific HTTP clients for Nexus CLI commands."""

from nexus.cli.clients.agent_ext import AgentExtClient
from nexus.cli.clients.base import BaseServiceClient, NexusAPIError
from nexus.cli.clients.conflicts import ConflictsClient
from nexus.cli.clients.delegation import DelegationClient
from nexus.cli.clients.graph import GraphClient
from nexus.cli.clients.identity import IdentityClient
from nexus.cli.clients.manifest import ManifestClient
from nexus.cli.clients.rlm import RLMClient
from nexus.cli.clients.scheduler import SchedulerClient
from nexus.cli.clients.secrets_audit import SecretsAuditClient
from nexus.cli.clients.share import ShareClient
from nexus.cli.clients.upload import UploadClient

__all__ = [
    "AgentExtClient",
    "BaseServiceClient",
    "ConflictsClient",
    "DelegationClient",
    "GraphClient",
    "IdentityClient",
    "ManifestClient",
    "NexusAPIError",
    "RLMClient",
    "SchedulerClient",
    "SecretsAuditClient",
    "ShareClient",
    "UploadClient",
]
