"""Version management operations for NexusFS.

This module contains file version tracking operations:
- get_version: Retrieve a specific version of a file
- list_versions: List all versions of a file
- rollback: Rollback to a previous version
- diff_versions: Compare two versions
"""

from __future__ import annotations

import builtins
import difflib
from typing import TYPE_CHECKING, Any

from nexus.core.exceptions import NexusFileNotFoundError
from nexus.core.permissions import Permission
from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.core.router import PathRouter
    from nexus.storage.metadata_store import SQLAlchemyMetadataStore


class NexusFSVersionsMixin:
    """Mixin providing version management operations for NexusFS."""

    # Type hints for attributes that will be provided by NexusFS parent class
    if TYPE_CHECKING:
        metadata: SQLAlchemyMetadataStore
        router: PathRouter
        tenant_id: str | None
        agent_id: str | None
        is_admin: bool

        def _validate_path(self, path: str) -> str: ...
        def _check_permission(
            self, path: str, permission: Permission, context: OperationContext | None
        ) -> None: ...

    @rpc_expose(description="Get specific file version")
    def get_version(self, path: str, version: int) -> bytes:
        """Get a specific version of a file.

        Retrieves the content for a specific version from CAS using the
        version's content hash.

        Args:
            path: Virtual file path
            version: Version number to retrieve

        Returns:
            File content as bytes for the specified version

        Raises:
            NexusFileNotFoundError: If file or version doesn't exist
            InvalidPathError: If path is invalid

        Example:
            >>> # Get version 2 of a file
            >>> content_v2 = nx.get_version("/workspace/data.txt", version=2)
        """
        path = self._validate_path(path)

        # Get version metadata
        version_meta = self.metadata.get_version(path, version)
        if version_meta is None:
            raise NexusFileNotFoundError(f"{path} (version {version})")

        # Ensure version has content hash
        if version_meta.etag is None:
            raise NexusFileNotFoundError(f"{path} (version {version}) has no content")

        # Read content from CAS using the version's content hash
        route = self.router.route(
            path,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            is_admin=self.is_admin,
            check_write=False,
        )

        content = route.backend.read_content(version_meta.etag)
        return content

    @rpc_expose(description="List file versions")
    def list_versions(self, path: str) -> builtins.list[dict[str, Any]]:
        """List all versions of a file.

        Returns version history with metadata for each version.

        Args:
            path: Virtual file path

        Returns:
            List of version info dicts ordered by version number (newest first)

        Raises:
            InvalidPathError: If path is invalid

        Example:
            >>> versions = nx.list_versions("/workspace/SKILL.md")
            >>> for v in versions:
            ...     print(f"v{v['version']}: {v['size']} bytes, {v['created_at']}")
        """
        path = self._validate_path(path)
        return self.metadata.list_versions(path)

    @rpc_expose(description="Rollback file to previous version")
    def rollback(self, path: str, version: int, context: OperationContext | None = None) -> None:
        """Rollback file to a previous version.

        Updates the file to point to an older version's content from CAS.
        Creates a new version entry marking this as a rollback.

        Args:
            path: Virtual file path
            version: Version number to rollback to
            context: Optional operation context for permission checks

        Raises:
            NexusFileNotFoundError: If file or version doesn't exist
            InvalidPathError: If path is invalid
            PermissionError: If user doesn't have write permission

        Example:
            >>> # Rollback to version 2
            >>> nx.rollback("/workspace/data.txt", version=2)
        """
        path = self._validate_path(path)

        # Check write permission
        self._check_permission(path, Permission.WRITE, context)

        # Route to backend
        route = self.router.route(
            path,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            is_admin=self.is_admin,
            check_write=True,
        )

        # Check readonly
        if route.readonly:
            raise PermissionError(f"Cannot rollback read-only path: {path}")

        # Perform rollback in metadata store
        self.metadata.rollback(path, version)

        # Invalidate cache
        if self.metadata._cache_enabled and self.metadata._cache:
            self.metadata._cache.invalidate_path(path)

    @rpc_expose(description="Compare file versions")
    def diff_versions(
        self, path: str, v1: int, v2: int, mode: str = "metadata"
    ) -> dict[str, Any] | str:
        """Compare two versions of a file.

        Args:
            path: Virtual file path
            v1: First version number
            v2: Second version number
            mode: Diff mode - "metadata" (default) or "content"

        Returns:
            For "metadata" mode: Dict with metadata differences
            For "content" mode: Unified diff string

        Raises:
            NexusFileNotFoundError: If file or version doesn't exist
            InvalidPathError: If path is invalid
            ValueError: If mode is invalid

        Examples:
            >>> # Get metadata diff
            >>> diff = nx.diff_versions("/workspace/file.txt", v1=1, v2=3)
            >>> print(f"Size changed: {diff['size_v1']} -> {diff['size_v2']}")

            >>> # Get content diff
            >>> diff_text = nx.diff_versions("/workspace/file.txt", v1=1, v2=3, mode="content")
            >>> print(diff_text)
        """
        path = self._validate_path(path)

        if mode not in ("metadata", "content"):
            raise ValueError(f"Invalid mode: {mode}. Must be 'metadata' or 'content'")

        # Get metadata diff
        meta_diff = self.metadata.get_version_diff(path, v1, v2)

        if mode == "metadata":
            return meta_diff

        # Content diff mode
        if not meta_diff["content_changed"]:
            return "(no content changes)"

        # Retrieve both versions' content
        content1 = self.get_version(path, v1).decode("utf-8", errors="replace")
        content2 = self.get_version(path, v2).decode("utf-8", errors="replace")

        # Generate unified diff
        lines1 = content1.splitlines(keepends=True)
        lines2 = content2.splitlines(keepends=True)

        diff_lines = list(
            difflib.unified_diff(
                lines1,
                lines2,
                fromfile=f"{path} (v{v1})",
                tofile=f"{path} (v{v2})",
                lineterm="",
            )
        )

        return "\n".join(diff_lines)
