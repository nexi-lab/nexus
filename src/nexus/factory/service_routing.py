"""Service wiring — canonical service name & export maps.

Issue #1502: ``bind_wired_services()`` and the setattr wiring path have been
deleted.  All service access now goes through ``nx.service("name")``.

Issue #1708: Single entry point via ``enlist_wired_services()`` which calls
``coordinator.enlist()`` for each service.  Coordinator is always available
for all deployment profiles (BLM optional).
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# EXPORT_SYMBOL declarations: each service's public API surface
# ---------------------------------------------------------------------------

_CANONICAL_EXPORTS: dict[str, tuple[str, ...]] = {
    "search": ("glob", "grep", "list", "semantic_search"),
    "rebac": ("rebac_check", "rebac_create", "rebac_list_tuples", "rebac_expand"),
    "events": ("wait_for_changes", "on_mutation", "locked"),
    "mount": ("add_mount", "remove_mount", "list_mounts"),
    "gateway": (
        "mkdir",
        "sys_write",
        "sys_read",
        "sys_readdir",
        "sys_access",
        "metadata_get",
        "metadata_put",
        "metadata_list",
        "metadata_delete",
    ),
    "sync": ("sync_mount",),
    "sync_job": ("get_job", "list_jobs", "sync_mount_async", "cancel_sync_job"),
    "mount_persist": (
        "save_mount",
        "load_mount",
        "load_all_mounts",
        "list_saved_mounts",
        "delete_saved_mount",
    ),
    "mcp": ("mcp_list_mounts", "mcp_connect"),
    "oauth": ("list_providers", "list_credentials", "revoke_credential"),
    "share_link": ("create_share_link", "get_share_link", "list_share_links", "revoke_share_link"),
    "time_travel": ("get_file_at_operation", "list_files_at_operation"),
    "operations": ("list_operations", "get_last_operation", "undo_by_id"),
    "workspace_rpc": (
        "workspace_snapshot",
        "workspace_restore",
        "workspace_log",
        "register_workspace",
        "unregister_workspace",
        "list_workspaces",
    ),
    "agent_rpc": ("register_agent", "update_agent", "list_agents", "get_agent", "delete_agent"),
    "acp_rpc": (
        "acp_call",
        "acp_list_agents",
        "acp_list_processes",
        "acp_kill",
        "acp_set_system_prompt",
        "acp_get_system_prompt",
        "acp_set_enabled_skills",
        "acp_get_enabled_skills",
        "acp_history",
    ),
    "user_provisioning": ("provision_user", "deprovision_user"),
    "sandbox_rpc": ("sandbox_create", "sandbox_run", "sandbox_list", "sandbox_status"),
    "metadata_export": ("export_metadata", "import_metadata"),
}

# ---------------------------------------------------------------------------
# Canonical name mapping: WiredServices field → short registry key
# ---------------------------------------------------------------------------

_CANONICAL_NAMES: dict[str, str] = {
    "rebac_service": "rebac",
    "mount_service": "mount",
    "gateway": "gateway",
    "sync_service": "sync",
    "sync_job_service": "sync_job",
    "mount_persist_service": "mount_persist",
    "mcp_service": "mcp",
    "oauth_service": "oauth",
    "search_service": "search",
    "share_link_service": "share_link",
    "events_service": "events",
    "time_travel_service": "time_travel",
    "operations_service": "operations",
    "workspace_rpc_service": "workspace_rpc",
    "agent_rpc_service": "agent_rpc",
    "acp_rpc_service": "acp_rpc",
    "user_provisioning_service": "user_provisioning",
    "sandbox_rpc_service": "sandbox_rpc",
    "metadata_export_service": "metadata_export",
}


async def enlist_wired_services(coordinator: Any, wired: Any) -> int:
    """Enlist WiredServices via coordinator.enlist() (#1708).

    Iterates ``_CANONICAL_NAMES``, extracts each non-None service from
    ``wired`` (WiredServices dataclass or dict), and calls
    ``await coordinator.enlist()`` with canonical name + exports.

    All wired services are Q1 (restart-required) — no HotSwappable or PersistentService
    — so enlist() auto-detects and registers them without lifecycle side effects.

    Returns the number of services enlisted.
    """
    count = 0
    for src_key, canonical in _CANONICAL_NAMES.items():
        val = wired.get(src_key) if isinstance(wired, dict) else getattr(wired, src_key, None)
        if val is None:
            continue
        exports = _CANONICAL_EXPORTS.get(canonical, ())
        await coordinator.enlist(canonical, val, exports=exports, allow_overwrite=True)
        count += 1
    return count
