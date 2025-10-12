"""Core functionality for Nexus filesystem."""

from nexus.core.client import NexusClient
from nexus.core.embedded import Embedded, EmbeddedConfig
from nexus.core.exceptions import NexusError

__all__ = ["NexusClient", "Embedded", "EmbeddedConfig", "NexusError"]
