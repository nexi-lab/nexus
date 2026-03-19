"""Boot Tier 2b (WIRED) — services needing NexusFS reference."""

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from nexus.core.config import BrickServices, WiredServices
    from nexus.core.router import PathRouter

logger = logging.getLogger(__name__)


async def _boot_wired_services(
    nx: Any,
    router: "PathRouter",
    system_services: Any,
    brick_services: "BrickServices",
    brick_on: Callable[[str], bool] | None = None,
) -> "WiredServices":
    """Boot Tier 2b (WIRED) — services needing NexusFS reference.

    Two-phase init: called AFTER NexusFS construction in ``create_nexus_fs()``.
    ``NexusFSGateway`` breaks the circular dependency between kernel and services.

    Profile gating is applied via ``brick_on`` — same callback used by other tiers.
    Services that fail to construct are set to None (degraded mode).

    Issue #643: Migrated from ``NexusFS._wire_services()`` to factory.py
    so the kernel never imports or creates services.

    Issue #1767: Takes ``router`` directly instead of KernelServices wrapper
    (KernelServices only wrapped the router, which is already on nx.router).

    Args:
        nx: The NexusFS instance (already constructed).
        router: PathRouter (Tier 0 — router only).
        system_services: SystemServices container (Tier 1 — rebac, permissions, etc.).
        brick_services: BrickServices container (Tier 2).
        brick_on: Callable ``(name: str) -> bool`` for profile-based gating.

    Returns:
        WiredServices frozen dataclass (some fields may be None).
    """
    from nexus.core.config import WiredServices as _WiredServices
    from nexus.factory._helpers import _make_gate

    t0 = time.perf_counter()
    _on = _make_gate(brick_on)

    # Resolve the root backend from the router.
    # NexusFS no longer has a .backend attribute — all backends live on the router.
    _root_backend: Any = None
    try:
        _root_backend = nx.router.route("/").backend
    except Exception:
        logger.debug("[BOOT:WIRED] No root backend mounted — services needing backend will degrade")

    # --- NexusFSGateway: adapter breaking circular dep (Issue #1287) ---
    gateway: Any = None
    try:
        from nexus.system_services.gateway import NexusFSGateway

        gateway = NexusFSGateway(nx)
        logger.debug("[BOOT:WIRED] NexusFSGateway created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] NexusFSGateway unavailable: %s", exc)

    # --- IPC KernelVFSAdapter: deferred to initialize() via _initialize_wired_ipc() ---

    # --- ReBACService: Permission and access control operations ---
    rebac_service: Any = None
    try:
        from nexus.bricks.rebac.rebac_service import ReBACService

        rebac_service = ReBACService(
            rebac_manager=system_services.rebac_manager,
            enforce_permissions=nx._enforce_permissions,
            enable_audit_logging=True,
            circuit_breaker=brick_services.rebac_circuit_breaker,
            file_reader=nx.sys_read,
            permission_enforcer=system_services.permission_enforcer,
        )
        logger.debug("[BOOT:WIRED] ReBACService created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] ReBACService unavailable: %s", exc)

    # --- OAuthCredentialService: OAuth credential lifecycle (brick) ---
    # Created before MCPService because mcp_connect() needs credential_service.
    oauth_service: Any = None
    if _on("sandbox"):
        try:
            import os

            from nexus.bricks.auth.oauth.credential_service import OAuthCredentialService

            oauth_service = OAuthCredentialService(
                oauth_factory=None,
                token_manager=None,
                database_url=os.getenv("TOKEN_MANAGER_DB"),
                oauth_config=getattr(nx._config, "oauth", None) if nx._config else None,
            )
            logger.debug("[BOOT:WIRED] OAuthCredentialService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] OAuthCredentialService unavailable: %s", exc)
    else:
        logger.debug("[BOOT:WIRED] OAuthCredentialService disabled by profile")

    # --- MCPService: Model Context Protocol operations ---
    mcp_service: Any = None
    if _on("mcp"):
        try:
            from nexus.bricks.mcp.mcp_service import MCPService

            mcp_service = MCPService(
                filesystem=nx,
                credential_service=oauth_service,
                mount_lister=lambda: [(m.mount_point, "mounted") for m in router.list_mounts()],
            )
            logger.debug("[BOOT:WIRED] MCPService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] MCPService unavailable: %s", exc)
    else:
        logger.debug("[BOOT:WIRED] MCPService disabled by profile")

    # --- SyncService: Sync operations (gateway-dependent) ---
    sync_service: Any = None
    if gateway is not None:
        try:
            from nexus.system_services.sync.sync_service import SyncService

            sync_service = SyncService(gateway)
            logger.debug("[BOOT:WIRED] SyncService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] SyncService unavailable: %s", exc)

    # --- SyncJobService: Sync job management ---
    sync_job_service: Any = None
    if gateway is not None and sync_service is not None:
        try:
            from nexus.system_services.sync.sync_job_service import SyncJobService

            sync_job_service = SyncJobService(gateway, sync_service)
            logger.debug("[BOOT:WIRED] SyncJobService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] SyncJobService unavailable: %s", exc)

    # --- MountPersistService: Mount persistence ---
    # Created with mount_service=None initially; wired after MountService creation below.
    mount_persist_service: Any = None
    if gateway is not None:
        try:
            from nexus.bricks.mount.mount_persist_service import MountPersistService

            mount_persist_service = MountPersistService(
                mount_manager=system_services.mount_manager,
                mount_service=None,  # wired after MountService creation below
                sync_service=sync_service,
            )
            logger.debug("[BOOT:WIRED] MountPersistService created (mount_service pending)")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] MountPersistService unavailable: %s", exc)

    # --- MountService: Dynamic backend mounting operations ---
    # Moved after sub-services so DI deps are available (Issue #636).
    mount_service: Any = None
    try:
        from nexus.bricks.mount.mount_service import MountService

        mount_service = MountService(
            router=router,
            mount_manager=system_services.mount_manager,
            nexus_fs=nx,
            gateway=gateway,
            sync_service=sync_service,
            sync_job_service=sync_job_service,
            mount_persist_service=mount_persist_service,
            oauth_service=oauth_service,
        )
        logger.debug("[BOOT:WIRED] MountService created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] MountService unavailable: %s", exc)

    # Wire MountPersistService -> MountService (break circular dep)
    if mount_persist_service is not None and mount_service is not None:
        mount_persist_service._mounts_ref = mount_service

    # --- SearchService: list/glob/grep are kernel-level VFS operations (Issue #2194) ---
    # Always create SearchService regardless of "search" brick — basic directory
    # listing, glob matching, and grep must work even in KERNEL-only mode.
    # The "search" brick gates advanced features (semantic search, indexing),
    # not core filesystem enumeration.
    search_service: Any = None
    try:
        from nexus.bricks.search.search_service import SearchService

        search_service = SearchService(
            metadata_store=nx.metadata,
            permission_enforcer=system_services.permission_enforcer,
            router=router,
            rebac_manager=system_services.rebac_manager,
            enforce_permissions=getattr(nx, "_enforce_permissions", True),
            default_context=getattr(nx, "_default_context", None),
            record_store=getattr(nx, "_record_store", None),
            gateway=gateway,
        )
        logger.debug("[BOOT:WIRED] SearchService created (kernel-level)")
    except Exception as exc:
        logger.warning(
            "[BOOT:WIRED] SearchService unavailable (glob/grep will not work): %s",
            exc,
            exc_info=True,
        )

    # Wire SearchService -> MountService for post-mount indexing (Issue #3148)
    if mount_service is not None and search_service is not None:
        mount_service._search_service = search_service

    # Wire MountService -> WorkflowServices for scheduled sync (Issue #3148)
    if mount_service is not None:
        workflow_engine = getattr(nx, "workflow_engine", None)
        if workflow_engine is not None:
            _wf_services = getattr(workflow_engine, "_services", None)
            if _wf_services is not None and hasattr(_wf_services, "mount_sync"):
                _wf_services.mount_sync = mount_service
                logger.debug("[BOOT:WIRED] MountService -> WorkflowServices.mount_sync")

    # --- ShareLinkService: Share link operations ---
    share_link_service: Any = None
    if _on("discovery"):
        try:
            from nexus.bricks.share_link.share_link_service import ShareLinkService

            share_link_service = ShareLinkService(
                gateway=gateway,
                enforce_permissions=nx._enforce_permissions,
            )
            logger.debug("[BOOT:WIRED] ShareLinkService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] ShareLinkService unavailable: %s", exc)
    else:
        logger.debug("[BOOT:WIRED] ShareLinkService disabled by profile")

    # --- EventsService: File watching + advisory locking ---
    # EventsService is a VFSObserver — receives FileEvents via kernel OBSERVE.
    # Factory registers it on dispatch in orchestrator.py after construction.
    events_service: Any = None
    if _on("ipc"):
        try:
            from nexus.system_services.lifecycle.events_service import EventsService

            events_service = EventsService(
                event_bus=system_services.event_bus,
                lock_manager=system_services.lock_manager,
            )
            logger.debug("[BOOT:WIRED] EventsService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] EventsService unavailable: %s", exc)
    else:
        logger.debug("[BOOT:WIRED] EventsService disabled by profile")

    # --- RPC / helper services (Issue #2133) ---
    # Pre-extract optional NexusFS attrs to avoid mypy getattr+None inference issues
    _nx_default_context: Any = getattr(nx, "_default_context", None)
    _nx_session_factory: Any = getattr(nx, "SessionLocal", None)
    workspace_rpc_service: Any = None
    try:
        from nexus.system_services.workspace.workspace_rpc_service import WorkspaceRPCService

        workspace_rpc_service = WorkspaceRPCService(
            workspace_manager=system_services.workspace_manager,
            workspace_registry=system_services.workspace_registry,
            vfs=nx,
            default_context=_nx_default_context,
            snapshot_service=brick_services.snapshot_service,
        )
        logger.debug("[BOOT:WIRED] WorkspaceRPCService created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] WorkspaceRPCService unavailable: %s", exc)

    agent_rpc_service: Any = None
    try:
        from nexus.system_services.agents.agent_rpc_service import AgentRPCService

        agent_rpc_service = AgentRPCService(
            vfs=nx,
            metastore=nx.metadata,
            session_factory=_nx_session_factory,
            record_store=nx._record_store,
            entity_registry=system_services.entity_registry,
            rebac_manager=system_services.rebac_manager,
            wallet_provisioner=brick_services.wallet_provisioner,
            api_key_creator=brick_services.api_key_creator,
            key_service=getattr(nx, "_key_service", None),
            rmdir_fn=nx.sys_rmdir if hasattr(nx, "sys_rmdir") else None,
            rebac_create_fn=(rebac_service.rebac_create_sync if rebac_service else None),
            rebac_list_tuples_fn=(rebac_service.rebac_list_tuples_sync if rebac_service else None),
            rebac_delete_fn=(rebac_service.rebac_delete_sync if rebac_service else None),
        )
        logger.debug("[BOOT:WIRED] AgentRPCService created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] AgentRPCService unavailable: %s", exc)

    # ProcResolver moved to orchestrator._register_vfs_hooks() (Issue #1570)

    acp_rpc_service: Any = None
    _acp_service = getattr(system_services, "acp_service", None)
    if _acp_service is None:
        # System tier didn't create AcpService — construct inline.
        try:
            from nexus.core.agent_registry import AgentRegistry
            from nexus.system_services.acp.service import AcpService

            _acp_pt = getattr(system_services, "agent_registry", None)
            if _acp_pt is None:
                _acp_pt = AgentRegistry()
            _acp_service = AcpService(
                agent_registry=_acp_pt,
                zone_id=ROOT_ZONE_ID,
            )
            logger.debug("[BOOT:WIRED] AcpService created (inline)")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] AcpService unavailable: %s", exc)
    if _acp_service is not None:
        # Late-bind NexusFS for VFS-routed file I/O (``everything is a file``).
        if hasattr(_acp_service, "bind_fs"):
            _acp_service.bind_fs(nx)
        # Late-bind PipeManager for DT_PIPE registration of agent stdio.
        if hasattr(_acp_service, "bind_pipe_manager"):
            _acp_service.bind_pipe_manager(getattr(nx, "_pipe_manager", None))
        try:
            from nexus.system_services.acp.acp_rpc_service import AcpRPCService

            acp_rpc_service = AcpRPCService(acp_service=_acp_service)
            logger.debug("[BOOT:WIRED] AcpRPCService created")
        except Exception as exc:
            logger.warning("[BOOT:WIRED] AcpRPCService unavailable: %s", exc)

    user_provisioning_service: Any = None
    try:
        from nexus.system_services.lifecycle.user_provisioning import UserProvisioningService

        user_provisioning_service = UserProvisioningService(
            vfs=nx,
            session_factory=_nx_session_factory,
            entity_registry=system_services.entity_registry,
            api_key_creator=brick_services.api_key_creator,
            backend=_root_backend,
            rebac_manager=system_services.rebac_manager,
            rmdir_fn=nx.sys_rmdir if hasattr(nx, "sys_rmdir") else None,
            rebac_create_fn=(rebac_service.rebac_create_sync if rebac_service else None),
            rebac_delete_fn=(rebac_service.rebac_delete_sync if rebac_service else None),
            register_workspace_fn=(
                workspace_rpc_service.register_workspace if workspace_rpc_service else None
            ),
            register_agent_fn=(agent_rpc_service.register_agent if agent_rpc_service else None),
            list_cache=getattr(nx, "_list_cache", None),
            exists_cache=getattr(nx, "_exists_cache", None),
        )
        logger.debug("[BOOT:WIRED] UserProvisioningService created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] UserProvisioningService unavailable: %s", exc)

    sandbox_rpc_service: Any = None
    if _on("sandbox"):
        try:
            from nexus.sandbox.sandbox_rpc_service import SandboxRPCService

            sandbox_rpc_service = SandboxRPCService(
                session_factory=_nx_session_factory,
                default_context=_nx_default_context,
                config=nx._config,
            )
            logger.debug("[BOOT:WIRED] SandboxRPCService created")
        except ImportError:
            logger.debug("[BOOT:WIRED] SandboxRPCService not installed")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] SandboxRPCService unavailable: %s", exc)

    # --- MetadataExportService: JSONL metadata export/import (Issue #662) ---
    metadata_export_service: Any = None
    try:
        from nexus.factory._metadata_export import create_metadata_export_service

        metadata_export_service = create_metadata_export_service(nx)
    except Exception as exc:
        logger.debug("[BOOT:WIRED] MetadataExportService unavailable: %s", exc)

    descendant_checker: Any = None
    try:
        from nexus.system_services.namespace.descendant_access import DescendantAccessChecker

        descendant_checker = DescendantAccessChecker(
            rebac_manager=system_services.rebac_manager,
            rebac_service=rebac_service,
            dir_visibility_cache=system_services.dir_visibility_cache,
            permission_enforcer=system_services.permission_enforcer,
            metadata_store=nx.metadata,
        )
        logger.debug("[BOOT:WIRED] DescendantAccessChecker created")
    except Exception as exc:
        logger.debug("[BOOT:WIRED] DescendantAccessChecker unavailable: %s", exc)

    # --- TimeTravelService: historical operation-point queries (Issue #882) ---
    time_travel_service: Any = None
    if _nx_session_factory is not None:
        try:
            from nexus.bricks.versioning.time_travel_service import TimeTravelService

            time_travel_service = TimeTravelService(
                session_factory=_nx_session_factory,
                backend=_root_backend,
                default_zone_id=getattr(_nx_default_context, "zone_id", None),
            )
            logger.debug("[BOOT:WIRED] TimeTravelService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] TimeTravelService unavailable: %s", exc)

    # --- OperationsService: audit trail queries + undo (Issue #882) ---
    operations_service: Any = None
    if _nx_session_factory is not None:
        try:
            from nexus.bricks.versioning.operation_undo_service import OperationUndoService
            from nexus.bricks.versioning.operations_service import OperationsService

            _undo_service = OperationUndoService(
                router=router,
                write_fn=nx.sys_write,
                delete_fn=nx.sys_unlink,
                rename_fn=nx.sys_rename,
                exists_fn=nx.sys_access,
                fallback_backend=getattr(nx, "backend", None),
            )
            operations_service = OperationsService(
                session_factory=_nx_session_factory,
                undo_service=_undo_service,
            )
            logger.debug("[BOOT:WIRED] OperationsService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] OperationsService unavailable: %s", exc)

    result = _WiredServices(
        rebac_service=rebac_service,
        mount_service=mount_service,
        gateway=gateway,
        sync_service=sync_service,
        sync_job_service=sync_job_service,
        mount_persist_service=mount_persist_service,
        mcp_service=mcp_service,
        oauth_service=oauth_service,
        search_service=search_service,
        share_link_service=share_link_service,
        events_service=events_service,
        time_travel_service=time_travel_service,
        operations_service=operations_service,
        workspace_rpc_service=workspace_rpc_service,
        agent_rpc_service=agent_rpc_service,
        acp_rpc_service=acp_rpc_service,
        user_provisioning_service=user_provisioning_service,
        sandbox_rpc_service=sandbox_rpc_service,
        metadata_export_service=metadata_export_service,
        descendant_checker=descendant_checker,
    )

    elapsed = time.perf_counter() - t0
    active = sum(1 for f in result.__dataclass_fields__ if getattr(result, f) is not None)
    logger.info(
        "[BOOT:WIRED] %d/%d services ready (%.3fs)",
        active,
        len(result.__dataclass_fields__),
        elapsed,
    )
    return result


