"""Boot Tier 2b (WIRED) — services needing NexusFS reference."""

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.config import BrickServices, KernelServices, WiredServices

logger = logging.getLogger(__name__)


def _boot_wired_services(
    nx: Any,
    kernel_services: "KernelServices",
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

    Issue #2133: Typed with KernelServices + BrickServices
    and returns WiredServices instead of dict[str, Any].

    Args:
        nx: The NexusFS instance (already constructed).
        kernel_services: KernelServices container (Tier 0 — router only).
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
        from nexus.services.gateway import NexusFSGateway

        gateway = NexusFSGateway(nx)
        logger.debug("[BOOT:WIRED] NexusFSGateway created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] NexusFSGateway unavailable: %s", exc)

    # --- ReBACService: Permission and access control operations ---
    rebac_service: Any = None
    try:
        from nexus.bricks.rebac.rebac_service import ReBACService

        rebac_service = ReBACService(
            rebac_manager=system_services.rebac_manager,
            enforce_permissions=nx._enforce_permissions,
            enable_audit_logging=True,
            circuit_breaker=brick_services.rebac_circuit_breaker,
            file_reader=lambda path: nx.read(path),
            permission_enforcer=nx._permission_enforcer,
        )
        logger.debug("[BOOT:WIRED] ReBACService created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] ReBACService unavailable: %s", exc)

    # --- MCPService: Model Context Protocol operations ---
    mcp_service: Any = None
    if _on("mcp"):
        try:
            from nexus.bricks.mcp.mcp_service import MCPService

            mcp_service = MCPService(filesystem=nx)
            logger.debug("[BOOT:WIRED] MCPService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] MCPService unavailable: %s", exc)
    else:
        logger.debug("[BOOT:WIRED] MCPService disabled by profile")

    # --- LLMService + LLMSubsystem: LLM integration ---
    llm_service: Any = None
    llm_subsystem: Any = None
    if _on("llm"):
        try:
            from nexus.bricks.llm.llm_service import LLMService

            llm_service = LLMService(nexus_fs=nx)

            from nexus.services.subsystems.llm_subsystem import LLMSubsystem

            llm_subsystem = LLMSubsystem(llm_service=llm_service)
            logger.debug("[BOOT:WIRED] LLMService + LLMSubsystem created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] LLMService unavailable: %s", exc)
    else:
        logger.debug("[BOOT:WIRED] LLMService disabled by profile")

    # --- OAuthService: OAuth authentication operations ---
    oauth_service: Any = None
    if _on("sandbox"):
        try:
            import os

            from nexus.services.oauth.oauth_service import OAuthService

            oauth_service = OAuthService(
                oauth_factory=None,
                token_manager=None,
                filesystem=nx,
                database_url=os.getenv("TOKEN_MANAGER_DB"),
                oauth_config=getattr(nx._config, "oauth", None) if nx._config else None,
                mount_lister=lambda: [
                    (m.mount_point, "mounted") for m in kernel_services.router.list_mounts()
                ],
            )
            logger.debug("[BOOT:WIRED] OAuthService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] OAuthService unavailable: %s", exc)
    else:
        logger.debug("[BOOT:WIRED] OAuthService disabled by profile")

    # --- MountCoreService: Internal mount operations (gateway-dependent) ---
    mount_core_service: Any = None
    if gateway is not None:
        try:
            from nexus.services.mount.mount_core_service import MountCoreService

            mount_core_service = MountCoreService(gateway)
            logger.debug("[BOOT:WIRED] MountCoreService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] MountCoreService unavailable: %s", exc)

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
    mount_persist_service: Any = None
    if mount_core_service is not None:
        try:
            from nexus.services.mount.mount_persist_service import MountPersistService

            mount_persist_service = MountPersistService(
                mount_manager=system_services.mount_manager,
                mount_service=mount_core_service,
                sync_service=sync_service,
            )
            logger.debug("[BOOT:WIRED] MountPersistService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] MountPersistService unavailable: %s", exc)

    # --- MountService: Dynamic backend mounting operations ---
    # Moved after sub-services so DI deps are available (Issue #636).
    mount_service: Any = None
    try:
        from nexus.services.mount.mount_service import MountService

        mount_service = MountService(
            router=kernel_services.router,
            mount_manager=system_services.mount_manager,
            nexus_fs=nx,
            sync_service=sync_service,
            sync_job_service=sync_job_service,
            mount_core_service=mount_core_service,
            mount_persist_service=mount_persist_service,
            oauth_service=oauth_service,
        )
        logger.debug("[BOOT:WIRED] MountService created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] MountService unavailable: %s", exc)

    # --- SkillService: Skill management (Issue #2035) ---
    skill_service: Any = brick_services.skill_service
    if skill_service is None and _on("skills") and gateway is not None:
        try:
            from nexus.bricks.skills.skill_service_adapter import SkillService as _SkillService

            skill_service = _SkillService(gateway=gateway)
            logger.debug("[BOOT:WIRED] SkillService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] SkillService unavailable: %s", exc)
    elif not _on("skills"):
        logger.debug("[BOOT:WIRED] SkillService disabled by profile")

    # --- SkillPackageService: Skill export/import/validate (Issue #2035) ---
    skill_package_service: Any = getattr(brick_services, "skill_package_service", None)
    if skill_package_service is None and _on("skills") and skill_service is not None:
        try:
            from nexus.bricks.skills.package_service import SkillPackageService as _SkillPkgSvc

            skill_package_service = _SkillPkgSvc(
                fs=skill_service._fs,
                perms=skill_service._perms,
                skill_service=skill_service,
            )
            logger.debug("[BOOT:WIRED] SkillPackageService created")
        except Exception:
            logger.warning("[BOOT:WIRED] SkillPackageService unavailable (optional)")

    # --- SearchService: list/glob/grep are kernel-level VFS operations (Issue #2194) ---
    # Always create SearchService regardless of "search" brick — basic directory
    # listing, glob matching, and grep must work even in KERNEL-only mode.
    # The "search" brick gates advanced features (semantic search, indexing),
    # not core filesystem enumeration.
    search_service: Any = None
    try:
        from nexus.services.search.search_service import SearchService

        search_service = SearchService(
            metadata_store=nx.metadata,
            permission_enforcer=getattr(nx, "_permission_enforcer", None),
            router=kernel_services.router,
            rebac_manager=system_services.rebac_manager,
            enforce_permissions=getattr(nx, "_enforce_permissions", True),
            default_context=getattr(nx, "_default_context", None),
            record_store=getattr(nx, "_record_store", None),
            gateway=gateway,
        )
        logger.debug("[BOOT:WIRED] SearchService created (kernel-level)")
    except Exception as exc:
        logger.debug("[BOOT:WIRED] SearchService unavailable: %s", exc)

    # --- ShareLinkService: Share link operations ---
    share_link_service: Any = None
    if _on("discovery"):
        try:
            from nexus.services.share_link.share_link_service import ShareLinkService

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
    events_service: Any = None
    if _on("ipc"):
        try:
            from nexus.system_services.lifecycle.events_service import EventsService

            events_service = EventsService(
                backend=_root_backend,
                event_bus=brick_services.event_bus,
                lock_manager=brick_services.lock_manager,
                zone_id=None,
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
    _nx_memory_config: Any = getattr(nx, "_memory_config", None)

    workspace_rpc_service: Any = None
    try:
        from nexus.services.workspace_rpc_service import WorkspaceRPCService

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
        from nexus.services.agents.agent_rpc_service import AgentRPCService

        agent_rpc_service = AgentRPCService(
            vfs=nx,
            metastore=nx.metadata,
            session_factory=_nx_session_factory,
            record_store=nx._record_store,
            agent_registry=getattr(nx, "_agent_registry", None),
            entity_registry=system_services.entity_registry,
            rebac_manager=system_services.rebac_manager,
            wallet_provisioner=brick_services.wallet_provisioner,
            api_key_creator=brick_services.api_key_creator,
            key_service=getattr(nx, "_key_service", None),
            rmdir_fn=nx.rmdir if hasattr(nx, "rmdir") else None,
            rebac_create_fn=(rebac_service.rebac_create_sync if rebac_service else None),
            rebac_list_tuples_fn=(rebac_service.rebac_list_tuples_sync if rebac_service else None),
            rebac_delete_fn=(rebac_service.rebac_delete_sync if rebac_service else None),
        )
        logger.debug("[BOOT:WIRED] AgentRPCService created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] AgentRPCService unavailable: %s", exc)

    user_provisioning_service: Any = None
    try:
        from nexus.services.user_provisioning import UserProvisioningService

        user_provisioning_service = UserProvisioningService(
            vfs=nx,
            session_factory=_nx_session_factory,
            entity_registry=system_services.entity_registry,
            api_key_creator=brick_services.api_key_creator,
            backend=_root_backend,
            rebac_manager=system_services.rebac_manager,
            rmdir_fn=nx.rmdir if hasattr(nx, "rmdir") else None,
            rebac_create_fn=(rebac_service.rebac_create_sync if rebac_service else None),
            rebac_delete_fn=(rebac_service.rebac_delete_sync if rebac_service else None),
            register_workspace_fn=(
                workspace_rpc_service.register_workspace if workspace_rpc_service else None
            ),
            register_agent_fn=(agent_rpc_service.register_agent if agent_rpc_service else None),
            skills_import_fn=getattr(nx, "skills_import", None),
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

    ace_rpc_service: Any = None
    try:
        from nexus.services.ace_rpc_service import ACERPCService

        ace_rpc_service = ACERPCService(
            session_factory=_nx_session_factory,
            backend=_root_backend,
            default_context=_nx_default_context,
            entity_registry=system_services.entity_registry,
            ensure_entity_registry_fn=getattr(nx, "_ensure_entity_registry", None),
        )
        logger.debug("[BOOT:WIRED] ACERPCService created")
    except Exception as exc:
        logger.debug("[BOOT:WIRED] ACERPCService unavailable: %s", exc)

    descendant_checker: Any = None
    try:
        from nexus.services.descendant_access import DescendantAccessChecker

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

    memory_provider: Any = None
    try:
        from nexus.bricks.memory.memory_provider import MemoryProvider

        memory_provider = MemoryProvider(
            session_factory=_nx_session_factory,
            backend=_root_backend,
            entity_registry=system_services.entity_registry,
            enable_paging=getattr(nx, "_enable_memory_paging", True),
            main_capacity=getattr(nx, "_memory_main_capacity", 100),
            recall_max_age_hours=getattr(nx, "_memory_recall_max_age_hours", 24.0),
            memory_config=_nx_memory_config,
        )
        logger.debug("[BOOT:WIRED] MemoryProvider created")
    except Exception as exc:
        logger.debug("[BOOT:WIRED] MemoryProvider unavailable: %s", exc)

    # --- TimeTravelService: historical operation-point queries (Issue #882) ---
    time_travel_service: Any = None
    if _nx_session_factory is not None:
        try:
            from nexus.services.versioning.time_travel_service import TimeTravelService

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
            from nexus.services.versioning.operation_undo_service import OperationUndoService
            from nexus.services.versioning.operations_service import OperationsService

            _undo_service = OperationUndoService(
                router=kernel_services.router,
                write_fn=nx.write,
                delete_fn=nx.delete,
                rename_fn=nx.rename,
                exists_fn=nx.exists,
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
        mount_core_service=mount_core_service,
        sync_service=sync_service,
        sync_job_service=sync_job_service,
        mount_persist_service=mount_persist_service,
        mcp_service=mcp_service,
        llm_service=llm_service,
        llm_subsystem=llm_subsystem,
        oauth_service=oauth_service,
        skill_service=skill_service,
        skill_package_service=skill_package_service,
        search_service=search_service,
        share_link_service=share_link_service,
        events_service=events_service,
        task_queue_service=brick_services.task_queue_service,
        time_travel_service=time_travel_service,
        operations_service=operations_service,
        workspace_rpc_service=workspace_rpc_service,
        agent_rpc_service=agent_rpc_service,
        user_provisioning_service=user_provisioning_service,
        sandbox_rpc_service=sandbox_rpc_service,
        metadata_export_service=metadata_export_service,
        ace_rpc_service=ace_rpc_service,
        descendant_checker=descendant_checker,
        memory_provider=memory_provider,
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
