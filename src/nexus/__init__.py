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

from pathlib import Path

from nexus.config import NexusConfig, load_config
from nexus.core.embedded import Embedded
from nexus.core.exceptions import (
    BackendError,
    InvalidPathError,
    MetadataError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
)
from nexus.core.filesystem import NexusFilesystem
from nexus.core.router import NamespaceConfig

# TODO: Import other modules when they are implemented
# from nexus.core.client import NexusClient
# from nexus.interface import NexusInterface


def connect(
    config: str | Path | dict | NexusConfig | None = None,
) -> NexusFilesystem:
    """
    Connect to Nexus filesystem.

    This is the main entry point for using Nexus. It auto-detects the deployment
    mode from configuration and returns the appropriate client.

    Args:
        config: Configuration source:
            - None: Auto-discover from environment/files (default)
            - str/Path: Path to config file
            - dict: Configuration dictionary
            - NexusConfig: Already loaded config

    Returns:
        NexusFilesystem instance (mode-dependent):
            - Embedded mode: Returns Embedded instance
            - Monolithic mode: Returns MonolithClient (not yet implemented)
            - Distributed mode: Returns DistributedClient (not yet implemented)

        All modes implement the NexusFilesystem interface, ensuring consistent
        API across deployment modes.

    Raises:
        ValueError: If configuration is invalid
        NotImplementedError: If mode is not yet implemented

    Example:
        >>> import nexus
        >>> nx = nexus.connect()
        >>> nx.write("/workspace/file.txt", b"Hello World")
        >>> content = nx.read("/workspace/file.txt")
    """
    # Load configuration
    cfg = load_config(config)

    # Return appropriate client based on mode
    if cfg.mode == "embedded":
        # Provide default if None (shouldn't happen due to config defaults, but type checker needs this)
        data_dir = cfg.data_dir if cfg.data_dir is not None else "./nexus-data"

        # Parse custom namespaces from config
        custom_namespaces = None
        if cfg.namespaces:
            custom_namespaces = [
                NamespaceConfig(
                    name=ns["name"],
                    readonly=ns.get("readonly", False),
                    admin_only=ns.get("admin_only", False),
                    requires_tenant=ns.get("requires_tenant", True),
                )
                for ns in cfg.namespaces
            ]

        return Embedded(
            data_dir=data_dir,
            db_path=cfg.db_path,
            tenant_id=cfg.tenant_id,
            agent_id=cfg.agent_id,
            is_admin=cfg.is_admin,
            custom_namespaces=custom_namespaces,
        )
    elif cfg.mode in ["monolithic", "distributed"]:
        # TODO: Implement in v0.2.0+
        raise NotImplementedError(
            f"{cfg.mode} mode is not yet implemented. "
            f"Currently only 'embedded' mode is supported in v0.1.0. "
            f"Set mode='embedded' in your config or NEXUS_MODE environment variable."
        )
    else:
        raise ValueError(f"Unknown mode: {cfg.mode}")


__all__ = [
    # Version
    "__version__",
    # Main entry point
    "connect",
    # Configuration
    "NexusConfig",
    "load_config",
    # Core interfaces
    "NexusFilesystem",  # Abstract base class for all filesystem modes
    # Embedded mode (for advanced usage)
    "Embedded",
    # Exceptions
    "NexusError",
    "NexusFileNotFoundError",
    "NexusPermissionError",
    "BackendError",
    "InvalidPathError",
    "MetadataError",
]
