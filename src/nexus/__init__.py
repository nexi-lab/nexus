"""
Nexus: AI-Native Distributed Filesystem Architecture

Nexus is a complete AI agent infrastructure platform that combines distributed
unified filesystem, self-evolving agent memory, intelligent document processing,
and seamless deployment across three modes.

Three Deployment Modes, One Codebase:
- Embedded: Zero-deployment, library mode (like SQLite)
- Monolithic: Single server for teams
- Distributed: Kubernetes-ready for enterprise scale

Usage:
    import nexus

    # Mode auto-detected from config file or environment
    nx = nexus.connect()

    async with nx:
        await nx.write("/workspace/data.txt", b"Hello World")
        content = await nx.read("/workspace/data.txt")
"""

__version__ = "0.1.0"
__author__ = "Nexus Team"
__license__ = "Apache-2.0"

from nexus.config import NexusConfig, load_config

# TODO: Import other modules when they are implemented
# from nexus.core.client import NexusClient
# from nexus.core.embedded import Embedded, EmbeddedConfig
# from nexus.core.exceptions import (
#     BackendError,
#     FileNotFoundError,
#     NexusError,
#     PermissionError,
# )
# from nexus.interface import NexusInterface


__all__ = [
    # Version
    "__version__",
    # Configuration
    "NexusConfig",
    "load_config",
]
