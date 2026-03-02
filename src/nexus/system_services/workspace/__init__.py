"""Workspace service domain -- SYSTEM tier.

Canonical location for workspace management services.
"""

from nexus.system_services.workspace.context_branch import ContextBranchService
from nexus.system_services.workspace.overlay_resolver import OverlayResolver
from nexus.system_services.workspace.workspace_manager import WorkspaceManager
from nexus.system_services.workspace.workspace_rpc_service import WorkspaceRPCService

__all__ = [
    "ContextBranchService",
    "OverlayResolver",
    "WorkspaceManager",
    "WorkspaceRPCService",
]
