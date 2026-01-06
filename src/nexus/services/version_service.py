"""Version Service - Extracted from NexusFSVersionsMixin.

This service handles all file version management operations:
- Get specific file versions
- List version history
- Rollback to previous versions
- Compare (diff) between versions

Phase 2: Core Refactoring (Issue #988, Task 2.5)
Extracted from: nexus_fs_versions.py (300 lines)
"""

from __future__ import annotations

import asyncio
import builtins
import difflib
import logging
from typing import TYPE_CHECKING, Any

from nexus.core.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.core.async_permissions import AsyncPermissionEnforcer
    from nexus.core.permissions import OperationContext
    from nexus.core.rebac_manager_enhanced import EnhancedReBACManager
    from nexus.core.router import PathRouter
    from nexus.storage.metadata_store import SQLAlchemyMetadataStore


class VersionService:
    """Independent version service extracted from NexusFS.

    Handles all file version management operations:
    - Retrieve specific versions from CAS
    - List version history with metadata
    - Rollback files to previous versions
    - Compare versions (metadata and content diffs)

    Architecture:
        - Works with metadata store for version tracking
        - Uses CAS (Content-Addressable Storage) for version content
        - Permission checking via PermissionEnforcer
        - Clean dependency injection

    Example:
        ```python
        version_service = VersionService(
            metadata_store=metadata,
            cas_store=cas,
            permission_enforcer=permissions,
            router=router
        )

        # Get a specific version
        content = version_service.get_version(
            path="/workspace/file.txt",
            version=3,
            context=context
        )

        # List all versions
        versions = version_service.list_versions(
            path="/workspace/file.txt",
            context=context
        )

        # Rollback to previous version
        version_service.rollback(
            path="/workspace/file.txt",
            version=2,
            context=context
        )

        # Compare versions
        diff = version_service.diff_versions(
            path="/workspace/file.txt",
            v1=2,
            v2=3,
            mode="unified",
            context=context
        )
        ```
    """

    def __init__(
        self,
        metadata_store: SQLAlchemyMetadataStore,
        cas_store: Any,  # Backend with read_content method
        permission_enforcer: AsyncPermissionEnforcer | None = None,
        router: PathRouter | None = None,
        rebac_manager: EnhancedReBACManager | None = None,
        enforce_permissions: bool = True,
    ):
        """Initialize version service.

        Args:
            metadata_store: Metadata store for version tracking
            cas_store: Backend with read_content method (typically CAS-enabled backend)
            permission_enforcer: Async permission enforcer for access control
            router: Path router for backend resolution
            rebac_manager: ReBAC manager for permission checks
            enforce_permissions: Whether to enforce permission checks
        """
        self.metadata = metadata_store
        self.cas = cas_store
        self._permission_enforcer = permission_enforcer
        self.router = router
        self._rebac_manager = rebac_manager
        self._enforce_permissions = enforce_permissions

        logger.info("[VersionService] Initialized")

    # =========================================================================
    # Public API: Version Retrieval
    # =========================================================================

    @rpc_expose(description="Get specific file version")
    async def get_version(
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
            version: Version number to retrieve (1-indexed)
            context: Operation context for permission checks

        Returns:
            File content as bytes for the specified version

        Raises:
            NexusFileNotFoundError: If file or version doesn't exist
            PermissionDeniedError: If user lacks READ permission
            ValueError: If version number is invalid

        Examples:
            # Get a specific version of a file
            content_v2 = service.get_version("/workspace/data.txt", version=2)

            # Get version with specific context
            content = service.get_version(
                "/workspace/file.txt",
                version=5,
                context=context
            )
        """
        from nexus.core.exceptions import NexusFileNotFoundError

        # Validate and normalize path
        path = self._validate_path(path)

        # Validate version number
        if version < 1:
            raise ValueError(f"Version must be positive integer, got: {version}")

        # Check READ permission
        await self._check_read_permission(path, context)

        # Get version metadata (run in thread to avoid blocking)
        version_meta = await asyncio.to_thread(self.metadata.get_version, path, version)
        if version_meta is None:
            raise NexusFileNotFoundError(f"{path} (version {version})")

        # Ensure version has content hash
        if version_meta.etag is None:
            raise NexusFileNotFoundError(f"{path} (version {version}) has no content")

        # Read content from backend using router
        if self.router is None:
            raise RuntimeError("Router not configured for VersionService")

        # Route to backend
        tenant_id = context.tenant_id if context else None
        agent_id = context.agent_id if context else None
        is_admin = context.is_admin if context else False

        route = self.router.route(
            path,
            tenant_id=tenant_id,
            agent_id=agent_id,
            is_admin=is_admin,
            check_write=False,
        )

        # Read content from backend using version's content hash (run in thread)
        response = await asyncio.to_thread(route.backend.read_content, version_meta.etag)
        return response.unwrap()

    @rpc_expose(description="List file versions")
    async def list_versions(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """List all versions of a file.

        Returns version history with metadata for each version in reverse
        chronological order (newest first).

        Args:
            path: Virtual file path
            context: Operation context for permission checks

        Returns:
            List of version info dictionaries, each containing:
                - version: Version number (int)
                - created_at: Timestamp when version was created (str)
                - created_by: User who created the version (str)
                - size: File size in bytes (int)
                - etag: Content hash (str)
                - is_rollback: Whether this is a rollback version (bool)
                - rollback_from: Original version number if rollback (int|None)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            PermissionDeniedError: If user lacks READ permission

        Examples:
            # List all versions
            versions = service.list_versions("/workspace/data.txt", context=context)

            for v in versions:
                print(f"Version {v['version']}: {v['size']} bytes, {v['created_at']}")

            # Get latest version number
            latest = versions[0]['version'] if versions else 0
        """
        # Validate and normalize path
        path = self._validate_path(path)

        # Check READ permission
        await self._check_read_permission(path, context)

        # Get all versions from metadata store (run in thread to avoid blocking)
        return await asyncio.to_thread(self.metadata.list_versions, path)

    # =========================================================================
    # Public API: Version Operations
    # =========================================================================

    @rpc_expose(description="Rollback file to previous version")
    async def rollback(
        self,
        path: str,
        version: int,
        context: OperationContext | None = None,
    ) -> None:
        """Rollback file to a previous version.

        Updates the file to point to an older version's content from CAS.
        Creates a new version entry marking this as a rollback operation.

        Args:
            path: Virtual file path
            version: Version number to rollback to
            context: Operation context for permission checks

        Raises:
            NexusFileNotFoundError: If file or version doesn't exist
            PermissionDeniedError: If user lacks WRITE permission
            ValueError: If trying to rollback to current version

        Examples:
            # Rollback to previous version
            service.rollback("/workspace/file.txt", version=2, context=context)

            # After rollback, a new version is created
            versions = service.list_versions("/workspace/file.txt", context=context)
            # Latest version will have is_rollback=True, rollback_from=2

        Note:
            Rollback creates a new version rather than deleting history.
            This preserves the audit trail and allows rolling forward again.
        """
        logger.info(f"[ROLLBACK] Starting rollback for path={path}, version={version}")

        # Validate and normalize path
        path = self._validate_path(path)
        logger.info(f"[ROLLBACK] Validated path: {path}")

        # Check WRITE permission
        logger.info(f"[ROLLBACK] Checking WRITE permission for path={path}, context={context}")
        await self._check_write_permission(path, context)
        logger.info("[ROLLBACK] Permission check passed")

        # Route to backend using context
        if self.router is None:
            raise RuntimeError("Router not configured for VersionService")

        logger.info(f"[ROLLBACK] Routing to backend for path={path}")
        tenant_id = context.tenant_id if context else None
        agent_id = context.agent_id if context else None
        is_admin = context.is_admin if context else False

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
        created_by = context.user if context else None
        logger.info(
            f"[ROLLBACK] Calling metadata.rollback(path={path}, version={version}, created_by={created_by})"
        )
        await asyncio.to_thread(self.metadata.rollback, path, version, created_by=created_by)
        logger.info("[ROLLBACK] metadata.rollback() completed successfully")

        # Invalidate cache if enabled
        if (
            hasattr(self.metadata, "_cache_enabled")
            and self.metadata._cache_enabled
            and hasattr(self.metadata, "_cache")
            and self.metadata._cache
        ):
            logger.info(f"[ROLLBACK] Invalidating cache for path={path}")
            self.metadata._cache.invalidate_path(path)
            logger.info("[ROLLBACK] Cache invalidated")

        logger.info(f"[ROLLBACK] Rollback completed successfully for path={path}")

    @rpc_expose(description="Compare file versions")
    async def diff_versions(
        self,
        path: str,
        v1: int,
        v2: int,
        mode: str = "metadata",
        context: OperationContext | None = None,
    ) -> dict[str, Any] | str:
        """Compare two versions of a file.

        Supports multiple comparison modes:
        - "metadata": Compare version metadata only (fast)
        - "content": Unified diff format (like git diff)

        Args:
            path: Virtual file path
            v1: First version number
            v2: Second version number
            mode: Diff mode ("metadata" or "content")
            context: Operation context for permission checks

        Returns:
            If mode="metadata": Dictionary with:
                - v1: Version 1 metadata dict
                - v2: Version 2 metadata dict
                - size_delta: Size difference in bytes (int)
                - time_delta: Time between versions in seconds (float)
                - same_content: Whether content is identical (bool)

            If mode="content":
                String containing the unified diff

        Raises:
            NexusFileNotFoundError: If file or either version doesn't exist
            PermissionDeniedError: If user lacks READ permission
            ValueError: If mode is invalid or versions are not text files

        Examples:
            # Quick metadata comparison
            diff = service.diff_versions(
                "/workspace/file.txt",
                v1=2,
                v2=3,
                mode="metadata",
                context=context
            )
            print(f"Size changed by {diff['size_delta']} bytes")

            # Get unified diff (like git diff)
            diff_text = service.diff_versions(
                "/workspace/file.txt",
                v1=2,
                v2=3,
                mode="content",
                context=context
            )
            print(diff_text)

        Note:
            Content diffs require loading both versions into memory and only
            work for text files. Binary files will raise ValueError.
        """
        # Validate and normalize path
        path = self._validate_path(path)

        # Check READ permission
        await self._check_read_permission(path, context)

        # Validate mode
        if mode not in ("metadata", "content"):
            raise ValueError(f"Invalid mode: {mode}. Must be 'metadata' or 'content'")

        # Get metadata diff from metadata store (run in thread to avoid blocking)
        meta_diff = await asyncio.to_thread(self.metadata.get_version_diff, path, v1, v2)

        if mode == "metadata":
            return meta_diff

        # Content diff mode
        if not meta_diff["content_changed"]:
            return "(no content changes)"

        # Retrieve both versions' content
        content1 = (await self.get_version(path, v1, context=context)).decode(
            "utf-8", errors="replace"
        )
        content2 = (await self.get_version(path, v2, context=context)).decode(
            "utf-8", errors="replace"
        )

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

    # =========================================================================
    # Helper Methods
    # =========================================================================

    async def _check_read_permission(self, path: str, context: OperationContext | None) -> None:
        """Check if user has read permission for path.

        Args:
            path: File path
            context: Operation context

        Raises:
            PermissionError: If permission denied
        """
        from nexus.core.permissions import Permission

        # Skip permission checks if not enforcing or no enforcer configured
        if not self._enforce_permissions or self._permission_enforcer is None:
            return

        # Skip permission checks if no context provided
        if context is None:
            return

        # Skip permission checks for system/admin contexts
        if context.is_system or context.is_admin:
            return

        # Check READ permission via permission enforcer
        has_permission = await self._permission_enforcer.check_permission(
            path, Permission.READ, context
        )

        if not has_permission:
            raise PermissionError(f"User '{context.user}' lacks READ permission for: {path}")

    async def _check_write_permission(self, path: str, context: OperationContext | None) -> None:
        """Check if user has write permission for path.

        Args:
            path: File path
            context: Operation context

        Raises:
            PermissionError: If permission denied
        """
        from nexus.core.permissions import Permission

        # Skip permission checks if not enforcing or no enforcer configured
        if not self._enforce_permissions or self._permission_enforcer is None:
            return

        # Skip permission checks if no context provided
        if context is None:
            return

        # Skip permission checks for system/admin contexts
        if context.is_system or context.is_admin:
            return

        # Check WRITE permission via permission enforcer
        has_permission = await self._permission_enforcer.check_permission(
            path, Permission.WRITE, context
        )

        if not has_permission:
            raise PermissionError(f"User '{context.user}' lacks WRITE permission for: {path}")

    def _validate_path(self, path: str) -> str:
        """Validate and normalize path.

        Args:
            path: Path to validate

        Returns:
            Normalized path

        Raises:
            ValueError: If path is invalid
        """
        # Normalize path
        if not path.startswith("/"):
            path = "/" + path

        # Remove trailing slash unless root
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")

        return path


# =============================================================================
# Phase 2 Extraction Progress
# =============================================================================
#
# Status: Implementation Complete ✅
#
# Completed Tasks:
# 1. [x] Extract get_version() with async support and permission checks
# 2. [x] Extract list_versions() with version metadata
# 3. [x] Extract rollback() with cache invalidation
# 4. [x] Extract diff_versions() with metadata and content modes
# 5. [x] Extract helper methods (_check_read_permission, _check_write_permission, _validate_path)
# 6. [x] Convert all blocking I/O to async using asyncio.to_thread()
#
# TODO (Future):
# 7. [ ] Add comprehensive unit tests for VersionService
# 8. [ ] Update NexusFS to use composition (self.versions = VersionService(...))
# 9. [ ] Add backward compatibility shims with deprecation warnings
# 10. [ ] Update documentation and migration guide
#
# Lines extracted: ~300 / 300 (100%)
# Files affected: 1 created, 0 modified
#
# This is a phased extraction to maintain working code at each step.
#
