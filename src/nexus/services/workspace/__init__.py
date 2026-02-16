"""Workspace and memory registration service."""

from nexus.services.workspace.workspace_registry import (
    MemoryConfig as MemoryConfig,
)
from nexus.services.workspace.workspace_registry import (
    WorkspaceConfig as WorkspaceConfig,
)
from nexus.services.workspace.workspace_registry import (
    WorkspaceRegistry as WorkspaceRegistry,
)

__all__ = ["MemoryConfig", "WorkspaceConfig", "WorkspaceRegistry"]
