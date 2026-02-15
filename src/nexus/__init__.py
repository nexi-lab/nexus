"""
Nexus: AI-Native Distributed Filesystem Architecture

Nexus is a complete AI agent infrastructure platform that combines distributed
unified filesystem, self-evolving agent memory, intelligent document processing,
and seamless deployment across three modes.

Three Deployment Modes, One Codebase:
- Standalone: Single-node redb, no Raft (like SQLite) — default mode
- Remote: Thin HTTP client via RemoteNexusFS
- Federation: ZoneManager + Raft consensus for multi-node clusters

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

from __future__ import annotations

__version__ = "0.7.1.dev0"
__author__ = "Nexi Lab Team"
__license__ = "Apache-2.0"

from typing import TYPE_CHECKING, Any, cast

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
    from nexus.core._metadata_generated import FileMetadataProtocol
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


def _setup_multi_zone(zone_mgr: Any, peers: list[str] | None, max_retries: int = 5) -> None:
    """Create/join zones and mount DT_MOUNT entries from env vars.

    Env vars:
        NEXUS_ZONE_CREATE: comma-separated zone IDs to create (leader node)
        NEXUS_ZONE_JOIN:   comma-separated zone IDs to join (follower nodes)
        NEXUS_MOUNTS:      comma-separated /path=zone_id mount declarations

    Args:
        zone_mgr: ZoneManager instance
        peers: List of peer addresses
        max_retries: Number of retries for Raft leader election errors
    """
    import logging
    import os
    import posixpath
    import time

    logger = logging.getLogger(__name__)

    zone_create_str = os.environ.get("NEXUS_ZONE_CREATE", "")
    zone_join_str = os.environ.get("NEXUS_ZONE_JOIN", "")
    mounts_str = os.environ.get("NEXUS_MOUNTS", "")

    def _with_leader_retry(fn: Any, zid: str, action: str) -> None:
        """Retry zone operations that fail due to Raft leader election."""
        for attempt in range(max_retries):
            try:
                fn(zid, peers=peers)
                return
            except RuntimeError as e:
                if "not leader" in str(e).lower() and attempt < max_retries - 1:
                    wait = 0.5 * (attempt + 1)
                    logger.warning(
                        "Zone %s %s failed (not leader), retrying in %.1fs (%d/%d)",
                        zid,
                        action,
                        wait,
                        attempt + 1,
                        max_retries,
                    )
                    time.sleep(wait)
                    continue
                raise

    for zid in (z.strip() for z in zone_create_str.split(",") if z.strip()):
        if zid not in zone_mgr.list_zones():
            _with_leader_retry(zone_mgr.create_zone, zid, "create")

    for zid in (z.strip() for z in zone_join_str.split(",") if z.strip()):
        if zid not in zone_mgr.list_zones():
            _with_leader_retry(zone_mgr.join_zone, zid, "join")

    if not mounts_str:
        return

    from nexus.core._metadata_generated import DT_DIR, DT_MOUNT, FileMetadata
    from nexus.raft.zone_path_resolver import ZonePathResolver

    resolver = ZonePathResolver(zone_mgr, root_zone_id="root")
    for entry in mounts_str.split(","):
        entry = entry.strip()
        if "=" not in entry:
            continue
        mpath, tzid = entry.split("=", 1)
        mpath, tzid = mpath.strip(), tzid.strip()
        if not mpath or not tzid:
            continue
        parent = posixpath.dirname(mpath)
        name = posixpath.basename(mpath)
        if not name:
            continue
        resolved = resolver.resolve(parent)
        rel_path = posixpath.join(resolved.path, name)
        store = zone_mgr.get_store(resolved.zone_id)
        if store is None:
            continue
        existing = store.get(rel_path)
        if existing and existing.entry_type == DT_MOUNT:
            continue  # Already mounted (idempotent)
        if not existing:
            store.put(
                FileMetadata(
                    path=rel_path,
                    backend_name="",
                    physical_path="",
                    size=0,
                    entry_type=DT_DIR,
                )
            )
        zone_mgr.mount(resolved.zone_id, rel_path, tzid)


def connect(
    config: str | Path | dict | NexusConfig | None = None,
) -> NexusFilesystem:
    """
    Connect to Nexus filesystem.

    This is the main entry point for using Nexus. It dispatches based on the
    deployment mode in configuration:

    - **standalone** (default): Single-node redb, no Raft. Like SQLite.
    - **remote**: Thin HTTP client via RemoteNexusFS.
    - **federation**: ZoneManager + Raft consensus for multi-node clusters.

    Args:
        config: Configuration source:
            - None: Auto-discover from environment/files (default)
            - str/Path: Path to config file
            - dict: Configuration dictionary
            - NexusConfig: Already loaded config

    Returns:
        NexusFilesystem instance (mode-dependent):
            - remote: Returns RemoteNexusFS (thin HTTP client)
            - standalone/federation: Returns NexusFS with local backend

        All modes implement the NexusFilesystem interface, ensuring consistent
        API across deployment modes.

    Raises:
        ValueError: If configuration is invalid or mode is unknown

    Examples:
        Remote mode (production client):
            >>> nx = nexus.connect(config={
            ...     "mode": "remote",
            ...     "url": "http://localhost:2026",
            ...     "api_key": "your-api-key"
            ... })

        Standalone mode (development/testing):
            >>> nx = nexus.connect()
            >>> nx.write("/workspace/file.txt", b"Hello World")

        Federation mode (multi-node cluster):
            >>> # Requires NEXUS_PEERS, NEXUS_NODE_ID, NEXUS_BIND_ADDR env vars
            >>> nx = nexus.connect(config={"mode": "federation"})
    """
    import os
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

    # ── Mode: remote ─────────────────────────────────────────────────
    if cfg.mode == "remote":
        server_url = cfg.url or os.getenv("NEXUS_URL")
        if not server_url:
            raise ValueError(
                "mode='remote' requires a server URL. "
                "Set 'url' in config or NEXUS_URL environment variable."
            )
        api_key = cfg.api_key or os.getenv("NEXUS_API_KEY")
        timeout = int(cfg.timeout) if hasattr(cfg, "timeout") else 30
        connect_timeout = int(cfg.connect_timeout) if hasattr(cfg, "connect_timeout") else 5
        return cast(NexusFilesystem, RemoteNexusFS(
            server_url=server_url,
            api_key=api_key,
            timeout=timeout,
            connect_timeout=connect_timeout,
        ))

    # ── Modes: standalone / federation ───────────────────────────────
    if cfg.mode not in ("standalone", "federation"):
        raise ValueError(
            f"Unknown mode: '{cfg.mode}'. Must be one of: standalone, remote, federation"
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

    # Create backend based on configuration
    backend: Backend
    if cfg.backend == "gcs":
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
        data_dir = cfg.data_dir if cfg.data_dir is not None else "./nexus-data"
        backend = LocalBackend(root_path=Path(data_dir).resolve())
        metadata_path = cfg.db_path or str(Path(data_dir) / "metadata")

    # Create metadata store based on mode
    if cfg.mode == "federation":
        metadata_store = _create_federation_metastore(metadata_path)
    else:
        # standalone: single-node embedded Raft (no peers)
        metadata_store = RaftMetadataStore.embedded(metadata_path)

    # Permission defaults: standalone without explicit config → permissive
    enforce_permissions = cfg.enforce_permissions
    if config is None or isinstance(config, dict) and "enforce_permissions" not in config:
        enforce_permissions = False

    # Zone isolation: default enabled for security
    enforce_zone_isolation = cfg.enforce_zone_isolation
    if config is None or isinstance(config, dict) and "enforce_zone_isolation" not in config:
        enforce_zone_isolation = True

    # Tiger Cache
    enable_tiger_cache_env = os.getenv("NEXUS_ENABLE_TIGER_CACHE", "true").lower()
    enable_tiger_cache = enable_tiger_cache_env in ("true", "1", "yes")

    # RecordStore (Four Pillars)
    record_store = SQLAlchemyRecordStore(db_path=cfg.db_path)

    # Create NexusFS via factory
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


def _create_federation_metastore(metadata_path: str) -> FileMetadataProtocol:
    """Create a federation-mode metadata store using ZoneManager + Raft.

    Requires the Rust extension built with --features full.
    Reads cluster config from env vars (SSOT):
        NEXUS_NODE_ID, NEXUS_DATA_DIR, NEXUS_BIND_ADDR, NEXUS_PEERS
    """
    import os
    from pathlib import Path

    try:
        from nexus.raft import ZoneAwareMetadataStore
        from nexus.raft.zone_manager import ZoneManager
    except ImportError as err:
        raise ImportError(
            "mode='federation' requires the Rust Raft extension built with --features full. "
            "Build with: maturin develop -m rust/nexus_raft/Cargo.toml --features full"
        ) from err

    nexus_peers = os.environ.get("NEXUS_PEERS", "")
    node_id = int(os.environ.get("NEXUS_NODE_ID", "1"))
    bind_addr = os.environ.get("NEXUS_BIND_ADDR", "0.0.0.0:2126")
    zones_dir = os.environ.get("NEXUS_DATA_DIR", str(Path(metadata_path).parent / "zones"))

    peers = [p.strip() for p in nexus_peers.split(",") if p.strip()] if nexus_peers else None
    zone_mgr = ZoneManager(node_id=node_id, base_path=zones_dir, bind_addr=bind_addr)
    zone_mgr.bootstrap(root_zone_id="root", peers=peers)
    _setup_multi_zone(zone_mgr, peers)
    return ZoneAwareMetadataStore.from_zone_manager(zone_mgr, root_zone_id="root")


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
