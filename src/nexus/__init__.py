"""
Nexus = filesystem/context plane.

Nexus combines a VFS-style filesystem interface with deployment-aware context,
storage, and service composition for agent systems.

Deployment profiles control which bricks are enabled:
- minimal: Bare VFS, storage only
- embedded: Storage + eventlog
- lite: Core services
- full: All bricks (default)
- cloud: All bricks + federation
- innovation: All bricks + startup validation (experimental)
- remote: Thin gRPC client (RemoteBackend + RemoteServiceProxy)

SDK vs CLI:
-----------
For programmatic access (building tools, libraries, integrations), use the SDK:

    from nexus.sdk import connect

    nx = connect(config={"profile": "minimal", "data_dir": "./nexus-data"})
    await nx.sys_write("/workspace/data.txt", b"Hello World")
    content = await nx.sys_read("/workspace/data.txt")

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
nexus.core.nexus_fs and nexus.remote are only loaded when
first accessed. This reduces import time from ~10s to ~1s for simple use cases.
"""

import logging
import os as _os
from typing import TYPE_CHECKING, Any, cast

__version__ = "0.9.6"  # release version
__author__ = "Nexi Lab Team"
__license__ = "Apache-2.0"

# =============================================================================
# LAZY IMPORTS for performance optimization
# =============================================================================
# These modules are imported lazily via __getattr__ to avoid loading heavy
# dependencies (nexus_fs, remote) on module import.
# This significantly speeds up CLI startup and FUSE mount initialization.

if TYPE_CHECKING:
    # Type hints for IDE support - these don't trigger actual imports
    from pathlib import Path

    from nexus.backends.base.backend import Backend
    from nexus.backends.storage.cas_gcs import CASGCSBackend as GCSBackend
    from nexus.backends.storage.cas_local import CASLocalBackend
    from nexus.config import NexusConfig, load_config
    from nexus.contracts.exceptions import (
        BackendError,
        InvalidPathError,
        MetadataError,
        NexusError,
        NexusFileNotFoundError,
        NexusPermissionError,
    )
    from nexus.contracts.filesystem.filesystem_abc import NexusFilesystemABC as NexusFilesystem
    from nexus.core.metastore import MetastoreABC
    from nexus.core.nexus_fs import NexusFS

# =============================================================================
# Lightweight imports (always loaded) - these are fast
# =============================================================================
from nexus.contracts.exceptions import (
    BackendError,
    InvalidPathError,
    MetadataError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
)

# All mutable state (data, metastore, record store, etc.) lives under this directory.
NEXUS_STATE_DIR = _os.path.expanduser("~/.nexus")

logger = logging.getLogger(__name__)

# Module-level cache for lazy imports
_lazy_imports_cache: dict[str, Any] = {}

