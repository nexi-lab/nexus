"""
Nexus SDK - Clean programmatic interface for third-party tools.

This module provides a clean, stable API for building custom tools and interfaces
on top of Nexus, without any CLI dependencies. Use this SDK to build:
- Custom GUIs and TUIs
- Web interfaces
- IDE plugins
- Custom automation tools
- Language bindings

The SDK interface is stable and semantic-versioned separately from CLI changes.

Quick Start (Server Mode - Recommended):
    >>> from nexus.sdk import connect
    >>>
    >>> # Start server first: nexusd --host 0.0.0.0 --port 2026
    >>> # Set environment: export NEXUS_URL=http://localhost:2026
    >>>
    >>> # Connect to Nexus server (thin HTTP client)
    >>> nx = connect()
    >>>
    >>> # File operations
    >>> nx.sys_write("/workspace/file.txt", b"Hello World")
    >>> content = nx.sys_read("/workspace/file.txt")
    >>> nx.sys_unlink("/workspace/file.txt")
    >>>
    >>> # Discovery
    >>> files = nx.sys_readdir("/workspace", recursive=True)
    >>> python_files = nx.glob("**/*.py")
    >>> todos = nx.grep("TODO", file_pattern="**/*.py")

Quick Start (Local - Development Only):
    >>> # No server required, but less suitable for production
    >>> nx = connect(config={"data_dir": "./nexus-data"})
    >>> nx.sys_write("/workspace/file.txt", b"Hello World")

Configuration:
    >>> # Server mode with auto-discovery (recommended)
    >>> # Checks NEXUS_URL and NEXUS_API_KEY environment variables
    >>> nx = connect()
    >>>
    >>> # Server mode with explicit config
    >>> nx = connect(config={
    ...     "url": "http://localhost:2026",
    ...     "api_key": "your-api-key"
    ... })
    >>>
    >>> # Local mode (development/testing only)
    >>> nx = connect(config={
    ...     "data_dir": "./nexus-data"
    ... })
    >>>
    >>> # From config file
    >>> nx = connect(config="/path/to/nexus.yaml")
"""

__all__ = [
    # Main entry point
    "connect",
    # Configuration
    "Config",
    "load_config",
    # Core interfaces
    "Filesystem",
    "NexusFS",
    # Backends
    "Backend",
    "CASLocalBackend",
    "PathLocalBackend",
    "CASGCSBackend",
    # Exceptions
    "NexusError",
    "FileNotFoundError",
    "PermissionError",
    "BackendError",
    "InvalidPathError",
    "MetadataError",
    "ValidationError",
    # Permissions
    "OperationContext",
    "PermissionEnforcer",
    # ReBAC
    "ReBACManager",
    "ReBACTuple",
    "Entity",
    "WILDCARD_SUBJECT",
    "ConsistencyLevel",
    "CheckResult",
    "GraphLimitExceeded",
]

# Re-export from core modules with cleaner names
from pathlib import Path
from typing import Union

from nexus.backends.base.backend import Backend
from nexus.backends.storage.cas_gcs import CASGCSBackend
from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.backends.storage.path_local import PathLocalBackend
from nexus.bricks.rebac.domain import WILDCARD_SUBJECT, Entity, ReBACTuple
from nexus.bricks.rebac.enforcer import PermissionEnforcer
from nexus.bricks.rebac.manager import (
    CheckResult,
    ConsistencyLevel,
    GraphLimitExceeded,
    ReBACManager,
)
from nexus.config import NexusConfig as Config
from nexus.config import load_config
from nexus.contracts.exceptions import (
    BackendError,
    InvalidPathError,
    MetadataError,
    NexusError,
    ValidationError,
)
from nexus.contracts.exceptions import (
    NexusFileNotFoundError as FileNotFoundError,
)
from nexus.contracts.exceptions import (
    NexusPermissionError as PermissionError,
)
from nexus.contracts.filesystem.filesystem_abc import NexusFilesystemABC as Filesystem
from nexus.contracts.types import OperationContext
from nexus.core.nexus_fs import NexusFS


def connect(
    config: str | Path | dict | Config | None = None,
) -> Filesystem:
    """
    Connect to Nexus filesystem.

    This is the main SDK entry point. It auto-detects the deployment mode
    from configuration and returns the appropriate client.

    Args:
        config: Configuration source:
            - None: Auto-discover from environment/files (default)
            - str/Path: Path to config file
            - dict: Configuration dictionary
            - Config: Already loaded config object

    Returns:
        Filesystem instance implementing the Nexus interface.

    Raises:
        ValueError: If configuration is invalid
        NotImplementedError: If mode is not yet implemented

    Examples:
        >>> # Use local backend (default)
        >>> nx = connect()
        >>> nx.sys_write("/workspace/file.txt", b"Hello World")
        >>> content = nx.sys_read("/workspace/file.txt")

        >>> # Use GCS backend
        >>> nx = connect(config={
        ...     "backend": "gcs",
        ...     "gcs_bucket_name": "my-bucket",
        ... })

        >>> # From config file
        >>> nx = connect(config="/path/to/nexus.yaml")
    """
    # Delegate to the main connect function from nexus package
    from nexus import connect as nexus_connect

    return nexus_connect(config)
