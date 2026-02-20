"""Tier 2 (BRICK) boot — optional services, silent on failure."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from nexus.factory.adapters import (
    _create_distributed_infra,
    _create_wallet_provisioner,
    _create_workflow_engine,
)
from nexus.factory.boot_context import _BootContext

if TYPE_CHECKING:
    from nexus.workflows.protocol import WorkflowProtocol

logger = logging.getLogger(__name__)


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
        Dict with 11 brick service entries (some may be None).
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
            from nexus.services.context_manifest.executors.snapshot_lookup_db import (
                CASManifestReader,
                DatabaseSnapshotLookup,
            )
            from nexus.services.context_manifest.executors.workspace_snapshot import (
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

        if importlib.util.find_spec("nexus.services.context_manifest.executors.memory_query"):
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

    # --- ReputationService (Issue #2131: extracted to bricks) ---
    reputation_service: Any = None
    if ctx.session_factory is not None:
        try:
            from nexus.bricks.reputation.reputation_service import ReputationService

            reputation_service = ReputationService(
                session_factory=ctx.session_factory,
            )
            logger.debug("[BOOT:BRICK] ReputationService created")
        except Exception as _rep_exc:
            logger.debug("[BOOT:BRICK] ReputationService unavailable: %s", _rep_exc)

    # --- DelegationService (Issue #2131: extracted to bricks) ---
    delegation_service: Any = None
    if ctx.session_factory is not None:
        try:
            from nexus.bricks.delegation.service import DelegationService

            delegation_service = DelegationService(
                session_factory=ctx.session_factory,
                rebac_manager=kernel["rebac_manager"],
                entity_registry=kernel.get("entity_registry"),
                reputation_service=reputation_service,
            )
            logger.debug("[BOOT:BRICK] DelegationService created")
        except Exception as _del_exc:
            logger.debug("[BOOT:BRICK] DelegationService unavailable: %s", _del_exc)

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
            ipc_provisioner = AgentProvisioner(
                storage=ipc_storage_driver,
                zone_id=_ipc_zone,
            )
            logger.debug("[BOOT:BRICK] IPC brick created (zone=%s)", _ipc_zone)
        except Exception as _ipc_exc:
            logger.debug("[BOOT:BRICK] IPC brick unavailable: %s", _ipc_exc)

    # --- Sandbox Brick: AgentEventLog (Issue #1307) ---
    agent_event_log: Any = None
    if ctx.session_factory is not None:
        try:
            from nexus.bricks.sandbox.events import AgentEventLog

            agent_event_log = AgentEventLog(session_factory=ctx.session_factory)
            logger.debug("[BOOT:BRICK] AgentEventLog created")
        except Exception as _ael_exc:
            logger.debug("[BOOT:BRICK] AgentEventLog unavailable: %s", _ael_exc)

    # --- Circuit Breaker for ReBAC DB Resilience (Issue #726, moved from kernel #2034) ---
    rebac_circuit_breaker: Any = None
    try:
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
    except Exception as _cb_exc:
        logger.warning(
            "[BOOT:BRICK] ReBAC circuit breaker unavailable — "
            "running without circuit-breaking protection: %s",
            _cb_exc,
        )

    # --- VersionService (Issue #2034: moved from kernel to brick tier) ---
    version_service: Any = None
    try:
        from nexus.services.version_service import VersionService

        version_service = VersionService(
            metadata_store=ctx.metadata_store,
            cas_store=ctx.backend,
            router=ctx.router,
            enforce_permissions=False,
            session_factory=ctx.session_factory,
        )
    except Exception as _vs_exc:
        logger.debug("[BOOT:BRICK] VersionService unavailable: %s", _vs_exc)

    # --- Skills Brick (Issue #2035) ---
    # Wired later in NexusFS._wire_services() via gateway adapters.
    # Flagged here for availability tracking.
    skill_service: Any = None
    skill_package_service: Any = None

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
        "agent_event_log": agent_event_log,
        "skill_service": skill_service,
        "skill_package_service": skill_package_service,
        "delegation_service": delegation_service,
        "reputation_service": reputation_service,
        "rebac_circuit_breaker": rebac_circuit_breaker,
        "version_service": version_service,
    }

    elapsed = time.perf_counter() - t0
    active = sum(1 for v in result.values() if v is not None)
    logger.info("[BOOT:BRICK] %d/%d services ready (%.3fs)", active, len(result), elapsed)
    return result


# ---------------------------------------------------------------------------
# Issue #1704: WorkflowEngine lifecycle adapter
# ---------------------------------------------------------------------------


class _WorkflowLifecycleAdapter:
    """Adapter: BrickLifecycleProtocol -> WorkflowEngine.

    WorkflowEngine exposes ``startup()`` but BrickLifecycleManager expects
    ``start()``.  This thin adapter bridges the naming mismatch.
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def start(self) -> None:
        if hasattr(self._engine, "startup"):
            await self._engine.startup()

    async def stop(self) -> None:
        pass  # WorkflowEngine has no explicit shutdown

    async def health_check(self) -> bool:
        if hasattr(self._engine, "health_check"):
            result: bool = await self._engine.health_check()
            return result
        return self._engine is not None


