"""Nexus Service Factory — userspace init system for NexusFS.

.. important:: ARCHITECTURAL DECISION (Task #23)

    This module is **NOT kernel code**. It lives at ``nexus/factory.py``
    (top-level, alongside ``server/``, ``cli/``, ``services/``) by design.

    **Linux analogy**: NexusFS kernel = ``/kernel/``. This factory = ``systemd``
    (``/usr/lib/systemd/``). Systemd knows which services to start and how to
    wire them together, but it is not part of the kernel.

    **Why it exists**: The NexusFS kernel (``nexus.core.nexus_fs.NexusFS``)
    accepts pre-built services via dependency injection and never auto-creates
    them. This factory provides the default wiring so that callers don't have
    to manually construct 10 services every time.

    **Future**: This module can be extracted into a separate package
    (e.g. ``nexus-bootstrap``) when the repo is split. The kernel
    (``nexus-core``) would have zero dependency on it. All coupling flows
    one way: factory → kernel, never kernel → factory.

    **Decision rationale**:
    - ``nexus/core/`` was rejected because that's the kernel tree.
    - ``nexus/services/`` was rejected because those are service facades
      (Phase 2 extractions), not the bootstrap orchestrator.
    - Top-level ``nexus/factory.py`` mirrors systemd's position as an
      independent top-level system component.

Usage::

    # Quick: single call creates kernel + services
    from nexus.factory import create_nexus_fs

    nx = create_nexus_fs(
        backend=LocalBackend(root_path="./data"),
        metadata_store=RaftMetadataStore.embedded("./raft"),
        record_store=SQLAlchemyRecordStore(db_path="./db.sqlite"),
        enforce_permissions=False,
    )

    # Advanced: create services separately, inject into kernel
    from nexus.factory import create_nexus_services

    services = create_nexus_services(
        record_store=record_store,
        metadata_store=metadata_store,
        backend=backend,
        router=my_router,
    )
    nx = NexusFS(backend=backend, metadata_store=metadata_store, **services)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.core._metadata_generated import FileMetadataProtocol
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.router import PathRouter
    from nexus.storage.record_store import RecordStoreABC


def _create_wallet_provisioner() -> Any:
    """Create a sync wallet provisioner for NexusFS agent registration.

    Returns a callable ``(agent_id: str, zone_id: str) -> None`` that creates
    a TigerBeetle wallet account. Returns None if tigerbeetle is not installed.

    Uses the sync TigerBeetle client (``tb.Client``) since NexusFS methods are
    synchronous. The client is lazily created on first call and reused.
    Account creation is idempotent (safe to call multiple times).
    """
    import logging
    import os

    logger = logging.getLogger(__name__)

    tb_address = os.environ.get("TIGERBEETLE_ADDRESS", "127.0.0.1:3000")
    tb_cluster = int(os.environ.get("TIGERBEETLE_CLUSTER_ID", "0"))
    pay_enabled = os.environ.get("NEXUS_PAY_ENABLED", "").lower() in ("true", "1", "yes")

    if not pay_enabled:
        logger.debug("[WALLET] NEXUS_PAY_ENABLED not set, wallet provisioner disabled")
        return None

    try:
        import tigerbeetle as _tb  # noqa: F401 — verify availability
    except ImportError:
        logger.debug("[WALLET] tigerbeetle package not installed, wallet provisioner disabled")
        return None

    # Shared state for the closure (lazy client)
    _state: dict[str, Any] = {"client": None}

    def _provision_wallet(agent_id: str, zone_id: str = "default") -> None:
        """Create TigerBeetle account for agent. Idempotent."""
        import tigerbeetle as tb

        from nexus.pay.constants import (
            ACCOUNT_CODE_WALLET,
            LEDGER_CREDITS,
            make_tb_account_id,
        )

        if _state["client"] is None:
            _state["client"] = tb.ClientSync(
                cluster_id=tb_cluster,
                replica_addresses=tb_address,
            )

        tb_id = make_tb_account_id(zone_id, agent_id)
        account = tb.Account(
            id=tb_id,
            ledger=LEDGER_CREDITS,
            code=ACCOUNT_CODE_WALLET,
            flags=tb.AccountFlags.DEBITS_MUST_NOT_EXCEED_CREDITS,
        )

        client = _state["client"]
        assert client is not None
        errors = client.create_accounts([account])
        # Ignore EXISTS (21) — idempotent operation
        if errors and errors[0].result not in (0, 21):
            raise RuntimeError(f"TigerBeetle account creation failed: {errors[0].result}")

    logger.info("[WALLET] Wallet provisioner enabled (TigerBeetle @ %s)", tb_address)
    return _provision_wallet


def create_nexus_services(
    record_store: RecordStoreABC,
    metadata_store: FileMetadataProtocol,
    backend: Backend,
    router: PathRouter,
    *,
    cache_ttl_seconds: int | None = 300,
    enforce_zone_isolation: bool = True,
    enable_tiger_cache: bool = True,
    allow_admin_bypass: bool = False,
    inherit_permissions: bool = True,
    enable_deferred_permissions: bool = True,
    deferred_flush_interval: float = 0.05,
    zone_id: str | None = None,
    agent_id: str | None = None,
    enable_write_buffer: bool | None = None,
) -> dict[str, Any]:
    """Create default services for NexusFS dependency injection.

    Builds all service instances that NexusFS accepts via constructor params.
    Services are wired together internally (e.g. PermissionEnforcer receives
    the rebac_manager created here).

    Args:
        record_store: RecordStoreABC instance (provides engine + session_factory).
        metadata_store: FileMetadataProtocol instance (for PermissionEnforcer).
        backend: Backend instance (for WorkspaceManager).
        router: PathRouter instance (for PermissionEnforcer object type resolution).
        cache_ttl_seconds: Cache TTL for ReBAC manager (default: 300).
        enforce_zone_isolation: Enable zone isolation in ReBAC (default: True).
        enable_tiger_cache: Enable Tiger Cache for materialized permissions (default: True).
        allow_admin_bypass: Allow admin users to bypass permission checks (default: False).
        inherit_permissions: Enable automatic parent tuple creation (default: True).
        enable_deferred_permissions: Enable async permission write batching (default: True).
        deferred_flush_interval: Flush interval in seconds (default: 0.05).
        zone_id: Default zone ID (for WorkspaceManager, embedded mode only).
        agent_id: Default agent ID (for WorkspaceManager, embedded mode only).
        enable_write_buffer: Use async WriteBuffer for PG sync (Issue #1246).

    Returns:
        Dict of keyword arguments ready to spread into ``NexusFS()``::

            services = create_nexus_services(record_store, metadata_store, backend, router)
            nx = NexusFS(backend=backend, metadata_store=metadata_store, **services)
    """
    engine = record_store.engine
    session_factory = record_store.session_factory

    # --- ReBAC Manager ---
    from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager

    rebac_manager = EnhancedReBACManager(
        engine=engine,
        cache_ttl_seconds=cache_ttl_seconds or 300,
        max_depth=10,
        enforce_zone_isolation=enforce_zone_isolation,
        enable_graph_limits=True,
        enable_tiger_cache=enable_tiger_cache,
    )

    # --- Circuit Breaker for ReBAC DB Resilience (Issue #726) ---
    from nexus.services.permissions.circuit_breaker import AsyncCircuitBreaker, CircuitBreakerConfig

    rebac_circuit_breaker = AsyncCircuitBreaker(
        name="rebac_db",
        config=CircuitBreakerConfig(
            failure_threshold=5,
            success_threshold=3,
            reset_timeout=30.0,
            failure_window=60.0,
        ),
    )

    # --- Directory Visibility Cache ---
    from nexus.services.permissions.dir_visibility_cache import DirectoryVisibilityCache

    dir_visibility_cache = DirectoryVisibilityCache(
        tiger_cache=getattr(rebac_manager, "_tiger_cache", None),
        ttl=cache_ttl_seconds or 300,
        max_entries=10000,
    )

    # Wire: rebac invalidation -> dir visibility cache
    rebac_manager.register_dir_visibility_invalidator(
        "nexusfs",
        lambda zone_id, path: dir_visibility_cache.invalidate_for_resource(path, zone_id),
    )

    # --- Audit Store ---
    from nexus.services.permissions.permissions_enhanced import AuditStore

    audit_store = AuditStore(engine=engine)

    # --- Entity Registry ---
    from nexus.services.permissions.entity_registry import EntityRegistry

    entity_registry = EntityRegistry(session_factory)

    # --- Permission Enforcer ---
    from nexus.core.permissions import PermissionEnforcer

    permission_enforcer = PermissionEnforcer(
        metadata_store=metadata_store,
        rebac_manager=rebac_manager,
        allow_admin_bypass=allow_admin_bypass,
        allow_system_bypass=True,
        audit_store=audit_store,
        admin_bypass_paths=[],
        router=router,
        entity_registry=entity_registry,
    )

    # --- Hierarchy Manager ---
    from nexus.services.permissions.hierarchy_manager import HierarchyManager

    hierarchy_manager = HierarchyManager(
        rebac_manager=rebac_manager,
        enable_inheritance=inherit_permissions,
    )

    # --- Deferred Permission Buffer ---
    from nexus.services.permissions.deferred_permission_buffer import DeferredPermissionBuffer

    deferred_permission_buffer = None
    if enable_deferred_permissions:
        deferred_permission_buffer = DeferredPermissionBuffer(
            rebac_manager=rebac_manager,
            hierarchy_manager=hierarchy_manager,
            flush_interval_sec=deferred_flush_interval,
        )
        deferred_permission_buffer.start()

    # --- Workspace Registry ---
    from nexus.core.workspace_registry import WorkspaceRegistry

    workspace_registry = WorkspaceRegistry(
        metadata=metadata_store,
        rebac_manager=rebac_manager,
        session_factory=session_factory,
    )

    # --- Mount Manager ---
    from nexus.core.mount_manager import MountManager

    mount_manager = MountManager(session_factory)

    # --- Workspace Manager ---
    from nexus.core.workspace_manager import WorkspaceManager

    workspace_manager = WorkspaceManager(
        metadata=metadata_store,
        backend=backend,
        rebac_manager=rebac_manager,
        zone_id=zone_id,
        agent_id=agent_id,
        session_factory=session_factory,
    )

    # --- RecordStore Syncer (Issue #1246) ---
    # Decision 13A: WriteBuffer auto-enabled for PostgreSQL.
    import os

    write_observer: Any = None
    db_url = getattr(record_store, "database_url", "")
    use_buffer = enable_write_buffer
    if use_buffer is None:
        env_val = os.environ.get("NEXUS_ENABLE_WRITE_BUFFER", "").lower()
        if env_val in ("true", "1", "yes"):
            use_buffer = True
        elif env_val in ("false", "0", "no"):
            use_buffer = False
        else:
            use_buffer = db_url.startswith(("postgres", "postgresql"))

    if use_buffer:
        from nexus.storage.record_store_syncer import BufferedRecordStoreSyncer

        write_observer = BufferedRecordStoreSyncer(session_factory)
        write_observer.start()
    else:
        from nexus.storage.record_store_syncer import RecordStoreSyncer

        write_observer = RecordStoreSyncer(session_factory)

    # --- VersionService (Task #45) ---
    # Version history queries go through RecordStore (VersionHistoryModel),
    # not through Metastore (sled doesn't track version history).
    from nexus.services.version_service import VersionService

    version_service = VersionService(
        metadata_store=metadata_store,
        cas_store=backend,
        router=router,
        enforce_permissions=False,  # Permission checks done at NexusFS level
        session_factory=session_factory,
    )

    # --- Observability Subsystem (Issue #1301) ---
    from nexus.core.config import ObservabilityConfig
    from nexus.services.subsystems.observability_subsystem import ObservabilitySubsystem

    observability_config = ObservabilityConfig()
    observability_subsystem = ObservabilitySubsystem(
        config=observability_config,
        engines=[engine],
    )

    # --- Chunked Upload Service (Issue #788) ---
    # Build upload config from environment variables (match NexusConfig field names)
    import os as _os

    from nexus.services.chunked_upload_service import ChunkedUploadConfig, ChunkedUploadService

    _upload_config_kwargs: dict[str, Any] = {}
    _upload_env_mapping = {
        "NEXUS_UPLOAD_MIN_CHUNK_SIZE": "min_chunk_size",
        "NEXUS_UPLOAD_MAX_CHUNK_SIZE": "max_chunk_size",
        "NEXUS_UPLOAD_DEFAULT_CHUNK_SIZE": "default_chunk_size",
        "NEXUS_UPLOAD_MAX_CONCURRENT": "max_concurrent_uploads",
        "NEXUS_UPLOAD_SESSION_TTL_HOURS": "session_ttl_hours",
        "NEXUS_UPLOAD_CLEANUP_INTERVAL": "cleanup_interval_seconds",
        "NEXUS_UPLOAD_MAX_SIZE": "max_upload_size",
    }
    for _env_var, _config_key in _upload_env_mapping.items():
        _val = _os.getenv(_env_var)
        if _val is not None:
            _upload_config_kwargs[_config_key] = int(_val)

    chunked_upload_service = ChunkedUploadService(
        session_factory=session_factory,
        backend=backend,
        metadata_store=metadata_store,
        config=ChunkedUploadConfig(**_upload_config_kwargs),
    )

    # --- Wallet Provisioner (Issue #1210) ---
    # Creates TigerBeetle wallet accounts on agent registration.
    # Uses sync TigerBeetle client since NexusFS methods are sync.
    # Gracefully no-ops if tigerbeetle package is not installed.
    wallet_provisioner = _create_wallet_provisioner()

    # --- Manifest Resolver (Issue #1427, #1428) ---
    # Only create FileGlobExecutor when backend has local storage.
    # For non-local backends (GCS/S3), file_glob sources are skipped.
    import logging as _logging

    _factory_logger = _logging.getLogger(__name__)
    manifest_resolver: Any = None
    manifest_metrics: Any = None

    try:
        from nexus.services.context_manifest import ManifestResolver
        from nexus.services.context_manifest.executors.file_glob import FileGlobExecutor
        from nexus.services.context_manifest.metrics import (
            ManifestMetricsConfig,
            ManifestMetricsObserver,
        )

        executors: dict[str, Any] = {}
        root_path = getattr(backend, "root_path", None)
        if root_path is not None:
            from pathlib import Path

            try:
                executors["file_glob"] = FileGlobExecutor(workspace_root=Path(root_path))
            except TypeError:
                _factory_logger.debug("Cannot create FileGlobExecutor: root_path=%r", root_path)

        # WorkspaceSnapshotExecutor (Issue #1428)
        try:
            from nexus.services.context_manifest.executors.snapshot_lookup_db import (
                CASManifestReader,
                DatabaseSnapshotLookup,
            )
            from nexus.services.context_manifest.executors.workspace_snapshot import (
                WorkspaceSnapshotExecutor,
            )

            snapshot_lookup = DatabaseSnapshotLookup(session_factory=session_factory)
            cas_reader = CASManifestReader(backend=backend)
            executors["workspace_snapshot"] = WorkspaceSnapshotExecutor(
                snapshot_lookup=snapshot_lookup,
                manifest_reader=cas_reader,
            )
        except ImportError as _snap_e:
            _factory_logger.debug("WorkspaceSnapshotExecutor unavailable: %s", _snap_e)

        # MemoryQueryExecutor (Issue #1428) — availability check only.
        # Memory instance is created per-agent at request time, so the executor
        # is wired in the resolve endpoint, not here.
        import importlib.util

        if importlib.util.find_spec("nexus.services.context_manifest.executors.memory_query"):
            _factory_logger.debug("MemoryQueryExecutor available for per-agent wiring")
        else:
            _factory_logger.debug("MemoryQueryExecutor module not found")

        # Metrics observer (Issue #1428)
        manifest_metrics = ManifestMetricsObserver(ManifestMetricsConfig())

        manifest_resolver = ManifestResolver(
            executors=executors,
            max_resolve_seconds=5.0,
            metrics_observer=manifest_metrics,
        )
        _factory_logger.debug(
            "[FACTORY] ManifestResolver created with %d executors", len(executors)
        )
    except ImportError as _e:
        _factory_logger.warning("Failed to create ManifestResolver: %s", _e)

    result = {
        "rebac_manager": rebac_manager,
        "dir_visibility_cache": dir_visibility_cache,
        "audit_store": audit_store,
        "entity_registry": entity_registry,
        "permission_enforcer": permission_enforcer,
        "hierarchy_manager": hierarchy_manager,
        "deferred_permission_buffer": deferred_permission_buffer,
        "workspace_registry": workspace_registry,
        "mount_manager": mount_manager,
        "workspace_manager": workspace_manager,
        "write_observer": write_observer,
        "version_service": version_service,
        "observability_subsystem": observability_subsystem,
        "wallet_provisioner": wallet_provisioner,
        "chunked_upload_service": chunked_upload_service,
        "manifest_resolver": manifest_resolver,
        "manifest_metrics": manifest_metrics,
        "rebac_circuit_breaker": rebac_circuit_breaker,
    }

    return result


def create_nexus_fs(
    backend: Backend,
    metadata_store: FileMetadataProtocol,
    record_store: RecordStoreABC | None = None,
    *,
    # Kernel config
    cache_store: Any = None,
    is_admin: bool = False,
    zone_id: str | None = None,
    agent_id: str | None = None,
    custom_namespaces: list[Any] | None = None,
    enable_metadata_cache: bool = True,
    cache_path_size: int = 512,
    cache_list_size: int = 1024,
    cache_kv_size: int = 256,
    cache_exists_size: int = 1024,
    cache_ttl_seconds: int | None = 300,
    enable_content_cache: bool = True,
    content_cache_size_mb: int = 256,
    auto_parse: bool = True,
    custom_parsers: list[dict[str, Any]] | None = None,
    parse_providers: list[dict[str, Any]] | None = None,
    enforce_permissions: bool = True,
    allow_admin_bypass: bool = False,
    enforce_zone_isolation: bool = True,
    audit_strict_mode: bool = True,
    enable_workflows: bool = True,
    workflow_engine: Any = None,
    coordination_url: str | None = None,
    enable_distributed_events: bool = True,
    enable_distributed_locks: bool = True,
    # Service config (only used when record_store is provided)
    inherit_permissions: bool = True,
    enable_tiger_cache: bool = True,
    enable_deferred_permissions: bool = True,
    deferred_flush_interval: float = 0.05,
    enable_write_buffer: bool | None = None,
) -> NexusFS:
    """Create NexusFS with default services — the recommended entry point.

    Handles router creation, service wiring, and kernel initialization in one
    call. Equivalent to what ``nexus.connect()`` does for embedded mode.

    For pure kernel mode (no services), use ``NexusFS()`` directly.

    Args:
        backend: Backend instance for file storage.
        metadata_store: FileMetadataProtocol instance.
        record_store: Optional RecordStoreABC. When provided, all services
            (ReBAC, Audit, Permissions, etc.) are created and injected.
        **config: All NexusFS kernel and service configuration parameters.

    Returns:
        Fully configured NexusFS instance with services injected.
    """
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.router import NamespaceConfig, PathRouter

    # Create and configure router (kernel component, but needed by services
    # before NexusFS is constructed — so we build it here and inject it)
    router = PathRouter()
    if custom_namespaces:
        for ns_config in custom_namespaces:
            if isinstance(ns_config, dict):
                ns_config = NamespaceConfig(**ns_config)
            router.register_namespace(ns_config)
    router.add_mount("/", backend, priority=0)

    # Create services if record_store is provided
    services: dict[str, Any] = {}
    if record_store is not None:
        services = create_nexus_services(
            record_store=record_store,
            metadata_store=metadata_store,
            backend=backend,
            router=router,
            cache_ttl_seconds=cache_ttl_seconds,
            enforce_zone_isolation=enforce_zone_isolation,
            enable_tiger_cache=enable_tiger_cache,
            allow_admin_bypass=allow_admin_bypass,
            inherit_permissions=inherit_permissions,
            enable_deferred_permissions=enable_deferred_permissions,
            deferred_flush_interval=deferred_flush_interval,
            zone_id=zone_id,
            agent_id=agent_id,
            enable_write_buffer=enable_write_buffer,
        )

    # Pop services that NexusFS doesn't accept as constructor params.
    # These are stored in nx._service_extras for server-layer access.
    service_extras: dict[str, Any] = {}
    for key in (
        "observability_subsystem",
        "chunked_upload_service",
        "manifest_resolver",
        "manifest_metrics",
        "rebac_circuit_breaker",
    ):
        val = services.pop(key, None)
        if val is not None:
            service_extras[key] = val

    nx = NexusFS(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        cache_store=cache_store,
        is_admin=is_admin,
        zone_id=zone_id,
        agent_id=agent_id,
        router=router,
        enable_metadata_cache=enable_metadata_cache,
        cache_path_size=cache_path_size,
        cache_list_size=cache_list_size,
        cache_kv_size=cache_kv_size,
        cache_exists_size=cache_exists_size,
        cache_ttl_seconds=cache_ttl_seconds,
        enable_content_cache=enable_content_cache,
        content_cache_size_mb=content_cache_size_mb,
        auto_parse=auto_parse,
        custom_parsers=custom_parsers,
        parse_providers=parse_providers,
        enforce_permissions=enforce_permissions,
        allow_admin_bypass=allow_admin_bypass,
        enforce_zone_isolation=enforce_zone_isolation,
        audit_strict_mode=audit_strict_mode,
        enable_workflows=enable_workflows,
        workflow_engine=workflow_engine,
        coordination_url=coordination_url,
        enable_distributed_events=enable_distributed_events,
        enable_distributed_locks=enable_distributed_locks,
        **services,
    )

    nx._service_extras = service_extras

    # Wire circuit breaker into ReBACService (Issue #726)
    cb = service_extras.get("rebac_circuit_breaker")
    if cb and hasattr(nx, "rebac_service"):
        nx.rebac_service._circuit_breaker = cb

    return nx