def _initialize_wired_ipc(nx: Any, brick_services: "BrickServices") -> None:
    """IPC adapter bind + mount — deferred from link() to initialize().

    Performs I/O (mkdir) so it cannot run in the pure-memory link() phase.
    """
    _ipc_adapter = getattr(brick_services, "ipc_storage_driver", None)
    if _ipc_adapter is not None and hasattr(_ipc_adapter, "bind"):
        try:
            _ipc_adapter.bind(nx)

            # Mount a LocalConnector at /agents for IPC file storage
            from pathlib import Path

            from nexus.backends.storage.local_connector import LocalConnectorBackend

            _ipc_data_dir = Path(getattr(nx, "_data_dir", "data")) / "ipc"
            _ipc_data_dir.mkdir(parents=True, exist_ok=True)
            _ipc_connector = LocalConnectorBackend(local_path=_ipc_data_dir)
            nx.router.add_mount("/agents", _ipc_connector)

            # Ensure the /agents metadata entry has target_zone_id set so
            # ZonePathResolver doesn't fail on it. sys_mkdir creates a DT_DIR
            # entry but doesn't set target_zone_id for the mount.
            try:
                from nexus.core.metadata import DT_DIR, DT_MOUNT

                _zone_id = getattr(nx, "_zone_id", None) or "root"
                existing = nx.metadata.get("/agents")
                if existing is not None and not existing.target_zone_id:
                    from dataclasses import replace as _replace

                    updated = _replace(
                        existing,
                        entry_type=DT_DIR | DT_MOUNT,
                        target_zone_id=_zone_id,
                    )
                    nx.metadata.put(updated)
                    logger.debug("[BOOT:WIRED] Set target_zone_id=%s on /agents mount", _zone_id)
            except Exception as e:
                logger.debug("[BOOT:WIRED] Could not set /agents target_zone_id: %s", e)

            logger.debug(
                "[BOOT:WIRED] IPC KernelVFSAdapter bound + LocalConnector mounted at /agents"
            )
        except Exception as exc:
            logger.warning("[BOOT:WIRED] IPC adapter bind failed: %s", exc)
