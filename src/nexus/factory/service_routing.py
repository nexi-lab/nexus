"""Service wiring — populates ``ServiceRegistry`` from WiredServices.

Issue #1502: ``bind_wired_services()`` and the setattr wiring path have been
deleted.  All service access now goes through ``nx.service("name")``.
The sole registration path is ``populate_service_registry()``.

Issue #1615: ``populate_service_registry()`` accepts either a raw
``ServiceRegistry`` or a ``ServiceLifecycleCoordinator`` — both expose
``register_service(name, instance, *, exports, is_remote)``.  When a
coordinator is provided, services are registered in both the Registry
and BrickLifecycleManager in one shot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.config import WiredServices
    from nexus.core.service_registry import ServiceRegistry
    from nexus.system_services.lifecycle.service_lifecycle_coordinator import (
        ServiceLifecycleCoordinator,
    )


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
    "user_provisioning_service": "user_provisioning",
    "sandbox_rpc_service": "sandbox_rpc",
    "metadata_export_service": "metadata_export",
}


def populate_service_registry(
    registrar: "ServiceRegistry | ServiceLifecycleCoordinator",
    wired: "WiredServices | dict[str, Any]",
    *,
    is_remote: bool = False,
) -> int:
    """Populate ServiceRegistry (or coordinator) from WiredServices.

    Accepts either a raw ``ServiceRegistry`` or a ``ServiceLifecycleCoordinator``.
    Both duck-type on ``register_service(name, instance, *, exports, is_remote)``.
    When a coordinator is provided, services are registered in both the
    ServiceRegistry and BrickLifecycleManager in one shot (Issue #1615).

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
