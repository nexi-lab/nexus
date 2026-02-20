"""Service wiring for NexusFS.

Extracted from NexusFS._wire_services() (Issue #2033) to reduce the NexusFS
class definition size. This module is imported lazily at the end of __init__.

The function mutates the NexusFS instance, setting service attributes directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass  # NexusFS is used at runtime, not for type hints


def wire_services(fs: Any) -> None:
    """Wire services that require a reference to the NexusFS instance.

    Services follow "accept or build" pattern (Issue #1519, 4B): if pre-built
    via KernelServices, use that instance; otherwise build internally.

    Args:
        fs: NexusFS instance to wire services onto.
    """
    brk_svc = fs._brick_services

    # VersionService: moved to BrickServices (Issue #2034)
    fs.version_service = brk_svc.version_service

    # Lazy-import services to avoid core/ → services/ top-level coupling (#1519)
    from nexus.services.llm_service import LLMService
    from nexus.services.mcp_service import MCPService
    from nexus.services.mount_service import MountService
    from nexus.services.oauth_service import OAuthService
    from nexus.services.search_service import SearchService

    # ReBACService: Permission and access control operations
    # Must be created before AgentRPCService and UserProvisioningService
    # which depend on rebac_service sync methods for DI.
    # ReBACService: check pre-built first, then build from kernel + brick services
    _pre_rebac = getattr(fs, "_pre_rebac_service", None)
    if _pre_rebac is not None:
        fs.rebac_service = _pre_rebac
    else:
        from nexus.services.rebac_service import ReBACService

        fs.rebac_service = ReBACService(
            rebac_manager=fs._rebac_manager,
            enforce_permissions=fs._enforce_permissions,
            enable_audit_logging=True,
            circuit_breaker=brk_svc.rebac_circuit_breaker,
            permission_enforcer=fs._permission_enforcer,
        )

    # WorkspaceRPCService: Replaces NexusFS workspace/memory/snapshot facades
    from nexus.services.workspace_rpc_service import WorkspaceRPCService

    fs._workspace_rpc_service = WorkspaceRPCService(
        workspace_manager=fs._workspace_manager,
        workspace_registry=fs._workspace_registry,
        vfs=fs,
        default_context=fs._default_context,
        snapshot_service=fs._snapshot_service,
    )

    # AgentRPCService: Replaces NexusFS agent management/lifecycle facades
    from nexus.services.agents.agent_rpc_service import AgentRPCService

    fs._agent_rpc_service = AgentRPCService(
        vfs=fs,
        metastore=fs.metadata,
        session_factory=fs.SessionLocal,
        agent_registry=fs._agent_registry,
        entity_registry=fs._entity_registry,
        rebac_manager=fs._rebac_manager,
        wallet_provisioner=fs._wallet_provisioner,
        api_key_creator=fs._api_key_creator,
        key_service=getattr(fs, "_key_service", None),
        rmdir_fn=fs.rmdir,
        rebac_create_fn=fs.rebac_service.rebac_create_sync,
        rebac_list_tuples_fn=fs.rebac_service.rebac_list_tuples_sync,
        rebac_delete_fn=fs.rebac_service.rebac_delete_sync,
    )

    # UserProvisioningService: Replaces NexusFS provision/deprovision facades
    from nexus.services.user_provisioning import UserProvisioningService

    fs._user_provisioning_service = UserProvisioningService(
        vfs=fs,
        session_factory=fs.SessionLocal,
        entity_registry=fs._entity_registry,
        api_key_creator=fs._api_key_creator,
        backend=getattr(fs, "backend", None),
        rebac_manager=fs._rebac_manager,
        rmdir_fn=fs.rmdir,
        rebac_create_fn=fs.rebac_service.rebac_create_sync,
        rebac_delete_fn=fs.rebac_service.rebac_delete_sync,
        register_workspace_fn=fs.register_workspace,
        register_agent_fn=fs.register_agent,
        skills_import_fn=getattr(fs, "skills_import", None),
        list_cache=getattr(fs, "_list_cache", None),
        exists_cache=getattr(fs, "_exists_cache", None),
    )

    # SandboxRPCService: Replaces NexusFS sandbox management facades
    # Optional — sandbox package may not be installed
    try:
        from nexus.sandbox.sandbox_rpc_service import SandboxRPCService

        fs._sandbox_rpc_service = SandboxRPCService(
            session_factory=fs.SessionLocal,
            default_context=fs._default_context,
            config=getattr(fs, "_config", None),
        )
    except ImportError:
        fs._sandbox_rpc_service = None

    # MetadataExportService: Replaces NexusFS export/import facades
    from nexus.services.metadata_export import MetadataExportService

    fs._metadata_export_service = MetadataExportService(
        metastore=fs.metadata,
        default_context=fs._default_context,
    )

    # ACERPCService: Replaces NexusFS ACE trajectory/playbook facades
    from nexus.services.ace_rpc_service import ACERPCService

    fs._ace_rpc_service = ACERPCService(
        session_factory=fs.SessionLocal,
        backend=fs.backend,
        default_context=fs._default_context,
        entity_registry=fs._entity_registry,
        ensure_entity_registry_fn=fs._ensure_entity_registry,
    )

    # MountService: Dynamic backend mounting operations
    fs.mount_service = MountService(
        router=fs.router,
        mount_manager=fs.mount_manager,
        nexus_fs=fs,
    )

    # MCPService: Model Context Protocol operations
    fs.mcp_service = MCPService(filesystem=fs)

    # LLMService: LLM integration operations
    fs.llm_service = LLMService(nexus_fs=fs)
    from nexus.services.subsystems.llm_subsystem import LLMSubsystem

    fs._llm_subsystem = LLMSubsystem(llm_service=fs.llm_service)

    # OAuthService: OAuth authentication operations
    fs.oauth_service = OAuthService(
        oauth_factory=None,
        token_manager=None,
        filesystem=fs,
    )

    # Shared gateway for all extracted services (Issue #1287)
    from nexus.services.gateway import NexusFSGateway

    fs._gateway = NexusFSGateway(fs)

    # Mount/sync services: always built at wire time (Issue #2034)
    from nexus.services.mount_core_service import MountCoreService

    fs._mount_core_service = MountCoreService(fs._gateway)

    from nexus.services.sync_service import SyncService

    fs._sync_service = SyncService(fs._gateway)

    from nexus.services.sync_job_service import SyncJobService

    fs._sync_job_service = SyncJobService(fs._gateway, fs._sync_service)

    from nexus.services.mount_persist_service import MountPersistService

    fs._mount_persist_service = MountPersistService(
        mount_manager=getattr(fs, "mount_manager", None),
        mount_service=fs._mount_core_service,
        sync_service=fs._sync_service,
    )

    # TaskQueueService: from BrickServices (Issue #655)
    if brk_svc.task_queue_service is not None:
        fs.task_queue_service = brk_svc.task_queue_service

    # SkillService: Skill management
    from nexus.services.skill_service import SkillService as _SkillService

    fs.skill_service = _SkillService(gateway=fs._gateway)

    # SkillPackageService: from BrickServices (Issue #2035)
    if brk_svc.skill_package_service is not None:
        fs.skill_package_service = brk_svc.skill_package_service

    # SearchService: always built at wire time (Issue #2034)
    _pre_search = getattr(fs, "_pre_search_service", None)
    if _pre_search is not None:
        fs.search_service = _pre_search
    else:
        fs.search_service = SearchService(
            metadata_store=fs.metadata,
            permission_enforcer=fs._permission_enforcer,
            router=fs.router,
            rebac_manager=fs._rebac_manager,
            enforce_permissions=fs._enforce_permissions,
            default_context=fs._default_context,
            record_store=fs._record_store,
            gateway=fs._gateway,
        )

    # ShareLinkService: Share link operations
    from nexus.services.share_link_service import ShareLinkService

    fs.share_link_service = ShareLinkService(
        gateway=fs._gateway,
        enforce_permissions=fs._enforce_permissions,
    )

    # EventsService: File watching + advisory locking
    _pre_events = getattr(fs, "_pre_events_service", None)
    if _pre_events is not None:
        fs.events_service = _pre_events
    else:
        from nexus.services.events_service import EventsService

        metadata_cache = None
        if hasattr(fs.metadata, "_cache"):
            metadata_cache = fs.metadata._cache

        fs.events_service = EventsService(
            backend=fs.backend,
            event_bus=fs._event_bus,
            lock_manager=fs._lock_manager,
            zone_id=None,
            metadata_cache=metadata_cache,
        )

    # DescendantAccessChecker: extracted from NexusFS (Issue #2033)
    from nexus.services.descendant_access import DescendantAccessChecker

    fs._descendant_checker = DescendantAccessChecker(
        rebac_manager=fs._rebac_manager,
        rebac_service=fs.rebac_service,
        dir_visibility_cache=fs._dir_visibility_cache,
        permission_enforcer=fs._permission_enforcer,
        metadata_store=fs.metadata,
    )

    # PermissionChecker: extracted from NexusFS._check_permission (Issue #2033)
    from nexus.core.permission_checker import PermissionChecker

    fs._permission_checker = PermissionChecker(
        permission_enforcer=fs._permission_enforcer,
        metadata_store=fs.metadata,
        default_context=fs._default_context,
        enforce_permissions=fs._enforce_permissions,
    )

    # MemoryProvider: extracted from NexusFS.memory property (Issue #2033)
    from nexus.services.memory_provider import MemoryProvider

    fs._memory_provider = MemoryProvider(
        session_factory=fs.SessionLocal,
        backend=fs.backend,
        entity_registry=fs._entity_registry,
        enable_paging=fs._enable_memory_paging,
        main_capacity=fs._memory_main_capacity,
        recall_max_age_hours=fs._memory_recall_max_age_hours,
        memory_config=fs._memory_config,
    )
