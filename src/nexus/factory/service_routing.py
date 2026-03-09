"""Service wiring — binds DI-resolved service instances onto NexusFS.

Issue #1410: SERVICE_METHODS, SERVICE_ALIASES, and resolve_service_attr()
deleted — zero callers (dead code from the retired __getattr__ proxy).

Issue #1452: ``populate_service_registry()`` added as dual-write companion.
During the transition period both ``bind_wired_services()`` (setattr) and
``populate_service_registry()`` (ServiceRegistry) are called; callers are
migrated from ``nx.xxx_service`` to ``nx.service("xxx")`` in Phase 2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.config import WiredServices
    from nexus.core.service_registry import ServiceRegistry


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
        "time_travel_service": "time_travel_service",
        "operations_service": "operations_service",
    }
    if isinstance(wired, dict):
        for src_key, target_attr in _SLOT_MAP.items():
            setattr(target, target_attr, wired.get(src_key))
        return
    for src_key, target_attr in _SLOT_MAP.items():
        setattr(target, target_attr, getattr(wired, src_key))


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

    Called alongside ``bind_wired_services()`` during the transition period.
    Extracts non-None service instances and registers them under canonical
    short names (e.g. ``"search"`` instead of ``"search_service"``).

    Returns the number of services registered.
    """
    services: dict[str, Any] = {}
    for src_key, canonical in _CANONICAL_NAMES.items():
        val = wired.get(src_key) if isinstance(wired, dict) else getattr(wired, src_key, None)
        if val is not None:
            services[canonical] = val
    return registry.register_many(services, is_remote=is_remote)
