"""Boot Tier 2 (BRICK) — optional, silent on failure.

Includes brick auto-discovery via ``brick_factory.py`` convention.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.factory._boot_context import _BootContext
from nexus.factory._helpers import _make_gate, _safe_create

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Brick auto-discovery (Issue #2180)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrickFactoryDescriptor:
    """Descriptor for a discoverable brick factory."""

    name: str | None  # Profile gate name (None = always enabled)
    result_key: str
    create_fn: Callable[..., Any]
    manifest: Any | None = None  # Optional BrickManifest instance


def _discover_brick_factories(tier: str = "independent") -> list[BrickFactoryDescriptor]:
    """Scan ``nexus/bricks/*/brick_factory.py`` for factory functions.

    Each discoverable brick provides a ``brick_factory.py`` module with:
    - ``BRICK_NAME``: Maps to deployment profile gate name (None = always on)
    - ``TIER``: ``"independent"`` or ``"dependent"``
    - ``RESULT_KEY``: Key in the result dict
    - ``MANIFEST``: Optional ``BrickManifest`` instance for import verification
    - ``create(ctx, system) -> Any``: Factory function
    """
    import importlib
    import pkgutil

    factories: list[BrickFactoryDescriptor] = []
    try:
        bricks_pkg = importlib.import_module("nexus.bricks")
    except ImportError:
        return factories

    for _, name, is_pkg in pkgutil.iter_modules(bricks_pkg.__path__):
        if not is_pkg:
            continue
        factory_module_name = f"nexus.bricks.{name}.brick_factory"
        try:
            mod = importlib.import_module(factory_module_name)
        except ImportError:
            continue  # Brick has no factory — skip

        if getattr(mod, "TIER", "independent") != tier:
            continue

        factories.append(
            BrickFactoryDescriptor(
                name=getattr(mod, "BRICK_NAME", name),
                result_key=mod.RESULT_KEY,
                create_fn=mod.create,
                manifest=getattr(mod, "MANIFEST", None),
            )
        )

    return factories


# ---------------------------------------------------------------------------
# Brick boot function
# ---------------------------------------------------------------------------


def _boot_independent_bricks(
    ctx: _BootContext,
    system: dict[str, Any],
    svc_on: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Boot Tier 2 (BRICK) — optional, silent on failure.

    Creates Search/Zoekt wiring, Wallet, Manifest, ToolNamespace,
    ChunkedUpload, Distributed infra, Workflow engine, API key creator.
    On failure: logs DEBUG, sets that service to None.

    Args:
        ctx: Boot context with shared dependencies.
        system: Services dict (provides rebac_manager,
            entity_registry, etc.).
        svc_on: Callable ``(name: str) -> bool`` for profile-based gating.
            When None, all bricks are enabled (backward-compatible default).

    Returns:
        Dict with service entries (some may be None).
    """
    t0 = time.perf_counter()
    _on = _make_gate(svc_on)

    # === Auto-discovered bricks ===
    auto_results: dict[str, Any] = {}
    for desc in _discover_brick_factories("independent"):
        # Manifest pre-check: verify imports once, skip if required modules missing
        _manifest_status: dict[str, bool] | None = None
        if desc.manifest is not None:
            _manifest_status = desc.manifest.verify_imports()
            assert _manifest_status is not None  # Set on line above
            _required_ok = all(
                _manifest_status.get(mod, False) for mod in desc.manifest.required_modules
            )
            if not _required_ok:
                logger.debug(
                    "[BOOT:BRICK] Skipping %s — required modules missing",
                    desc.result_key,
                )
                continue

        if desc.name is None:
            # No profile gate — always create
            auto_results[desc.result_key] = _safe_create(
                desc.result_key,
                lambda d=desc: d.create_fn(ctx, system),  # type: ignore[misc]
                lambda _name: True,  # always enabled
            )
        else:
            assert desc.name is not None
            auto_results[desc.result_key] = _safe_create(
                desc.name,
                lambda d=desc: d.create_fn(ctx, system),  # type: ignore[misc]
                _on,
            )

        # Log cached manifest status for successfully created bricks
        if (
            _manifest_status is not None
            and auto_results.get(desc.result_key) is not None
            and logger.isEnabledFor(logging.DEBUG)
        ):
            logger.debug(
                "[BOOT:BRICK] %s manifest: %s",
                desc.result_key,
                _manifest_status,
            )

    # === Manually-wired bricks (complex conditional logic) ===

    zoekt_write_observer: Any = None  # Issue #810: OBSERVE-phase Zoekt observer
    task_dispatch_consumer: Any = None  # Task Manager: DT_PIPE lifecycle consumer

    # --- Search Brick Import Validation (Issue #1520) ---
    if _on("search"):
        try:
            from nexus.bricks.search.manifest import verify_imports as _verify_search

            _search_status = _verify_search()
            logger.debug("[BOOT:BRICK] Search brick imports: %s", _search_status)
        except ImportError:
            logger.debug("[BOOT:BRICK] Search brick manifest not available")

        # Wire zoekt callbacks into backends (Issue #1520, #2188: DI via factory)
        # Issue #810: Route through ZoektWriteObserver (OBSERVE phase, non-blocking).
        try:
            from nexus.bricks.search.config import search_config_from_env
            from nexus.bricks.search.zoekt_client import ZoektIndexManager

            _search_cfg = search_config_from_env()
            if _search_cfg.zoekt_enabled:
                _zoekt_index_mgr = ZoektIndexManager(
                    index_dir=_search_cfg.zoekt_index_dir,
                    data_dir=_search_cfg.zoekt_data_dir,
                    debounce_seconds=_search_cfg.zoekt_debounce_seconds,
                    enabled=True,
                    index_binary=_search_cfg.zoekt_index_binary,
                )
                # Wrap in ZoektWriteObserver for OBSERVE-phase dispatch (#810)
                from nexus.factory.zoekt_observer import ZoektWriteObserver

                _zoekt_observer = ZoektWriteObserver(_zoekt_index_mgr)
                zoekt_write_observer = _zoekt_observer

                if (
                    hasattr(ctx.backend, "on_write_callback")
                    and ctx.backend.on_write_callback is None
                ):
                    ctx.backend.on_write_callback = _zoekt_observer.notify_write
                if (
                    hasattr(ctx.backend, "on_sync_callback")
                    and ctx.backend.on_sync_callback is None
                ):
                    ctx.backend.on_sync_callback = _zoekt_observer.notify_sync_complete
        except ImportError:
            logger.debug("[BOOT:BRICK] Zoekt not available, skipping callback wiring")
    else:
        logger.debug("[BOOT:BRICK] Search brick disabled by profile")

    # --- Task Manager Brick ---
    if _on("task_manager"):
        try:
            from nexus.task_manager.dispatch_consumer import TaskDispatchPipeConsumer

            task_dispatch_consumer = TaskDispatchPipeConsumer(
                acp_service=system.get("acp_service"),
                agent_registry=system.get("agent_registry"),
            )
            # TaskManagerService and TaskWriteHook are registered in _register_vfs_hooks
            # (needs NexusFS reference); consumer stored here for lifespan startup.
            logger.debug("[BOOT:BRICK] TaskDispatchPipeConsumer created")
        except Exception as exc:
            logger.warning("[BOOT:BRICK] task_manager unavailable: %s", exc)
    else:
        logger.debug("[BOOT:BRICK] task_manager brick disabled by profile")

    # --- Wallet Provisioner (Issue #1210) ---
    wallet_provisioner: Any = None
    if _on("pay"):
        from nexus.factory.wallet import create_wallet_provisioner

        wallet_provisioner = create_wallet_provisioner()
    else:
        logger.debug("[BOOT:BRICK] Pay brick disabled by profile")

    # --- Manifest Resolver (Issue #1427, #1428) ---
    manifest_resolver: Any = None
    manifest_metrics: Any = None
    if _on("skills"):
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
                )
                from nexus.bricks.context_manifest.executors.workspace_snapshot import (
                    WorkspaceSnapshotExecutor,
                )
                from nexus.storage.repositories.snapshot_lookup import DatabaseSnapshotLookup

                snapshot_lookup = DatabaseSnapshotLookup(record_store=ctx.record_store)
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
    else:
        logger.debug("[BOOT:BRICK] MCP/Manifest brick disabled by profile")

    # --- Tool Namespace Middleware (Issue #1272) ---
    tool_namespace_middleware = None
    if _on("mcp"):
        try:
            from nexus.bricks.mcp.middleware import ToolNamespaceMiddleware

            tool_namespace_middleware = ToolNamespaceMiddleware(
                rebac_manager=system["rebac_manager"],
                zone_id=ctx.zone_id,
                cache_ttl=ctx.cache_ttl_seconds or 300,
            )
            logger.debug("[BOOT:BRICK] ToolNamespaceMiddleware created (zone_id=%s)", ctx.zone_id)
        except ImportError as _e:
            logger.debug("[BOOT:BRICK] ToolNamespaceMiddleware unavailable: %s", _e)

    # --- Chunked Upload Service (Issue #788) ---
    chunked_upload_service: Any = None
    if _on("uploads"):
        try:
            import os as _os

            from nexus.bricks.upload.chunked_upload_service import (
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
                record_store=ctx.record_store,
                backend=ctx.backend,
                metadata_store=ctx.metadata_store,
                config=ChunkedUploadConfig(**_upload_config_kwargs),
            )
        except Exception as exc:
            logger.debug("[BOOT:BRICK] ChunkedUploadService unavailable: %s", exc)
    else:
        logger.debug("[BOOT:BRICK] Uploads brick disabled by profile")

    # --- Infrastructure: event bus + lock manager moved to _boot_services() ---

    # --- Workflow engine ---
    workflow_engine: Any = None
    if _on("workflows") and ctx.dist.enable_workflows:
        # Try to get Rust glob_match for performance (falls back to fnmatch)
        _glob_match_fn: Any = None
        try:
            from nexus.bricks.search.primitives import glob_fast

            _glob_match_fn = glob_fast.glob_match
        except ImportError:
            pass

        from nexus.factory._distributed import _create_workflow_engine

        workflow_engine = _create_workflow_engine(ctx.record_store, _glob_match_fn)
    elif not _on("workflows"):
        logger.debug("[BOOT:BRICK] Workflows brick disabled by profile")

    # --- API key creator is now auto-discovered via bricks/auth/brick_factory.py ---
    api_key_creator: Any = auto_results.pop("api_key_creator", None)

    # --- TransactionalSnapshotService (Issue #1752) ---
    snapshot_service: Any = auto_results.pop("snapshot_service", None)
    if snapshot_service is None:
        try:
            from nexus.bricks.snapshot.service import TransactionalSnapshotService
            from nexus.contracts.metadata import FileMetadata

            snapshot_service = TransactionalSnapshotService(
                record_store=ctx.record_store,
                cas_store=ctx.backend,
                metadata_store=ctx.metadata_store,
                metadata_factory=FileMetadata,
            )
        except ImportError as _snap_exc:
            logger.debug("[BOOT:BRICK] TransactionalSnapshotService unavailable: %s", _snap_exc)

    # --- DelegationService (Issue #2131: extracted to bricks) ---
    auto_results.pop("delegation_service", None)
    delegation_service: Any = None
    if ctx.record_store is not None:
        try:
            from nexus.bricks.delegation.service import DelegationService

            delegation_service = DelegationService(
                record_store=ctx.record_store,
                rebac_manager=system["rebac_manager"],
                entity_registry=system.get("entity_registry"),
            )
            logger.debug("[BOOT:BRICK] DelegationService created")
        except Exception as _del_exc:
            logger.debug("[BOOT:BRICK] DelegationService unavailable: %s", _del_exc)

    # --- IPC Brick (Issue #1727, LEGO §8: Filesystem-as-IPC) ---
    # IPC goes through the kernel VFS (NexusFS) directly.
    # A LocalConnector is mounted at /agents for actual file storage.
    # NexusFS reference is injected in _initialize_wired_ipc() after kernel init.
    ipc_provisioner: Any = None
    ipc_zone_id: str | None = None
    if not _on("ipc"):
        logger.debug("[BOOT:BRICK] IPC brick disabled by profile")
    else:
        ipc_zone_id = ctx.zone_id or ROOT_ZONE_ID
        logger.debug(
            "[BOOT:BRICK] IPC brick pre-registered (zone=%s, NexusFS pending)",
            ipc_zone_id,
        )

    # --- Sandbox Brick: AgentEventLog (Issue #1307) ---
    agent_event_log: Any = None
    if ctx.record_store is not None:
        try:
            from nexus.bricks.sandbox.events import AgentEventLog

            agent_event_log = AgentEventLog(record_store=ctx.record_store)
            logger.debug("[BOOT:BRICK] AgentEventLog created")
        except Exception as _ael_exc:
            logger.debug("[BOOT:BRICK] AgentEventLog unavailable: %s", _ael_exc)

    # --- VersionService (Issue #2034: moved from kernel to brick tier) ---
    version_service: Any = None
    try:
        from nexus.bricks.versioning.version_service import VersionService

        version_service = VersionService(
            metadata_store=ctx.metadata_store,
            cas_store=ctx.backend,
            kernel=ctx.kernel,
            dlc=ctx.dlc,
            enforce_permissions=False,
            record_store=ctx.record_store,
        )
        logger.debug("[BOOT:BRICK] VersionService created")
    except Exception as _vs_exc:
        logger.debug("[BOOT:BRICK] VersionService unavailable: %s", _vs_exc)

    # --- Governance Brick (Issue #2129) ---
    governance_anomaly_service: Any = None
    governance_collusion_service: Any = None
    governance_graph_service: Any = None
    governance_response_service: Any = None
    if ctx.record_store is not None:
        try:
            _gov_session_factory = ctx.record_store.async_session_factory

            from nexus.bricks.governance.anomaly_service import AnomalyService
            from nexus.bricks.governance.collusion_service import CollusionService
            from nexus.bricks.governance.governance_graph_service import GovernanceGraphService
            from nexus.bricks.governance.response_service import ResponseService

            governance_anomaly_service = AnomalyService(_gov_session_factory)
            governance_collusion_service = CollusionService(_gov_session_factory)
            governance_graph_service = GovernanceGraphService(_gov_session_factory)
            governance_response_service = ResponseService(
                session_factory=_gov_session_factory,
                anomaly_service=governance_anomaly_service,
                collusion_service=governance_collusion_service,
                graph_service=governance_graph_service,
            )
            logger.debug("[BOOT:BRICK] Governance services created")
        except Exception as _gov_exc:
            logger.debug("[BOOT:BRICK] Governance services unavailable: %s", _gov_exc)

    # --- ReBAC Circuit Breaker (Issue #2034: moved from kernel to brick tier) ---
    rebac_circuit_breaker: Any = None
    try:
        from nexus.bricks.rebac.circuit_breaker import AsyncCircuitBreaker, CircuitBreakerConfig

        _res = ctx.profile_tuning.resiliency
        rebac_circuit_breaker = AsyncCircuitBreaker(
            name="rebac_db",
            config=CircuitBreakerConfig(
                failure_threshold=_res.circuit_breaker_failure_threshold,
                success_threshold=3,
                reset_timeout=_res.circuit_breaker_timeout,
                failure_window=60.0,
            ),
        )
        logger.debug("[BOOT:BRICK] ReBAC circuit breaker created")
    except Exception as _cb_exc:
        logger.warning(
            "[BOOT:BRICK] ReBAC circuit breaker unavailable: %s. "
            "ReBAC will operate without circuit-breaking protection.",
            _cb_exc,
        )

    result = {
        "wallet_provisioner": wallet_provisioner,
        "manifest_resolver": manifest_resolver,
        "manifest_metrics": manifest_metrics,
        "tool_namespace_middleware": tool_namespace_middleware,
        "chunked_upload_service": chunked_upload_service,
        "workflow_engine": workflow_engine,
        "api_key_creator": api_key_creator,
        "snapshot_service": snapshot_service,
        "ipc_provisioner": ipc_provisioner,
        "ipc_zone_id": ipc_zone_id,
        "agent_event_log": agent_event_log,
        "delegation_service": delegation_service,
        "version_service": version_service,
        "rebac_circuit_breaker": rebac_circuit_breaker,
        # Governance Brick (Issue #2129)
        "governance_anomaly_service": governance_anomaly_service,
        "governance_collusion_service": governance_collusion_service,
        "governance_graph_service": governance_graph_service,
        "governance_response_service": governance_response_service,
        # OBSERVE-phase Zoekt observer (Issue #810)
        "zoekt_write_observer": zoekt_write_observer,
        # Task Manager Brick
        "task_dispatch_consumer": task_dispatch_consumer,
    }

    elapsed = time.perf_counter() - t0
    active = sum(1 for v in result.values() if v is not None)
    logger.info("[BOOT:BRICK] %d/%d services ready (%.3fs)", active, len(result), elapsed)
    return result


# ---------------------------------------------------------------------------
# Dependent bricks (Issue #1861) — run after independent bricks
# ---------------------------------------------------------------------------


def _boot_dependent_bricks(
    ctx: _BootContext,
    system: dict[str, Any],
    bricks: dict[str, Any],
) -> None:
    """Boot Tier 2b (DEPENDENT BRICK) — requires services from independent bricks.

    Discovers ``brick_factory.py`` modules with ``TIER="dependent"`` and
    collects their handler callbacks into ``bricks["artifact_observers"]``.

    Cross-brick factories (ToolInfo, GraphStore) are constructed here in the
    factory layer and injected into brick factories to respect LEGO Principle 3.
    """
    # Build cross-brick factories in the factory layer (not inside bricks)
    tool_info_factory: Callable[..., Any] | None = None
    graph_store_factory: Callable[..., Any] | None = None

    try:
        from nexus.bricks.discovery.tool_index import ToolInfo

        tool_info_factory = ToolInfo
    except ImportError:
        logger.debug("[BOOT:BRICK:DEP] ToolInfo not available")

    if ctx.record_store is not None:
        # Removed: txtai handles this (Issue #2663)
        # graph_store module was deleted; graph_store_factory stays None.
        logger.debug("[BOOT:BRICK:DEP] GraphStore not available (deleted, Issue #2663)")

    def _create_dependent(descriptor: BrickFactoryDescriptor) -> Any:
        return descriptor.create_fn(
            ctx,
            system,
            bricks,
            tool_info_factory=tool_info_factory,
            graph_store_factory=graph_store_factory,
        )

    artifact_observers: list[Any] = []

    for desc in _discover_brick_factories("dependent"):
        result = _safe_create(
            desc.result_key,
            partial(_create_dependent, desc),
            lambda _n: True,  # always enabled (no profile gate)
        )
        if result is None:
            continue

        handlers = result.get("handlers", [])
        if not handlers:
            continue

        # Collect handler functions as artifact observers
        for h in handlers:
            artifact_observers.append(h["handler"])

        logger.debug(
            "[BOOT:BRICK:DEP] Collected %d artifact observers from %s",
            len(handlers),
            desc.result_key,
        )

    bricks["artifact_observers"] = artifact_observers
    if artifact_observers:
        logger.debug(
            "[BOOT:BRICK:DEP] Total artifact observers: %d",
            len(artifact_observers),
        )
