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

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.bricks.workflows.protocol import WorkflowProtocol
    from nexus.core.config import (
        BrickServices,
        CacheConfig,
        DistributedConfig,
        KernelServices,
        PermissionConfig,
        SystemServices,
    )
    from nexus.core.metastore import MetastoreABC
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.router import PathRouter
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Boot context — carries shared deps between tier functions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BootContext:
    """Shared dependencies passed between tier boot functions.

    Built once at the start of ``create_nexus_services()`` and threaded
    through ``_boot_kernel_services``, ``_boot_system_services``, and
    ``_boot_brick_services`` so each tier function receives a clean,
    immutable snapshot of the boot-time configuration.
    """

    record_store: Any
    metadata_store: Any
    backend: Any
    router: Any
    engine: Any
    read_engine: Any  # Read replica engine (Issue #725); same as engine when no replica
    session_factory: Any
    perm: Any  # PermissionConfig
    cache_ttl_seconds: int | None
    dist: Any  # DistributedConfig
    zone_id: str | None
    agent_id: str | None
    enable_write_buffer: bool | None
    resiliency_raw: dict[str, Any] | None
    db_url: str
    profile_tuning: Any  # ProfileTuning (Issue #2071)


# =========================================================================
# Issue #1520: NexusFS → FileReaderProtocol adapter
# =========================================================================


class _NexusFSFileReader:
    """Adapts a NexusFS instance to the FileReaderProtocol interface.

    This adapter is the sole coupling point between the kernel (NexusFS)
    and the search brick. Search modules never import NexusFS directly;
    they receive a FileReaderProtocol at composition time.

    Usage::

        from nexus.factory import _NexusFSFileReader

        reader = _NexusFSFileReader(nexus_fs_instance)
        content = reader.read_text("/path/to/file.py")
    """

    def __init__(self, nx: Any) -> None:
        self._nx = nx

    def read_text(self, path: str) -> str:
        content_raw = self._nx.read(path)
        if isinstance(content_raw, bytes):
            return content_raw.decode("utf-8", errors="ignore")
        return str(content_raw)

    def get_searchable_text(self, path: str) -> str | None:
        result: str | None = self._nx.metadata.get_searchable_text(path)
        return result

    def list_files(self, path: str, recursive: bool = True) -> list[Any]:
        result = self._nx.list(path, recursive=recursive)
        items: list[Any] = result.items if hasattr(result, "items") else result
        return items

    def get_session(self) -> Any:
        return self._nx.SessionLocal()

    def get_path_id(self, path: str) -> str | None:
        from sqlalchemy import select

        from nexus.storage.models import FilePathModel

        with self._nx.SessionLocal() as session:
            stmt = select(FilePathModel.path_id).where(
                FilePathModel.virtual_path == path,
                FilePathModel.deleted_at.is_(None),
            )
            path_id: str | None = session.execute(stmt).scalar_one_or_none()
            return path_id

    def get_content_hash(self, path: str) -> str | None:
        from sqlalchemy import select

        from nexus.storage.models import FilePathModel

        with self._nx.SessionLocal() as session:
            stmt = select(FilePathModel.content_hash).where(
                FilePathModel.virtual_path == path,
                FilePathModel.deleted_at.is_(None),
            )
            content_hash: str | None = session.execute(stmt).scalar_one_or_none()
            return content_hash


