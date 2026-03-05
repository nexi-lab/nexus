"""Service wiring — binds DI-resolved service instances onto NexusFS."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.config import WiredServices

# ---------------------------------------------------------------------------
# Routing tables (moved verbatim from NexusFS._SERVICE_METHODS / _SERVICE_ALIASES)
# ---------------------------------------------------------------------------

SERVICE_METHODS: dict[str, str] = {
    # WorkspaceRPCService
    "workspace_snapshot": "_workspace_rpc_service",
    "workspace_restore": "_workspace_rpc_service",
    "workspace_log": "_workspace_rpc_service",
    "workspace_diff": "_workspace_rpc_service",
    "snapshot_begin": "_workspace_rpc_service",
    "snapshot_commit": "_workspace_rpc_service",
    "snapshot_rollback": "_workspace_rpc_service",
    "load_workspace_memory_config": "_workspace_rpc_service",
    "register_workspace": "_workspace_rpc_service",
    "unregister_workspace": "_workspace_rpc_service",
    "update_workspace": "_workspace_rpc_service",
    "list_workspaces": "_workspace_rpc_service",
    "get_workspace_info": "_workspace_rpc_service",
    "register_memory": "_workspace_rpc_service",
    "unregister_memory": "_workspace_rpc_service",
    "list_registered_memories": "_workspace_rpc_service",
    "get_memory_info": "_workspace_rpc_service",
    # AgentRPCService
    "register_agent": "_agent_rpc_service",
    "update_agent": "_agent_rpc_service",
    "list_agents": "_agent_rpc_service",
    "get_agent": "_agent_rpc_service",
    "delete_agent": "_agent_rpc_service",
    # SandboxRPCService
    "sandbox_create": "_sandbox_rpc_service",
    "sandbox_run": "_sandbox_rpc_service",
    "sandbox_validate": "_sandbox_rpc_service",
    "sandbox_pause": "_sandbox_rpc_service",
    "sandbox_resume": "_sandbox_rpc_service",
    "sandbox_stop": "_sandbox_rpc_service",
    "sandbox_list": "_sandbox_rpc_service",
    "sandbox_status": "_sandbox_rpc_service",
    "sandbox_get_or_create": "_sandbox_rpc_service",
    "sandbox_connect": "_sandbox_rpc_service",
    "sandbox_disconnect": "_sandbox_rpc_service",
    # MetadataExportService
    "export_metadata": "_metadata_export_service",
    "import_metadata": "_metadata_export_service",
    # MountCoreService
    "add_mount": "_mount_core_service",
    "remove_mount": "_mount_core_service",
    "list_connectors": "_mount_core_service",
    "list_mounts": "_mount_core_service",
    "get_mount": "_mount_core_service",
    "has_mount": "_mount_core_service",
    # MountPersistService
    "save_mount": "_mount_persist_service",
    "list_saved_mounts": "_mount_persist_service",
    "load_mount": "_mount_persist_service",
    "delete_saved_mount": "_mount_persist_service",
    # SearchService (Issue #1287 — glob/grep are kernel-level VFS operations)
    "glob": "search_service",
    "grep": "search_service",
    "glob_batch": "search_service",
}

SERVICE_ALIASES: dict[str, tuple[str, str]] = {
    "list_memories": ("_workspace_rpc_service", "list_registered_memories"),
    "sandbox_available": ("_sandbox_rpc_service", "sandbox_available"),
    "get_sync_job": ("_sync_job_service", "get_job"),
    "list_sync_jobs": ("_sync_job_service", "list_jobs"),
    # SearchService async methods: a-prefix removed when calling service
    "asemantic_search": ("search_service", "semantic_search"),
    "asemantic_search_index": ("search_service", "semantic_search_index"),
    "asemantic_search_stats": ("search_service", "semantic_search_stats"),
    # SyncService / SyncJobService (Issue #2033)
    "sync_mount": ("_sync_service", "sync_mount_flat"),
    "sync_mount_async": ("_sync_job_service", "sync_mount_async"),
    "cancel_sync_job": ("_sync_job_service", "cancel_sync_job"),
    # VersionService async methods (Issue #2033)
    "aget_version": ("version_service", "get_version"),
    "alist_versions": ("version_service", "list_versions"),
    "arollback": ("version_service", "rollback"),
    "adiff_versions": ("version_service", "diff_versions"),
    # ReBACService async methods (Issue #2033)
    "arebac_create": ("rebac_service", "rebac_create"),
    "arebac_delete": ("rebac_service", "rebac_delete"),
    "arebac_check": ("rebac_service", "rebac_check"),
    "arebac_check_batch": ("rebac_service", "rebac_check_batch"),
    "arebac_expand": ("rebac_service", "rebac_expand"),
    "arebac_explain": ("rebac_service", "rebac_explain"),
    "arebac_list_tuples": ("rebac_service", "rebac_list_tuples"),
    "aget_namespace": ("rebac_service", "get_namespace"),
    # ReBACService sync methods with _sync suffix (Issue #2033)
    "rebac_create": ("rebac_service", "rebac_create_sync"),
    "rebac_check": ("rebac_service", "rebac_check_sync"),
    "rebac_check_batch": ("rebac_service", "rebac_check_batch_sync"),
    "rebac_delete": ("rebac_service", "rebac_delete_sync"),
    "rebac_list_tuples": ("rebac_service", "rebac_list_tuples_sync"),
    "rebac_expand": ("rebac_service", "rebac_expand_sync"),
    "rebac_explain": ("rebac_service", "rebac_explain_sync"),
    "share_with_user": ("rebac_service", "share_with_user_sync"),
    "share_with_group": ("rebac_service", "share_with_group_sync"),
    "grant_consent": ("rebac_service", "grant_consent_sync"),
    "revoke_consent": ("rebac_service", "revoke_consent_sync"),
    "make_public": ("rebac_service", "make_public_sync"),
    "make_private": ("rebac_service", "make_private_sync"),
    "apply_dynamic_viewer_filter": ("rebac_service", "apply_dynamic_viewer_filter_sync"),
    "list_outgoing_shares": ("rebac_service", "list_outgoing_shares_sync"),
    "list_incoming_shares": ("rebac_service", "list_incoming_shares_sync"),
    "get_dynamic_viewer_config": ("rebac_service", "get_dynamic_viewer_config_sync"),
    "namespace_create": ("rebac_service", "namespace_create_sync"),
    "namespace_delete": ("rebac_service", "namespace_delete_sync"),
    "namespace_list": ("rebac_service", "namespace_list_sync"),
    "get_namespace": ("rebac_service", "get_namespace_sync"),
    # ReBACService direct methods (no _sync suffix)
    "rebac_expand_with_privacy": ("rebac_service", "rebac_expand_with_privacy_sync"),
}


def resolve_service_attr(obj: object, name: str) -> Any | None:
    """Resolve a service method by name.

    Two-phase lookup: aliases first (method name differs on service),
    then standard forwarding (same method name on service).

    Returns the bound method, or None if not found.
    """
    alias = SERVICE_ALIASES.get(name)
    if alias is not None:
        svc_attr, svc_method = alias
        svc = obj.__dict__.get(svc_attr)
        if svc is not None:
            return getattr(svc, svc_method)

    svc_attr_std = SERVICE_METHODS.get(name)
    if svc_attr_std is not None:
        svc = obj.__dict__.get(svc_attr_std)
        if svc is not None:
            return getattr(svc, name)

    return None

# ---------------------------------------------------------------------------
# bind_wired_services — moved from NexusFS._bind_wired_services
# ---------------------------------------------------------------------------


def bind_wired_services(target: object, wired: "WiredServices | dict[str, Any]") -> None:
    """Bind wired service instances onto *target* object.

    Called by factory (orchestrator / _remote) after NexusFS construction.

    Args:
        target: The NexusFS instance (or any object with matching attrs).
        wired: WiredServices dataclass (from _boot_wired_services).
               Also accepts dict for backward compatibility with tests.

    Issue #2133: Accepts WiredServices frozen dataclass.
    Issue #1381: Extracted from NexusFS to factory tier.
    """
    _SLOT_MAP: dict[str, str] = {
        "rebac_service": "rebac_service",
        "mount_service": "mount_service",
        "gateway": "_gateway",
        "mount_core_service": "_mount_core_service",
        "sync_service": "_sync_service",
        "sync_job_service": "_sync_job_service",
        "mount_persist_service": "_mount_persist_service",
        "mcp_service": "mcp_service",
        "llm_service": "llm_service",
        "oauth_service": "oauth_service",
        "search_service": "search_service",
        "share_link_service": "share_link_service",
        "events_service": "events_service",
        "workspace_rpc_service": "_workspace_rpc_service",
        "agent_rpc_service": "_agent_rpc_service",
        "user_provisioning_service": "_user_provisioning_service",
        "sandbox_rpc_service": "_sandbox_rpc_service",
        "metadata_export_service": "_metadata_export_service",
        "descendant_checker": "_descendant_checker",
        "memory_provider": "_memory_provider",
    }
    if isinstance(wired, dict):
        for src_key, target_attr in _SLOT_MAP.items():
            setattr(target, target_attr, wired.get(src_key))
        return
    for src_key, target_attr in _SLOT_MAP.items():
        setattr(target, target_attr, getattr(wired, src_key))
