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

Usage::

    # Quick: single call creates kernel + services
    from nexus.factory import create_nexus_fs

    nx = create_nexus_fs(
        backend=LocalBackend(root_path="./data"),
        metadata_store=RaftMetadataStore.embedded("./raft"),
        record_store=SQLAlchemyRecordStore(db_path="./db.sqlite"),
        permissions=PermissionConfig(enforce=False),
    )

    # Advanced: create services separately, inject into kernel
    from nexus.factory import create_nexus_services

    services = create_nexus_services(
        record_store=record_store,
        metadata_store=metadata_store,
        backend=backend,
        router=my_router,
    )
    nx = NexusFS(backend=backend, metadata_store=metadata_store, services=services)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.core._metadata_generated import FileMetadataProtocol
    from nexus.core.config import (
        CacheConfig,
        DistributedConfig,
        KernelServices,
        PermissionConfig,
    )
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


def _parse_resiliency_config(raw: dict[str, Any] | None) -> Any:
    """Convert raw YAML dict → frozen ``ResiliencyConfig`` dataclasses.

    Returns default config when *raw* is None or empty.  Falls back to
    default config on parse errors (logs the error).
    """
    import logging as _log

    from nexus.core.resiliency import (
        CircuitBreakerPolicy,
        ResiliencyConfig,
        RetryPolicy,
        TargetBinding,
        TimeoutPolicy,
        parse_duration,
    )

    if not raw:
        return ResiliencyConfig()

    _logger = _log.getLogger(__name__)

    try:
        timeouts: dict[str, TimeoutPolicy] = {"default": TimeoutPolicy()}
        for name, val in raw.get("timeouts", {}).items():
            if isinstance(val, dict):
                timeouts[name] = TimeoutPolicy(
                    seconds=parse_duration(val.get("seconds", 5.0)),
                )
            else:
                timeouts[name] = TimeoutPolicy(seconds=parse_duration(val))

        retries: dict[str, RetryPolicy] = {"default": RetryPolicy()}
        for name, val in raw.get("retries", {}).items():
            if isinstance(val, dict):
                retries[name] = RetryPolicy(
                    max_retries=int(val.get("max_retries", 3)),
                    max_interval=float(val.get("max_interval", 10.0)),
                    multiplier=float(val.get("multiplier", 2.0)),
                    min_wait=float(val.get("min_wait", 1.0)),
                )

        circuit_breakers: dict[str, CircuitBreakerPolicy] = {"default": CircuitBreakerPolicy()}
        for name, val in raw.get("circuit_breakers", {}).items():
            if isinstance(val, dict):
                circuit_breakers[name] = CircuitBreakerPolicy(
                    failure_threshold=int(val.get("failure_threshold", 5)),
                    success_threshold=int(val.get("success_threshold", 3)),
                    timeout=parse_duration(val.get("timeout", 30.0)),
                )

        targets: dict[str, TargetBinding] = {}
        for name, val in raw.get("targets", {}).items():
            if isinstance(val, dict):
                targets[name] = TargetBinding(
                    timeout=str(val.get("timeout", "default")),
                    retry=str(val.get("retry", "default")),
                    circuit_breaker=str(val.get("circuit_breaker", "default")),
                )

        return ResiliencyConfig(
            timeouts=timeouts,
            retries=retries,
            circuit_breakers=circuit_breakers,
            targets=targets,
        )
    except (ValueError, TypeError, AttributeError) as exc:
        _logger.error("Invalid resiliency config, using defaults: %s", exc)
        return ResiliencyConfig()


def create_record_store(
    *,
    db_url: str | None = None,
    db_path: str | None = None,
    create_tables: bool = True,
) -> RecordStoreABC:
    """Create a RecordStore with Cloud SQL support auto-detected from env.

    When the ``CLOUD_SQL_INSTANCE`` environment variable is set, the
    Cloud SQL Python Connector is used for IAM-authenticated connections
    (no passwords, no public IP).  Otherwise, the standard URL-based
    connection path is used.

    Args:
        db_url: Explicit database URL. Falls back to env vars.
        db_path: SQLite path (development only).
        create_tables: If True, run ``create_all`` on init. Set False
            in production when Alembic is the schema SSOT.

    Returns:
        Fully initialized ``SQLAlchemyRecordStore``.
    """
    import os

    from nexus.storage.record_store import SQLAlchemyRecordStore

    cloud_sql_instance = os.getenv("CLOUD_SQL_INSTANCE")
    if cloud_sql_instance:
        from nexus.storage.cloud_sql import create_cloud_sql_creators

        sync_creator, async_creator = create_cloud_sql_creators(
            instance_connection_name=cloud_sql_instance,
            db_user=os.getenv("CLOUD_SQL_USER", "nexus"),
            db_name=os.getenv("CLOUD_SQL_DB", "nexus"),
        )
        return SQLAlchemyRecordStore(
            db_url=db_url or "postgresql://",  # placeholder, creator overrides
            create_tables=create_tables,
            creator=sync_creator,
            async_creator=async_creator,
        )

    return SQLAlchemyRecordStore(
        db_url=db_url,
        db_path=db_path,
        create_tables=create_tables,
    )


