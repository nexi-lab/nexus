"""Core file operations for NexusFS.

This module contains the fundamental file operations:
- read: Read file content
- write: Write file content with optimistic concurrency control
- delete: Delete files
- rename: Rename/move files
- exists: Check file existence
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.core.exceptions import ConflictError, NexusFileNotFoundError
from nexus.core.metadata import FileMetadata
from nexus.core.permissions import Permission

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.core.permission_policy import PolicyMatcher
    from nexus.core.permissions import OperationContext
    from nexus.core.router import PathRouter
    from nexus.parsers.registry import ParserRegistry
    from nexus.storage.metadata_store import SQLAlchemyMetadataStore


class NexusFSCoreMixin:
    """Mixin providing core file operations for NexusFS."""

    # Type hints for attributes that will be provided by NexusFS parent class
    if TYPE_CHECKING:
        metadata: SQLAlchemyMetadataStore
        backend: Backend
        router: PathRouter
        tenant_id: str | None
        agent_id: str | None
        is_admin: bool
        auto_parse: bool
        parser_registry: ParserRegistry
        policy_matcher: PolicyMatcher
        _default_context: OperationContext
        _parser_threads: list[threading.Thread]
        _parser_threads_lock: threading.Lock

        def _validate_path(self, path: str) -> str: ...
        def _check_permission(
            self, path: str, permission: Permission, context: OperationContext | None
        ) -> None: ...
        def _inherit_permissions_from_parent(
            self, path: str, is_directory: bool
        ) -> tuple[str | None, str | None, int | None]: ...
        async def parse(self, path: str, store_result: bool = True) -> Any: ...

    def read(
        self, path: str, context: OperationContext | None = None, return_metadata: bool = False
    ) -> bytes | dict[str, Any]:
        """
        Read file content as bytes.

        Args:
            path: Virtual path to read
            context: Optional operation context for permission checks (uses default if not provided)
            return_metadata: If True, return dict with content and metadata (etag, version, modified_at).
                           If False, return only content bytes (default: False)

        Returns:
            If return_metadata=False: File content as bytes
            If return_metadata=True: Dict with keys:
                - content: File content as bytes
                - etag: Content hash (SHA-256) for optimistic concurrency
                - version: Current version number
                - modified_at: Last modification timestamp
                - size: File size in bytes

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If read operation fails
            AccessDeniedError: If access is denied based on tenant isolation
            PermissionError: If user doesn't have read permission

        Examples:
            >>> # Read content only
            >>> content = nx.read("/workspace/data.json")
            >>> print(content)
            b'{"key": "value"}'

            >>> # Read with metadata for optimistic concurrency
            >>> result = nx.read("/workspace/data.json", return_metadata=True)
            >>> content = result['content']
            >>> etag = result['etag']
            >>> # Later, write with version check
            >>> nx.write("/workspace/data.json", new_content, if_match=etag)
        """
        path = self._validate_path(path)

        # Check read permission (v0.3.0)
        self._check_permission(path, Permission.READ, context)

        # Route to backend with access control
        route = self.router.route(
            path,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            is_admin=self.is_admin,
            check_write=False,
        )

        # Check if file exists in metadata
        meta = self.metadata.get(path)
        if meta is None or meta.etag is None:
            raise NexusFileNotFoundError(path)

        # Read from routed backend using content hash
        content = route.backend.read_content(meta.etag)

        # Return content with metadata if requested
        if return_metadata:
            return {
                "content": content,
                "etag": meta.etag,
                "version": meta.version,
                "modified_at": meta.modified_at,
                "size": meta.size,
            }

        return content

    def write(
        self,
        path: str,
        content: bytes,
        context: OperationContext | None = None,
        if_match: str | None = None,
        if_none_match: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        Write content to a file with optional optimistic concurrency control.

        Creates parent directories if needed. Overwrites existing files.
        Updates metadata store.

        Automatically deduplicates content using CAS.

        Args:
            path: Virtual path to write
            content: File content as bytes
            context: Optional operation context for permission checks (uses default if not provided)
            if_match: Optional etag for optimistic concurrency control (v0.3.9).
                     If provided, write only succeeds if current file etag matches this value.
                     Prevents concurrent modification conflicts.
            if_none_match: If True, write only if file doesn't exist (create-only mode)
            force: If True, skip version check and overwrite unconditionally (dangerous!)

        Returns:
            Dict with metadata about the written file:
                - etag: Content hash (SHA-256) of the written content
                - version: New version number
                - modified_at: Modification timestamp
                - size: File size in bytes

        Raises:
            InvalidPathError: If path is invalid
            BackendError: If write operation fails
            AccessDeniedError: If access is denied (tenant isolation or read-only namespace)
            PermissionError: If path is read-only or user doesn't have write permission
            ConflictError: If if_match is provided and doesn't match current etag (v0.3.9)
            FileExistsError: If if_none_match=True and file already exists

        Examples:
            >>> # Simple write (no version checking)
            >>> result = nx.write("/workspace/data.json", b'{"key": "value"}')
            >>> print(result['etag'], result['version'])

            >>> # Optimistic concurrency control
            >>> result = nx.read("/workspace/data.json", return_metadata=True)
            >>> new_content = modify(result['content'])
            >>> try:
            ...     nx.write("/workspace/data.json", new_content, if_match=result['etag'])
            ... except ConflictError:
            ...     print("File was modified by another agent!")

            >>> # Create-only mode
            >>> nx.write("/workspace/new.txt", b'content', if_none_match=True)
        """
        path = self._validate_path(path)

        # Route to backend with write access check FIRST (to check tenant/agent isolation)
        # This must happen before permission check so AccessDeniedError is raised before PermissionError
        route = self.router.route(
            path,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            is_admin=self.is_admin,
            check_write=True,
        )

        # Check if path is read-only
        if route.readonly:
            raise PermissionError(f"Path is read-only: {path}")

        # Get existing metadata for permission check and update detection (single query)
        now = datetime.now(UTC)
        meta = self.metadata.get(path)

        # Check write permission (v0.3.0)
        # Only check permissions if the file is owned by the current user
        # This allows namespace routing to override Unix permissions when needed
        # Rationale: Namespace isolation is PRIMARY, Unix permissions are SECONDARY
        if meta is not None:
            ctx = context or self._default_context

            # Only check permissions if we own the file
            # If someone else owns it but the router allows write access, namespace wins
            if meta.owner == ctx.user:
                # Existing file owned by us - check permissions to prevent accidental overwrites
                self._check_permission(path, Permission.WRITE, context)
            # If file is owned by someone else, skip permission check
            # The router has already validated namespace-level access (tenant/agent isolation)
        # NOTE: For new files, we do NOT check parent directory permissions because:
        # 1. The router has already validated namespace-level access (tenant/agent isolation)
        # 2. The new file will get correct owner/group/mode via permission policies
        # 3. Checking parent permissions can cause false rejections when namespaces
        #    allow access but parent was created by a different user

        # Optimistic concurrency control (v0.3.9)
        if not force:
            # Check if_none_match (create-only mode)
            if if_none_match and meta is not None:
                raise FileExistsError(f"File already exists: {path}")

            # Check if_match (version check)
            if if_match is not None:
                if meta is None:
                    # File doesn't exist, can't match etag
                    raise ConflictError(
                        path=path,
                        expected_etag=if_match,
                        current_etag="(file does not exist)",
                    )
                elif meta.etag != if_match:
                    # Version mismatch - conflict detected!
                    raise ConflictError(
                        path=path,
                        expected_etag=if_match,
                        current_etag=meta.etag or "(no etag)",
                    )

        # Write to routed backend - returns content hash
        content_hash = route.backend.write_content(content)

        # NOTE: Do NOT delete old content when updating a file!
        # Version history (v0.3.5) preserves references to old content hashes.
        # Old content should only be deleted when ALL versions are deleted.
        # CAS reference counting handles cleanup automatically.

        # Apply permission policy for new files, preserve for existing files
        # Also apply policy if existing file has no permissions set (migration case)
        if meta is None or (meta.owner is None and meta.group is None and meta.mode is None):
            # New file or existing file without permissions - try policy first, then inheritance
            owner = None
            group = None
            mode = None

            # Build context for variable substitution in policy
            policy_context = {
                "agent_id": self.agent_id or "unknown",
                "tenant_id": self.tenant_id or "default",
                "user_id": self.agent_id or "unknown",
            }

            # Try permission policy first
            policy_result = self.policy_matcher.apply_policy(
                path=path,
                tenant_id=self.tenant_id,
                context=policy_context,
                is_directory=False,
            )

            if policy_result:
                owner, group, mode = policy_result
            else:
                # No policy matched - fall back to parent directory inheritance
                owner, group, mode = self._inherit_permissions_from_parent(path, is_directory=False)
        else:  # Existing file with permissions - preserve them
            owner = meta.owner
            group = meta.group
            mode = meta.mode

        # Calculate new version number (increment if updating)
        new_version = (meta.version + 1) if meta else 1

        # Store metadata with content hash as both etag and physical_path
        metadata = FileMetadata(
            path=path,
            backend_name=self.backend.name,
            physical_path=content_hash,  # CAS: hash is the "physical" location
            size=len(content),
            etag=content_hash,  # SHA-256 hash for integrity
            created_at=meta.created_at if meta else now,
            modified_at=now,
            version=new_version,
            owner=owner,  # Apply policy or inherit from parent
            group=group,  # Apply policy or inherit from parent
            mode=mode,  # Apply policy or inherit from parent
        )

        self.metadata.put(metadata)

        # Auto-parse file if enabled and format is supported
        if self.auto_parse:
            self._auto_parse_file(path)

        # Return metadata for optimistic concurrency control (v0.3.9)
        return {
            "etag": content_hash,
            "version": new_version,
            "modified_at": now,
            "size": len(content),
        }

    def _auto_parse_file(self, path: str) -> None:
        """Auto-parse a file in the background (fire-and-forget).

        Args:
            path: Virtual path to the file
        """
        try:
            # Check if parser is available for this file type
            self.parser_registry.get_parser(path)

            # Run parsing in a background thread (fire-and-forget)
            thread = threading.Thread(
                target=self._parse_in_thread,
                args=(path,),
                daemon=True,
            )
            # Track thread for graceful shutdown
            with self._parser_threads_lock:
                self._parser_threads.append(thread)
            thread.start()
        except Exception:
            # Silently ignore if no parser available or parsing fails
            pass

    def _parse_in_thread(self, path: str) -> None:
        """Parse file in a background thread.

        Args:
            path: Virtual path to the file
        """
        # Silently ignore parsing errors
        with contextlib.suppress(Exception):
            # Run async parse in a new event loop (thread-safe)
            asyncio.run(self.parse(path, store_result=True))

    def delete(self, path: str, context: OperationContext | None = None) -> None:
        """
        Delete a file.

        Removes file from backend and metadata store.
        Decrements reference count in CAS (only deletes when ref_count=0).

        Args:
            path: Virtual path to delete
            context: Optional operation context for permission checks (uses default if not provided)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If delete operation fails
            AccessDeniedError: If access is denied (tenant isolation or read-only namespace)
            PermissionError: If path is read-only or user doesn't have write permission
        """
        path = self._validate_path(path)

        # Route to backend with write access check FIRST (to check tenant/agent isolation)
        # This must happen before permission check so AccessDeniedError is raised before PermissionError
        route = self.router.route(
            path,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            is_admin=self.is_admin,
            check_write=True,
        )

        # Check if path is read-only
        if route.readonly:
            raise PermissionError(f"Cannot delete from read-only path: {path}")

        # Check if file exists in metadata
        meta = self.metadata.get(path)
        if meta is None:
            raise NexusFileNotFoundError(path)

        # Check write permission for delete (v0.3.0)
        # This comes AFTER tenant isolation check so AccessDeniedError takes precedence
        self._check_permission(path, Permission.WRITE, context)

        # Delete from routed backend CAS (decrements ref count)
        if meta.etag:
            route.backend.delete_content(meta.etag)

        # Remove from metadata
        self.metadata.delete(path)

    def rename(self, old_path: str, new_path: str) -> None:
        """
        Rename/move a file by updating its path in metadata.

        This is a metadata-only operation that does NOT copy file content.
        The file's content remains in the same location in CAS storage,
        only the virtual path is updated in the metadata database.

        This makes rename/move operations instant, regardless of file size.

        Args:
            old_path: Current virtual path
            new_path: New virtual path

        Raises:
            NexusFileNotFoundError: If source file doesn't exist
            FileExistsError: If destination path already exists
            InvalidPathError: If either path is invalid
            PermissionError: If either path is read-only
            AccessDeniedError: If access is denied (tenant isolation)

        Example:
            >>> nx.rename('/workspace/old.txt', '/workspace/new.txt')
            >>> nx.rename('/folder-a/file.txt', '/shared/folder-a/file.txt')
        """
        old_path = self._validate_path(old_path)
        new_path = self._validate_path(new_path)

        # Route both paths
        old_route = self.router.route(
            old_path,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            is_admin=self.is_admin,
            check_write=True,  # Need write access to source
        )
        new_route = self.router.route(
            new_path,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            is_admin=self.is_admin,
            check_write=True,  # Need write access to destination
        )

        # Check if paths are read-only
        if old_route.readonly:
            raise PermissionError(f"Cannot rename from read-only path: {old_path}")
        if new_route.readonly:
            raise PermissionError(f"Cannot rename to read-only path: {new_path}")

        # Check if source exists
        if not self.metadata.exists(old_path):
            raise NexusFileNotFoundError(old_path)

        # Check if destination already exists
        if self.metadata.exists(new_path):
            raise FileExistsError(f"Destination path already exists: {new_path}")

        # Perform metadata-only rename (no CAS I/O!)
        self.metadata.rename_path(old_path, new_path)

    def exists(self, path: str) -> bool:
        """
        Check if a file or directory exists.

        Args:
            path: Virtual path to check

        Returns:
            True if file or implicit directory exists, False otherwise
        """
        try:
            path = self._validate_path(path)
            # Check if file exists explicitly
            if self.metadata.exists(path):
                return True
            # Check if it's an implicit directory (has files beneath it)
            return self.metadata.is_implicit_directory(path)
        except Exception:  # InvalidPathError
            return False

    def _compute_etag(self, content: bytes) -> str:
        """
        Compute ETag for file content.

        Args:
            content: File content

        Returns:
            ETag (MD5 hash)
        """
        return hashlib.md5(content).hexdigest()
