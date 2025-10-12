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
from typing import Any, Dict, Optional, Union

from nexus.config import NexusConfig, load_config
from nexus.core.client import NexusClient
from nexus.core.embedded import Embedded, EmbeddedConfig
from nexus.core.exceptions import (
    BackendError,
    FileNotFoundError,
    NexusError,
    PermissionError,
)
from nexus.interface import NexusInterface


def connect(
    config: Optional[Union[str, Path, dict[str, Any], NexusConfig]] = None,
) -> NexusInterface:
    """
    Connect to Nexus filesystem.

    This is the unified entry point for all deployment modes. The mode is
    determined by configuration (file, environment, or provided dict), making
    your code deployment-mode agnostic.

    Args:
        config: Configuration source:
            - None: Auto-discover from nexus.yaml, ~/.nexus/config.yaml, or env vars
            - str/Path: Path to config file (YAML/TOML)
            - dict: Configuration dictionary
            - NexusConfig: Pre-loaded configuration object

    Returns:
        NexusInterface: Connection to Nexus filesystem (mode-specific implementation)

    Examples:
        # Auto-discover config (recommended)
        nx = nexus.connect()

        # Explicit config file
        nx = nexus.connect("./config.yaml")

        # Programmatic config
        nx = nexus.connect(config={
            "mode": "embedded",
            "data_dir": "./nexus-data"
        })

        # Use with async context manager
        async with nexus.connect() as nx:
            await nx.write("/workspace/file.txt", b"content")
            content = await nx.read("/workspace/file.txt")

    Configuration Discovery:
        1. Provided config parameter
        2. ./nexus.yaml or ./nexus.yml
        3. ~/.nexus/config.yaml
        4. Environment variables (NEXUS_MODE, NEXUS_DATA_DIR, etc.)
        5. Defaults (embedded mode with ./nexus-data)

    Migration Example:
        # Development (nexus.yaml)
        mode: embedded
        data_dir: ./nexus-data

        # Production (nexus.yaml)
        mode: distributed
        url: https://nexus.company.com

        # Code stays identical! Just swap config file.
    """
    # Load configuration
    cfg = load_config(config)

    # Factory: Create appropriate client based on mode
    if cfg.mode == "embedded":
        return _create_embedded_client(cfg)
    elif cfg.mode in ["monolithic", "distributed"]:
        return _create_remote_client(cfg)
    else:
        raise ValueError(f"Unknown deployment mode: {cfg.mode}")


def _create_embedded_client(config: NexusConfig) -> NexusInterface:
    """Create embedded mode client."""
    # Convert NexusConfig to EmbeddedConfig
    embedded_config = EmbeddedConfig(
        data_dir=config.data_dir or "./nexus-data",
        cache_size_mb=config.cache_size_mb,
        enable_vector_search=config.enable_vector_search,
        enable_llm_cache=config.enable_llm_cache,
        db_path=config.db_path,
    )
    return Embedded(embedded_config)


def _create_remote_client(config: NexusConfig) -> NexusInterface:
    """Create remote mode client (monolithic or distributed)."""
    if not config.url:
        raise ValueError(f"{config.mode} mode requires 'url' in configuration")
    if not config.api_key:
        raise ValueError(f"{config.mode} mode requires 'api_key' in configuration")

    return NexusClient(
        api_key=config.api_key,
        base_url=config.url,
        timeout=config.timeout,
    )


__all__ = [
    # Version
    "__version__",
    # Main API
    "connect",
    # Legacy direct imports (deprecated - use connect() instead)
    "NexusClient",
    "Embedded",
    "EmbeddedConfig",
    # Configuration
    "NexusConfig",
    "load_config",
    # Interface
    "NexusInterface",
    # Exceptions
    "NexusError",
    "FileNotFoundError",
    "PermissionError",
    "BackendError",
]
