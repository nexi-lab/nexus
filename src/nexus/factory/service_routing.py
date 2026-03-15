"""Service wiring — canonical service name & export maps.

Issue #1502: ``bind_wired_services()`` and the setattr wiring path have been
deleted.  All service access now goes through ``nx.service("name")``.

Issue #1615 / #1708: Services are registered via direct
``coordinator.register_service()`` or ``registry.register_service()`` calls
in ``_do_link()`` and ``_boot_remote_services()``.  This module provides
only the data maps (``_CANONICAL_NAMES``, ``_CANONICAL_EXPORTS``).
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
    "gateway": (),
    "sync": (),
    "sync_job": (),
    "mount_persist": (),
    "mcp": (),
    "oauth": (),
    "share_link": (),
    "time_travel": (),
    "operations": (),
    "workspace_rpc": (),
    "agent_rpc": (),
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
    "user_provisioning": (),
    "sandbox_rpc": (),
    "metadata_export": (),
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


def register_wired_services(
    registrar: Any,
    wired: Any,
    *,
    is_remote: bool = False,
) -> int:
    """Register WiredServices into a registrar (coordinator or registry).

    Iterates ``_CANONICAL_NAMES``, extracts each non-None service from
    ``wired`` (WiredServices dataclass or dict), and calls
    ``registrar.register_service()`` with canonical name + exports.

    Issue #1708: Renamed from ``populate_service_registry()``.

    Returns the number of services registered.
    """
    count = 0
    for src_key, canonical in _CANONICAL_NAMES.items():
        val = wired.get(src_key) if isinstance(wired, dict) else getattr(wired, src_key, None)
        if val is None:
            continue
        exports = _CANONICAL_EXPORTS.get(canonical, ())
        registrar.register_service(
            canonical,
            val,
            exports=exports,
            is_remote=is_remote,
        )
        count += 1
    return count