def create_nexus_services(
    record_store: RecordStoreABC,
    metadata_store: FileMetadataProtocol,
    backend: Backend,
    router: PathRouter,
    *,
    permissions: PermissionConfig | None = None,
    cache: CacheConfig | None = None,
    distributed: DistributedConfig | None = None,
    zone_id: str | None = None,
    agent_id: str | None = None,
    enable_write_buffer: bool | None = None,
    resiliency_raw: dict[str, Any] | None = None,
) -> KernelServices:
    """Create default services for NexusFS dependency injection.

    Builds all service instances and bundles them into a ``KernelServices``
    dataclass for clean injection into the kernel constructor.

    Args:
        record_store: RecordStoreABC instance (provides engine + session_factory).
        metadata_store: FileMetadataProtocol instance (for PermissionEnforcer).
        backend: Backend instance (for WorkspaceManager).
        router: PathRouter instance (for PermissionEnforcer object type resolution).
        permissions: Permission config (defaults from PermissionConfig()).
        cache: Cache config (for TTL values, defaults from CacheConfig()).
        distributed: Distributed config (for event bus/locks).
        zone_id: Default zone ID (for WorkspaceManager, embedded mode only).
        agent_id: Default agent ID (for WorkspaceManager, embedded mode only).
        enable_write_buffer: Use async WriteBuffer for PG sync (Issue #1246).
        resiliency_raw: Raw resiliency policy dict from YAML config.

    Returns:
        KernelServices with all services populated.
    """
    from nexus.core.config import CacheConfig as _CacheConfig
    from nexus.core.config import DistributedConfig as _DistributedConfig
    from nexus.core.config import KernelServices as _KernelServices
    from nexus.core.config import PermissionConfig as _PermissionConfig

    perm = permissions or _PermissionConfig()
    cache_cfg = cache or _CacheConfig()
    dist = distributed or _DistributedConfig()

    cache_ttl_seconds = cache_cfg.ttl_seconds
    engine = record_store.engine
    session_factory = record_store.session_factory

    # --- ReBAC Manager ---
    from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager

    rebac_manager = EnhancedReBACManager(
        engine=engine,
        cache_ttl_seconds=cache_ttl_seconds or 300,
        max_depth=10,
        enforce_zone_isolation=perm.enforce_zone_isolation,
        enable_graph_limits=True,
        enable_tiger_cache=perm.enable_tiger_cache,
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
    from nexus.services.permissions.enforcer import PermissionEnforcer

    permission_enforcer = PermissionEnforcer(
        metadata_store=metadata_store,
        rebac_manager=rebac_manager,
        allow_admin_bypass=perm.allow_admin_bypass,
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
        enable_inheritance=perm.inherit,
    )

    # --- Deferred Permission Buffer ---
    from nexus.services.permissions.deferred_permission_buffer import DeferredPermissionBuffer

    deferred_permission_buffer = None
    if perm.enable_deferred:
        deferred_permission_buffer = DeferredPermissionBuffer(
            rebac_manager=rebac_manager,
            hierarchy_manager=hierarchy_manager,
            flush_interval_sec=perm.deferred_flush_interval,
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
    from nexus.services.workspace_manager import WorkspaceManager

    workspace_manager = WorkspaceManager(
        metadata=metadata_store,
        backend=backend,
        rebac_manager=rebac_manager,
        zone_id=zone_id,
        agent_id=agent_id,
        session_factory=session_factory,
    )

    # --- RecordStore Syncer (Issue #1246) ---
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

    # --- Event Delivery Worker (Issue #1241) ---
    # Transactional outbox: polls undelivered operation_log rows and
    # dispatches FileEvents to EventBus/webhooks with at-least-once semantics.
    # Only enabled for PostgreSQL — SQLite doesn't support concurrent thread access.
    delivery_worker = None
    if db_url.startswith(("postgres", "postgresql")):
        try:
            from nexus.services.event_log.delivery_worker import EventDeliveryWorker

            delivery_worker = EventDeliveryWorker(
                session_factory=session_factory,
                poll_interval_ms=200,
                batch_size=50,
            )
            delivery_worker.start()
        except Exception as _dw_exc:
            import logging as _dw_logging

            _dw_logging.getLogger(__name__).warning("EventDeliveryWorker unavailable: %s", _dw_exc)

    # --- VersionService (Task #45) ---
    from nexus.services.version_service import VersionService

    version_service = VersionService(
        metadata_store=metadata_store,
        cas_store=backend,
        router=router,
        enforce_permissions=False,
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

    # --- Resiliency Subsystem (Issue #1366) ---
    from nexus.core.resiliency import ResiliencyManager, set_default_manager

    resiliency_config = _parse_resiliency_config(resiliency_raw)
    resiliency_manager = ResiliencyManager(config=resiliency_config)
    set_default_manager(resiliency_manager)

    # --- Chunked Upload Service (Issue #788) ---
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

    # --- Search Brick Import Validation (Issue #1520) ---
    import logging as _search_log

    _search_logger = _search_log.getLogger(__name__)
    try:
        from nexus.search.manifest import verify_imports as _verify_search

        _search_status = _verify_search()
        _search_logger.debug("[FACTORY] Search brick imports: %s", _search_status)
    except ImportError:
        _search_logger.debug("[FACTORY] Search brick manifest not available")

    # Wire zoekt callbacks into backends (Issue #1520)
    try:
        from nexus.search.zoekt_client import notify_zoekt_sync_complete, notify_zoekt_write

        if hasattr(backend, "_on_write_callback") and backend._on_write_callback is None:
            backend._on_write_callback = notify_zoekt_write
        if hasattr(backend, "on_sync_callback") and backend.on_sync_callback is None:
            backend.on_sync_callback = notify_zoekt_sync_complete
    except ImportError:
        _search_logger.debug("[FACTORY] Zoekt not available, skipping callback wiring")

    # --- Wallet Provisioner (Issue #1210) ---
    wallet_provisioner = _create_wallet_provisioner()

    # --- Manifest Resolver (Issue #1427, #1428) ---
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

        import importlib.util

        if importlib.util.find_spec("nexus.services.context_manifest.executors.memory_query"):
            _factory_logger.debug("MemoryQueryExecutor available for per-agent wiring")
        else:
            _factory_logger.debug("MemoryQueryExecutor module not found")

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

    # --- Tool Namespace Middleware (Issue #1272) ---
    tool_namespace_middleware = None
    try:
        from nexus.mcp.middleware import ToolNamespaceMiddleware

        tool_namespace_middleware = ToolNamespaceMiddleware(
            rebac_manager=rebac_manager,
            zone_id=zone_id,
            cache_ttl=cache_ttl_seconds or 300,
        )
        _factory_logger.debug("[FACTORY] ToolNamespaceMiddleware created (zone_id=%s)", zone_id)
    except ImportError as _e:
        _factory_logger.debug("ToolNamespaceMiddleware unavailable: %s", _e)

    # --- Infrastructure: event bus + lock manager (moved from NexusFS.__init__) ---
    event_bus: Any = None
    lock_manager: Any = None
    if dist.enable_locks or dist.enable_events:
        event_bus, lock_manager = _create_distributed_infra(
            dist,
            metadata_store,
            session_factory,
            dist.coordination_url,
        )

    # --- Workflow engine (moved from NexusFS.__init__) ---
    workflow_engine: Any = None
    if dist.enable_workflows:
        workflow_engine = _create_workflow_engine(session_factory, metadata_store)

    return _KernelServices(
        router=router,
        rebac_manager=rebac_manager,
        dir_visibility_cache=dir_visibility_cache,
        audit_store=audit_store,
        entity_registry=entity_registry,
        permission_enforcer=permission_enforcer,
        hierarchy_manager=hierarchy_manager,
        deferred_permission_buffer=deferred_permission_buffer,
        workspace_registry=workspace_registry,
        mount_manager=mount_manager,
        workspace_manager=workspace_manager,
        write_observer=write_observer,
        version_service=version_service,
        overlay_resolver=None,
        wallet_provisioner=wallet_provisioner,
        event_bus=event_bus,
        lock_manager=lock_manager,
        workflow_engine=workflow_engine,
        server_extras={
            "observability_subsystem": observability_subsystem,
            "chunked_upload_service": chunked_upload_service,
            "manifest_resolver": manifest_resolver,
            "manifest_metrics": manifest_metrics,
            "rebac_circuit_breaker": rebac_circuit_breaker,
            "tool_namespace_middleware": tool_namespace_middleware,
            "resiliency_manager": resiliency_manager,
            "delivery_worker": delivery_worker,
        },
    )


def _create_distributed_infra(
    dist: DistributedConfig,
    metadata_store: FileMetadataProtocol,
    session_factory: Any,
    coordination_url: str | None,
) -> tuple[Any, Any]:
    """Create event bus and lock manager (was NexusFS.__init__ lines 439-521).

    Returns (event_bus, lock_manager) tuple. Either may be None.
    """
    import logging

    logger = logging.getLogger(__name__)
    event_bus: Any = None
    lock_manager: Any = None

    try:
        # Initialize lock manager (uses Raft via metadata store)
        if dist.enable_locks:
            from nexus.core.distributed_lock import (
                RaftLockManager,
                set_distributed_lock_manager,
            )
            from nexus.storage.raft_metadata_store import RaftMetadataStore

            if isinstance(metadata_store, RaftMetadataStore):
                lock_manager = RaftLockManager(metadata_store)
                set_distributed_lock_manager(lock_manager)
                logger.info("Distributed lock manager initialized (Raft consensus)")
            else:
                logger.warning(
                    "Distributed locks require RaftMetadataStore, got %s. "
                    "Lock manager will not be initialized.",
                    type(metadata_store).__name__,
                )

        # Initialize event bus
        if dist.event_bus_backend == "nats":
            from nexus.core.event_bus import create_event_bus, set_global_event_bus

            event_bus = create_event_bus(
                backend="nats",
                nats_url=dist.nats_url,
                session_factory=session_factory,
            )
            set_global_event_bus(event_bus)
            logger.info(
                "Distributed event bus initialized (NATS JetStream: %s, SSOT: PostgreSQL)",
                dist.nats_url,
            )
        elif dist.enable_events:
            import os

            coordination_url_resolved = coordination_url or os.getenv("NEXUS_REDIS_URL")
            event_url_resolved = coordination_url_resolved or os.getenv("NEXUS_DRAGONFLY_CACHE_URL")
            if event_url_resolved:
                from nexus.cache.dragonfly import DragonflyClient
                from nexus.core.event_bus import RedisEventBus, set_global_event_bus

                event_client = DragonflyClient(url=event_url_resolved)
                event_bus = RedisEventBus(
                    event_client,
                    session_factory=session_factory,
                )
                set_global_event_bus(event_bus)
                logger.info(
                    "Distributed event bus initialized (dragonfly: %s, SSOT: PostgreSQL)",
                    event_url_resolved,
                )
    except ImportError as e:
        logger.warning("Could not initialize distributed event system: %s", e)

    return event_bus, lock_manager


def _create_workflow_engine(session_factory: Any, metadata_store: Any) -> Any:
    """Create workflow engine (was NexusFS.__init__ lines 529-555).

    Returns workflow engine or None if unavailable.
    """
    import logging

    logger = logging.getLogger(__name__)
    if session_factory is None:
        logger.warning("Workflows require record_store (session_factory), skipping")
        return None
    try:
        from nexus.workflows.engine import init_engine
        from nexus.workflows.storage import WorkflowStore

        workflow_store = WorkflowStore(
            session_factory=session_factory,
            zone_id="default",
        )
        return init_engine(
            metadata_store=metadata_store,
            plugin_registry=None,
            workflow_store=workflow_store,
        )
    except Exception:
        return None


def _post_init(nx: NexusFS) -> None:
    """Post-construction steps: mount restoration.

    Called after NexusFS is constructed to perform I/O that requires
    a fully-wired kernel instance.
    """
    import logging
    import os

    logger = logging.getLogger(__name__)

    # Load all saved mounts from database and activate them
    try:
        if hasattr(nx, "load_all_saved_mounts"):
            auto_sync = os.getenv("NEXUS_AUTO_SYNC_MOUNTS", "false").lower() in (
                "true",
                "1",
                "yes",
            )
            mount_result = nx.load_all_saved_mounts(auto_sync=auto_sync)
            if mount_result["loaded"] > 0 or mount_result["failed"] > 0:
                sync_msg = (
                    f", {mount_result['synced']} synced" if mount_result["synced"] > 0 else ""
                )
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


def create_nexus_fs(
    backend: Backend,
    metadata_store: FileMetadataProtocol,
    record_store: RecordStoreABC | None = None,
    *,
    cache_store: Any = None,
    is_admin: bool = False,
    custom_namespaces: list[Any] | None = None,
    cache: CacheConfig | None = None,
    permissions: PermissionConfig | None = None,
    distributed: DistributedConfig | None = None,
    memory: Any = None,
    parsing: Any = None,
    services: KernelServices | None = None,
    # Legacy flat params — translated to config objects for backward compat
    enforce_permissions: bool | None = None,
    allow_admin_bypass: bool | None = None,
    enforce_zone_isolation: bool | None = None,
    audit_strict_mode: bool | None = None,
    auto_parse: bool | None = None,
    enable_tiger_cache: bool | None = None,
    inherit_permissions: bool | None = None,
    enable_deferred_permissions: bool | None = None,
    deferred_flush_interval: float | None = None,
    enable_workflows: bool | None = None,
    coordination_url: str | None = None,
    enable_distributed_events: bool | None = None,
    enable_distributed_locks: bool | None = None,
    enable_metadata_cache: bool | None = None,
    cache_path_size: int | None = None,
    cache_list_size: int | None = None,
    cache_kv_size: int | None = None,
    cache_exists_size: int | None = None,
    cache_ttl_seconds: int | None = None,
    enable_content_cache: bool | None = None,
    content_cache_size_mb: int | None = None,
    parse_providers: list[dict[str, Any]] | None = None,
    enable_write_buffer: bool | None = None,
    enable_memory_paging: bool | None = None,
    memory_main_capacity: int | None = None,
    memory_recall_max_age_hours: float | None = None,
    # Deprecated — ignored
    zone_id: str | None = None,
    agent_id: str | None = None,
    custom_parsers: list[dict[str, Any]] | None = None,  # noqa: ARG001
    workflow_engine: Any = None,
) -> NexusFS:
    """Create NexusFS with default services — the recommended entry point.

    Accepts both new config objects and legacy flat params for backward
    compatibility. Config objects take precedence when both are provided.

    Args:
        backend: Backend instance for file storage.
        metadata_store: FileMetadataProtocol instance.
        record_store: Optional RecordStoreABC. When provided, all services
            (ReBAC, Audit, Permissions, etc.) are created and injected.
        cache: CacheConfig object (or build from legacy flat params).
        permissions: PermissionConfig object (or build from legacy flat params).
        distributed: DistributedConfig object (or build from legacy flat params).
        memory: MemoryConfig object.
        parsing: ParseConfig object.
        services: Pre-built KernelServices (skips create_nexus_services).

    Returns:
        Fully configured NexusFS instance with services injected.
    """
    from nexus.core.config import (
        CacheConfig as _CacheConfig,
    )
    from nexus.core.config import (
        DistributedConfig as _DistributedConfig,
    )
    from nexus.core.config import (
        MemoryConfig as _MemoryConfig,
    )
    from nexus.core.config import (
        ParseConfig as _ParseConfig,
    )
    from nexus.core.config import (
        PermissionConfig as _PermissionConfig,
    )
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.router import NamespaceConfig, PathRouter

    # Build config objects from legacy flat params if config objects not provided
    if cache is None:
        cache_kwargs: dict[str, Any] = {}
        if enable_metadata_cache is not None:
            cache_kwargs["enable_metadata_cache"] = enable_metadata_cache
        if cache_path_size is not None:
            cache_kwargs["path_size"] = cache_path_size
        if cache_list_size is not None:
            cache_kwargs["list_size"] = cache_list_size
        if cache_kv_size is not None:
            cache_kwargs["kv_size"] = cache_kv_size
        if cache_exists_size is not None:
            cache_kwargs["exists_size"] = cache_exists_size
        if cache_ttl_seconds is not None:
            cache_kwargs["ttl_seconds"] = cache_ttl_seconds
        if enable_content_cache is not None:
            cache_kwargs["enable_content_cache"] = enable_content_cache
        if content_cache_size_mb is not None:
            cache_kwargs["content_cache_size_mb"] = content_cache_size_mb
        cache = _CacheConfig(**cache_kwargs) if cache_kwargs else None

    if permissions is None:
        perm_kwargs: dict[str, Any] = {}
        if enforce_permissions is not None:
            perm_kwargs["enforce"] = enforce_permissions
        if inherit_permissions is not None:
            perm_kwargs["inherit"] = inherit_permissions
        if allow_admin_bypass is not None:
            perm_kwargs["allow_admin_bypass"] = allow_admin_bypass
        if enforce_zone_isolation is not None:
            perm_kwargs["enforce_zone_isolation"] = enforce_zone_isolation
        if audit_strict_mode is not None:
            perm_kwargs["audit_strict_mode"] = audit_strict_mode
        if enable_tiger_cache is not None:
            perm_kwargs["enable_tiger_cache"] = enable_tiger_cache
        if enable_deferred_permissions is not None:
            perm_kwargs["enable_deferred"] = enable_deferred_permissions
        if deferred_flush_interval is not None:
            perm_kwargs["deferred_flush_interval"] = deferred_flush_interval
        permissions = _PermissionConfig(**perm_kwargs) if perm_kwargs else None

    if distributed is None:
        dist_kwargs: dict[str, Any] = {}
        if coordination_url is not None:
            dist_kwargs["coordination_url"] = coordination_url
        if enable_distributed_events is not None:
            dist_kwargs["enable_events"] = enable_distributed_events
        if enable_distributed_locks is not None:
            dist_kwargs["enable_locks"] = enable_distributed_locks
        if enable_workflows is not None:
            dist_kwargs["enable_workflows"] = enable_workflows
        distributed = _DistributedConfig(**dist_kwargs) if dist_kwargs else None

    if memory is None:
        mem_kwargs: dict[str, Any] = {}
        if enable_memory_paging is not None:
            mem_kwargs["enable_paging"] = enable_memory_paging
        if memory_main_capacity is not None:
            mem_kwargs["main_capacity"] = memory_main_capacity
        if memory_recall_max_age_hours is not None:
            mem_kwargs["recall_max_age_hours"] = memory_recall_max_age_hours
        memory = _MemoryConfig(**mem_kwargs) if mem_kwargs else None

    if parsing is None:
        parse_kwargs: dict[str, Any] = {}
        if auto_parse is not None:
            parse_kwargs["auto_parse"] = auto_parse
        if parse_providers is not None:
            parse_kwargs["providers"] = tuple(parse_providers)
        parsing = _ParseConfig(**parse_kwargs) if parse_kwargs else None

    # Create and configure router
    router = PathRouter()
    if custom_namespaces:
        for ns_config in custom_namespaces:
            if isinstance(ns_config, dict):
                ns_config = NamespaceConfig(**ns_config)
            router.register_namespace(ns_config)
    router.add_mount("/", backend, priority=0)

    # Create services if record_store is provided and no pre-built services
    if services is None and record_store is not None:
        services = create_nexus_services(
            record_store=record_store,
            metadata_store=metadata_store,
            backend=backend,
            router=router,
            permissions=permissions,
            cache=cache,
            distributed=distributed,
            zone_id=zone_id,
            agent_id=agent_id,
            enable_write_buffer=enable_write_buffer,
        )
    elif services is None:
        from nexus.core.config import KernelServices as _KernelServices

        services = _KernelServices(router=router)
    else:
        # Use provided services but ensure router is set
        if services.router is None:
            services.router = router

    # Inject workflow_engine override if provided directly
    if workflow_engine is not None:
        services.workflow_engine = workflow_engine

    nx = NexusFS(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        cache_store=cache_store,
        is_admin=is_admin,
        custom_namespaces=custom_namespaces,
        cache=cache,
        permissions=permissions,
        distributed=distributed,
        memory=memory,
        parsing=parsing,
        services=services,
    )

    # Wire circuit breaker into ReBACService (Issue #726)
    cb = services.server_extras.get("rebac_circuit_breaker")
    if cb and hasattr(nx, "rebac_service"):
        nx.rebac_service._circuit_breaker = cb

    # Post-construction I/O (mount restoration, etc.)
    _post_init(nx)

    return nx
