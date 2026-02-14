"""Async unified filesystem implementation for Nexus (Phase 3 + Phase 4 Permissions).

Provides async/await interface for file operations by combining:
- AsyncMetadataStore (Phase 1) for metadata storage
- AsyncLocalBackend (Phase 2) for content storage
- AsyncPermissionEnforcer (Phase 4) for permission enforcement

Phase 4 (Issue #940): Full async permission enforcement integration with
AsyncReBACManager for non-blocking permission checks.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nexus.backends.async_local import AsyncLocalBackend
from nexus.core._metadata_generated import (
    DT_DIR,
    DT_REG,
    AsyncFileMetadataWrapper,
    FileMetadata,
)
from nexus.core.exceptions import (
    ConflictError,
    InvalidPathError,
    NexusFileNotFoundError,
    NexusPermissionError,
)
from nexus.core.permissions import OperationContext, Permission
from nexus.storage.content_cache import ContentCache

if TYPE_CHECKING:
    from nexus.core._metadata_generated import FileMetadataProtocol
    from nexus.services.permissions.async_permissions import AsyncPermissionEnforcer

logger = logging.getLogger(__name__)


class AsyncNexusFS:
    """
    Async unified filesystem for Nexus with permission enforcement.

    Provides async file operations (read, write, delete) with metadata tracking
    using content-addressable storage (CAS) for automatic deduplication.

    Phase 4 (Issue #940): Now includes full async permission enforcement via
    AsyncPermissionEnforcer integration. All operations check permissions
    when enforce_permissions=True.

    Example:
        ```python
        from nexus.storage.raft_metadata_store import RaftMetadataStore

        metadata_store = RaftMetadataStore.embedded("./raft")
        fs = AsyncNexusFS(
            backend_root=Path("./data"),
            metadata_store=metadata_store,
        )
        await fs.initialize()

        content = await fs.read("/path/to/file.txt")
        await fs.close()
        ```
    """

    def __init__(
        self,
        backend_root: str | Path,
        metadata_store: FileMetadataProtocol,
        tenant_id: str | None = None,
        enable_content_cache: bool = True,
        content_cache_size_mb: int = 256,
        enforce_permissions: bool = False,
        permission_enforcer: AsyncPermissionEnforcer | None = None,
    ):
        """
        Initialize async filesystem.

        Args:
            backend_root: Root directory for content storage
            metadata_store: FileMetadataProtocol instance (e.g. RaftMetadataStore)
            tenant_id: Default tenant ID for operations
            enable_content_cache: Enable in-memory content caching (default: True)
            content_cache_size_mb: Content cache size in MB (default: 256)
            enforce_permissions: If True, check permissions on operations (default: False)
            permission_enforcer: AsyncPermissionEnforcer instance for permission checks
        """
        self._backend_root = Path(backend_root)
        self._metadata_store = metadata_store
        self._tenant_id = tenant_id
        self._enforce_permissions = enforce_permissions
        self._permission_enforcer = permission_enforcer

        # Create content cache if enabled
        self._content_cache: ContentCache | None = None
        if enable_content_cache:
            self._content_cache = ContentCache(max_size_mb=content_cache_size_mb)

        # Components (initialized in initialize())
        self._backend: AsyncLocalBackend | None = None
        self._metadata: AsyncFileMetadataWrapper | None = None
        self._initialized = False

        # Default context for operations when none is provided
        self._default_context = OperationContext(
            user="system",
            groups=[],
            is_system=True,
            zone_id=tenant_id,  # Map tenant_id to zone_id
        )

    @property
    def tenant_id(self) -> str | None:
        """Default tenant ID."""
        return self._tenant_id

    @property
    def backend(self) -> AsyncLocalBackend:
        """Content storage backend."""
        if self._backend is None:
            raise RuntimeError("AsyncNexusFS not initialized. Call initialize() first.")
        return self._backend

    @property
    def metadata(self) -> AsyncFileMetadataWrapper:
        """Async metadata store."""
        if self._metadata is None:
            raise RuntimeError("AsyncNexusFS not initialized. Call initialize() first.")
        return self._metadata

    async def initialize(self) -> None:
        """Initialize filesystem components."""
        if self._initialized:
            return

        # Initialize backend
        self._backend = AsyncLocalBackend(
            root_path=self._backend_root,
            content_cache=self._content_cache,
        )
        await self._backend.initialize()

        # Wrap sync metadata store with async facade (SSOT-generated)
        self._metadata = AsyncFileMetadataWrapper(self._metadata_store)

        self._initialized = True

    async def close(self) -> None:
        """Close filesystem and release resources."""
        if self._backend:
            await self._backend.close()
        self._initialized = False

    # === Permission Checking (Phase 4 - Issue #940) ===

    def _get_context(self, context: OperationContext | None) -> OperationContext:
        """Get operation context, using default if none provided."""
        return context if context is not None else self._default_context

    async def _acheck_permission(
        self,
        path: str,
        permission: Permission,
        context: OperationContext | None = None,
    ) -> None:
        """Check permission asynchronously, raising NexusPermissionError if denied.

        Args:
            path: Virtual path to check permission for
            permission: Permission to check (READ, WRITE, EXECUTE)
            context: Operation context with user identity

        Raises:
            NexusPermissionError: If permission is denied
        """
        if not self._enforce_permissions:
            return

        ctx = self._get_context(context)

        # System and admin bypass all permission checks
        if ctx.is_system or ctx.is_admin:
            return

        if self._permission_enforcer is None:
            # No enforcer = permissive mode
            return

        # Check permission via AsyncPermissionEnforcer
        allowed = await self._permission_enforcer.check_permission(path, permission, ctx)

        if not allowed:
            permission_name = permission.name if hasattr(permission, "name") else str(permission)
            raise NexusPermissionError(
                path=path,
                message=f"Permission denied: {ctx.user} does not have {permission_name} permission on {path}",
            )

    # === Path Utilities ===

    def _validate_path(self, path: str) -> str:
        """Validate and normalize path."""
        if not path:
            raise InvalidPathError("Path cannot be empty")

        # Normalize path (remove duplicate slashes, etc.)
        path = re.sub(r"/+", "/", path)

        # Ensure absolute path
        if not path.startswith("/"):
            raise InvalidPathError(f"Path must be absolute: {path}")

        # Remove trailing slash (except for root)
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")

        return path

    def _get_parent_path(self, path: str) -> str | None:
        """Get parent path."""
        if path == "/":
            return None
        parts = path.rsplit("/", 1)
        return parts[0] if parts[0] else "/"

    async def _ensure_parent_dirs(self, path: str) -> None:
        """Ensure parent directories exist in metadata.

        Handles race conditions when multiple tasks try to create the same
        parent directory by catching unique constraint violations.
        """
        parent = self._get_parent_path(path)
        if parent is None or parent == "/":
            return

        # Check if parent exists
        if not await self.metadata.aexists(parent):
            # Recursively ensure grandparent exists
            await self._ensure_parent_dirs(parent)

            # Create parent directory entry
            now = datetime.now(UTC)
            dir_meta = FileMetadata(
                path=parent,
                size=0,
                created_at=now,
                modified_at=now,
                entry_type=DT_DIR,
                backend_name="local",
                physical_path=parent,  # Use path as physical_path for directories
            )

            try:
                await self.metadata.aput(dir_meta)
            except Exception as e:
                # Handle race condition - another task might have created
                # the directory between our exists check and put
                error_str = str(e).lower()
                if (
                    "unique" in error_str or "duplicate" in error_str
                ) and await self.metadata.aexists(parent):
                    # Directory was created by another task - that's fine
                    return
                # Re-raise if it's not a duplicate key error
                raise

    # === Core File Operations ===

    async def write(
        self,
        path: str,
        content: bytes | str,
        if_match: str | None = None,
        if_none_match: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """
        Write content to a file with optional optimistic concurrency control.

        Creates parent directories if needed. Overwrites existing files.

        Args:
            path: Virtual path to write
            content: File content as bytes or str
            if_match: Optional etag for optimistic concurrency control
            if_none_match: If True, write only if file doesn't exist
            context: Operation context for permission checking

        Returns:
            Dict with metadata about the written file

        Raises:
            InvalidPathError: If path is invalid
            ConflictError: If if_match doesn't match current etag
            FileExistsError: If if_none_match=True and file exists
            NexusPermissionError: If user lacks WRITE permission
        """
        # Auto-convert str to bytes
        if isinstance(content, str):
            content = content.encode("utf-8")

        path = self._validate_path(path)

        # Check WRITE permission (Phase 4 - Issue #940)
        await self._acheck_permission(path, Permission.WRITE, context)

        # Get existing metadata for concurrency control
        existing_meta = await self.metadata.aget(path)

        # Optimistic concurrency control
        if if_none_match and existing_meta is not None:
            raise FileExistsError(f"File already exists: {path}")

        if if_match is not None:
            if existing_meta is None:
                raise ConflictError(
                    path=path,
                    expected_etag=if_match,
                    current_etag="(file does not exist)",
                )
            elif existing_meta.etag != if_match:
                raise ConflictError(
                    path=path,
                    expected_etag=if_match,
                    current_etag=existing_meta.etag or "(no etag)",
                )

        # Ensure parent directories exist
        await self._ensure_parent_dirs(path)

        # Write content to backend
        write_result = await self.backend.write_content(content)
        content_hash = write_result.unwrap()

        # Update metadata
        now = datetime.now(UTC)
        new_version = (existing_meta.version + 1) if existing_meta else 1

        file_meta = FileMetadata(
            path=path,
            size=len(content),
            etag=content_hash,
            created_at=existing_meta.created_at if existing_meta else now,
            modified_at=now,
            entry_type=DT_REG,
            version=new_version,
            backend_name="local",
            physical_path=content_hash,
        )

        await self.metadata.aput(file_meta)

        return {
            "etag": content_hash,
            "version": new_version,
            "modified_at": now.isoformat(),
            "size": len(content),
        }

    async def read(
        self,
        path: str,
        return_metadata: bool = False,
        context: OperationContext | None = None,
    ) -> bytes | dict[str, Any]:
        """
        Read file content.

        Args:
            path: Virtual path to read
            return_metadata: If True, return dict with content and metadata
            context: Operation context for permission checking

        Returns:
            File content as bytes, or dict with content and metadata

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            NexusPermissionError: If user lacks READ permission
        """
        path = self._validate_path(path)

        # Check READ permission (Phase 4 - Issue #940)
        await self._acheck_permission(path, Permission.READ, context)

        # Get metadata
        meta = await self.metadata.aget(path)
        if meta is None:
            raise NexusFileNotFoundError(path=path)

        if meta.is_dir:
            raise NexusFileNotFoundError(path=path, message=f"Path is a directory: {path}")

        if meta.etag is None:
            raise NexusFileNotFoundError(path=path, message=f"File has no content: {path}")

        # Read content from backend
        read_result = await self.backend.read_content(meta.etag)
        content = read_result.unwrap()

        if return_metadata:
            return {
                "content": content,
                "etag": meta.etag,
                "version": meta.version,
                "modified_at": meta.modified_at.isoformat() if meta.modified_at else None,
                "size": meta.size,
            }

        return content

    async def delete(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """
        Delete a file.

        Args:
            path: Virtual path to delete
            context: Operation context for permission checking

        Returns:
            Dict indicating deletion success

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            NexusPermissionError: If user lacks WRITE permission (delete = write)
        """
        path = self._validate_path(path)

        # Check WRITE permission (delete requires write - Phase 4 - Issue #940)
        await self._acheck_permission(path, Permission.WRITE, context)

        # Get metadata
        meta = await self.metadata.aget(path)
        if meta is None:
            raise NexusFileNotFoundError(path=path)

        # Delete content from backend (decrements ref count)
        if meta.etag:
            await self.backend.delete_content(meta.etag)

        # Delete metadata
        await self.metadata.adelete(path)

        return {"deleted": True, "path": path}

    async def exists(
        self,
        path: str,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> bool:
        """
        Check if path exists.

        Args:
            path: Virtual path to check
            context: Operation context (reserved for future permission checks)

        Returns:
            True if path exists
        """
        path = self._validate_path(path)
        # Note: exists doesn't require permission check - just checks existence
        return await self.metadata.aexists(path)

    # === Directory Operations ===

    async def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | None = None,
    ) -> None:
        """
        Create a directory.

        Args:
            path: Directory path to create
            parents: Create parent directories if needed
            exist_ok: Don't raise error if directory exists
            context: Operation context for permission checking
        """
        path = self._validate_path(path)

        # Check WRITE permission for creating directory (Phase 4 - Issue #940)
        await self._acheck_permission(path, Permission.WRITE, context)

        # Check if already exists
        if await self.metadata.aexists(path):
            if exist_ok:
                return
            raise FileExistsError(f"Directory already exists: {path}")

        # Ensure parents exist
        if parents:
            await self._ensure_parent_dirs(path)
        else:
            parent = self._get_parent_path(path)
            if parent and parent != "/" and not await self.metadata.aexists(parent):
                raise FileNotFoundError(f"Parent directory does not exist: {parent}")

        # Create directory
        now = datetime.now(UTC)
        dir_meta = FileMetadata(
            path=path,
            size=0,
            created_at=now,
            modified_at=now,
            entry_type=DT_DIR,
            backend_name="local",
            physical_path=path,  # Use path as physical_path for directories
        )
        await self.metadata.aput(dir_meta)

    async def list_dir(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> list[str]:
        """
        List directory contents.

        When permission enforcement is enabled, only returns items the user
        has READ permission for.

        Args:
            path: Directory path to list
            context: Operation context for permission filtering

        Returns:
            List of items (files and subdirectories) user has access to
        """
        path = self._validate_path(path)

        # Ensure path ends with / for prefix matching
        prefix = path if path.endswith("/") else f"{path}/"
        if prefix == "//":
            prefix = "/"

        # Get direct children from metadata (recursive=False)
        children = await self.metadata.alist(prefix=prefix, recursive=False)

        # Filter by permissions if enforcement is enabled (Phase 4 - Issue #940)
        if self._enforce_permissions and self._permission_enforcer:
            ctx = self._get_context(context)
            if not ctx.is_system and not ctx.is_admin:
                # Get all child paths
                child_paths = [child.path for child in children]

                # Bulk filter by READ permission
                allowed_paths = await self._permission_enforcer.filter_paths_by_permission(
                    child_paths, ctx
                )
                allowed_set = set(allowed_paths)

                # Filter children to only allowed ones
                children = [child for child in children if child.path in allowed_set]

        result = []
        for child_meta in children:
            # Extract name from path
            child_path = child_meta.path
            # Remove the prefix to get just the name
            if child_path.startswith(prefix):
                name = child_path[len(prefix) :]
            else:
                name = child_path.rsplit("/", 1)[-1]

            # Skip empty names (can happen with prefix matching)
            if not name:
                continue

            # Add trailing slash for directories
            if child_meta.is_dir:
                name = f"{name}/"

            result.append(name)

        return result

    # === Streaming Operations ===

    async def stream_read(
        self,
        path: str,
        chunk_size: int = 65536,
        context: OperationContext | None = None,
    ) -> AsyncIterator[bytes]:
        """
        Stream read file content in chunks.

        Args:
            path: Virtual path to read
            chunk_size: Size of each chunk in bytes
            context: Operation context for permission checking

        Yields:
            Byte chunks of the content
        """
        path = self._validate_path(path)

        # Check READ permission (Phase 4 - Issue #940)
        await self._acheck_permission(path, Permission.READ, context)

        # Get metadata
        meta = await self.metadata.aget(path)
        if meta is None or meta.etag is None:
            raise NexusFileNotFoundError(path=path)

        # Stream from backend
        async for chunk in self.backend.stream_content(meta.etag, chunk_size=chunk_size):
            yield chunk

    async def stream_read_range(
        self,
        path: str,
        start: int,
        end: int,
        chunk_size: int = 8192,
        context: OperationContext | None = None,
    ) -> AsyncIterator[bytes]:
        """Stream a byte range of file content.

        Args:
            path: Virtual path to read
            start: First byte position (inclusive, 0-based)
            end: Last byte position (inclusive, 0-based)
            chunk_size: Size of each chunk in bytes
            context: Operation context for permission checking

        Yields:
            Byte chunks covering the requested range
        """
        path = self._validate_path(path)
        await self._acheck_permission(path, Permission.READ, context)

        meta = await self.metadata.aget(path)
        if meta is None or meta.etag is None:
            raise NexusFileNotFoundError(path=path)

        async for chunk in self.backend.stream_range(meta.etag, start, end, chunk_size=chunk_size):
            yield chunk

    async def stream_write(
        self,
        path: str,
        chunks: AsyncIterator[bytes],
    ) -> dict[str, Any]:
        """
        Write content from an async iterator of chunks.

        Args:
            path: Virtual path to write
            chunks: Async iterator yielding byte chunks

        Returns:
            Dict with metadata about the written file
        """
        path = self._validate_path(path)

        # Ensure parent directories exist
        await self._ensure_parent_dirs(path)

        # Write stream to backend
        write_result = await self.backend.write_stream(chunks)
        content_hash = write_result.unwrap()

        # Get size from backend
        size_result = await self.backend.get_content_size(content_hash)
        size = size_result.unwrap()

        # Get existing metadata
        existing_meta = await self.metadata.aget(path)

        # Update metadata
        now = datetime.now(UTC)
        new_version = (existing_meta.version + 1) if existing_meta else 1

        file_meta = FileMetadata(
            path=path,
            size=size,
            etag=content_hash,
            created_at=existing_meta.created_at if existing_meta else now,
            modified_at=now,
            entry_type=DT_REG,
            version=new_version,
            backend_name="local",
            physical_path=content_hash,
        )

        await self.metadata.aput(file_meta)

        return {
            "etag": content_hash,
            "version": new_version,
            "modified_at": now.isoformat(),
            "size": size,
        }

    # === Metadata Operations ===

    async def get_metadata(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> FileMetadata | None:
        """
        Get file metadata.

        Args:
            path: Virtual path
            context: Operation context for permission checking

        Returns:
            FileMetadata or None if not found
        """
        path = self._validate_path(path)

        # Check READ permission for metadata (Phase 4 - Issue #940)
        await self._acheck_permission(path, Permission.READ, context)

        return await self.metadata.aget(path)

    # === Batch Operations ===

    async def batch_read(
        self,
        paths: list[str],
        context: OperationContext | None = None,
    ) -> dict[str, bytes | None]:
        """
        Read multiple files in batch.

        Args:
            paths: List of virtual paths to read
            context: Operation context for permission checking

        Returns:
            Dict mapping path to content (or None if not found)
        """
        result: dict[str, bytes | None] = {}

        # Check READ permission for all paths (Phase 4 - Issue #940)
        for path in paths:
            validated_path = self._validate_path(path)
            await self._acheck_permission(validated_path, Permission.READ, context)

        # Get metadata for all paths
        etag_to_paths: dict[str, list[str]] = {}
        for path in paths:
            path = self._validate_path(path)
            meta = await self.metadata.aget(path)
            if meta is None or meta.etag is None:
                result[path] = None
            else:
                if meta.etag not in etag_to_paths:
                    etag_to_paths[meta.etag] = []
                etag_to_paths[meta.etag].append(path)

        # Batch read content by etag
        if etag_to_paths:
            content_results = await self.backend.batch_read_content(list(etag_to_paths.keys()))

            # Map content back to paths
            for etag, file_paths in etag_to_paths.items():
                content = content_results.get(etag)
                for file_path in file_paths:
                    result[file_path] = content

        return result
