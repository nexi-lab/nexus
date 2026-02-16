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
    >>> # Start server first: nexus serve --host 0.0.0.0 --port 2026
    >>> # Set environment: export NEXUS_URL=http://localhost:2026
    >>>
    >>> # Connect to Nexus server (thin HTTP client)
    >>> nx = connect()
    >>>
    >>> # File operations
    >>> nx.write("/workspace/file.txt", b"Hello World")
    >>> content = nx.read("/workspace/file.txt")
    >>> nx.delete("/workspace/file.txt")
    >>>
    >>> # Discovery
    >>> files = nx.list("/workspace", recursive=True)
    >>> python_files = nx.glob("**/*.py")
    >>> todos = nx.grep("TODO", file_pattern="**/*.py")

Quick Start (Standalone Mode - Development Only):
    >>> # No server required, but less suitable for production
    >>> nx = connect(config={"mode": "standalone", "data_dir": "./nexus-data"})
    >>> nx.write("/workspace/file.txt", b"Hello World")

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
    >>> # Standalone mode (development/testing only)
    >>> nx = connect(config={
    ...     "mode": "standalone",
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
    # Core interfaces (ABCs / protocols — no concrete implementations)
    "Filesystem",
    "Backend",
    # Exceptions
    "NexusError",
    "FileNotFoundError",
    "PermissionError",
    "BackendError",
    "InvalidPathError",
    "MetadataError",
    "ValidationError",
    # Skills System
    "SkillRegistry",
    "SkillExporter",
    "SkillManager",
    "SkillParser",
    "Skill",
    "SkillMetadata",
    "SkillNotFoundError",
    "SkillDependencyError",
    "SkillManagerError",
    "SkillParseError",
    "SkillExportError",
    # Permissions
    "OperationContext",
    # ReBAC types (data classes only — no service internals)
    "ReBACTuple",
    "Entity",
    "WILDCARD_SUBJECT",
    # Router
    "NamespaceConfig",
]

# Re-export ABCs, interfaces, and data types only — no concrete implementations.
# Per KERNEL-ARCHITECTURE: "Drivers are config-time DI" — SDK consumers must not
# depend on specific driver classes (LocalBackend, GCSBackend) or service internals
# (PermissionEnforcer, EnhancedReBACManager).  Use connect() to get a Filesystem.
from pathlib import Path

from nexus.backends.backend import Backend
from nexus.config import NexusConfig as Config
from nexus.config import load_config
from nexus.core.exceptions import (
    BackendError,
    InvalidPathError,
    MetadataError,
    NexusError,
    ValidationError,
)
from nexus.core.exceptions import (
    NexusFileNotFoundError as FileNotFoundError,
)
from nexus.core.exceptions import (
    NexusPermissionError as PermissionError,
)
from nexus.core.filesystem import NexusFilesystem as Filesystem
from nexus.core.permissions import OperationContext
from nexus.core.rebac import WILDCARD_SUBJECT, Entity, ReBACTuple
from nexus.core.router import NamespaceConfig
from nexus.skills import (
    Skill,
    SkillDependencyError,
    SkillExporter,
    SkillExportError,
    SkillManager,
    SkillManagerError,
    SkillMetadata,
    SkillNotFoundError,
    SkillParseError,
    SkillParser,
    SkillRegistry,
)


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
        >>> nx.write("/workspace/file.txt", b"Hello World")
        >>> content = nx.read("/workspace/file.txt")

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
