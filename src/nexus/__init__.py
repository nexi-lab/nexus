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

PERFORMANCE NOTE:
-----------------
This module uses lazy imports to minimize startup time. Heavy modules like
nexus.skills, nexus.core.nexus_fs, and nexus.remote are only loaded when
first accessed. This reduces import time from ~10s to ~1s for simple use cases.
"""

__version__ = "0.7.1.dev0"
__author__ = "Nexi Lab Team"
__license__ = "Apache-2.0"

from typing import TYPE_CHECKING, Any

# =============================================================================
# LAZY IMPORTS for performance optimization
# =============================================================================
# These modules are imported lazily via __getattr__ to avoid loading heavy
# dependencies (skills, nexus_fs, remote) on module import.
# This significantly speeds up CLI startup and FUSE mount initialization.

if TYPE_CHECKING:
    # Type hints for IDE support - these don't trigger actual imports
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

# =============================================================================
# Lightweight imports (always loaded) - these are fast
# =============================================================================
from nexus.core.exceptions import (
    BackendError,
    InvalidPathError,
    MetadataError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
)

# Module-level cache for lazy imports
_lazy_imports_cache: dict[str, Any] = {}

# Mapping of attribute names to their import paths
_LAZY_IMPORTS = {
    # Backends
    "Backend": ("nexus.backends.backend", "Backend"),
    "LocalBackend": ("nexus.backends.local", "LocalBackend"),
    "GCSBackend": ("nexus.backends.gcs", "GCSBackend"),
    # Config
    "NexusConfig": ("nexus.config", "NexusConfig"),
    "load_config": ("nexus.config", "load_config"),
    # Core - heavy
    "NexusFilesystem": ("nexus.core.filesystem", "NexusFilesystem"),
    "NexusFS": ("nexus.core.nexus_fs", "NexusFS"),
    "NamespaceConfig": ("nexus.core.router", "NamespaceConfig"),
    # Remote - needed for FUSE mount
    "RemoteNexusFS": ("nexus.remote", "RemoteNexusFS"),
    # Skills - very heavy
    "Skill": ("nexus.skills", "Skill"),
    "SkillDependencyError": ("nexus.skills", "SkillDependencyError"),
    "SkillExporter": ("nexus.skills", "SkillExporter"),
    "SkillExportError": ("nexus.skills", "SkillExportError"),
    "SkillManager": ("nexus.skills", "SkillManager"),
    "SkillManagerError": ("nexus.skills", "SkillManagerError"),
    "SkillMetadata": ("nexus.skills", "SkillMetadata"),
    "SkillNotFoundError": ("nexus.skills", "SkillNotFoundError"),
    "SkillParseError": ("nexus.skills", "SkillParseError"),
    "SkillParser": ("nexus.skills", "SkillParser"),
    "SkillRegistry": ("nexus.skills", "SkillRegistry"),
}


def __getattr__(name: str) -> Any:
    """Lazy import for heavy dependencies.

    This function is called when an attribute is not found in the module.
    It loads the requested module/class on demand, significantly reducing
    import time for simple use cases.
    """
    # Check cache first
    if name in _lazy_imports_cache:
        return _lazy_imports_cache[name]

    # Check if this is a lazy import
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        import importlib

        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        _lazy_imports_cache[name] = value
        return value

    # Special case: connect function (defined below but needs lazy deps)
    if name == "connect":
        return connect

    raise AttributeError(f"module 'nexus' has no attribute {name!r}")


def connect(
    config: "str | Path | dict | NexusConfig | None" = None,
) -> "NexusFilesystem":
    """
    Connect to Nexus filesystem.

    This is the main entry point for using Nexus. It auto-detects the deployment
    mode from configuration and returns the appropriate client.

    **Connection Priority**:
    1. If `url` is set in config or `NEXUS_URL` environment variable → Remote mode (RemoteNexusFS)
    2. Otherwise → Embedded mode (NexusFS with local backend)

    **Recommended**: Use server mode for production, embedded mode for development/testing only.

    Args:
        config: Configuration source:
            - None: Auto-discover from environment/files (default)
            - str/Path: Path to config file
            - dict: Configuration dictionary
            - NexusConfig: Already loaded config

    Returns:
        NexusFilesystem instance (mode-dependent):
            - Remote/Server mode: Returns RemoteNexusFS (thin HTTP client)
            - Embedded mode: Returns NexusFS with LocalBackend

        All modes implement the NexusFilesystem interface, ensuring consistent
        API across deployment modes.

    Raises:
        ValueError: If configuration is invalid
        NotImplementedError: If mode is not yet implemented

    Examples:
        Server mode (recommended for production):
            >>> import nexus
            >>> # Requires nexus server running (nexus serve)
            >>> # export NEXUS_URL=http://localhost:2026
            >>> # export NEXUS_API_KEY=your-api-key
            >>> nx = nexus.connect()
            >>> nx.write("/workspace/file.txt", b"Hello World")

        Server mode with explicit config:
            >>> nx = nexus.connect(config={
            ...     "url": "http://localhost:2026",
            ...     "api_key": "your-api-key"
            ... })

        Embedded mode (development/testing only):
            >>> # No NEXUS_URL set
            >>> nx = nexus.connect()
            >>> nx.write("/workspace/file.txt", b"Hello World")

        Explicit embedded mode:
            >>> nx = nexus.connect(config={
            ...     "mode": "embedded",
            ...     "data_dir": "./nexus-data"
            ... })
    """
    import os
    import warnings
    from pathlib import Path

    # Lazy load dependencies
    from nexus.backends.backend import Backend
    from nexus.backends.local import LocalBackend
    from nexus.config import NexusConfig, load_config
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.router import NamespaceConfig
    from nexus.remote import RemoteNexusFS
    from nexus.storage.raft_metadata_store import RaftMetadataStore
    from nexus.storage.record_store import SQLAlchemyRecordStore

    # Load configuration
    cfg = load_config(config)

    # Check for unimplemented modes first
    if cfg.mode in ["monolithic", "distributed"]:
        raise NotImplementedError(
            f"{cfg.mode} mode is not yet implemented. "
            f"Currently only 'embedded' mode is supported. "
            f"For multi-zone deployments, use server mode instead."
        )

    # PRIORITY 1: Check for server URL (remote mode)
    # If url is explicitly set in config or NEXUS_URL env var, use RemoteNexusFS
    # IMPORTANT: If mode is explicitly set to "embedded" in config, skip URL check
    # This allows server mode to force local NexusFS even when NEXUS_URL is set
    explicit_embedded = isinstance(config, dict) and config.get("mode") == "embedded"

    if not explicit_embedded:
        server_url = cfg.url or os.getenv("NEXUS_URL")
        if server_url:
            # Remote/Server mode: thin HTTP client
            api_key = cfg.api_key or os.getenv("NEXUS_API_KEY")

            # Connection parameters with sensible defaults
            timeout = int(cfg.timeout) if hasattr(cfg, "timeout") else 30
            connect_timeout = int(cfg.connect_timeout) if hasattr(cfg, "connect_timeout") else 5

            return RemoteNexusFS(
                server_url=server_url,
                api_key=api_key,
                timeout=timeout,
                connect_timeout=connect_timeout,
            )

    # PRIORITY 2: Embedded mode (local backend)
    # Only used if no URL is configured
    # Return appropriate client based on mode
    if cfg.mode == "embedded":
        # Warn if embedded mode is being used without explicit intent
        # (i.e., user didn't explicitly set mode="embedded")
        if config is None or (isinstance(config, dict) and "mode" not in config):
            warnings.warn(
                "Embedded mode is intended for development and testing only. "
                "For production deployments, use server mode:\n"
                "  1. Start server: nexus serve --host 0.0.0.0 --port 2026\n"
                "  2. Set environment: export NEXUS_URL=http://localhost:2026\n"
                "  3. Connect: nx = nexus.connect()\n"
                "To silence this warning, explicitly set mode='embedded' in your config.",
                UserWarning,
                stacklevel=2,
            )

        # Parse custom namespaces from config
        custom_namespaces = None
        if cfg.namespaces:
            custom_namespaces = [
                NamespaceConfig(
                    name=ns["name"],
                    readonly=ns.get("readonly", False),
                    admin_only=ns.get("admin_only", False),
                    requires_zone=ns.get("requires_zone", True),
                )
                for ns in cfg.namespaces
            ]

        def _create_metadata_store(metadata_path: str) -> RaftMetadataStore:
            """Create metadata store, auto-detecting cluster mode from env."""
            nexus_peers = os.environ.get("NEXUS_PEERS", "")
            if nexus_peers:
                node_id = int(os.environ.get("NEXUS_NODE_ID", "1"))
                bind_addr = os.environ.get("NEXUS_BIND_ADDR", "0.0.0.0:2126")
                peers = [p.strip() for p in nexus_peers.split(",") if p.strip()]
                return RaftMetadataStore.consensus(
                    node_id=node_id,
                    db_path=metadata_path,
                    bind_address=bind_addr,
                    peers=peers,
                )
            return RaftMetadataStore.embedded(metadata_path)

        # Create backend based on configuration
        backend: Backend
        if cfg.backend == "gcs":
            # GCS backend - import lazily to avoid loading google.cloud.storage on startup
            from nexus.backends.gcs import GCSBackend

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
            metadata_path = cfg.db_path or str(Path("./nexus-gcs-metadata"))
        else:
            # Local backend (default)
            data_dir = cfg.data_dir if cfg.data_dir is not None else "./nexus-data"
            backend = LocalBackend(root_path=Path(data_dir).resolve())
            metadata_path = cfg.db_path or str(Path(data_dir) / "metadata")

        # Metadata store: auto-detect cluster mode from env (NEXUS_PEERS)
        metadata_store = _create_metadata_store(metadata_path)

        # Embedded mode: default to no permissions (like SQLite)
        # User can explicitly enable with config={"enforce_permissions": True}
        enforce_permissions = cfg.enforce_permissions
        if config is None:
            # No explicit config provided - use sensible embedded defaults
            enforce_permissions = False
        elif isinstance(config, dict) and "enforce_permissions" not in config:
            # Dict config without explicit enforce_permissions - use embedded default
            enforce_permissions = False

        # Handle zone isolation configuration
        # Default: enabled for security unless explicitly disabled
        enforce_zone_isolation = cfg.enforce_zone_isolation
        if config is None:
            # No explicit config - use secure default (enabled)
            enforce_zone_isolation = True
        elif isinstance(config, dict) and "enforce_zone_isolation" not in config:
            # Dict config without explicit setting - use secure default
            enforce_zone_isolation = True

        # Handle Tiger Cache configuration
        # Default: enabled for PostgreSQL backends (provides materialized permissions)
        # Can be disabled via NEXUS_ENABLE_TIGER_CACHE=false env var
        enable_tiger_cache_env = os.getenv("NEXUS_ENABLE_TIGER_CACHE", "true").lower()
        enable_tiger_cache = enable_tiger_cache_env in ("true", "1", "yes")

        # Create RecordStore for Services layer (Task #14: Four Pillars)
        # User Space selects the driver; Kernel only sees RecordStoreABC
        record_store = SQLAlchemyRecordStore(db_path=cfg.db_path)

        # Create NexusFS instance via factory (Task #23: kernel does not auto-create services)
        from nexus.factory import create_nexus_fs

        nx_fs = create_nexus_fs(
            backend=backend,
            metadata_store=metadata_store,
            record_store=record_store,
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
            parse_providers=cfg.parse_providers,
            enforce_permissions=enforce_permissions,
            allow_admin_bypass=cfg.allow_admin_bypass,
            enforce_zone_isolation=enforce_zone_isolation,
            enable_workflows=cfg.enable_workflows,
            enable_tiger_cache=enable_tiger_cache,
        )

        # Set memory config for Memory API
        if cfg.zone_id or cfg.user_id or cfg.agent_id:
            nx_fs._memory_config = {
                "zone_id": cfg.zone_id,
                "user_id": cfg.user_id,
                "agent_id": cfg.agent_id,
            }

        # Store config for OAuth factory and other components that need it
        nx_fs._config = cfg

        return nx_fs
    else:
        # This should never be reached as unimplemented modes are checked at the top
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
    # Exceptions (always loaded - lightweight)
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
    "SkillNotFoundError",
    "SkillDependencyError",
    "SkillManagerError",
    "SkillParseError",
    "SkillExportError",
]