def _create_wallet_provisioner() -> Any:
    """Create a sync wallet provisioner for NexusFS agent registration.

    Returns a callable ``(agent_id: str, zone_id: str) -> None`` that creates
    a TigerBeetle wallet account. Returns None if tigerbeetle is not installed.

    Uses the sync TigerBeetle client (``tb.Client``) since NexusFS methods are
    synchronous. The client is lazily created on first call and reused.
    Account creation is idempotent (safe to call multiple times).
    """
    import os

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

        from nexus.bricks.pay.constants import (
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
        logger.error("Invalid resiliency config, using defaults: %s", exc)
        return ResiliencyConfig()


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


def _boot_kernel_services(ctx: _BootContext) -> dict[str, Any]:
    """Boot Tier 0 (KERNEL) — mandatory services that are fatal on failure.

    Creates ReBAC, permissions, workspace, syncer, and version services.
    On failure: raises ``BootError`` and logs at CRITICAL.
    Does NOT call ``.start()`` on background threads — that is deferred to
    ``_start_background_services()``.

    Returns:
        Dict with 13 kernel service entries.
    """
    from nexus.core.exceptions import BootError

    t0 = time.perf_counter()
    try:
        # Config-time dialect flag (KERNEL-ARCHITECTURE §7)
        _is_pg = not ctx.db_url.startswith("sqlite")

        # --- ReBAC Manager ---
        from nexus.rebac.manager import EnhancedReBACManager

        rebac_manager = EnhancedReBACManager(
            engine=ctx.engine,
            cache_ttl_seconds=ctx.cache_ttl_seconds or 300,
            max_depth=10,
            enforce_zone_isolation=ctx.perm.enforce_zone_isolation,
            enable_graph_limits=True,
            enable_tiger_cache=ctx.perm.enable_tiger_cache,
            read_engine=ctx.read_engine,
            is_postgresql=_is_pg,
        )

        # --- Circuit Breaker for ReBAC DB Resilience (Issue #726) ---
        from nexus.rebac.circuit_breaker import AsyncCircuitBreaker, CircuitBreakerConfig

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
        from nexus.rebac.cache.visibility import DirectoryVisibilityCache

        dir_visibility_cache = DirectoryVisibilityCache(
            tiger_cache=getattr(rebac_manager, "_tiger_cache", None),
            ttl=ctx.cache_ttl_seconds or 300,
            max_entries=10000,
        )

        # Wire: rebac invalidation -> dir visibility cache
        rebac_manager.register_dir_visibility_invalidator(
            "nexusfs",
            lambda zone_id, path: dir_visibility_cache.invalidate_for_resource(path, zone_id),
        )

        # --- Audit Store ---
        from nexus.rebac.permissions_enhanced import AuditStore

        audit_store = AuditStore(engine=ctx.engine, is_postgresql=_is_pg)

        # --- Entity Registry ---
        from nexus.rebac.entity_registry import EntityRegistry

        entity_registry = EntityRegistry(ctx.session_factory)

        # --- Permission Enforcer ---
        from nexus.rebac.enforcer import PermissionEnforcer

        permission_enforcer = PermissionEnforcer(
            metadata_store=ctx.metadata_store,
            rebac_manager=rebac_manager,
            allow_admin_bypass=ctx.perm.allow_admin_bypass,
            allow_system_bypass=True,
            audit_store=audit_store,
            admin_bypass_paths=[],
            router=ctx.router,
            entity_registry=entity_registry,
        )

        # --- Hierarchy Manager ---
        from nexus.rebac.hierarchy_manager import HierarchyManager

        hierarchy_manager = HierarchyManager(
            rebac_manager=rebac_manager,
            enable_inheritance=ctx.perm.inherit,
        )

        # --- Deferred Permission Buffer (constructed, NOT started) ---
        from nexus.rebac.deferred_permission_buffer import DeferredPermissionBuffer

        deferred_permission_buffer = None
        if ctx.perm.enable_deferred:
            deferred_permission_buffer = DeferredPermissionBuffer(
                rebac_manager=rebac_manager,
                hierarchy_manager=hierarchy_manager,
                flush_interval_sec=ctx.perm.deferred_flush_interval,
            )

        # --- Workspace Registry ---
        from nexus.services.workspace.workspace_registry import WorkspaceRegistry

        workspace_registry = WorkspaceRegistry(
            metadata=ctx.metadata_store,
            rebac_manager=rebac_manager,
            session_factory=ctx.session_factory,
        )

        # --- Mount Manager ---
        from nexus.services.mount_manager import MountManager

        mount_manager = MountManager(ctx.record_store)

        # --- Workspace Manager ---
        from nexus.services.workspace_manager import WorkspaceManager

        workspace_manager = WorkspaceManager(
            metadata=ctx.metadata_store,
            backend=ctx.backend,
            rebac_manager=rebac_manager,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
            session_factory=ctx.session_factory,
        )

        # --- RecordStore Syncer (constructed, NOT started) ---
        import os

        write_observer: Any = None
        use_buffer = ctx.enable_write_buffer
        if use_buffer is None:
            env_val = os.environ.get("NEXUS_ENABLE_WRITE_BUFFER", "").lower()
            if env_val in ("true", "1", "yes"):
                use_buffer = True
            elif env_val in ("false", "0", "no"):
                use_buffer = False
            else:
                use_buffer = ctx.db_url.startswith(("postgres", "postgresql"))

        if use_buffer:
            from nexus.storage.record_store_syncer import BufferedRecordStoreWriteObserver

            _st = ctx.profile_tuning.storage
            write_observer = BufferedRecordStoreWriteObserver(
                ctx.session_factory,
                strict_mode=ctx.perm.audit_strict_mode,
                flush_interval_ms=_st.write_buffer_flush_ms,
                max_buffer_size=_st.write_buffer_max_size,
            )
        else:
            from nexus.storage.record_store_syncer import RecordStoreWriteObserver

            write_observer = RecordStoreWriteObserver(
                ctx.session_factory,
                strict_mode=ctx.perm.audit_strict_mode,
            )

        # --- VersionService (Task #45) ---
        from nexus.services.version_service import VersionService

        version_service = VersionService(
            metadata_store=ctx.metadata_store,
            cas_store=ctx.backend,
            router=ctx.router,
            enforce_permissions=False,
            session_factory=ctx.session_factory,
        )

        result = {
            "rebac_manager": rebac_manager,
            "rebac_circuit_breaker": rebac_circuit_breaker,
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
        }

        elapsed = time.perf_counter() - t0
        logger.info("[BOOT:KERNEL] %d services ready (%.3fs)", len(result), elapsed)
        return result

    except Exception as exc:
        logger.critical("[BOOT:KERNEL] Fatal: %s", exc)
        raise BootError(str(exc), tier="kernel") from exc


def _boot_system_services(ctx: _BootContext, kernel: dict[str, Any]) -> dict[str, Any]:
    """Boot Tier 1 (SYSTEM) — degraded-mode on failure.

    Creates AgentRegistry, NamespaceManager, AsyncVFSRouter,
    EventDeliveryWorker, ObservabilitySubsystem, ResiliencyManager.
    On failure: logs WARNING, sets that service to None.

    Returns:
        Dict with 8 system service entries (some may be None).
    """
    t0 = time.perf_counter()

    # --- Agent Registry (Issue #1502) ---
    agent_registry: Any = None
    async_agent_registry: Any = None
    if ctx.session_factory is not None:
        try:
            from nexus.services.agents.agent_registry import AgentRegistry
            from nexus.services.agents.async_agent_registry import AsyncAgentRegistry

            agent_registry = AgentRegistry(
                session_factory=ctx.session_factory,
                entity_registry=kernel["entity_registry"],
                flush_interval=60,
            )
            async_agent_registry = AsyncAgentRegistry(agent_registry)
            logger.debug("[BOOT:SYSTEM] AgentRegistry + AsyncAgentRegistry created")
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] AgentRegistry unavailable: %s", exc)

    # --- Namespace Manager (Issue #1502) ---
    namespace_manager: Any = None
    async_namespace_manager: Any = None
    try:
        from nexus.rebac.async_namespace_manager import AsyncNamespaceManager
        from nexus.rebac.namespace_factory import (
            create_namespace_manager as _create_ns_manager,
        )

        namespace_manager = _create_ns_manager(
            rebac_manager=kernel["rebac_manager"],
            record_store=ctx.record_store,
        )
        async_namespace_manager = AsyncNamespaceManager(namespace_manager)
        logger.debug("[BOOT:SYSTEM] NamespaceManager + AsyncNamespaceManager created")
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] NamespaceManager unavailable: %s", exc)

    # --- Async VFS Router (Issue #1502) ---
    async_vfs_router: Any = None
    try:
        from nexus.services.routing.async_router import AsyncVFSRouter

        async_vfs_router = AsyncVFSRouter(ctx.router)
        logger.debug("[BOOT:SYSTEM] AsyncVFSRouter created")
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] AsyncVFSRouter unavailable: %s", exc)

    # --- Event Delivery Worker (Issue #1241, constructed, NOT started) ---
    delivery_worker = None
    if ctx.db_url.startswith(("postgres", "postgresql")):
        try:
            from nexus.services.event_log.delivery_worker import EventDeliveryWorker

            delivery_worker = EventDeliveryWorker(
                session_factory=ctx.session_factory,
                poll_interval_ms=200,
                batch_size=50,
                use_row_locking=True,
            )
        except Exception as exc:
            logger.warning("[BOOT:SYSTEM] EventDeliveryWorker unavailable: %s", exc)

    # --- Observability Subsystem (Issue #1301) ---
    observability_subsystem: Any = None
    try:
        from nexus.core.config import ObservabilityConfig
        from nexus.services.subsystems.observability_subsystem import ObservabilitySubsystem

        # Instrument both primary and replica pools (Issue #725)
        obs_engines = [ctx.engine]
        if ctx.record_store.has_read_replica:
            obs_engines.append(ctx.read_engine)
        observability_subsystem = ObservabilitySubsystem(
            config=ObservabilityConfig(),
            engines=obs_engines,
        )
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] ObservabilitySubsystem unavailable: %s", exc)

    # --- Resiliency Subsystem (Issue #1366) ---
    resiliency_manager: Any = None
    try:
        from nexus.core.resiliency import ResiliencyManager, set_default_manager

        resiliency_config = _parse_resiliency_config(ctx.resiliency_raw)
        resiliency_manager = ResiliencyManager(config=resiliency_config)
        set_default_manager(resiliency_manager)
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] ResiliencyManager unavailable: %s", exc)

    # --- Context Branch Service (Issue #1315) ---
    context_branch_service: Any = None
    try:
        from nexus.services.context_branch import ContextBranchService

        context_branch_service = ContextBranchService(
            workspace_manager=kernel["workspace_manager"],
            session_factory=ctx.session_factory,
            rebac_manager=kernel["rebac_manager"],
            default_zone_id=ctx.zone_id,
            default_agent_id=ctx.agent_id,
        )
        logger.debug("[BOOT:SYSTEM] ContextBranchService created")
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] ContextBranchService unavailable: %s", exc)

    # --- Hook Engine chain: PluginHooks → AsyncHookEngine → ScopedHookEngine (Issue #1257) ---
    scoped_hook_engine: Any = None
    try:
        from nexus.plugins.async_hooks import AsyncHookEngine
        from nexus.plugins.hooks import PluginHooks
        from nexus.services.hook_engine import ScopedHookEngine

        plugin_hooks = PluginHooks()
        async_hook_engine = AsyncHookEngine(inner=plugin_hooks)
        scoped_hook_engine = ScopedHookEngine(inner=async_hook_engine)
        logger.debug("[BOOT:SYSTEM] ScopedHookEngine created")
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] ScopedHookEngine unavailable: %s", exc)

    # --- Brick Lifecycle Manager (Issue #1704) ---
    brick_lifecycle_manager: Any = None
    try:
        from nexus.services.brick_lifecycle import BrickLifecycleManager

        brick_lifecycle_manager = BrickLifecycleManager(hook_engine=scoped_hook_engine)
        logger.debug(
            "[BOOT:SYSTEM] BrickLifecycleManager created (hook_engine=%s)",
            "enabled" if scoped_hook_engine else "disabled",
        )
    except Exception as exc:
        logger.warning("[BOOT:SYSTEM] BrickLifecycleManager unavailable: %s", exc)

    # TODO: EventLog, Hook, Scheduler services (not yet implemented)

    result = {
        "agent_registry": agent_registry,
        "async_agent_registry": async_agent_registry,
        "namespace_manager": namespace_manager,
        "async_namespace_manager": async_namespace_manager,
        "async_vfs_router": async_vfs_router,
        "delivery_worker": delivery_worker,
        "observability_subsystem": observability_subsystem,
        "resiliency_manager": resiliency_manager,
        "context_branch_service": context_branch_service,
        "brick_lifecycle_manager": brick_lifecycle_manager,
        "scoped_hook_engine": scoped_hook_engine,
    }

    elapsed = time.perf_counter() - t0
    active = sum(1 for v in result.values() if v is not None)
    logger.info("[BOOT:SYSTEM] %d/%d services ready (%.3fs)", active, len(result), elapsed)
    return result