# ---------------------------------------------------------------------------
# Issue #1704: Register factory-created bricks with lifecycle manager
# ---------------------------------------------------------------------------

_FACTORY_BRICKS: list[tuple[str, str]] = [
    ("manifest_resolver", "ManifestProtocol"),
    ("chunked_upload_service", "ChunkedUploadProtocol"),
    ("snapshot_service", "SnapshotProtocol"),
    ("task_queue_service", "TaskQueueProtocol"),
    ("ipc_vfs_driver", "IPCProtocol"),
    ("wallet_provisioner", "WalletProtocol"),
    ("delegation_service", "DelegationProtocol"),
    ("reputation_service", "ReputationProtocol"),
]

# Entries intentionally NOT registered with lifecycle manager.
# CI test ``test_all_brick_dict_keys_accounted_for`` will fail if a new
# key appears in ``_boot_brick_services()`` without being added here or
# to ``_FACTORY_BRICKS``.
_FACTORY_SKIP: frozenset[str] = frozenset(
    {
        "event_bus",  # infrastructure, not a brick
        "lock_manager",  # infrastructure, not a brick
        "api_key_creator",  # class reference, not instance
        "tool_namespace_middleware",  # stateless middleware, no lifecycle
        "manifest_metrics",  # observability helper, not a brick
        "ipc_storage_driver",  # internal to ipc_vfs_driver
        "ipc_provisioner",  # provisioning helper, not a brick
        "skill_service",  # wired later via NexusFS gateway adapters
        "skill_package_service",  # wired later via NexusFS gateway adapters
        "agent_event_log",  # event log, not a lifecycle brick
        "rebac_circuit_breaker",  # infrastructure, not a lifecycle brick
        "version_service",  # wired via BrickServices, lifecycle managed separately
    }
)


def _register_factory_bricks(
    manager: Any,
    brick_dict: dict[str, Any],
) -> None:
    """Register Tier 2 bricks from ``_boot_brick_services()`` with the lifecycle manager.

    Skips infrastructure entries (event_bus, lock_manager, etc.) and None values.
    WorkflowEngine gets a thin adapter since its startup API differs.
    """
    for name, protocol in _FACTORY_BRICKS:
        instance = brick_dict.get(name)
        if instance is not None:
            manager.register(name, instance, protocol_name=protocol)

    # WorkflowEngine needs adapter (startup() != start())
    wf = brick_dict.get("workflow_engine")
    if wf is not None:
        manager.register(
            "workflow_engine",
            _WorkflowLifecycleAdapter(wf),
            protocol_name="WorkflowProtocol",
        )
