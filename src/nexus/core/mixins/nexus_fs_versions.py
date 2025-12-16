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
from typing import TYPE_CHECKING, Any, cast

from nexus.core.exceptions import NexusFileNotFoundError
from nexus.core.permissions import Permission
from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.core.router import PathRouter
    from nexus.storage.metadata_store import SQLAlchemyMetadataStore


class NexusFSVersionsMixin:
    """Mixin providing version management operations for NexusFS."""

    # Type hints for attributes/methods that will be provided by NexusFS parent class
    if TYPE_CHECKING:
        metadata: SQLAlchemyMetadataStore
        router: PathRouter
        is_admin: bool

        @property
        def tenant_id(self) -> str | None: ...
        @property
        def agent_id(self) -> str | None: ...

        def _validate_path(self, path: str) -> str: ...
        def _check_permission(
            self, path: str, permission: Permission, context: OperationContext | None
        ) -> None: ...
        def _get_routing_params(
            self, context: OperationContext | dict[Any, Any] | None
        ) -> tuple[str | None, str | None, bool]: ...
        def _get_created_by(
            self, context: OperationContext | dict[Any, Any] | None
        ) -> str | None: ...

    @rpc_expose(description="Get specific file version")
    def get_version(
        self,
        path: str,
        version: int,
        context: OperationContext | None = None,
    ) -> bytes:
        """Get a specific version of a file.

        Retrieves the content for a specific version from CAS using the
        version's content hash.

        Args:
            path: Virtual file path
            version: Version number to retrieve
            context: Operation context for permission checks (uses default if None)

        Returns:
            File content as bytes for the specified version

        Raises:
            NexusFileNotFoundError: If file or version doesn't exist
            InvalidPathError: If path is invalid
            PermissionError: If user doesn't have READ permission

        Example:
            >>> # Get a specific version of a file
            >>> content_v2 = nx.get_version("/workspace/data.txt", version=2)
            >>>
            >>> # Get version with specific context
            >>> ctx = OperationContext(user="alice", groups=[])
            >>> content = nx.get_version("/workspace/file.txt", 5, context=ctx)
        """
        path = self._validate_path(path)

        # Use provided context or default
        ctx = context if context is not None else self._default_context  # type: ignore[attr-defined]

        # Check READ permission (cast to satisfy type checker)
        self._check_permission(path, Permission.READ, cast("OperationContext | None", ctx))

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
            tenant_id=ctx.tenant_id,
            agent_id=ctx.agent_id,
            is_admin=ctx.is_admin,
            check_write=False,
        )

        content = route.backend.read_content(version_meta.etag)
        return content

    @rpc_expose(description="List file versions")
    def list_versions(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """List all versions of a file.

        Returns version history with metadata for each version.

        Args:
            path: Virtual file path
            context: Operation context for permission checks (uses default if None)

        Returns:
            List of version info dicts ordered by version number (newest first)

        Raises:
            InvalidPathError: If path is invalid
            PermissionError: If user doesn't have READ permission

        Example:
            >>> versions = nx.list_versions("/workspace/SKILL.md")
            >>> for v in versions:
            ...     print(f"v{v['version']}: {v['size']} bytes, {v['created_at']}")
            >>>
            >>> # List versions with specific context
            >>> ctx = OperationContext(user="alice", groups=[])
            >>> versions = nx.list_versions("/workspace/file.txt", context=ctx)
        """
        path = self._validate_path(path)

        # Use provided context or default
        ctx = context if context is not None else self._default_context  # type: ignore[attr-defined]

        # Check READ permission (cast to satisfy type checker)
        self._check_permission(path, Permission.READ, cast("OperationContext | None", ctx))

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
            >>> # Rollback to a specific version
            >>> nx.rollback("/workspace/data.txt", version=2)
        """
        import logging

        logger = logging.getLogger(__name__)

        logger.info(f"[ROLLBACK] Starting rollback for path={path}, version={version}")
        path = self._validate_path(path)
        logger.info(f"[ROLLBACK] Validated path: {path}")

        # Check write permission
        logger.info(f"[ROLLBACK] Checking WRITE permission for path={path}, context={context}")
        self._check_permission(path, Permission.WRITE, context)
        logger.info("[ROLLBACK] Permission check passed")

        # Route to backend using context
        logger.info(f"[ROLLBACK] Routing to backend for path={path}")
        tenant_id, agent_id, is_admin = self._get_routing_params(context)
        route = self.router.route(
            path,
            tenant_id=tenant_id,
            agent_id=agent_id,
            is_admin=is_admin,
            check_write=True,
        )
        logger.info(f"[ROLLBACK] Route: backend={route.backend}, readonly={route.readonly}")

        # Check readonly
        if route.readonly:
            raise PermissionError(f"Cannot rollback read-only path: {path}")

        # Perform rollback in metadata store
        # Extract created_by from context for version history tracking
        created_by = self._get_created_by(context)
        logger.info(
            f"[ROLLBACK] Calling metadata.rollback(path={path}, version={version}, created_by={created_by})"
        )
        self.metadata.rollback(path, version, created_by=created_by)
        logger.info("[ROLLBACK] metadata.rollback() completed successfully")

        # Invalidate cache
        if self.metadata._cache_enabled and self.metadata._cache:
            logger.info(f"[ROLLBACK] Invalidating cache for path={path}")
            self.metadata._cache.invalidate_path(path)
            logger.info("[ROLLBACK] Cache invalidated")

        logger.info(f"[ROLLBACK] Rollback completed successfully for path={path}")

    @rpc_expose(description="Compare file versions")
    def diff_versions(
        self,
        path: str,
        v1: int,
        v2: int,
        mode: str = "metadata",
        context: OperationContext | None = None,
    ) -> dict[str, Any] | str:
        """Compare two versions of a file.

        Args:
            path: Virtual file path
            v1: First version number
            v2: Second version number
            mode: Diff mode - "metadata" (default) or "content"
            context: Operation context for permission checks (uses default if None)

        Returns:
            For "metadata" mode: Dict with metadata differences
            For "content" mode: Unified diff string

        Raises:
            NexusFileNotFoundError: If file or version doesn't exist
            InvalidPathError: If path is invalid
            ValueError: If mode is invalid
            PermissionError: If user doesn't have READ permission

        Examples:
            >>> # Get metadata diff
            >>> diff = nx.diff_versions("/workspace/file.txt", v1=1, v2=3)
            >>> print(f"Size changed: {diff['size_v1']} -> {diff['size_v2']}")

            >>> # Get content diff
            >>> diff_text = nx.diff_versions("/workspace/file.txt", v1=1, v2=3, mode="content")
            >>> print(diff_text)
            >>>
            >>> # Diff with specific context
            >>> ctx = OperationContext(user="alice", groups=[])
            >>> diff = nx.diff_versions("/workspace/file.txt", 1, 3, context=ctx)
        """
        path = self._validate_path(path)

        # Use provided context or default
        ctx = context if context is not None else self._default_context  # type: ignore[attr-defined]

        # Check READ permission (cast to satisfy type checker)
        self._check_permission(path, Permission.READ, cast("OperationContext | None", ctx))

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
        content1 = self.get_version(path, v1, context=ctx).decode("utf-8", errors="replace")
        content2 = self.get_version(path, v2, context=ctx).decode("utf-8", errors="replace")

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
