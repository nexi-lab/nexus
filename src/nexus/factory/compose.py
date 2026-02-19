"""Composition root — create_nexus_services(), create_nexus_fs(), create_record_store()."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.factory.boot_context import _BootContext
from nexus.factory.bricks import _boot_brick_services
from nexus.factory.kernel import _boot_kernel_services
from nexus.factory.system import _boot_system_services, _start_background_services

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.core.config import (
        CacheConfig,
        DistributedConfig,
        KernelServices,
        PermissionConfig,
    )
    from nexus.core.metastore import MetastoreABC
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.router import PathRouter
    from nexus.storage.record_store import RecordStoreABC
    from nexus.workflows.protocol import WorkflowProtocol

logger = logging.getLogger(__name__)


def create_record_store(
    *,
    db_url: str | None = None,
    db_path: str | None = None,
    create_tables: bool = True,
) -> RecordStoreABC:
    """Create a RecordStore with Cloud SQL and read replica support auto-detected from env.

    When the ``CLOUD_SQL_INSTANCE`` environment variable is set, the
    Cloud SQL Python Connector is used for IAM-authenticated connections
    (no passwords, no public IP).  Otherwise, the standard URL-based
    connection path is used.

    Read replica support (Issue #725):
    - ``NEXUS_READ_REPLICA_URL``: Standard read replica connection string
    - ``CLOUD_SQL_READ_INSTANCE``: Cloud SQL read replica instance

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

    read_replica_url = os.getenv("NEXUS_READ_REPLICA_URL")

    cloud_sql_instance = os.getenv("CLOUD_SQL_INSTANCE")
    if cloud_sql_instance:
        from nexus.storage.cloud_sql import create_cloud_sql_creators

        sync_creator, async_creator = create_cloud_sql_creators(
            instance_connection_name=cloud_sql_instance,
            db_user=os.getenv("CLOUD_SQL_USER", "nexus"),
            db_name=os.getenv("CLOUD_SQL_DB", "nexus"),
        )

        # Cloud SQL read replica support (Issue #725)
        read_replica_creator = None
        async_read_replica_creator = None
        cloud_sql_read_instance = os.getenv("CLOUD_SQL_READ_INSTANCE")
        if cloud_sql_read_instance:
            read_sync, read_async = create_cloud_sql_creators(
                instance_connection_name=cloud_sql_read_instance,
                db_user=os.getenv("CLOUD_SQL_USER", "nexus"),
                db_name=os.getenv("CLOUD_SQL_DB", "nexus"),
            )
            read_replica_creator = read_sync
            async_read_replica_creator = read_async
            # Use placeholder URL for read replica engine
            read_replica_url = read_replica_url or "postgresql://"

        return SQLAlchemyRecordStore(
            db_url=db_url or "postgresql://",  # placeholder, creator overrides
            create_tables=create_tables,
            creator=sync_creator,
            async_creator=async_creator,
            read_replica_url=read_replica_url,
            read_replica_creator=read_replica_creator,
            async_read_replica_creator=async_read_replica_creator,
        )

    return SQLAlchemyRecordStore(
        db_url=db_url,
        db_path=db_path,
        create_tables=create_tables,
        read_replica_url=read_replica_url,
    )


def create_nexus_services(
    record_store: RecordStoreABC,
    metadata_store: MetastoreABC,
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
    enabled_bricks: frozenset[str] | None = None,
) -> KernelServices:
    """Create default services for NexusFS dependency injection.

    Orchestrates 3-tier boot sequence:

    1. **Kernel** — mandatory (ReBAC, permissions, workspace, sync, version).
       Failure raises ``BootError``.
    2. **System** — degraded-mode (agent registry, namespace, observability,
       resiliency). Failure warns + ``None``.
    3. **Brick** — optional (search, wallet, manifest, upload, distributed).
       Failure is silent (DEBUG) + ``None``.

    Background threads (``.start()``) are deferred until all three tiers
    are constructed.

    Args:
        record_store: RecordStoreABC instance (provides engine + session_factory).
        metadata_store: MetastoreABC instance (for PermissionEnforcer).
        backend: Backend instance (for WorkspaceManager).
        router: PathRouter instance (for PermissionEnforcer object type resolution).
        permissions: Permission config (defaults from PermissionConfig()).
        cache: Cache config (for TTL values, defaults from CacheConfig()).
        distributed: Distributed config (for event bus/locks).
        zone_id: Default zone ID (for WorkspaceManager, embedded mode only).
        agent_id: Default agent ID (for WorkspaceManager, embedded mode only).
        enable_write_buffer: Use async WriteBuffer for PG sync (Issue #1246).
        resiliency_raw: Raw resiliency policy dict from YAML config.
        enabled_bricks: Set of brick names to enable. When None, all bricks
            are enabled (backward-compatible default = FULL profile).

    Returns:
        KernelServices with all services populated (None for disabled bricks).
    """
    import logging as _factory_logging

    _factory_log = _factory_logging.getLogger(__name__)

    from nexus.core.config import CacheConfig as _CacheConfig
    from nexus.core.config import DistributedConfig as _DistributedConfig
    from nexus.core.config import KernelServices as _KernelServices
    from nexus.core.config import PermissionConfig as _PermissionConfig

    # --- Profile-based brick gating (Issue #1389) ---
    from nexus.core.deployment_profile import DeploymentProfile

    if enabled_bricks is None:
        enabled_bricks = DeploymentProfile.FULL.default_bricks()

    def _brick_on(name: str) -> bool:
        return name in enabled_bricks  # type: ignore[operator]

    _factory_log.info(
        "Factory: enabled_bricks=%d/%d %s",
        len(enabled_bricks),
        20,
        sorted(enabled_bricks),
    )

    # --- Performance tuning (Issue #2071) ---
    import os

    from nexus.core.performance_tuning import resolve_profile_tuning

    _profile_str = os.environ.get("NEXUS_PROFILE", "full")
    try:
        _factory_profile = DeploymentProfile(_profile_str)
    except ValueError:
        _factory_profile = DeploymentProfile.FULL
    _profile_tuning = resolve_profile_tuning(_factory_profile)

    perm = permissions or _PermissionConfig()
    cache_cfg = cache or _CacheConfig()
    dist = distributed or _DistributedConfig()

    ctx = _BootContext(
        record_store=record_store,
        metadata_store=metadata_store,
        backend=backend,
        router=router,
        engine=record_store.engine,
        read_engine=record_store.read_engine,
        session_factory=record_store.session_factory,
        perm=perm,
        cache_ttl_seconds=cache_cfg.ttl_seconds,
        dist=dist,
        zone_id=zone_id,
        agent_id=agent_id,
        enable_write_buffer=enable_write_buffer,
        resiliency_raw=resiliency_raw,
        db_url=getattr(record_store, "database_url", ""),
        profile_tuning=_profile_tuning,
    )

    # --- Tier 0: KERNEL (fatal on failure) ---
    kernel = _boot_kernel_services(ctx)

    # --- Tier 1: SYSTEM (degraded on failure) ---
    system = _boot_system_services(ctx, kernel)

    # --- Tier 2: BRICK (optional) ---
    brick = _boot_brick_services(ctx, kernel)

    # --- Start background threads post-construction ---
    _start_background_services(kernel, system)

    # --- Assemble KernelServices ---
    return _KernelServices(
        # Kernel tier
        router=router,
        rebac_manager=kernel["rebac_manager"],
        rebac_circuit_breaker=kernel["rebac_circuit_breaker"],
        dir_visibility_cache=kernel["dir_visibility_cache"],
        audit_store=kernel["audit_store"],
        entity_registry=kernel["entity_registry"],
        permission_enforcer=kernel["permission_enforcer"],
        hierarchy_manager=kernel["hierarchy_manager"],
        deferred_permission_buffer=kernel["deferred_permission_buffer"],
        workspace_registry=kernel["workspace_registry"],
        mount_manager=kernel["mount_manager"],
        workspace_manager=kernel["workspace_manager"],
        context_branch_service=system.get("context_branch_service"),
        write_observer=kernel["write_observer"],
        version_service=kernel["version_service"],
        # System tier
        agent_registry=system["agent_registry"],
        async_agent_registry=system["async_agent_registry"],
        namespace_manager=system["namespace_manager"],
        async_namespace_manager=system["async_namespace_manager"],
        async_vfs_router=system["async_vfs_router"],
        resiliency_manager=system["resiliency_manager"],
        brick_lifecycle_manager=system.get("brick_lifecycle_manager"),
        scoped_hook_engine=system.get("scoped_hook_engine"),
        # Brick tier
        overlay_resolver=None,
        wallet_provisioner=brick["wallet_provisioner"],
        event_bus=brick["event_bus"],
        lock_manager=brick["lock_manager"],
        workflow_engine=brick["workflow_engine"],
        # Server-layer services (first-class fields)
        observability_subsystem=system["observability_subsystem"],
        chunked_upload_service=brick["chunked_upload_service"],
        manifest_resolver=brick["manifest_resolver"],
        manifest_metrics=brick["manifest_metrics"],
        tool_namespace_middleware=brick["tool_namespace_middleware"],
        delivery_worker=system["delivery_worker"],
        api_key_creator=brick["api_key_creator"],
        snapshot_service=brick["snapshot_service"],
        task_queue_service=brick["task_queue_service"],
    )


def create_nexus_fs(
    backend: Backend,
    metadata_store: MetastoreABC,
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
    enable_write_buffer: bool | None = None,
    enabled_bricks: frozenset[str] | None = None,
    zone_id: str | None = None,
    agent_id: str | None = None,
    workflow_engine: WorkflowProtocol | None = None,
) -> NexusFS:
    """Create NexusFS with default services — the recommended entry point.

    Args:
        backend: Backend instance for file storage.
        metadata_store: MetastoreABC instance.
        record_store: Optional RecordStoreABC. When provided, all services
            (ReBAC, Audit, Permissions, etc.) are created and injected.
        cache_store: CacheStoreABC instance for ephemeral cache.
        is_admin: Whether the instance has admin privileges.
        custom_namespaces: Custom namespace configurations.
        cache: CacheConfig object.
        permissions: PermissionConfig object.
        distributed: DistributedConfig object.
        memory: MemoryConfig object.
        parsing: ParseConfig object.
        services: Pre-built KernelServices (skips create_nexus_services).
        enable_write_buffer: Use async WriteBuffer for PG sync.
        enabled_bricks: Set of brick names to enable.
        zone_id: Default zone ID (for WorkspaceManager, embedded mode).
        agent_id: Default agent ID (for WorkspaceManager, embedded mode).
        workflow_engine: Pre-built workflow engine override.

    Returns:
        Fully configured NexusFS instance with services injected.
    """
    from nexus.core.config import (
        DistributedConfig as _DistributedConfig,
    )
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.router import NamespaceConfig, PathRouter

    # Create and configure router
    router = PathRouter()
    if custom_namespaces:
        for ns_config in custom_namespaces:
            if isinstance(ns_config, dict):
                ns_config = NamespaceConfig(**ns_config)
            router.register_namespace(ns_config)
    router.add_mount("/", backend, priority=0)

    # KERNEL-ARCHITECTURE §2: No CacheStore → EventBus disabled.
    _has_real_cache = cache_store is not None
    if _has_real_cache:
        from nexus.core.cache_store import NullCacheStore as _NullCacheStore

        if isinstance(cache_store, _NullCacheStore):
            _has_real_cache = False
    if not _has_real_cache:
        _base_dist = distributed or _DistributedConfig()
        if _base_dist.enable_events:
            from dataclasses import replace as _dc_replace

            distributed = _dc_replace(_base_dist, enable_events=False)
            logger.debug("EventBus disabled: no CacheStore provided (KERNEL-ARCHITECTURE §2)")

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
            enabled_bricks=enabled_bricks,
        )
    elif services is None:
        from nexus.core.config import KernelServices as _KernelServices

        services = _KernelServices(router=router)
    else:
        # Use provided services but ensure router is set (frozen — use replace)
        if services.router is None:
            from dataclasses import replace as _dc_replace

            services = _dc_replace(services, router=router)

    # Inject workflow_engine override if provided directly (frozen — use replace)
    if workflow_engine is not None:
        from dataclasses import replace as _dc_replace

        services = _dc_replace(services, workflow_engine=workflow_engine)

    # Create ParsersBrick — owns both registries (Issue #1523)
    from nexus.parsers.brick import ParsersBrick

    parsers_brick = ParsersBrick(parsing_config=parsing)
    _parse_fn = parsers_brick.create_parse_fn()

    # Create CacheBrick — owns all cache domain services (Issue #1524)
    from nexus.cache.brick import CacheBrick

    _cache_brick = CacheBrick(
        cache_store=cache_store,
        record_store=record_store,
    )

    # Create content cache (Issue #657)
    _content_cache = None
    if cache is not None and cache.enable_content_cache and backend.has_root_path is True:
        from nexus.storage.content_cache import ContentCache

        _content_cache = ContentCache(max_size_mb=cache.content_cache_size_mb)

    # Create VFS lock manager (Issue #657)
    from nexus.core.lock_fast import create_vfs_lock_manager

    _vfs_lock_manager = create_vfs_lock_manager()

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
        kernel_services=services,
        parse_fn=_parse_fn,
        content_cache=_content_cache,
        parser_registry=parsers_brick.parser_registry,
        provider_registry=parsers_brick.provider_registry,
        vfs_lock_manager=_vfs_lock_manager,
    )

    # Attach CacheBrick to NexusFS for server layer access (Issue #1524)
    nx._cache_brick = _cache_brick  # type: ignore[attr-defined]

    return nx