# Mapping of attribute names to their import paths
_LAZY_IMPORTS = {
    # Backends
    "Backend": ("nexus.backends.base.backend", "Backend"),
    "CASLocalBackend": ("nexus.backends.storage.cas_local", "CASLocalBackend"),
    "GCSBackend": ("nexus.backends.storage.cas_gcs", "CASGCSBackend"),
    # Config
    "NexusConfig": ("nexus.config", "NexusConfig"),
    "load_config": ("nexus.config", "load_config"),
    # Core - heavy
    "NexusFilesystem": ("nexus.contracts.filesystem.filesystem_abc", "NexusFilesystemABC"),
    "NexusFS": ("nexus.core.nexus_fs", "NexusFS"),
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


async def connect(
    config: "str | Path | dict | NexusConfig | None" = None,
) -> "NexusFilesystem":
    """
    Connect to Nexus filesystem.

    This is the main entry point for using Nexus. It dispatches based on the
    deployment profile in configuration:

    - **profile="remote"**: Thin gRPC client (RemoteBackend + RemoteServiceProxy).
    - **All other profiles**: Local NexusFS. Federation (Raft + ZoneManager) is
      auto-detected based on whether the Rust extensions are importable.

    Args:
        config: Configuration source:
            - None: Auto-discover from environment/files (default)
            - str/Path: Path to config file
            - dict: Configuration dictionary
            - NexusConfig: Already loaded config

    Returns:
        NexusFilesystem instance. All profiles implement the NexusFilesystem
        interface, ensuring consistent API.

    Raises:
        ValueError: If configuration is invalid

    Examples:
        Remote profile (production client):
            >>> nx = nexus.connect(config={
            ...     "profile": "remote",
            ...     "url": "http://localhost:2026",
            ...     "api_key": "your-api-key"
            ... })

        Default (development/testing):
            >>> nx = nexus.connect()
            >>> await nx.sys_write("/workspace/file.txt", b"Hello World")

        Federation (auto-detected when Rust extensions available):
            >>> # Requires NEXUS_NODE_ID, NEXUS_BIND_ADDR env vars
            >>> nx = nexus.connect(config={"profile": "cloud"})
    """
    import os
    from pathlib import Path

    from nexus.config import NexusConfig, load_config

    # Load configuration
    cfg = load_config(config)

    # ── Profile: remote ──────────────────────────────────────────────
    if cfg.profile == "remote":
        from urllib.parse import urlparse

        server_url = cfg.url or os.getenv("NEXUS_URL")
        if not server_url:
            raise ValueError(
                "profile='remote' requires a server URL. "
                "Set 'url' in config or NEXUS_URL environment variable."
            )
        api_key = cfg.api_key or os.getenv("NEXUS_API_KEY")
        timeout = int(cfg.timeout) if hasattr(cfg, "timeout") else 30
        connect_timeout = int(cfg.connect_timeout) if hasattr(cfg, "connect_timeout") else 5

        # Build gRPC address from NEXUS_URL hostname + gRPC port.
        # Port precedence: NEXUS_GRPC_PORT env > nexus.yaml ports.grpc > default 2028
        _grpc_port_str = os.getenv("NEXUS_GRPC_PORT")
        if not _grpc_port_str:
            try:
                import yaml as _yaml

                _pf = Path("nexus.yaml")
                if _pf.exists():
                    with open(_pf) as _f:
                        _pc = _yaml.safe_load(_f) or {}
                    _grpc_port_str = str(_pc.get("ports", {}).get("grpc", ""))
            except Exception:
                pass
        grpc_port = int(_grpc_port_str) if _grpc_port_str else 2028
        parsed = urlparse(server_url)
        grpc_address = f"{parsed.hostname}:{grpc_port}"

        # Single shared RPCTransport (gRPC channel) for all remote proxies.
        from nexus.remote.rpc_transport import RPCTransport

        # TLS auto-discovery (3-tier precedence):
        #   1. NEXUS_DATA_DIR env var (2-phase bootstrap provisioned certs)
        #   2. nexus.yaml in CWD (data_dir written by `nexus up`)
        #   3. NexusConfig.data_dir field
        _tls_config = None
        _data_dir = os.getenv("NEXUS_DATA_DIR")
        if not _data_dir:
            # Read data_dir from nexus.yaml in CWD (written by nexus up)
            _project_yaml = Path("nexus.yaml")
            if _project_yaml.exists():
                try:
                    import yaml as _yaml

                    with open(_project_yaml) as _f:
                        _project_cfg = _yaml.safe_load(_f) or {}
                    _data_dir = _project_cfg.get("data_dir")
                except Exception:
                    pass
        if not _data_dir:
            _data_dir = getattr(cfg, "data_dir", None)

        if _data_dir:
            from nexus.security.tls.config import ZoneTlsConfig

            _tls_config = ZoneTlsConfig.from_data_dir(_data_dir)

        transport = RPCTransport(
            server_address=grpc_address,
            auth_token=api_key,
            timeout=float(timeout),
            connect_timeout=float(connect_timeout),
            tls_config=_tls_config,
        )

        # RemoteBackend + RemoteMetastore — stateless proxies, server is SSOT.
        from nexus.backends.storage.remote import RemoteBackend
        from nexus.storage.remote_metastore import RemoteMetastore

        remote_backend = RemoteBackend(transport)
        remote_metastore = RemoteMetastore(transport)

        # Build a lightweight NexusFS directly — no factory, no bricks.
        # Server is SSOT; client just proxies calls via gRPC.
        # No parser registries — remote delegates all parsing to the server.
        from nexus.core.config import BrickServices as _BrickServices
        from nexus.core.config import PermissionConfig as _PermissionConfig
        from nexus.core.nexus_fs import NexusFS as _RemoteNexusFS
        from nexus.core.router import PathRouter as _PathRouter

        _router = _PathRouter(remote_metastore)
        _router.add_mount("/", remote_backend)

        from nexus.core.config import KernelServices as _KernelServices

        nfs = _RemoteNexusFS(
            metadata_store=remote_metastore,
            permissions=_PermissionConfig(enforce=False),
            kernel_services=_KernelServices(router=_router),
            brick_services=_BrickServices(),
        )
        # Issue #1801: inject default context for REMOTE profile
        from nexus.contracts.types import OperationContext as _RemoteOC

        nfs._default_context = _RemoteOC(user_id="remote", groups=[], is_admin=False)

        # Wire service proxies for REMOTE profile (Issue #1171).
        # Fills all 25+ service slots with RemoteServiceProxy — forwards
        # method calls to the server via gRPC.
        from nexus.factory._remote import _boot_remote_services

        await _boot_remote_services(nfs, call_rpc=transport.call_rpc)
        nfs._register_runtime_closeable(transport)

        return nfs

    # ── Local node (single-node or federated, auto-detected) ────────
    # Heavy imports for local profiles
    from nexus.backends.base.backend import Backend
    from nexus.backends.storage.cas_local import CASLocalBackend
    from nexus.core.nexus_fs import NexusFS

    # Create backend based on configuration
    backend: Backend
    if cfg.backend == "gcs":
        from nexus.backends.storage.cas_gcs import CASGCSBackend

        if not cfg.gcs_bucket_name:
            raise ValueError(
                "gcs_bucket_name is required when backend='gcs'. "
                "Set gcs_bucket_name in your config or NEXUS_GCS_BUCKET_NAME environment variable."
            )
        backend = CASGCSBackend(
            bucket_name=cfg.gcs_bucket_name,
            project_id=cfg.gcs_project_id,
            credentials_path=cfg.gcs_credentials_path,
        )
        nexus_root = NEXUS_STATE_DIR
        data_dir = str(Path(nexus_root) / "data")
    else:
        data_dir = cfg.data_dir if cfg.data_dir is not None else str(Path(NEXUS_STATE_DIR) / "data")
        # nexus_root hosts sibling state directories (metastore, record_store).
        # When data_dir is explicitly provided (e.g. --data-dir /some/path), USE
        # data_dir itself as nexus_root so metastore goes inside it — this avoids
        # polluting the parent directory (which could be /tmp or /) and ensures
        # each data_dir is fully self-contained.  When data_dir is the default
        # (~/.nexus/data), the parent (~/.nexus) is still used as nexus_root
        # for backward compatibility.
        nexus_root = data_dir if cfg.data_dir is not None else str(Path(data_dir).parent)
        if cfg.backend == "path_local":
            from nexus.backends.storage.path_local import PathLocalBackend

            backend = PathLocalBackend(root_path=Path(data_dir).resolve())
        else:
            backend = CASLocalBackend(root_path=Path(data_dir).resolve())

    # Resolve paths — new fields take precedence, db_path is legacy fallback
    metadata_path = cfg.metastore_path or cfg.db_path or str(Path(nexus_root) / "metastore")
    record_store_path = cfg.record_store_path or None

    # Create metadata store — auto-detect federation capability
    metadata_store: MetastoreABC
    zone_mgr = None

    try:
        from nexus.contracts.constants import DEFAULT_GRPC_BIND_ADDR
        from nexus.raft import FederatedMetadataProxy
        from nexus.raft.zone_manager import ZoneManager

        node_id = int(os.environ.get("NEXUS_NODE_ID", "1"))
        bind_addr = os.environ.get("NEXUS_BIND_ADDR", DEFAULT_GRPC_BIND_ADDR)
        advertise_addr = os.environ.get("NEXUS_ADVERTISE_ADDR")
        zones_dir = os.environ.get("NEXUS_DATA_DIR", str(Path(metadata_path).parent / "zones"))

        # K3s-style pre-provision: if NEXUS_JOIN_TOKEN is set and certs
        # don't exist yet, provision TLS from the leader BEFORE creating
        # ZoneManager (so Raft transport starts with mTLS from the start).
        join_token = os.environ.get("NEXUS_JOIN_TOKEN")
        if join_token:
            tls_dir_pre = Path(zones_dir) / "tls"
            if not (tls_dir_pre / "node.pem").exists():
                # Find a peer address to join from NEXUS_PEERS
                join_peer = None
                for entry in (os.environ.get("NEXUS_PEERS", "")).split(","):
                    entry = entry.strip()
                    if "@" in entry:
                        peer_id_str, peer_addr = entry.split("@", 1)
                        if peer_id_str.strip() != str(node_id):
                            join_peer = peer_addr.strip()
                            break
                if join_peer:
                    from _nexus_raft import join_cluster as _join_cluster

                    logger.info(
                        "NEXUS_JOIN_TOKEN set -- provisioning TLS from %s",
                        join_peer,
                    )
                    _join_cluster(join_peer, join_token, node_id, str(tls_dir_pre))
                    logger.info("TLS provisioning complete")
                else:
                    raise RuntimeError(
                        "NEXUS_JOIN_TOKEN set but no peer found in NEXUS_PEERS to join"
                    )

        zone_mgr = ZoneManager(
            node_id=node_id,
            base_path=zones_dir,
            bind_addr=bind_addr,
            advertise_addr=advertise_addr,
        )

        # Parse peer addresses for multi-node Raft groups
        peers_str = os.environ.get("NEXUS_PEERS", "")
        peers = [p.strip() for p in peers_str.split(",") if p.strip()] if peers_str else []

        # Detect joiner vs first-node:
        # Joiner = has all cert files (pre-provisioned by join_cluster above)
        # but no join-token (not the CA holder / first node)
        tls_dir = Path(zones_dir) / "tls"
        is_joiner = (
            (tls_dir / "ca.pem").exists()
            and (tls_dir / "node.pem").exists()
            and (tls_dir / "node-key.pem").exists()
            and not (tls_dir / "join-token").exists()
        )

        if is_joiner:
            zone_mgr.join_zone("root", peers=peers if peers else None)
            logger.info("Joiner node: joined root zone (certs provisioned)")
        else:
            # First node — auto_generate_tls creates CA + certs,
            # ZoneManager starts with mTLS from the beginning.
            zone_mgr.bootstrap(peers=peers if peers else None)

        # Static Day-1 topology from env vars (idempotent)
        zones_str = os.environ.get("NEXUS_FEDERATION_ZONES", "")
        mounts_str = os.environ.get("NEXUS_FEDERATION_MOUNTS", "")
        if zones_str:
            zones = [z.strip() for z in zones_str.split(",") if z.strip()]
            mounts: dict[str, str] = {}
            if mounts_str:
                for pair in mounts_str.split(","):
                    path, zone_id = pair.strip().split("=", 1)
                    mounts[path.strip()] = zone_id.strip()
            zone_mgr.bootstrap_static(zones=zones, peers=peers, mounts=mounts)
        metadata_store = FederatedMetadataProxy.from_zone_manager(zone_mgr)
    except ImportError:
        zone_mgr = None
        # Raft extensions not available — single-node embedded Raft, with fallback
        try:
            from nexus.storage.raft_metadata_store import RaftMetadataStore

            metadata_store = RaftMetadataStore.embedded(metadata_path)
        except (RuntimeError, ImportError):
            from nexus.storage.dict_metastore import DictMetastore

            dict_metastore_path = Path(metadata_path).with_suffix(".json")
            logger.info(
                "Rust metastore not available; using JSON-backed DictMetastore fallback at %s. "
                "Build rust/nexus_raft with maturin develop -m rust/nexus_raft/Cargo.toml "
                "--features python for the durable metastore.",
                dict_metastore_path,
            )
            metadata_store = DictMetastore(dict_metastore_path)
    except RuntimeError as exc:
        if "ZoneManager requires PyO3 build with --features full" not in str(exc):
            raise

        zone_mgr = None
        logger.info(
            "Federation extensions unavailable for local connect(); "
            "falling back to single-node metadata store"
        )
        try:
            from nexus.storage.raft_metadata_store import RaftMetadataStore

            metadata_store = RaftMetadataStore.embedded(metadata_path)
        except (RuntimeError, ImportError):
            from nexus.storage.dict_metastore import DictMetastore

            dict_metastore_path = Path(metadata_path).with_suffix(".json")
            logger.info(
                "Rust metastore not available; using JSON-backed DictMetastore fallback at %s. "
                "Build rust/nexus_raft with maturin develop -m rust/nexus_raft/Cargo.toml "
                "--features python for the durable metastore.",
                dict_metastore_path,
            )
            metadata_store = DictMetastore(dict_metastore_path)

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

    # RecordStore (Four Pillars) — created from NEXUS_RECORD_STORE_PATH or
    # NEXUS_DATABASE_URL.  Passing None gives a bare kernel (storage-only)
    # where all service-layer features (audit log, versioning, ReBAC, Memory
    # API, etc.) are skipped.  The factory handles record_store=None gracefully.
    _database_url = os.environ.get("NEXUS_DATABASE_URL")
    if record_store_path:
        from nexus.storage.record_store import SQLAlchemyRecordStore

        record_store = SQLAlchemyRecordStore(db_path=record_store_path)
    elif _database_url:
        from nexus.storage.record_store import SQLAlchemyRecordStore

        record_store = SQLAlchemyRecordStore(db_url=_database_url)
    else:
        record_store = None

    # Build config objects from NexusConfig fields (Issue #1391)
    from nexus.core.config import (
        CacheConfig,
        DistributedConfig,
        ParseConfig,
        PermissionConfig,
    )

    cache_cfg = CacheConfig(
        path_size=cfg.cache_path_size,
        list_size=cfg.cache_list_size,
        kv_size=cfg.cache_kv_size,
        exists_size=cfg.cache_exists_size,
        ttl_seconds=cfg.cache_ttl_seconds,
    )

    perm_cfg = PermissionConfig(
        enforce=enforce_permissions,
        allow_admin_bypass=cfg.allow_admin_bypass,
        enforce_zone_isolation=enforce_zone_isolation,
        enable_tiger_cache=enable_tiger_cache,
    )

    dist_cfg = DistributedConfig(
        enable_workflows=cfg.enable_workflows,
    )

    parse_cfg = ParseConfig(
        auto_parse=cfg.auto_parse,
        providers=tuple(cfg.parse_providers) if cfg.parse_providers else None,
    )

    # --- Profile resolution (Issue #1708) ---
    from nexus.contracts.deployment_profile import DeploymentProfile, resolve_enabled_bricks

    if cfg.profile == "auto":
        from nexus.lib.device_capabilities import detect_capabilities, suggest_profile

        caps = detect_capabilities()
        resolved_profile = suggest_profile(caps)
        logger.info(
            "Auto-detected profile: %s (RAM=%dMB, GPU=%s, cores=%d)",
            resolved_profile,
            caps.memory_mb,
            caps.has_gpu,
            caps.cpu_cores,
        )
    else:
        resolved_profile = DeploymentProfile(cfg.profile)
        # Warn if explicit profile may exceed device capabilities
        from nexus.lib.device_capabilities import (
            detect_capabilities,
            warn_if_profile_exceeds_device,
        )

        caps = detect_capabilities()
        warn_if_profile_exceeds_device(resolved_profile, caps)

    # Apply FeaturesConfig overrides (Issue #1389 — was unused in connect())
    overrides = cfg.features.to_overrides() if cfg.features else {}
    enabled_bricks = resolve_enabled_bricks(resolved_profile, overrides=overrides)

    # Audit strict mode: env var override (default True for compliance)
    from nexus.contracts.types import AuditConfig

    _audit_strict = os.environ.get("NEXUS_AUDIT_STRICT_MODE", "true").lower() not in (
        "false",
        "0",
        "no",
    )
    audit_cfg = AuditConfig(strict_mode=_audit_strict)

    # Create NexusFS via factory
    from nexus.factory import create_nexus_fs

    nx_fs = await create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        is_admin=cfg.is_admin,
        cache=cache_cfg,
        permissions=perm_cfg,
        distributed=dist_cfg,
        parsing=parse_cfg,
        enabled_bricks=enabled_bricks,
        audit=audit_cfg,
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

    # Store zone manager for federation topology initialization (health check)
    if zone_mgr is not None:
        nx_fs._zone_mgr = zone_mgr

        # Register federation content resolver (PRE-DISPATCH, Issue #163)
        # Registered LAST so Pipe/Memory/VirtualView resolvers get priority.
        await _register_federation_resolver(nx_fs, zone_mgr)

    # Restore saved mounts (application-layer startup I/O)
    await _restore_mounts(nx_fs)

    return nx_fs


async def _register_federation_resolver(nx_fs: "NexusFS", zone_mgr: Any) -> None:
    """Register federation resolvers via coordinator.enlist() (#163, #1625, #1710).

    Registration order matters — IPC resolver is registered FIRST so remote
    DT_PIPE/DT_STREAM are intercepted before the content resolver.  Content
    resolver is registered LAST as a generic fallback for CAS-backed content.

    Both resolvers implement HotSwappable and are enlisted via the unified
    coordinator.enlist() entry point (#1710).
    """
    from nexus.raft.federation_content_resolver import FederationContentResolver
    from nexus.raft.federation_ipc_resolver import FederationIPCResolver

    _coordinator = nx_fs._service_coordinator

    # IPC resolver — remote DT_PIPE/DT_STREAM (#1625)
    ipc_resolver = FederationIPCResolver(
        metastore=nx_fs.metadata,
        self_address=zone_mgr.advertise_addr,
        tls_config=zone_mgr.tls_config,
    )
    await _coordinator.enlist("federation_ipc", ipc_resolver)

    # Content resolver — remote CAS content (#163)
    content_resolver = FederationContentResolver(
        metastore=nx_fs.metadata,
        self_address=zone_mgr.advertise_addr,
        tls_config=zone_mgr.tls_config,
    )
    await _coordinator.enlist("federation_content", content_resolver)

    logger.info("Federation resolvers registered: IPC + Content (self=%s)", zone_mgr.advertise_addr)


async def _restore_mounts(nx_fs: "NexusFS") -> None:
    """Restore saved mounts from database at application startup.

    This is application-layer I/O that runs after NexusFS construction.
    The factory itself never performs I/O — callers decide when to
    restore mounts.
    """
    import os

    try:
        auto_sync = os.getenv("NEXUS_AUTO_SYNC_MOUNTS", "false").lower() in (
            "true",
            "1",
            "yes",
        )
        mount_result = await nx_fs.service("mount_persist").load_all_mounts(auto_sync=auto_sync)
        if mount_result["loaded"] > 0 or mount_result["failed"] > 0:
            sync_msg = f", {mount_result['synced']} synced" if mount_result["synced"] > 0 else ""
            logger.info(
                "Mount restoration: %d loaded%s, %d failed",
                mount_result["loaded"],
                sync_msg,
                mount_result["failed"],
            )
            if not auto_sync and mount_result["loaded"] > 0:
                logger.info(
                    "Auto-sync disabled for fast startup. "
                    "Use sync_mount() or set NEXUS_AUTO_SYNC_MOUNTS=true"
                )
            for error in mount_result.get("errors", []):
                logger.error("  Mount error: %s", error)
    except Exception as e:
        logger.warning("Failed to load saved mounts during initialization: %s", e)


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
    # Backends
    "CASLocalBackend",
    "GCSBackend",
    # Exceptions (always loaded - lightweight)
    "NexusError",
    "NexusFileNotFoundError",
    "NexusPermissionError",
    "BackendError",
    "InvalidPathError",
    "MetadataError",
]
