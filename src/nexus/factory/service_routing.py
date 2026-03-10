"""Service wiring — registers DI-resolved service instances into ServiceRegistry.

Issue #1452 Phase 2c: ``bind_wired_services()`` deleted — all callers migrated
to ``nx.service("xxx")`` via ServiceRegistry.  ``populate_service_registry()``
is now the sole registration path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.config import WiredServices
    from nexus.core.service_registry import ServiceRegistry


# ---------------------------------------------------------------------------
# EXPORT_SYMBOL declarations: each service's public API surface
# ---------------------------------------------------------------------------

_CANONICAL_EXPORTS: dict[str, tuple[str, ...]] = {
    "search": ("glob", "grep", "list", "semantic_search"),
    "rebac": ("rebac_check", "rebac_create", "rebac_list_tuples", "rebac_expand"),
    "events": ("wait_for_changes", "on_mutation", "locked"),
    "mount": ("add_mount", "remove_mount", "list_mounts"),
    "gateway": (),
    "mount_core": (),
    "sync": (),
    "sync_job": (),
    "mount_persist": (),
    "mcp": (),
    "llm": (),
    "oauth": (),
    "share_link": (),
    "time_travel": (),
    "operations": (),
    "workspace_rpc": (),
    "agent_rpc": (),
    "user_provisioning": (),
    "sandbox_rpc": (),
    "metadata_export": (),
    "descendant_checker": (),
    "memory_provider": (),
}

# ---------------------------------------------------------------------------
# Canonical name mapping: WiredServices field → short registry key
# ---------------------------------------------------------------------------

_CANONICAL_NAMES: dict[str, str] = {
    "rebac_service": "rebac",
    "mount_service": "mount",
    "gateway": "gateway",
    "mount_core_service": "mount_core",
    "sync_service": "sync",
    "sync_job_service": "sync_job",
    "mount_persist_service": "mount_persist",
    "mcp_service": "mcp",
    "llm_service": "llm",
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
    "descendant_checker": "descendant_checker",
    "memory_provider": "memory_provider",
}


def populate_service_registry(
    registry: "ServiceRegistry",
    wired: "WiredServices | dict[str, Any]",
    *,
    is_remote: bool = False,
) -> int:
    """Dual-write companion — populate ServiceRegistry from WiredServices.

    Sole registration path — extracts non-None service instances and registers
    them under canonical short names (e.g. ``"search"`` instead of ``"search_service"``).

    Returns the number of services registered.
    """
    count = 0
    for src_key, canonical in _CANONICAL_NAMES.items():
        val = wired.get(src_key) if isinstance(wired, dict) else getattr(wired, src_key, None)
        if val is None:
            continue
        exports = _CANONICAL_EXPORTS.get(canonical, ())
        registry.register_service(
            canonical,
            val,
            exports=exports,
            is_remote=is_remote,
        )
        count += 1
    return count
