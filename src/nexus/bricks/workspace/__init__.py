"""Workspace registration service."""

from nexus.bricks.workspace.workspace_registry import (
    WorkspaceConfig as WorkspaceConfig,
)
from nexus.bricks.workspace.workspace_registry import (
    WorkspaceRegistry as WorkspaceRegistry,
)

__all__ = ["WorkspaceConfig", "WorkspaceRegistry"]
