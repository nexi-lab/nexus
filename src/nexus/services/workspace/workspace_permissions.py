"""Shared workspace permission checking utility (Issue #1315, C2-B).

Extracted from WorkspaceManager._check_workspace_permission() to enable reuse
by both WorkspaceManager and ContextBranchService without DRY violation.
"""

import logging
from typing import TYPE_CHECKING

from nexus.contracts.exceptions import NexusPermissionError

if TYPE_CHECKING:
    from nexus.contracts.protocols.rebac import ReBACBrickProtocol

logger = logging.getLogger(__name__)

# Permission mapping: workspace operations → file-level ReBAC permissions
_WRITE_PERMISSIONS = frozenset(
    {
        "snapshot:create",
        "snapshot:restore",
        "branch:create",
        "branch:delete",
        "branch:merge",
        "branch:checkout",
        "branch:explore",
    }
)

_READ_PERMISSIONS = frozenset(
    {
        "snapshot:list",
        "snapshot:diff",
        "branch:list",
        "branch:read",
        "branch:log",
        "branch:diff",
    }
)


def check_workspace_permission(
    rebac_manager: "ReBACBrickProtocol | None",
    workspace_path: str,
    permission: str,
    user_id: str | None = None,
    agent_id: str | None = None,
    zone_id: str | None = None,
    default_agent_id: str | None = None,
    default_zone_id: str | None = None,
) -> None:
    """Check if user or agent has permission on a workspace.

    Args:
        rebac_manager: ReBAC manager for permission checks (None = allow all)
        workspace_path: Path to workspace
        permission: Permission to check (e.g., 'snapshot:create', 'branch:merge')
        user_id: User ID to check (for user operations)
        agent_id: Agent ID to check (for agent operations)
        zone_id: Zone ID for isolation
        default_agent_id: Fallback agent ID if agent_id not provided
        default_zone_id: Fallback zone ID if zone_id not provided

    Raises:
        NexusPermissionError: If permission check fails
    """
    if not rebac_manager:
        logger.warning(
            "No ReBAC manager configured, allowing %s on %s",
            permission,
            workspace_path,
        )
        return

    check_agent_id = agent_id or default_agent_id
    check_zone_id = zone_id or default_zone_id

    # Determine subject
    if check_agent_id:
        subject = ("agent", check_agent_id)
        subject_desc = f"agent={check_agent_id}"
    elif user_id:
        subject = ("user", user_id)
        subject_desc = f"user={user_id}"
    else:
        logger.error(
            "No user_id or agent_id provided for permission check: %s on %s",
            permission,
            workspace_path,
        )
        raise NexusPermissionError(
            f"{permission} on workspace {workspace_path} (no user_id or agent_id provided)"
        )

    # Map workspace permission to file-level permission
    if permission in _WRITE_PERMISSIONS:
        file_permission = "write"
    elif permission in _READ_PERMISSIONS:
        file_permission = "read"
    else:
        logger.warning("Unknown workspace permission: %s, defaulting to write", permission)
        file_permission = "write"

    has_permission = rebac_manager.rebac_check(
        subject=subject,
        permission=file_permission,
        object=("file", workspace_path),
        zone_id=check_zone_id,
    )

    if not has_permission:
        logger.warning(
            "Permission denied for %s, permission=%s (mapped to %s), workspace=%s, zone=%s",
            subject_desc,
            permission,
            file_permission,
            workspace_path,
            check_zone_id,
        )
        raise NexusPermissionError(f"Permission denied: {permission} on workspace {workspace_path}")
