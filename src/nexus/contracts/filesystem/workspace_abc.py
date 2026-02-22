"""Workspace sub-ABC for filesystem implementations.

Extracted from core/filesystem.py (Issue #2424) following the
``collections.abc`` composition pattern.

Contains: workspace snapshot/restore/log/diff + register/unregister/list/get
"""

import builtins
from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Any


class WorkspaceABC(ABC):
    """Workspace versioning and registry operations."""

    # === Workspace Versioning ===

    @abstractmethod
    def workspace_snapshot(
        self,
        workspace_path: str | None = None,
        description: str | None = None,
        tags: builtins.list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a snapshot of a registered workspace.

        Args:
            workspace_path: Path to registered workspace
            description: Human-readable description
            tags: List of tags for categorization

        Returns:
            Snapshot metadata dict

        Raises:
            ValueError: If workspace_path not provided
            BackendError: If snapshot cannot be created
        """
        ...

    @abstractmethod
    def workspace_restore(
        self,
        snapshot_number: int,
        workspace_path: str | None = None,
    ) -> dict[str, Any]:
        """Restore workspace to a previous snapshot.

        Args:
            snapshot_number: Snapshot version number to restore
            workspace_path: Path to registered workspace

        Returns:
            Restore operation result

        Raises:
            ValueError: If workspace_path not provided
            NexusFileNotFoundError: If snapshot not found
        """
        ...

    @abstractmethod
    def workspace_log(
        self,
        workspace_path: str | None = None,
        limit: int = 100,
    ) -> builtins.list[dict[str, Any]]:
        """List snapshot history for workspace.

        Args:
            workspace_path: Path to registered workspace
            limit: Maximum number of snapshots to return

        Returns:
            List of snapshot metadata dicts (most recent first)

        Raises:
            ValueError: If workspace_path not provided
        """
        ...

    @abstractmethod
    def workspace_diff(
        self,
        snapshot_1: int,
        snapshot_2: int,
        workspace_path: str | None = None,
    ) -> dict[str, Any]:
        """Compare two workspace snapshots.

        Args:
            snapshot_1: First snapshot number
            snapshot_2: Second snapshot number
            workspace_path: Path to registered workspace

        Returns:
            Diff dict with added, removed, modified files

        Raises:
            ValueError: If workspace_path not provided
            NexusFileNotFoundError: If either snapshot not found
        """
        ...

    # === Workspace Registry ===

    @abstractmethod
    def register_workspace(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        ttl: timedelta | None = None,
    ) -> dict[str, Any]:
        """Register a workspace path.

        Args:
            path: Path to register as workspace
            name: Optional workspace name
            description: Optional description
            created_by: User/agent who created the workspace
            tags: Optional tags
            metadata: Optional metadata
            session_id: If provided, workspace is session-scoped
            ttl: Time-to-live for auto-expiry

        Returns:
            Workspace registration info
        """
        ...

    @abstractmethod
    def unregister_workspace(self, path: str) -> bool:
        """Unregister a workspace path.

        Args:
            path: Workspace path to unregister

        Returns:
            True if unregistered, False if not found
        """
        ...

    @abstractmethod
    def list_workspaces(self, context: Any | None = None) -> builtins.list[dict]:
        """List all registered workspaces.

        Args:
            context: Optional operation context for filtering

        Returns:
            List of workspace info dicts
        """
        ...

    @abstractmethod
    def get_workspace_info(self, path: str) -> dict | None:
        """Get workspace information.

        Args:
            path: Workspace path

        Returns:
            Workspace info dict or None if not found
        """
        ...
