"""
Nexus — filesystem/context plane (Rust kernel).

Nexus combines a VFS-style filesystem interface with deployment-aware context,
storage, and service composition for agent systems.

Deployment profiles control which bricks are enabled:
- slim: Bare VFS, kernel only
- embedded: Eventlog only
- lite: Core services
- full: All bricks (default)
- cloud: All bricks + federation
- remote: Thin gRPC client (RemoteBackend + RemoteServiceProxy)

SDK vs CLI:
-----------
For programmatic access (building tools, libraries, integrations), use the SDK:

    from nexus.sdk import connect

    nx = connect(config={"profile": "embedded", "data_dir": "./nexus-data"})
    nx.sys_write("/workspace/data.txt", b"Hello World")
    content = nx.sys_read("/workspace/data.txt")

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

__version__ = "0.10.0"  # release version
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
# Resolved lazily so HOME changes after import (e.g., per-agent isolation) take effect.
# Override via NEXUS_STATE_DIR env var. Access via ``nexus.NEXUS_STATE_DIR`` (routed
# through ``__getattr__``) or call ``_resolve_state_dir()`` directly inside this module.
def _resolve_state_dir() -> str:
    override = _os.environ.get("NEXUS_STATE_DIR")
    if override:
        return override
    return _os.path.expanduser("~/.nexus")


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
    "NexusFS": ("nexus.core.nexus_fs", "NexusFS"),
    # Slim package top-level API (nexus.mount / nexus.mount_sync)
    "mount": ("nexus.fs", "mount"),
    "mount_sync": ("nexus.fs", "mount_sync"),
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

    # Dynamic state dir — resolved on every access so HOME / NEXUS_STATE_DIR
    # changes after import are honored (per-agent HOME isolation, Koi hosts).
    if name == "NEXUS_STATE_DIR":
        return _resolve_state_dir()

    raise AttributeError(f"module 'nexus' has no attribute {name!r}")


def _open_local_metastore(metadata_path: str, kernel: object = None) -> "MetastoreABC":
    """Open a local metadata store.

    F3 C4: the Rust kernel is the single source of truth for metastore
    state — this helper always returns a ``RustMetastoreProxy``. When
    no ``kernel`` is supplied, a bare one is constructed on the fly
    (``Kernel::new()`` installs a ``MemoryMetastore`` by default, so
    quickstarts / slim SDK boots without a redb file still get a
    working metastore for the session). Passing a ``metadata_path``
    that already exists as a ``.redb`` file wires it through
    ``kernel.set_metastore_path``.
    """
    from pathlib import Path

    _redb_path = Path(metadata_path).with_suffix(".redb")

    if kernel is None:
        from nexus_kernel import Kernel as _Kernel

        kernel = _Kernel()

    from nexus.core.metastore import RustMetastoreProxy

    try:
        return RustMetastoreProxy(
            kernel, str(_redb_path) if _redb_path.exists() or _redb_path.parent.exists() else None
        )
    except Exception as e:
        # An existing on-disk store that we can't open is a hard error:
        # silently falling back would hide previously written data.
        if _redb_path.exists():
            raise RuntimeError(
                f"RustMetastoreProxy failed for existing {_redb_path}: {e}. "
                "Refusing to fall back to a different metadata format. "
                "Rebuild: cd rust/kernel && maturin develop --release"
            ) from e
        raise


def connect(
    config: "str | Path | dict | NexusConfig | None" = None,
) -> "NexusFS":
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
        NexusFS instance. All profiles implement the NexusFS
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
            >>> nx.sys_write("/workspace/file.txt", b"Hello World")

        Federation (auto-detected when Rust extensions available):
            >>> # Requires NEXUS_PEERS, NEXUS_BIND_ADDR env vars
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

        # TLS: NEXUS_GRPC_TLS env var overrides all other TLS signals.
        #   true  → force TLS
        #   false → force insecure
        #   unset → fall back to nexus.yaml tls / NEXUS_DATA_DIR auto-detect
        _tls_config = None
        _grpc_tls_env = os.getenv("NEXUS_GRPC_TLS", "").lower()
        _tls_enabled = _grpc_tls_env in ("true", "1", "yes")
        _tls_disabled = _grpc_tls_env in ("false", "0", "no")
        _tls_from_config = False  # set when nexus.yaml explicitly enables TLS
        _data_dir = os.getenv("NEXUS_DATA_DIR")
        if _data_dir and not _tls_disabled:
            _tls_enabled = True  # Auto-detect from NEXUS_DATA_DIR (backward compat)
        if not _data_dir:
            _project_yaml = Path("nexus.yaml")
            if _project_yaml.exists():
                try:
                    import yaml as _yaml

                    with open(_project_yaml) as _f:
                        _project_cfg = _yaml.safe_load(_f) or {}
                    _data_dir = _project_cfg.get("data_dir")
                    # nexus.yaml tls: only used when env var is unset
                    if not _grpc_tls_env:
                        _tls_from_config = bool(_project_cfg.get("tls"))
                        _tls_enabled = _tls_from_config
                except Exception:
                    pass
        if not _data_dir:
            _data_dir = getattr(cfg, "data_dir", None)

        if _data_dir and _tls_enabled:
            from nexus.security.tls.config import ZoneTlsConfig

            # TLS explicitly requested (env var or config) → check both layouts
            # NEXUS_DATA_DIR auto-detect only → Raft-only (backward compat)
            _tls_intentional = _grpc_tls_env in ("true", "1", "yes") or _tls_from_config
            _tls_config = (
                ZoneTlsConfig.from_data_dir_any(_data_dir)
                if _tls_intentional
                else ZoneTlsConfig.from_data_dir(_data_dir)
            )

        # Fail closed: NEXUS_GRPC_TLS=true but no certs resolved.
        # As a last resort, check NEXUS_TLS_* env vars — but only when
        # TLS was explicitly requested, to avoid stale env vars from a
        # previous session flipping a plaintext stack onto mTLS.
        _tls_explicit = _grpc_tls_env in ("true", "1", "yes")
        if _tls_explicit and _tls_config is None and os.getenv("NEXUS_TLS_CERT"):
            import contextlib

            from nexus.security.tls.config import ZoneTlsConfig

            with contextlib.suppress(Exception):
                _tls_config = ZoneTlsConfig.from_env()
        if _tls_explicit and _tls_config is None:
            raise RuntimeError(
                "NEXUS_GRPC_TLS=true but no TLS certificates found. "
                "Provide certs via NEXUS_TLS_CERT/KEY/CA, "
                "in {data_dir}/tls/, or set data_dir in nexus.yaml."
            )

        transport = RPCTransport(
            server_address=grpc_address,
            auth_token=api_key,
            timeout=float(timeout),
            connect_timeout=float(connect_timeout),
            tls_config=_tls_config,
        )

        # Rust-native remote wiring (Issue #1134 Phase 4, a803a9d63):
        # the root mount carries backend_type="remote" + connection params,
        # and PyKernel::sys_setattr constructs both the Rust RemoteBackend
        # and the Rust RemoteMetastore from those params — no Python shim.
        # Metastore is a stock RustMetastoreProxy: the single Rust kernel
        # routes per-mount reads/writes to the remote backend it built.
        from nexus.contracts.metadata import DT_MOUNT
        from nexus.contracts.types import OperationContext as _RemoteOC
        from nexus.core.config import PermissionConfig as _PermissionConfig
        from nexus.core.nexus_fs import NexusFS as _RemoteNexusFS

        remote_metastore = _open_local_metastore(":memory:")
        nfs = _RemoteNexusFS(
            metadata_store=remote_metastore,
            permissions=_PermissionConfig(enforce=False),
            init_cred=_RemoteOC(user_id="remote", groups=[], is_admin=False),
        )

        nfs.sys_setattr(
            "/",
            entry_type=DT_MOUNT,
            backend_type="remote",
            backend_name="remote",
            server_address=grpc_address,
            remote_auth_token=api_key,
            remote_ca_pem=(_tls_config.ca_pem.decode() if _tls_config else None),
            remote_cert_pem=(_tls_config.node_cert_pem.decode() if _tls_config else None),
            remote_key_pem=(_tls_config.node_key_pem.decode() if _tls_config else None),
            remote_timeout=float(timeout),
        )

        # Wire service proxies for REMOTE profile (Issue #1171).
        # Fills all 25+ service slots with RemoteServiceProxy — forwards
        # method calls to the server via gRPC.
        from nexus.factory._remote import (
            _boot_remote_services,
            install_remote_kernel_rpc_overrides,
        )

        _boot_remote_services(nfs, call_rpc=transport.call_rpc)
        install_remote_kernel_rpc_overrides(nfs, transport)
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
        nexus_root = _resolve_state_dir()
        data_dir = str(Path(nexus_root) / "data")
    else:
        data_dir = (
            cfg.data_dir if cfg.data_dir is not None else str(Path(_resolve_state_dir()) / "data")
        )
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
            # Parse tiering config from YAML if present (Issue #3406)
            tiering_cfg = None
            if cfg.tiering and cfg.tiering.get("enabled"):
                from nexus.core.config import TieringConfig

                t = cfg.tiering
                tiering_cfg = TieringConfig(
                    enabled=True,
                    quiet_period_seconds=float(t.get("quiet_period", 3600)),
                    min_volume_size_bytes=int(t.get("min_volume_size", 100 * 1024 * 1024)),
                    cloud_backend=str(t.get("cloud_backend", "gcs")),
                    cloud_bucket=str(t.get("cloud_bucket", "")),
                    upload_rate_limit_bytes=int(t.get("upload_rate_limit", 25 * 1024 * 1024)),
                    sweep_interval_seconds=float(t.get("sweep_interval", 60)),
                    local_cache_size_bytes=int(t.get("local_cache_size", 10 * 1024 * 1024 * 1024)),
                    burst_read_threshold=int(t.get("burst_read_threshold", 5)),
                    burst_read_window_seconds=float(t.get("burst_read_window", 60)),
                )
            backend = CASLocalBackend(
                root_path=Path(data_dir).resolve(),
                tiering_config=tiering_cfg,
            )

    # Resolve paths — new fields take precedence, db_path is legacy fallback
    metadata_path = cfg.metastore_path or cfg.db_path or str(Path(nexus_root) / "metastore")
    record_store_path = cfg.record_store_path or None

    # --- Profile resolution (Issue #1708, moved before metadata store for federation gating) ---
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

    # Apply FeaturesConfig overrides (Issue #1389)
    overrides = cfg.features.to_overrides() if cfg.features else {}
    enabled_bricks = resolve_enabled_bricks(resolved_profile, overrides=overrides)

    # Create Rust kernel early so RustMetastoreProxy can use it.
    # Route through _rust_compat so stale binaries (missing Kernel methods)
    # are caught here and never passed to RustMetastoreProxy (Issue #3712).
    _early_kernel = None
    try:
        from nexus._rust_compat import RUST_AVAILABLE as _RUST_AVAILABLE
        from nexus._rust_compat import Kernel as _Kernel

        if _RUST_AVAILABLE and _Kernel is not None:
            _early_kernel = _Kernel()
    except Exception:
        pass

    # Create metadata store — kernel owns federation bootstrap since
    # R20.18.5. When federation env vars are set (NEXUS_HOSTNAME /
    # NEXUS_PEERS / NEXUS_FEDERATION_ZONES), Kernel::new reads them
    # in Rust and stands up the raft::ZoneManager internally; the
    # metadata store wrapper below uses the same kernel so writes
    # route through MountTable to the per-zone ZoneMetastore. When
    # those env vars are unset, Kernel::new is a no-op and the
    # store is backed by LocalMetastore/MemoryMetastore as before.
    metadata_store: MetastoreABC = _open_local_metastore(metadata_path, kernel=_early_kernel)
    # Python no longer owns a FederationService object. `federation=None`
    # below just drops a dead kwarg into the orchestrator; _lifecycle.py
    # handles the None path.
    federation = None

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
    # NEXUS_DATABASE_URL (both flow into ``cfg`` via env overrides, or via
    # explicit config keys from callers like nexusd --database-url).
    # Passing None gives a bare kernel (storage-only) where all service-layer
    # features (audit log, versioning, ReBAC, Memory API, etc.) are skipped.
    # The factory handles record_store=None gracefully.
    _database_url = cfg.database_url
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

    nx_fs = create_nexus_fs(
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
        federation=federation,
        security=getattr(cfg, "security", None),
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

    # Register federation content resolver (PRE-DISPATCH, Issue #163)
    # Registered LAST so Pipe/Memory/VirtualView resolvers get priority.
    # Federation is already enlisted in ServiceRegistry by _wire_services().
    if federation is not None:
        _register_federation_resolver(nx_fs, federation, backend)

    # Restore saved mounts (application-layer startup I/O)
    _restore_mounts(nx_fs)

    # Start audit hook if federation is active (requires a loaded Raft zone).
    _init_audit_hook(nx_fs)

    return nx_fs


def _register_federation_resolver(nx_fs: "NexusFS", federation: Any, backend: Any) -> None:
    """Wire federation transport — set TLS config on RPCTransportPool.

    Content locality (remote reads) and IPC (remote pipe/stream) are now
    handled transparently by the Rust kernel routing and RPCTransportPool.
    No PRE-DISPATCH resolvers needed.

    The old FederationContentResolver, FederationIPCResolver, and
    FederatedMetadataProxy have been deleted — their functionality is
    subsumed by PathRouter per-mount metastore (#3580) and backend_key()
    write enrichment.
    """
    _ = backend  # unused after resolver deletion
    _zone_mgr = federation.zone_manager

    # Set TLS config on transport pool now that federation is initialized
    if (
        hasattr(nx_fs, "_transport_pool")
        and nx_fs._transport_pool is not None
        and _zone_mgr.tls_config
    ):
        nx_fs._transport_pool.set_tls_config(_zone_mgr.tls_config)

    logger.info("Federation transport configured (TLS=%s)", _zone_mgr.tls_config is not None)


def _init_audit_hook(nx_fs: "NexusFS") -> None:
    """Start the AuditHook if federation is active.

    Wires a Rust AuditHook to a WAL-replicated DT_STREAM so every VFS
    operation is durably recorded. Requires a loaded Raft zone — silently
    skips if federation is not yet active (standalone / dev-mode deployments).

    The audit stream is readable at ``/audit/traces/`` via sys_read.
    """
    kernel = getattr(nx_fs, "_kernel", None)
    if kernel is None:
        return

    audit_zone = "root"
    audit_stream_path = "/audit/traces/"

    try:
        kernel.start_audit_hook(audit_zone, audit_stream_path)
        logger.info("Audit hook started: zone=%s stream=%s", audit_zone, audit_stream_path)
    except RuntimeError as e:
        # Federation not active or zone not loaded — expected in dev mode.
        logger.debug("Audit hook not started (federation inactive): %s", e)
    except Exception as e:
        logger.warning("Failed to start audit hook: %s", e)


def _restore_mounts(nx_fs: "NexusFS") -> None:
    """Restore saved mounts from database at application startup.

    This is application-layer I/O that runs after NexusFS construction.
    The factory itself never performs I/O — callers decide when to
    restore mounts.
    """
    try:
        mount_result = nx_fs.service("mount_persist").load_all_mounts()
        if mount_result["loaded"] > 0 or mount_result["failed"] > 0:
            logger.info(
                "Mount restoration: %d loaded, %d failed",
                mount_result["loaded"],
                mount_result["failed"],
            )
            for error in mount_result.get("errors", []):
                logger.error("  Mount error: %s", error)
    except Exception as e:
        logger.warning("Failed to load saved mounts during initialization: %s", e)

    # Start replication scanners for mounts that have a replication policy.
    # Runs after mount activation so the kernel router is ready.
    _start_replication_scanners(nx_fs)


def _start_replication_scanners(nx_fs: "NexusFS") -> None:
    """Start background replication scanners for mounts with replication policies.

    Reads the replication field from each saved mount config and calls
    `kernel.start_replication_scanner()` for zone/mount combos that opt in.
    """
    kernel = getattr(nx_fs, "_kernel", None)
    if kernel is None:
        return

    mount_persist = nx_fs.service("mount_persist")
    if mount_persist is None:
        return

    try:
        manager = getattr(mount_persist, "_manager", None)
        if manager is None:
            return
        mounts = manager.list_mounts()
    except Exception as e:
        logger.warning("_start_replication_scanners: could not list mounts: %s", e)
        return

    import json as _json

    for mount in mounts:
        replication = mount.get("replication")
        if not replication:
            continue
        zone_id = mount.get("zone_id") or "root"
        mount_point = mount.get("mount_point", "")
        # Build a minimal single-policy JSON for this mount.
        # Only "all-voters" is supported today; extend here when more targets land.
        target: dict = {"type": "all_voters"}
        policies_json = _json.dumps([{"path_prefix": mount_point, "target": target}])
        try:
            kernel.start_replication_scanner(zone_id, policies_json, 2000)
            logger.info(
                "Started replication scanner: zone=%s mount=%s policy=%s",
                zone_id,
                mount_point,
                replication,
            )
        except Exception as e:
            logger.warning("Failed to start replication scanner for %s: %s", mount_point, e)


__all__ = [
    # Version
    "__version__",
    # Main entry points
    "connect",
    "mount",
    "mount_sync",
    # Configuration
    "NexusConfig",
    "load_config",
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
