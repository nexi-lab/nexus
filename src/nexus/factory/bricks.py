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
        "skill_service": skill_service,
        "skill_package_service": skill_package_service,
    }

    elapsed = time.perf_counter() - t0
    active = sum(1 for v in result.values() if v is not None)
    logger.info("[BOOT:BRICK] %d/%d services ready (%.3fs)", active, len(result), elapsed)
    return result