def _resolve_tasks_db_path(backend: Any) -> str:
    """Resolve the fjall database path for TaskQueueService.

    Priority:
    1. NEXUS_TASKS_DB_PATH environment variable
    2. NEXUS_DATA_DIR/tasks-db
    3. backend.root_path/../tasks-db (alongside backend storage)
    4. .nexus-data/tasks-db (fallback)
    """
    import os

    env_path = os.environ.get("NEXUS_TASKS_DB_PATH")
    if env_path:
        return env_path

    data_dir = os.environ.get("NEXUS_DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, "tasks-db")

    root_path = getattr(backend, "root_path", None)
    if root_path is not None:
        return os.path.join(str(root_path), "tasks-db")

    return os.path.join(".nexus-data", "tasks-db")


def _boot_brick_services(ctx: _BootContext, kernel: dict[str, Any]) -> dict[str, Any]:
    """Boot Tier 2 (BRICK) — optional, silent on failure.

    Creates Search/Zoekt wiring, Wallet, Manifest, ToolNamespace,
    ChunkedUpload, Distributed infra, Workflow engine, API key creator.
    On failure: logs DEBUG, sets that service to None.

    Returns:
        Dict with 9 brick service entries (some may be None).
    """
    t0 = time.perf_counter()

    # --- Search Brick Import Validation (Issue #1520) ---
    try:
        from nexus.search.manifest import verify_imports as _verify_search

        _search_status = _verify_search()
        logger.debug("[BOOT:BRICK] Search brick imports: %s", _search_status)
    except ImportError:
        logger.debug("[BOOT:BRICK] Search brick manifest not available")

    # Wire zoekt callbacks into backends (Issue #1520)
    try:
        from nexus.search.zoekt_client import notify_zoekt_sync_complete, notify_zoekt_write

        if hasattr(ctx.backend, "on_write_callback") and ctx.backend.on_write_callback is None:
            ctx.backend.on_write_callback = notify_zoekt_write
        if hasattr(ctx.backend, "on_sync_callback") and ctx.backend.on_sync_callback is None:
            ctx.backend.on_sync_callback = notify_zoekt_sync_complete
    except ImportError:
        logger.debug("[BOOT:BRICK] Zoekt not available, skipping callback wiring")

    # --- Wallet Provisioner (Issue #1210) ---
    wallet_provisioner = _create_wallet_provisioner()

    # --- Manifest Resolver (Issue #1427, #1428) ---
    manifest_resolver: Any = None
    manifest_metrics: Any = None
    try:
        from nexus.bricks.context_manifest import ManifestResolver
        from nexus.bricks.context_manifest.executors.file_glob import FileGlobExecutor
        from nexus.bricks.context_manifest.metrics import (
            ManifestMetricsConfig,
            ManifestMetricsObserver,
        )

        executors: dict[str, Any] = {}
        root_path = getattr(ctx.backend, "root_path", None)
        if root_path is not None:
            from pathlib import Path

            try:
                executors["file_glob"] = FileGlobExecutor(workspace_root=Path(root_path))
            except TypeError:
                logger.debug("Cannot create FileGlobExecutor: root_path=%r", root_path)

        # WorkspaceSnapshotExecutor (Issue #1428)
        try:
            from nexus.bricks.context_manifest.executors.snapshot_lookup_db import (
                CASManifestReader,
                DatabaseSnapshotLookup,
            )
            from nexus.bricks.context_manifest.executors.workspace_snapshot import (
                WorkspaceSnapshotExecutor,
            )

            snapshot_lookup = DatabaseSnapshotLookup(session_factory=ctx.session_factory)
            cas_reader = CASManifestReader(backend=ctx.backend)
            executors["workspace_snapshot"] = WorkspaceSnapshotExecutor(
                snapshot_lookup=snapshot_lookup,
                manifest_reader=cas_reader,
            )
        except ImportError as _snap_e:
            logger.debug("WorkspaceSnapshotExecutor unavailable: %s", _snap_e)

        import importlib.util

        if importlib.util.find_spec("nexus.bricks.context_manifest.executors.memory_query"):
            logger.debug("MemoryQueryExecutor available for per-agent wiring")
        else:
            logger.debug("MemoryQueryExecutor module not found")

        manifest_metrics = ManifestMetricsObserver(ManifestMetricsConfig())

        manifest_resolver = ManifestResolver(
            executors=executors,
            max_resolve_seconds=5.0,
            metrics_observer=manifest_metrics,
        )
        logger.debug("[BOOT:BRICK] ManifestResolver created with %d executors", len(executors))
    except ImportError as _e:
        logger.debug("[BOOT:BRICK] ManifestResolver unavailable: %s", _e)

    # --- Tool Namespace Middleware (Issue #1272) ---
    tool_namespace_middleware = None
    try:
        from nexus.mcp.middleware import ToolNamespaceMiddleware

        tool_namespace_middleware = ToolNamespaceMiddleware(
            rebac_manager=kernel["rebac_manager"],
            zone_id=ctx.zone_id,
            cache_ttl=ctx.cache_ttl_seconds or 300,
        )
        logger.debug("[BOOT:BRICK] ToolNamespaceMiddleware created (zone_id=%s)", ctx.zone_id)
    except ImportError as _e:
        logger.debug("[BOOT:BRICK] ToolNamespaceMiddleware unavailable: %s", _e)

    # --- Chunked Upload Service (Issue #788) ---
    chunked_upload_service: Any = None
    try:
        import os as _os

        from nexus.services.chunked_upload_service import (
            ChunkedUploadConfig,
            ChunkedUploadService,
        )

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
            session_factory=ctx.session_factory,
            backend=ctx.backend,
            metadata_store=ctx.metadata_store,
            config=ChunkedUploadConfig(**_upload_config_kwargs),
        )
    except Exception as exc:
        logger.debug("[BOOT:BRICK] ChunkedUploadService unavailable: %s", exc)

    # --- Infrastructure: event bus + lock manager ---
    event_bus: Any = None
    lock_manager: Any = None
    if ctx.dist.enable_locks or ctx.dist.enable_events:
        event_bus, lock_manager = _create_distributed_infra(
            ctx.dist,
            ctx.metadata_store,
            ctx.session_factory,
            ctx.dist.coordination_url,
        )

    # --- Workflow engine ---
    workflow_engine: WorkflowProtocol | None = None
    if ctx.dist.enable_workflows:
        # Try to get Rust glob_match for performance (falls back to fnmatch)
        _glob_match_fn: Any = None
        try:
            from nexus.core import glob_fast

            _glob_match_fn = glob_fast.glob_match
        except ImportError:
            pass
        workflow_engine = _create_workflow_engine(ctx.record_store, _glob_match_fn)

    # --- API key creator (Issue #1519, 3A: inject server auth into kernel) ---
    api_key_creator: Any = None
    try:
        from nexus.server.auth.database_key import DatabaseAPIKeyAuth

        api_key_creator = DatabaseAPIKeyAuth
    except ImportError:
        pass  # Server auth not available (e.g. embedded mode)

    # --- TransactionalSnapshotService (Issue #1752) ---
    snapshot_service: Any = None
    try:
        from nexus.services.snapshot.service import TransactionalSnapshotService

        snapshot_service = TransactionalSnapshotService(
            session_factory=ctx.session_factory,
            cas_store=ctx.backend,
            metadata_store=ctx.metadata_store,
        )
    except ImportError as _snap_exc:
        logger.debug("[BOOT:BRICK] TransactionalSnapshotService unavailable: %s", _snap_exc)

    # --- TaskQueueService (Issue #655) ---
    task_queue_service: Any = None
    try:
        from nexus.services.task_queue_service import TaskQueueService

        task_queue_service = TaskQueueService(
            db_path=_resolve_tasks_db_path(ctx.backend),
        )
    except Exception as _tq_exc:
        logger.debug("[BOOT:BRICK] TaskQueueService unavailable: %s", _tq_exc)

    # --- IPC Brick (Issue #1727, LEGO §8: Filesystem-as-IPC) ---
    ipc_storage_driver: Any = None
    ipc_vfs_driver: Any = None
    ipc_provisioner: Any = None
    if ctx.session_factory is not None:
        try:
            from nexus.ipc.driver import IPCVFSDriver
            from nexus.ipc.provisioning import AgentProvisioner
            from nexus.ipc.storage.recordstore_driver import RecordStoreStorageDriver

            ipc_storage_driver = RecordStoreStorageDriver(
                session_factory=ctx.session_factory,
            )

            _ipc_zone = ctx.zone_id or "root"
            ipc_vfs_driver = IPCVFSDriver(
                storage=ipc_storage_driver,
                zone_id=_ipc_zone,
            )

            # Mount at /agents in the PathRouter (higher priority than default /)
            ctx.router.add_mount("/agents", ipc_vfs_driver, priority=10)

            ipc_provisioner = AgentProvisioner(
                storage=ipc_storage_driver,
                zone_id=_ipc_zone,
            )
            logger.debug(
                "[BOOT:BRICK] IPC brick created (zone=%s, storage=RecordStoreStorageDriver)",
                _ipc_zone,
            )
        except Exception as _ipc_exc:
            logger.warning("[BOOT:BRICK] IPC brick unavailable: %s", _ipc_exc)

    result = {
        "wallet_provisioner": wallet_provisioner,
        "manifest_resolver": manifest_resolver,
        "manifest_metrics": manifest_metrics,
        "tool_namespace_middleware": tool_namespace_middleware,
        "chunked_upload_service": chunked_upload_service,
        "event_bus": event_bus,
        "lock_manager": lock_manager,
        "workflow_engine": workflow_engine,
        "api_key_creator": api_key_creator,
        "snapshot_service": snapshot_service,
        "task_queue_service": task_queue_service,
        "ipc_storage_driver": ipc_storage_driver,
        "ipc_vfs_driver": ipc_vfs_driver,
        "ipc_provisioner": ipc_provisioner,
    }

    elapsed = time.perf_counter() - t0
    active = sum(1 for v in result.values() if v is not None)
    logger.info("[BOOT:BRICK] %d/%d services ready (%.3fs)", active, len(result), elapsed)
    return result


