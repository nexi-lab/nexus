"""Service wiring — binds DI-resolved service instances onto NexusFS."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.config import WiredServices


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
