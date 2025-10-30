"""
Nexus: AI-Native Distributed Filesystem Architecture

Nexus is a complete AI agent infrastructure platform that combines distributed
unified filesystem, self-evolving agent memory, intelligent document processing,
and seamless deployment across three modes.

Three Deployment Modes, One Codebase:
- Embedded: Zero-deployment, library mode (like SQLite)
- Monolithic: Single server for teams
- Distributed: Kubernetes-ready for enterprise scale

SDK vs CLI:
-----------
For programmatic access (building tools, libraries, integrations), use the SDK:

    from nexus.sdk import connect

    nx = connect()
    nx.write("/workspace/data.txt", b"Hello World")
    content = nx.read("/workspace/data.txt")

For command-line usage, use the nexus CLI:

    $ nexus ls /workspace
    $ nexus write /file.txt "content"

Backward Compatibility:
-----------------------
    import nexus

    nx = nexus.connect()  # Still works, but prefer nexus.sdk.connect()

The main nexus module re-exports core functionality for backward compatibility.
New projects should use nexus.sdk for a cleaner API.
"""

__version__ = "0.3.9"
__author__ = "Nexus Team"
__license__ = "Apache-2.0"

from pathlib import Path

from nexus.backends.backend import Backend
from nexus.backends.gcs import GCSBackend
from nexus.backends.local import LocalBackend
from nexus.config import NexusConfig, load_config
from nexus.core.exceptions import (
    BackendError,
    InvalidPathError,
    MetadataError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
)
from nexus.core.filesystem import NexusFilesystem
from nexus.core.nexus_fs import NexusFS
from nexus.core.router import NamespaceConfig
from nexus.remote import RemoteNexusFS

# Skills system
from nexus.skills import (
    Skill,
    SkillDependencyError,
    SkillExporter,
    SkillExportError,
    SkillExportManifest,
    SkillManager,
    SkillManagerError,
    SkillMetadata,
    SkillNotFoundError,
    SkillParseError,
    SkillParser,
    SkillRegistry,
)

# Planned imports for future modules:
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
            - Embedded mode: Returns NexusFS with LocalBackend
            - Server mode: Returns NexusFS with GCSBackend
            - Cloud mode: Returns CloudClient (not yet implemented)

        All modes implement the NexusFilesystem interface, ensuring consistent
        API across deployment modes.

    Raises:
        ValueError: If configuration is invalid
        NotImplementedError: If mode is not yet implemented

    Examples:
        Use local backend (default):
            >>> import nexus
            >>> nx = nexus.connect()
            >>> nx.write("/workspace/file.txt", b"Hello World")
            >>> content = nx.read("/workspace/file.txt")

        Use GCS backend via config:
            >>> nx = nexus.connect(config={
            ...     "backend": "gcs",
            ...     "gcs_bucket_name": "my-bucket",
            ... })

        Use GCS backend via environment variables:
            >>> # export NEXUS_BACKEND=gcs
            >>> # export NEXUS_GCS_BUCKET_NAME=my-bucket
            >>> nx = nexus.connect()
    """
    # Load configuration
    cfg = load_config(config)

    # Return appropriate client based on mode
    if cfg.mode == "embedded":
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

        # Create backend based on configuration
        backend: Backend
        if cfg.backend == "gcs":
            # GCS backend
            if not cfg.gcs_bucket_name:
                raise ValueError(
                    "gcs_bucket_name is required when backend='gcs'. "
                    "Set gcs_bucket_name in your config or NEXUS_GCS_BUCKET_NAME environment variable."
                )
            backend = GCSBackend(
                bucket_name=cfg.gcs_bucket_name,
                project_id=cfg.gcs_project_id,
                credentials_path=cfg.gcs_credentials_path,
            )
            # Default db_path for GCS backend
            db_path = cfg.db_path
            if db_path is None:
                # Store metadata DB locally
                db_path = str(Path("./nexus-gcs-metadata.db"))
        else:
            # Local backend (default)
            data_dir = cfg.data_dir if cfg.data_dir is not None else "./nexus-data"
            backend = LocalBackend(root_path=Path(data_dir).resolve())
            # Default db_path for local backend
            # Only use SQLite path if no database URL is configured
            db_path = cfg.db_path
            import os

            if (
                db_path is None
                and not os.getenv("NEXUS_DATABASE_URL")
                and not os.getenv("POSTGRES_URL")
            ):
                db_path = str(Path(data_dir) / "metadata.db")

        # Embedded mode: default to no permissions (like SQLite)
        # User can explicitly enable with config={"enforce_permissions": True}
        enforce_permissions = cfg.enforce_permissions
        if config is None:
            # No explicit config provided - use sensible embedded defaults
            enforce_permissions = False
        elif isinstance(config, dict) and "enforce_permissions" not in config:
            # Dict config without explicit enforce_permissions - use embedded default
            enforce_permissions = False

        # Create NexusFS instance
        nx_fs = NexusFS(
            backend=backend,
            db_path=db_path,
            is_admin=cfg.is_admin,
            custom_namespaces=custom_namespaces,
            enable_metadata_cache=cfg.enable_metadata_cache,
            cache_path_size=cfg.cache_path_size,
            cache_list_size=cfg.cache_list_size,
            cache_kv_size=cfg.cache_kv_size,
            cache_exists_size=cfg.cache_exists_size,
            cache_ttl_seconds=cfg.cache_ttl_seconds,
            auto_parse=cfg.auto_parse,
            custom_parsers=cfg.parsers,
            enforce_permissions=enforce_permissions,
            enable_workflows=cfg.enable_workflows,  # v0.7.0: Workflow automation
        )

        # Set memory config for Memory API
        if cfg.tenant_id or cfg.user_id or cfg.agent_id:
            nx_fs._memory_config = {
                "tenant_id": cfg.tenant_id,
                "user_id": cfg.user_id,
                "agent_id": cfg.agent_id,
            }

        return nx_fs
    elif cfg.mode in ["monolithic", "distributed"]:
        raise NotImplementedError(
            f"{cfg.mode} mode is not yet implemented. "
            f"Currently only 'embedded' mode is supported. "
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
    # Filesystem implementation
    "NexusFS",
    "RemoteNexusFS",  # Remote filesystem client
    # Backends
    "LocalBackend",
    "GCSBackend",
    # Exceptions
    "NexusError",
    "NexusFileNotFoundError",
    "NexusPermissionError",
    "BackendError",
    "InvalidPathError",
    "MetadataError",
    # Router
    "NamespaceConfig",
    # Skills System
    "SkillRegistry",
    "SkillExporter",
    "SkillManager",
    "SkillParser",
    "Skill",
    "SkillMetadata",
    "SkillExportManifest",
    "SkillNotFoundError",
    "SkillDependencyError",
    "SkillManagerError",
    "SkillParseError",
    "SkillExportError",
]