def _start_background_services(kernel: dict[str, Any], system: dict[str, Any]) -> None:
    """Start background threads after all tiers are constructed.

    Deferred from tier construction so that all services are wired before
    any background I/O begins.
    """
    # Deferred Permission Buffer (kernel tier)
    dpb = kernel.get("deferred_permission_buffer")
    if dpb is not None and hasattr(dpb, "start"):
        dpb.start()
        logger.debug("[BOOT:BG] DeferredPermissionBuffer started")

    # Write Observer — only BufferedRecordStoreWriteObserver needs .start()
    wo = kernel.get("write_observer")
    if wo is not None and hasattr(wo, "start"):
        from nexus.storage.record_store_syncer import BufferedRecordStoreWriteObserver

        if isinstance(wo, BufferedRecordStoreWriteObserver):
            wo.start()
            logger.debug("[BOOT:BG] BufferedRecordStoreWriteObserver started")

    # Event Delivery Worker (system tier)
    dw = system.get("delivery_worker")
    if dw is not None and hasattr(dw, "start"):
        dw.start()
        logger.debug("[BOOT:BG] EventDeliveryWorker started")


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
) -> tuple[KernelServices, SystemServices, BrickServices]:
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
        Tuple of (KernelServices, SystemServices, BrickServices).

    .. versionchanged:: Issue #2034
        Returns a 3-tuple instead of a single KernelServices.
    """
    import logging as _factory_logging

    _factory_log = _factory_logging.getLogger(__name__)

    from nexus.core.config import BrickServices as _BrickServices
    from nexus.core.config import CacheConfig as _CacheConfig
    from nexus.core.config import DistributedConfig as _DistributedConfig
    from nexus.core.config import KernelServices as _KernelServices
    from nexus.core.config import PermissionConfig as _PermissionConfig
    from nexus.core.config import SystemServices as _SystemServices

    # --- Profile-based brick gating (Issue #1389) ---
    from nexus.core.deployment_profile import DeploymentProfile

    if enabled_bricks is None:
        enabled_bricks = DeploymentProfile.FULL.default_bricks()

    def _brick_on(name: str) -> bool:
        return name in enabled_bricks

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
    kernel_dict = _boot_kernel_services(ctx)

    # --- Tier 1: SYSTEM (degraded on failure) ---
    system_dict = _boot_system_services(ctx, kernel_dict)

    # --- Tier 2: BRICK (optional) ---
    brick_dict = _boot_brick_services(ctx, kernel_dict)

    # --- Start background threads post-construction ---
    _start_background_services(kernel_dict, system_dict)

    # --- Assemble 3-tier containers (Issue #2034) ---
    kernel_services = _KernelServices(
        router=router,
        rebac_manager=kernel_dict["rebac_manager"],
        dir_visibility_cache=kernel_dict["dir_visibility_cache"],
        audit_store=kernel_dict["audit_store"],
        entity_registry=kernel_dict["entity_registry"],
        permission_enforcer=kernel_dict["permission_enforcer"],
        hierarchy_manager=kernel_dict["hierarchy_manager"],
        deferred_permission_buffer=kernel_dict["deferred_permission_buffer"],
        workspace_registry=kernel_dict["workspace_registry"],
        mount_manager=kernel_dict["mount_manager"],
        workspace_manager=kernel_dict["workspace_manager"],
        write_observer=kernel_dict["write_observer"],
        version_service=kernel_dict["version_service"],
    )

    system_services = _SystemServices(
        agent_registry=system_dict["agent_registry"],
        async_agent_registry=system_dict["async_agent_registry"],
        namespace_manager=system_dict["namespace_manager"],
        async_namespace_manager=system_dict["async_namespace_manager"],
        context_branch_service=system_dict.get("context_branch_service"),
        scoped_hook_engine=system_dict.get("scoped_hook_engine"),
        brick_lifecycle_manager=system_dict.get("brick_lifecycle_manager"),
        delivery_worker=system_dict["delivery_worker"],
        observability_subsystem=system_dict["observability_subsystem"],
        resiliency_manager=system_dict["resiliency_manager"],
    )

    brick_services = _BrickServices(
        event_bus=brick_dict["event_bus"],
        lock_manager=brick_dict["lock_manager"],
        workflow_engine=brick_dict["workflow_engine"],
        rebac_circuit_breaker=kernel_dict["rebac_circuit_breaker"],
        wallet_provisioner=brick_dict["wallet_provisioner"],
        chunked_upload_service=brick_dict["chunked_upload_service"],
        manifest_resolver=brick_dict["manifest_resolver"],
        tool_namespace_middleware=brick_dict["tool_namespace_middleware"],
        api_key_creator=brick_dict["api_key_creator"],
        snapshot_service=brick_dict["snapshot_service"],
        task_queue_service=brick_dict["task_queue_service"],
        # IPC Brick (Issue #1727, LEGO §8)
        ipc_storage_driver=brick_dict["ipc_storage_driver"],
        ipc_vfs_driver=brick_dict["ipc_vfs_driver"],
        ipc_provisioner=brick_dict["ipc_provisioner"],
    )

    return kernel_services, system_services, brick_services


def _create_distributed_infra(
    dist: DistributedConfig,
    metadata_store: MetastoreABC,
    session_factory: Any,
    coordination_url: str | None,
) -> tuple[Any, Any]:
    """Create event bus and lock manager (was NexusFS.__init__ lines 439-521).

    Returns (event_bus, lock_manager) tuple.
    Either event_bus or lock_manager may be None.
    """
    event_bus: Any = None
    lock_manager: Any = None

    try:
        # Initialize lock manager (uses Raft via metadata store)
        if dist.enable_locks:
            from nexus.core.distributed_lock import LockStoreProtocol
            from nexus.raft.lock_manager import (
                RaftLockManager,
                set_distributed_lock_manager,
            )

            if isinstance(metadata_store, LockStoreProtocol):
                lock_manager = RaftLockManager(metadata_store)
                set_distributed_lock_manager(lock_manager)
                logger.info("Distributed lock manager initialized (Raft consensus)")
            else:
                logger.warning(
                    "Distributed locks require LockStoreProtocol-compatible store, got %s. "
                    "Lock manager will not be initialized.",
                    type(metadata_store).__name__,
                )

        # Initialize event bus
        if dist.event_bus_backend == "nats":
            from nexus.services.event_bus.factory import create_event_bus

            event_bus = create_event_bus(
                backend="nats",
                nats_url=dist.nats_url,
                session_factory=session_factory,
            )
            logger.info(
                "Distributed event bus initialized (NATS JetStream: %s, SSOT: PostgreSQL)",
                dist.nats_url,
            )
        elif dist.enable_events:
            import os

            coordination_url_resolved = coordination_url or os.getenv("NEXUS_REDIS_URL")
            event_url_resolved = coordination_url_resolved or os.getenv("NEXUS_DRAGONFLY_URL")
            if event_url_resolved:
                from nexus.cache.dragonfly import DragonflyClient
                from nexus.services.event_bus.redis import RedisEventBus

                event_client = DragonflyClient(url=event_url_resolved)
                event_bus = RedisEventBus(
                    event_client,
                    session_factory=session_factory,
                )
                logger.info(
                    "Distributed event bus initialized (dragonfly: %s, SSOT: PostgreSQL)",
                    event_url_resolved,
                )
    except ImportError as e:
        logger.warning("Could not initialize distributed event system: %s", e)

    return event_bus, lock_manager


def _create_workflow_engine(
    record_store: Any, glob_match_fn: Any = None
) -> WorkflowProtocol | None:
    """Create workflow engine with async store and DI.

    Args:
        record_store: RecordStoreABC instance (has async_session_factory property).
        glob_match_fn: Optional glob match function (Rust glob_fast in production).

    Returns workflow engine or None if unavailable.
    """
    if record_store is None:
        logger.warning("Workflows require record_store, skipping")
        return None
    try:
        from nexus.bricks.workflows.engine import WorkflowEngine
        from nexus.bricks.workflows.protocol import WorkflowServices
        from nexus.bricks.workflows.storage import WorkflowStore
        from nexus.constants import ROOT_ZONE_ID
        from nexus.storage.models import WorkflowExecutionModel, WorkflowModel

        workflow_store = WorkflowStore(
            session_factory=record_store.async_session_factory,
            workflow_model=WorkflowModel,
            execution_model=WorkflowExecutionModel,
            zone_id=ROOT_ZONE_ID,
        )
        services = WorkflowServices(glob_match=glob_match_fn)
        return WorkflowEngine(workflow_store=workflow_store, services=services)
    except Exception as e:
        logger.warning("Failed to create workflow engine: %s", e)
        return None


def _create_provider_registry(parsing: Any) -> Any:
    """Create ProviderRegistry with auto-discovered providers (Issue #657)."""
    from nexus.parsers.providers import ProviderRegistry
    from nexus.parsers.providers.base import ProviderConfig

    registry = ProviderRegistry()
    if parsing is None:
        registry.auto_discover()
        return registry
    parse_providers = [dict(p) for p in parsing.providers] if parsing.providers else None
    if parse_providers:
        configs = [
            ProviderConfig(
                name=p.get("name", "unknown"),
                enabled=p.get("enabled", True),
                priority=p.get("priority", 50),
                api_key=p.get("api_key"),
                api_url=p.get("api_url"),
                supported_formats=p.get("supported_formats"),
            )
            for p in parse_providers
        ]
        registry.auto_discover(configs)
    else:
        registry.auto_discover()
    return registry


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
    kernel_services: KernelServices | None = None,
    system_services: SystemServices | None = None,
    brick_services: BrickServices | None = None,
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
        kernel_services: Pre-built KernelServices (skips create_nexus_services).
        system_services: Pre-built SystemServices.
        brick_services: Pre-built BrickServices.
        enable_write_buffer: Use async WriteBuffer for PG sync.
        enabled_bricks: Set of brick names to enable.
        zone_id: Default zone ID (for WorkspaceManager, embedded mode).
        agent_id: Default agent ID (for WorkspaceManager, embedded mode).
        workflow_engine: Pre-built workflow engine override.

    Returns:
        Fully configured NexusFS instance with services injected.

    .. versionchanged:: Issue #2034
        ``services`` param replaced by ``kernel_services``, ``system_services``,
        ``brick_services`` (3-tier split).
    """
    from nexus.core.config import BrickServices as _BrickServices
    from nexus.core.config import (
        DistributedConfig as _DistributedConfig,
    )
    from nexus.core.config import KernelServices as _KernelServices
    from nexus.core.config import SystemServices as _SystemServices
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
    if kernel_services is None and record_store is not None:
        kernel_services, system_services, brick_services = create_nexus_services(
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
    elif kernel_services is None:
        kernel_services = _KernelServices(router=router)
    else:
        # Use provided services but ensure router is set (frozen — use replace)
        if kernel_services.router is None:
            from dataclasses import replace as _dc_replace

            kernel_services = _dc_replace(kernel_services, router=router)

    # Default system/brick to empty containers when not provided
    if system_services is None:
        system_services = _SystemServices()
    if brick_services is None:
        brick_services = _BrickServices()

    from dataclasses import replace as _dc_replace

    # Inject workflow_engine override if provided directly (frozen — use replace)
    if workflow_engine is not None:
        brick_services = _dc_replace(brick_services, workflow_engine=workflow_engine)

    # Create ParsersBrick — owns both registries (Issue #1523)
    from nexus.parsers.brick import ParsersBrick

    parsers_brick = ParsersBrick(parsing_config=parsing)
    _parse_fn = parsers_brick.create_parse_fn()

    # Create CacheBrick — owns all cache domain services (Issue #1524)
    from nexus.bricks.cache.brick import CacheBrick

    _cache_brick = CacheBrick(
        cache_store=cache_store,
        record_store=record_store,
    )
    brick_services = _dc_replace(brick_services, cache_brick=_cache_brick)

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
        kernel_services=kernel_services,
        system_services=system_services,
        brick_services=brick_services,
        parse_fn=_parse_fn,
        content_cache=_content_cache,
        parser_registry=parsers_brick.parser_registry,
        provider_registry=parsers_brick.provider_registry,
        vfs_lock_manager=_vfs_lock_manager,
    )

    return nx
