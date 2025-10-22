"""Unified filesystem implementation for Nexus."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import fnmatch
import hashlib
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from nexus.backends.backend import Backend
from nexus.core.exceptions import ConflictError, InvalidPathError, NexusFileNotFoundError
from nexus.core.export_import import (
    CollisionDetail,
    ExportFilter,
    ImportOptions,
    ImportResult,
)
from nexus.core.filesystem import NexusFilesystem
from nexus.core.metadata import FileMetadata
from nexus.core.permissions import OperationContext, Permission
from nexus.core.router import NamespaceConfig, PathRouter
from nexus.parsers import MarkItDownParser, ParserRegistry
from nexus.parsers.types import ParseResult
from nexus.storage.metadata_store import SQLAlchemyMetadataStore


class NexusFS(NexusFilesystem):
    """
    Unified filesystem for Nexus.

    Provides file operations (read, write, delete) with metadata tracking
    using content-addressable storage (CAS) for automatic deduplication.

    Works with any backend (local, GCS, S3, etc.) that implements the Backend interface.

    All backends use CAS by default for:
    - Automatic deduplication (same content stored once)
    - Content integrity (hash verification)
    - Efficient storage
    """

    def __init__(
        self,
        backend: Backend,
        db_path: str | Path | None = None,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        is_admin: bool = False,
        custom_namespaces: list[NamespaceConfig] | None = None,
        enable_metadata_cache: bool = True,
        cache_path_size: int = 512,
        cache_list_size: int = 128,
        cache_kv_size: int = 256,
        cache_exists_size: int = 1024,
        cache_ttl_seconds: int | None = 300,
        auto_parse: bool = True,
        custom_parsers: list[dict[str, Any]] | None = None,
        enforce_permissions: bool = True,
    ):
        """
        Initialize filesystem.

        Args:
            backend: Backend instance for storing file content (LocalBackend, GCSBackend, etc.)
            db_path: Path to SQLite metadata database (auto-generated if None)
            tenant_id: Tenant identifier for multi-tenant isolation (optional)
            agent_id: Agent identifier for agent-level isolation in /workspace (optional)
            is_admin: Whether this instance has admin privileges (default: False)
            custom_namespaces: Additional custom namespace configurations (optional)
            enable_metadata_cache: Enable in-memory metadata caching (default: True)
            cache_path_size: Max entries for path metadata cache (default: 512)
            cache_list_size: Max entries for directory listing cache (default: 128)
            cache_kv_size: Max entries for file metadata KV cache (default: 256)
            cache_exists_size: Max entries for existence check cache (default: 1024)
            cache_ttl_seconds: Cache TTL in seconds, None = no expiry (default: 300)
            auto_parse: Automatically parse files on write (default: True)
            custom_parsers: Custom parser configurations from config (optional)
            enforce_permissions: Enable permission enforcement on file operations (default: True)
        """
        # Store backend
        self.backend = backend

        # Store tenant and agent context
        self.tenant_id = tenant_id
        self.agent_id = agent_id
        self.is_admin = is_admin
        self.auto_parse = auto_parse

        # Initialize metadata store (using new SQLAlchemy-based store)
        if db_path is None:
            # Default to current directory
            db_path = Path("./nexus-metadata.db")
        self.metadata = SQLAlchemyMetadataStore(
            db_path=db_path,
            enable_cache=enable_metadata_cache,
            cache_path_size=cache_path_size,
            cache_list_size=cache_list_size,
            cache_kv_size=cache_kv_size,
            cache_exists_size=cache_exists_size,
            cache_ttl_seconds=cache_ttl_seconds,
        )

        # Initialize path router with default namespaces
        self.router = PathRouter()

        # Register custom namespaces if provided
        if custom_namespaces:
            for ns_config in custom_namespaces:
                self.router.register_namespace(ns_config)

        # Mount backend
        self.router.add_mount("/", self.backend, priority=0)

        # Initialize parser registry with default MarkItDown parser
        self.parser_registry = ParserRegistry()
        self.parser_registry.register(MarkItDownParser())

        # Load custom parsers from config
        if custom_parsers:
            self._load_custom_parsers(custom_parsers)

        # Track active parser threads for graceful shutdown
        self._parser_threads: list[threading.Thread] = []
        self._parser_threads_lock = threading.Lock()

        # Initialize permission policy system
        from nexus.core.permission_policy import PolicyMatcher, create_default_policies
        from nexus.storage.policy_store import PolicyStore

        # Load policies from database
        with self.metadata.SessionLocal() as session:
            policy_store = PolicyStore(session)
            policies = policy_store.list_policies(tenant_id=self.tenant_id)

            # If no policies exist, create and store default ones
            if not policies:
                default_policies = create_default_policies()
                for policy in default_policies:
                    policy_store.create_policy(policy)
                policies = default_policies

        self.policy_matcher = PolicyMatcher(policies)

        # Initialize permission enforcer (v0.3.0)
        from nexus.core.acl import ACLStore
        from nexus.core.permissions import OperationContext, PermissionEnforcer
        from nexus.core.rebac_manager import ReBACManager

        # Create default operation context from init parameters
        # This context is used for all operations unless overridden per-call
        user = agent_id or tenant_id or "system"
        groups: list[str] = []
        if tenant_id:
            groups.append(tenant_id)

        self._default_context = OperationContext(
            user=user,
            groups=groups,
            is_admin=is_admin,
            is_system=(user == "system"),
        )

        # Initialize ACL and ReBAC stores for multi-layer permission checking
        acl_store = ACLStore(metadata_store=self.metadata)

        # Initialize ReBACManager with same database as metadata store
        self._rebac_manager = ReBACManager(
            db_path=str(self.metadata.db_path),
            cache_ttl_seconds=cache_ttl_seconds or 300,
            max_depth=10,
        )

        # Initialize permission enforcer with full multi-layer support
        self._permission_enforcer = PermissionEnforcer(
            metadata_store=self.metadata,
            acl_store=acl_store,
            rebac_manager=self._rebac_manager,
        )

        # Permission enforcement is opt-in for backward compatibility
        # Set enforce_permissions=True in init to enable permission checks
        self._enforce_permissions = enforce_permissions

    def _load_custom_parsers(self, parser_configs: list[dict[str, Any]]) -> None:
        """
        Dynamically load and register custom parsers from configuration.

        Args:
            parser_configs: List of parser configurations, each containing:
                - module: Python module path (e.g., "my_parsers.csv_parser")
                - class: Parser class name (e.g., "CSVParser")
                - priority: Optional priority (default: 50)
                - enabled: Optional enabled flag (default: True)
        """
        import importlib

        for config in parser_configs:
            # Skip disabled parsers
            if not config.get("enabled", True):
                continue

            try:
                module_path = config.get("module")
                class_name = config.get("class")

                if not module_path or not class_name:
                    continue

                # Dynamically import the module
                module = importlib.import_module(module_path)

                # Get the parser class
                parser_class = getattr(module, class_name)

                # Get priority (default: 50)
                priority = config.get("priority", 50)

                # Instantiate the parser with priority
                parser_instance = parser_class(priority=priority)

                # Register with registry
                self.parser_registry.register(parser_instance)

            except Exception:
                # Silently skip parsers that fail to load
                # This prevents config errors from breaking the entire system
                # In production environments, enable logging to see errors
                pass

    def _validate_path(self, path: str) -> str:
        """
        Validate virtual path.

        Args:
            path: Virtual path to validate

        Returns:
            Normalized path

        Raises:
            InvalidPathError: If path is invalid
        """
        if not path:
            raise InvalidPathError("", "Path cannot be empty")

        # Ensure path starts with /
        if not path.startswith("/"):
            path = "/" + path

        # Check for invalid characters
        invalid_chars = ["\0", "\n", "\r"]
        for char in invalid_chars:
            if char in path:
                raise InvalidPathError(path, f"Path contains invalid character: {repr(char)}")

        # Check for parent directory traversal
        if ".." in path:
            raise InvalidPathError(path, "Path contains '..' segments")

        return path

    def _compute_etag(self, content: bytes) -> str:
        """
        Compute ETag for file content.

        Args:
            content: File content

        Returns:
            ETag (MD5 hash)
        """
        return hashlib.md5(content).hexdigest()

    def _get_parent_path(self, path: str) -> str | None:
        """
        Get parent directory path from a file path.

        Args:
            path: Virtual file path

        Returns:
            Parent directory path, or None if path is root

        Examples:
            >>> fs._get_parent_path("/workspace/file.txt")
            '/workspace'
            >>> fs._get_parent_path("/file.txt")
            '/'
            >>> fs._get_parent_path("/")
            None
        """
        if path == "/":
            return None

        # Remove trailing slash if present
        path = path.rstrip("/")

        # Find last slash
        last_slash = path.rfind("/")
        if last_slash == 0:
            # Parent is root
            return "/"
        elif last_slash > 0:
            return path[:last_slash]
        else:
            # No parent (shouldn't happen for valid paths)
            return None

    def _inherit_permissions_from_parent(
        self, path: str, is_directory: bool
    ) -> tuple[str | None, str | None, int | None]:
        """
        Inherit permissions from parent directory.

        Args:
            path: Virtual path of the new file/directory
            is_directory: Whether the new item is a directory

        Returns:
            Tuple of (owner, group, mode) inherited from parent, or (None, None, None) if no inheritance
        """
        from nexus.core.permissions import FileMode, FilePermissions, PermissionInheritance

        # Get parent path
        parent_path = self._get_parent_path(path)
        if parent_path is None:
            # No parent (root level)
            return (None, None, None)

        # Get parent metadata
        parent_meta = self.metadata.get(parent_path)
        if parent_meta is None or parent_meta.owner is None:
            # Parent doesn't exist or has no permissions set
            return (None, None, None)

        # Create FilePermissions from parent metadata
        try:
            parent_perms = FilePermissions(
                owner=parent_meta.owner,
                group=parent_meta.group or parent_meta.owner,
                mode=FileMode(parent_meta.mode if parent_meta.mode is not None else 0o755),
            )
        except Exception:
            # Failed to create parent permissions
            return (None, None, None)

        # Use PermissionInheritance to compute child permissions
        inheritance = PermissionInheritance()
        child_perms = inheritance.inherit_from_parent(parent_perms, is_directory)

        return (child_perms.owner, child_perms.group, child_perms.mode.mode)

    def _check_permission(
        self,
        path: str,
        permission: Permission,
        context: OperationContext | None = None,
    ) -> None:
        """Check if operation is permitted.

        Args:
            path: Virtual file path
            permission: Permission to check (READ, WRITE, EXECUTE)
            context: Optional operation context (defaults to self._default_context)

        Raises:
            PermissionError: If access is denied
        """
        # Skip if permission enforcement is disabled
        if not self._enforce_permissions:
            return

        # Use default context if none provided
        ctx = context or self._default_context

        # Check permission using enforcer
        if not self._permission_enforcer.check(path, permission, ctx):
            raise PermissionError(
                f"Access denied: User '{ctx.user}' does not have {permission.name} "
                f"permission for '{path}'"
            )

    def _create_directory_metadata(self, path: str) -> None:
        """
        Create metadata entry for a directory.

        Args:
            path: Virtual path to directory
        """
        now = datetime.now(UTC)

        # Inherit permissions from parent directory
        owner, group, mode = self._inherit_permissions_from_parent(path, is_directory=True)

        # Create a marker for the directory in metadata
        # We use an empty content hash as a placeholder
        empty_hash = hashlib.sha256(b"").hexdigest()

        metadata = FileMetadata(
            path=path,
            backend_name=self.backend.name,
            physical_path=empty_hash,  # Placeholder for directory
            size=0,  # Directories have size 0
            etag=empty_hash,
            mime_type="inode/directory",  # MIME type for directories
            created_at=now,
            modified_at=now,
            version=1,
            owner=owner,
            group=group,
            mode=mode or 0o755,  # Default directory mode if no inheritance
        )

        self.metadata.put(metadata)

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

        # Check write permission (v0.3.0)
        # For existing files, check file's write permission
        # For new files, check parent directory's write permission (if parent exists)
        if self.metadata.exists(path):
            # Existing file - check file's own write permission
            self._check_permission(path, Permission.WRITE, context)
        else:
            # New file - check parent directory's write permission
            # Only check if parent exists (parent will be created automatically if needed)
            parent_path = self._get_parent_path(path)
            if parent_path and self.metadata.exists(parent_path):
                self._check_permission(parent_path, Permission.WRITE, context)

        # Route to backend with write access check
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

        # Get existing metadata for update detection
        now = datetime.now(UTC)
        meta = self.metadata.get(path)

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
        except InvalidPathError:
            return False

    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        prefix: str | None = None,
        context: OperationContext | None = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """
        List files in a directory.

        Args:
            path: Directory path to list (default: "/")
            recursive: If True, list all files recursively; if False, list only direct children (default: True)
            details: If True, return detailed metadata; if False, return paths only (default: False)
            prefix: (Deprecated) Path prefix to filter by - for backward compatibility.
                    When used, lists all files recursively with this prefix.
            context: Optional operation context for permission filtering (uses default if not provided)

        Returns:
            List of file paths (if details=False) or list of file metadata dicts (if details=True).
            Each metadata dict contains: path, size, modified_at, etag
            Results are filtered by read permission.

        Examples:
            # List all files recursively (default)
            fs.list()  # Returns: ["/file1.txt", "/dir/file2.txt", "/dir/subdir/file3.txt"]

            # List files in root directory only (non-recursive)
            fs.list("/", recursive=False)  # Returns: ["/file1.txt"]

            # List files recursively with details
            fs.list(details=True)  # Returns: [{"path": "/file1.txt", "size": 100, ...}, ...]

            # Old API (deprecated but supported)
            fs.list(prefix="/dir")  # Returns all files under /dir recursively
        """
        # Handle backward compatibility with old 'prefix' parameter
        if prefix is not None:
            # Old API: list(prefix="/path") - always recursive
            if prefix:
                prefix = self._validate_path(prefix)
            all_files = self.metadata.list(prefix)
            results = all_files
        else:
            # New API: list(path="/", recursive=False)
            if path:
                path = self._validate_path(path)

            # Ensure path ends with / for directory listing
            if not path.endswith("/"):
                path = path + "/"

            # Get all files with this prefix
            all_files = self.metadata.list(path if path != "/" else "")

            if recursive:
                # Include all files under this path
                results = all_files
            else:
                # Only include files directly in this directory (no subdirectories)
                results = []
                for meta in all_files:
                    # Remove the prefix to get relative path
                    rel_path = meta.path[len(path) :] if path != "/" else meta.path[1:]
                    # If there's no "/" in the relative path, it's in this directory
                    if "/" not in rel_path:
                        results.append(meta)

        # Filter by read permission (v0.3.0)
        if self._enforce_permissions:
            ctx = context or self._default_context
            result_paths = [meta.path for meta in results]
            allowed_paths = self._permission_enforcer.filter_list(result_paths, ctx)
            # Filter results to only include allowed paths
            results = [meta for meta in results if meta.path in allowed_paths]

        # Sort by path name
        results.sort(key=lambda m: m.path)

        # Add directories to results (infer from file paths + check backend)
        # This ensures empty directories show up in listings
        directories = set()

        if not recursive:
            # For non-recursive listings, infer immediate subdirectories from file paths
            base_path = path if path != "/" else ""

            # Get all files to infer directories
            all_files_for_dirs = self.metadata.list(base_path)
            for meta in all_files_for_dirs:
                # Get relative path
                rel_path = meta.path[len(path) :] if path != "/" else meta.path[1:]
                # Check if there's a directory component
                if "/" in rel_path:
                    # Extract first directory component
                    dir_name = rel_path.split("/")[0]
                    dir_path = path + dir_name if path != "/" else "/" + dir_name
                    directories.add(dir_path)

            # Check backend for empty directories (directories with no files)
            # This catches newly created directories using the helper method
            backend_dirs = self._get_backend_directory_entries(path)
            directories.update(backend_dirs)

        if details:
            file_results = [
                {
                    "path": meta.path,
                    "size": meta.size,
                    "modified_at": meta.modified_at,
                    "created_at": meta.created_at,
                    "etag": meta.etag,
                    "mime_type": meta.mime_type,
                    "is_directory": False,
                }
                for meta in results
            ]

            # Add directory entries
            dir_results = [
                {
                    "path": dir_path,
                    "size": 0,
                    "modified_at": None,
                    "created_at": None,
                    "etag": None,
                    "mime_type": None,
                    "is_directory": True,
                }
                for dir_path in sorted(directories)
            ]

            # Combine and sort
            all_results = file_results + dir_results
            all_results.sort(key=lambda x: str(x["path"]))
            return all_results
        else:
            # Return paths only
            all_paths = [meta.path for meta in results] + sorted(directories)
            all_paths.sort()
            return all_paths

    def glob(self, pattern: str, path: str = "/") -> builtins.list[str]:
        """
        Find files matching a glob pattern.

        Supports standard glob patterns:
        - `*` matches any sequence of characters (except `/`)
        - `**` matches any sequence of characters including `/` (recursive)
        - `?` matches any single character
        - `[...]` matches any character in the brackets

        Args:
            pattern: Glob pattern to match (e.g., "**/*.py", "data/*.csv", "test_*.py")
            path: Base path to search from (default: "/")

        Returns:
            List of matching file paths, sorted by name

        Examples:
            # Find all Python files recursively
            fs.glob("**/*.py")  # Returns: ["/src/main.py", "/tests/test_foo.py", ...]

            # Find all CSV files in data directory
            fs.glob("*.csv", "/data")  # Returns: ["/data/file1.csv", "/data/file2.csv"]

            # Find all test files
            fs.glob("test_*.py")  # Returns: ["/test_foo.py", "/test_bar.py"]
        """
        if path:
            path = self._validate_path(path)

        # Get all files
        all_files = self.metadata.list("")

        # Build full pattern
        if not path.endswith("/"):
            path = path + "/"
        if path == "/":
            full_pattern = pattern
        else:
            # Remove leading / from path for pattern matching
            base_path = path[1:] if path.startswith("/") else path
            full_pattern = base_path + pattern

        # Match files against pattern
        # Handle ** for recursive matching
        if "**" in full_pattern:
            # Convert glob pattern to regex
            # Split by ** to handle recursive matching
            parts = full_pattern.split("**")

            regex_parts = []
            for i, part in enumerate(parts):
                if i > 0:
                    # ** matches zero or more path segments
                    # This can be empty or ".../", so use (?:.*/)? for optional match
                    regex_parts.append("(?:.*/)?")

                # Escape and convert wildcards in this part
                escaped = re.escape(part)
                escaped = escaped.replace(r"\*", "[^/]*")
                escaped = escaped.replace(r"\?", ".")
                escaped = escaped.replace(r"\[", "[").replace(r"\]", "]")

                # Remove leading / from all parts since it's handled by ** or the anchor
                # Note: re.escape() doesn't escape /, so we check for it directly
                while escaped.startswith("/"):
                    escaped = escaped[1:]

                regex_parts.append(escaped)

            regex_pattern = "^/" + "".join(regex_parts) + "$"

            matches = []
            for meta in all_files:
                if re.match(regex_pattern, meta.path):
                    matches.append(meta.path)
        else:
            # Use fnmatch for simpler patterns
            matches = []
            for meta in all_files:
                # Remove leading / for matching
                file_path = meta.path[1:] if meta.path.startswith("/") else meta.path
                if fnmatch.fnmatch(file_path, full_pattern):
                    matches.append(meta.path)

        return sorted(matches)

    def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 1000,
        search_mode: str = "auto",
    ) -> builtins.list[dict[str, Any]]:
        r"""
        Search file contents using regex patterns.

        Args:
            pattern: Regex pattern to search for in file contents
            path: Base path to search from (default: "/")
            file_pattern: Optional glob pattern to filter files (e.g., "*.py")
            ignore_case: If True, perform case-insensitive search (default: False)
            max_results: Maximum number of results to return (default: 1000)
            search_mode: Content search mode (default: "auto")
                - "auto": Try parsed text first, fallback to raw (default)
                - "parsed": Only search parsed text (skip files without parsed content)
                - "raw": Only search raw file content (skip parsing)

        Returns:
            List of match dicts, each containing:
            - file: File path
            - line: Line number (1-indexed)
            - content: Matched line content
            - match: The matched text
            - source: Source type - "parsed" or "raw"

        Examples:
            # Search for "TODO" in all files (auto mode - tries parsed first)
            fs.grep("TODO")
            # Returns: [{"file": "/main.py", "line": 42, "content": "...", "source": "raw"}, ...]

            # Search for function definitions in Python files
            fs.grep(r"def \w+", file_pattern="**/*.py")

            # Search only parsed text from PDFs
            fs.grep("revenue", file_pattern="**/*.pdf", search_mode="parsed")

            # Search only raw content (skip parsing)
            fs.grep("TODO", search_mode="raw")

            # Case-insensitive search
            fs.grep("error", ignore_case=True)
        """
        if path:
            path = self._validate_path(path)

        # Validate search_mode
        valid_modes = {"auto", "parsed", "raw"}
        if search_mode not in valid_modes:
            raise ValueError(
                f"Invalid search_mode: {search_mode}. Must be one of: {', '.join(valid_modes)}"
            )

        # Compile regex pattern
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}") from e

        # Get files to search
        files: list[str]
        if file_pattern:
            files = self.glob(file_pattern, path)
        else:
            # Get all files under path
            if not path.endswith("/"):
                path = path + "/"
            prefix = path if path != "/" else ""
            all_files = self.metadata.list(prefix)
            files = [meta.path for meta in all_files]

        # Search through files
        results: list[dict[str, Any]] = []
        for file_path in files:
            if len(results) >= max_results:
                break

            try:
                text: str | None = None
                source: str = "raw"

                # Get parsed text if needed
                if search_mode in ("auto", "parsed"):
                    parsed_text = self.metadata.get_file_metadata(file_path, "parsed_text")
                    if parsed_text:
                        text = parsed_text
                        source = "parsed"

                # Get raw text if needed
                if text is None and search_mode in ("auto", "raw"):
                    # Read raw content
                    content = self.read(file_path)

                    # Type narrowing: when return_metadata=False (default), result is bytes
                    assert isinstance(content, bytes), "Expected bytes from read()"

                    # Try to decode as text
                    try:
                        text = content.decode("utf-8")
                        source = "raw"
                    except UnicodeDecodeError:
                        # Skip binary files
                        continue

                # Skip if no text available
                if text is None:
                    continue

                # Search line by line
                for line_num, line in enumerate(text.splitlines(), start=1):
                    if len(results) >= max_results:
                        break

                    match = regex.search(line)
                    if match:
                        results.append(
                            {
                                "file": file_path,
                                "line": line_num,
                                "content": line,
                                "match": match.group(0),
                                "source": source,
                            }
                        )

            except Exception:
                # Skip files that can't be read
                continue

        return results

    # === Directory Operations ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | None = None,
    ) -> None:
        """
        Create a directory.

        Args:
            path: Virtual path to directory
            parents: Create parent directories if needed (like mkdir -p)
            exist_ok: Don't raise error if directory exists
            context: Optional operation context for permission checks (uses default if not provided)

        Raises:
            FileExistsError: If directory exists and exist_ok=False
            FileNotFoundError: If parent doesn't exist and parents=False
            InvalidPathError: If path is invalid
            BackendError: If operation fails
            AccessDeniedError: If access is denied (tenant isolation or read-only namespace)
            PermissionError: If path is read-only or user doesn't have write permission on parent
        """
        path = self._validate_path(path)

        # Check write permission on parent directory (v0.3.0)
        # Only check if parent exists (skip permission check for root directory)
        parent_path = self._get_parent_path(path)
        if parent_path and self.metadata.exists(parent_path):
            self._check_permission(parent_path, Permission.WRITE, context)

        # Route to backend with write access check (mkdir requires write permission)
        route = self.router.route(
            path,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            is_admin=self.is_admin,
            check_write=True,
        )

        # Check if path is read-only
        if route.readonly:
            raise PermissionError(f"Cannot create directory in read-only path: {path}")

        # Check if directory already exists (either as file or implicit directory)
        existing = self.metadata.get(path)
        is_implicit_dir = existing is None and self.metadata.is_implicit_directory(path)

        if existing is not None or is_implicit_dir:
            # When parents=True, behave like mkdir -p (don't raise error if exists)
            if not exist_ok and not parents:
                raise FileExistsError(f"Directory already exists: {path}")
            # If exist_ok=True (or parents=True) and directory exists, we still create metadata if it doesn't exist
            if existing is not None:
                # Metadata already exists, nothing to do
                return

        # Create directory in backend
        route.backend.mkdir(route.backend_path, parents=parents, exist_ok=True)

        # Create metadata entries for parent directories if parents=True
        if parents:
            # Create metadata for all parent directories that don't have it
            parent_path = self._get_parent_path(path)
            parents_to_create = []

            while parent_path and parent_path != "/":
                if not self.metadata.exists(parent_path):
                    parents_to_create.append(parent_path)
                else:
                    # Parent exists, stop walking up
                    break
                parent_path = self._get_parent_path(parent_path)

            # Create parents from top to bottom (reverse order)
            for parent_dir in reversed(parents_to_create):
                self._create_directory_metadata(parent_dir)

        # Create explicit metadata entry for the directory
        self._create_directory_metadata(path)

    def rmdir(self, path: str, recursive: bool = False) -> None:
        """
        Remove a directory.

        Args:
            path: Virtual path to directory
            recursive: Remove non-empty directory (like rm -rf)

        Raises:
            OSError: If directory not empty and recursive=False
            NexusFileNotFoundError: If directory doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If operation fails
            AccessDeniedError: If access is denied (tenant isolation or read-only namespace)
            PermissionError: If path is read-only
        """
        import errno

        path = self._validate_path(path)

        # Route to backend with write access check (rmdir requires write permission)
        route = self.router.route(
            path,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            is_admin=self.is_admin,
            check_write=True,
        )

        # Check readonly
        if route.readonly:
            raise PermissionError(f"Cannot remove directory from read-only path: {path}")

        # Check if directory contains any files in metadata store
        # Normalize path to ensure it ends with /
        dir_path = path if path.endswith("/") else path + "/"
        files_in_dir = self.metadata.list(dir_path)

        if files_in_dir:
            # Directory is not empty
            if not recursive:
                # Raise OSError with ENOTEMPTY errno (same as os.rmdir behavior)
                raise OSError(errno.ENOTEMPTY, f"Directory not empty: {path}")

            # Recursive mode - delete all files in directory
            # Use batch delete for better performance (single transaction instead of N queries)
            file_paths = [file_meta.path for file_meta in files_in_dir]

            # Delete content from backend for each file
            for file_meta in files_in_dir:
                if file_meta.etag:
                    with contextlib.suppress(Exception):
                        route.backend.delete_content(file_meta.etag)

            # Batch delete from metadata store
            self.metadata.delete_batch(file_paths)

        # Remove directory in backend (if it still exists)
        # In CAS systems, the directory may no longer exist after deleting its contents
        with contextlib.suppress(NexusFileNotFoundError):
            route.backend.rmdir(route.backend_path, recursive=recursive)

    def is_directory(self, path: str) -> bool:
        """
        Check if path is a directory (explicit or implicit).

        Args:
            path: Virtual path to check

        Returns:
            True if path is a directory, False otherwise
        """
        try:
            path = self._validate_path(path)
            # Route with access control (read permission needed to check)
            route = self.router.route(
                path,
                tenant_id=self.tenant_id,
                agent_id=self.agent_id,
                is_admin=self.is_admin,
                check_write=False,
            )
            # Check if it's an explicit directory in the backend
            if route.backend.is_directory(route.backend_path):
                return True
            # Check if it's an implicit directory (has files beneath it)
            return self.metadata.is_implicit_directory(path)
        except (InvalidPathError, Exception):
            return False

    def get_available_namespaces(self) -> builtins.list[str]:
        """
        Get list of available namespace directories.

        Returns the built-in namespaces that should appear at root level.
        Filters based on admin context only - tenant filtering happens
        when accessing files within namespaces, not for listing directories.

        Returns:
            List of namespace names (e.g., ["workspace", "shared", "external"])

        Examples:
            # Get namespaces for current user context
            namespaces = fs.get_available_namespaces()
            # Returns: ["archives", "external", "shared", "workspace"]
            # (excludes "system" if not admin)
        """
        namespaces = []

        for name, config in self.router._namespaces.items():
            # Include namespace if it's not admin-only OR user is admin
            # Note: We show all namespaces regardless of tenant_id.
            # Tenant filtering happens when accessing files within the namespace.
            if not config.admin_only or self.is_admin:
                namespaces.append(name)

        return sorted(namespaces)

    def _get_backend_directory_entries(self, path: str) -> set[str]:
        """
        Get directory entries from backend for empty directory detection.

        This helper method queries the backend's list_dir() to find directories
        that don't contain any files (empty directories). It handles routing
        and error cases gracefully.

        Args:
            path: Virtual path to list (e.g., "/", "/workspace")

        Returns:
            Set of directory paths that exist in the backend
        """
        directories = set()

        try:
            # For root path, directly use the backend (router doesn't handle "/" well)
            if path == "/":
                try:
                    entries = self.backend.list_dir("")
                    for entry in entries:
                        if entry.endswith("/"):  # Directory marker
                            dir_name = entry.rstrip("/")
                            dir_path = "/" + dir_name
                            directories.add(dir_path)
                except NotImplementedError:
                    # Backend doesn't support list_dir - skip
                    pass
                except Exception:
                    # Other errors - skip silently (best-effort)
                    pass
            else:
                # Non-root path - use router
                route = self.router.route(
                    path.rstrip("/"),
                    tenant_id=self.tenant_id,
                    agent_id=self.agent_id,
                    is_admin=self.is_admin,
                    check_write=False,
                )
                backend_path = route.backend_path

                try:
                    entries = route.backend.list_dir(backend_path)
                    for entry in entries:
                        if entry.endswith("/"):  # Directory marker
                            dir_name = entry.rstrip("/")
                            dir_path = path + dir_name if path != "/" else "/" + dir_name
                            directories.add(dir_path)
                except NotImplementedError:
                    # Backend doesn't support list_dir - skip
                    pass
                except Exception:
                    # Other errors - skip silently (best-effort)
                    pass

        except Exception:
            # Ignore routing errors - directory detection is best-effort
            pass

        return directories

    # === Metadata Export/Import ===

    def export_metadata(
        self,
        output_path: str | Path,
        filter: ExportFilter | None = None,
        prefix: str = "",  # Backward compatibility
    ) -> int:
        """
        Export metadata to JSONL file for backup and migration.

        Each line in the output file is a JSON object containing:
        - path: Virtual file path
        - backend_name: Backend identifier
        - physical_path: Physical storage path (content hash in CAS)
        - size: File size in bytes
        - etag: Content hash (SHA-256)
        - mime_type: MIME type (optional)
        - created_at: Creation timestamp (ISO format)
        - modified_at: Modification timestamp (ISO format)
        - version: Version number
        - custom_metadata: Dict of custom key-value metadata (optional)

        Output is sorted by path for clean git diffs.

        Args:
            output_path: Path to output JSONL file
            filter: Export filter options (tenant_id, path_prefix, after_time, include_deleted)
            prefix: (Deprecated) Path prefix filter for backward compatibility

        Returns:
            Number of files exported

        Examples:
            # Export all metadata
            count = fs.export_metadata("backup.jsonl")

            # Export with filters
            from nexus.core.export_import import ExportFilter
            from datetime import datetime
            filter = ExportFilter(
                path_prefix="/workspace",
                after_time=datetime(2024, 1, 1),
                tenant_id="acme-corp"
            )
            count = fs.export_metadata("backup.jsonl", filter=filter)
        """
        import json

        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Handle backward compatibility and create filter
        if filter is None:
            filter = ExportFilter(path_prefix=prefix)
        elif prefix:
            # If both provided, prefix takes precedence for backward compat
            filter.path_prefix = prefix

        # Get all files matching prefix
        all_files = self.metadata.list(filter.path_prefix)

        # Apply filters
        filtered_files = []
        for file_meta in all_files:
            # Filter by modification time
            if filter.after_time and file_meta.modified_at:
                # Ensure both timestamps are timezone-aware for comparison
                file_time = file_meta.modified_at
                filter_time = filter.after_time
                if file_time.tzinfo is None:
                    file_time = file_time.replace(tzinfo=UTC)
                if filter_time.tzinfo is None:
                    filter_time = filter_time.replace(tzinfo=UTC)

                if file_time < filter_time:
                    continue

            # Note: include_deleted and tenant_id filtering would require
            # database-level support. For now, we skip these filters.
            # TODO: Add deleted_at column support and tenant filtering

            filtered_files.append(file_meta)

        # Sort by path for clean git diffs (deterministic output)
        filtered_files.sort(key=lambda m: m.path)

        count = 0

        with output_file.open("w", encoding="utf-8") as f:
            for file_meta in filtered_files:
                # Build base metadata dict
                metadata_dict: dict[str, Any] = {
                    "path": file_meta.path,
                    "backend_name": file_meta.backend_name,
                    "physical_path": file_meta.physical_path,
                    "size": file_meta.size,
                    "etag": file_meta.etag,
                    "mime_type": file_meta.mime_type,
                    "created_at": (
                        file_meta.created_at.isoformat() if file_meta.created_at else None
                    ),
                    "modified_at": (
                        file_meta.modified_at.isoformat() if file_meta.modified_at else None
                    ),
                    "version": file_meta.version,
                }

                # Try to get custom metadata for this file (if any)
                # Note: This is optional - files may not have custom metadata
                try:
                    if isinstance(self.metadata, SQLAlchemyMetadataStore):
                        # Get all custom metadata keys for this path
                        # We need to query the database directly for all keys
                        with self.metadata.SessionLocal() as session:
                            from nexus.storage.models import FileMetadataModel, FilePathModel

                            # Get path_id
                            path_stmt = select(FilePathModel.path_id).where(
                                FilePathModel.virtual_path == file_meta.path,
                                FilePathModel.deleted_at.is_(None),
                            )
                            path_id = session.scalar(path_stmt)

                            if path_id:
                                # Get all custom metadata
                                meta_stmt = select(FileMetadataModel).where(
                                    FileMetadataModel.path_id == path_id
                                )
                                custom_meta = {}
                                for meta_item in session.scalars(meta_stmt):
                                    if meta_item.value:
                                        custom_meta[meta_item.key] = json.loads(meta_item.value)

                                if custom_meta:
                                    metadata_dict["custom_metadata"] = custom_meta
                except Exception:
                    # Ignore errors when fetching custom metadata
                    pass

                # Write JSON line
                f.write(json.dumps(metadata_dict) + "\n")
                count += 1

        return count

    def import_metadata(
        self,
        input_path: str | Path,
        options: ImportOptions | None = None,
        overwrite: bool = False,  # Backward compatibility
        skip_existing: bool = True,  # Backward compatibility
    ) -> ImportResult:
        """
        Import metadata from JSONL file.

        IMPORTANT: This only imports metadata records, not the actual file content.
        The content must already exist in the CAS storage (matched by content hash).
        This is useful for:
        - Restoring metadata after database corruption
        - Migrating metadata between instances (with same CAS content)
        - Creating alternative path mappings to existing content

        Args:
            input_path: Path to input JSONL file
            options: Import options (conflict mode, dry-run, preserve IDs)
            overwrite: (Deprecated) If True, overwrite existing (backward compat)
            skip_existing: (Deprecated) If True, skip existing (backward compat)

        Returns:
            ImportResult with counts and collision details

        Raises:
            ValueError: If JSONL format is invalid
            FileNotFoundError: If input file doesn't exist

        Examples:
            # Import metadata (skip existing - default)
            result = fs.import_metadata("backup.jsonl")
            print(f"Created {result.created}, updated {result.updated}, skipped {result.skipped}")

            # Import with conflict resolution
            from nexus.core.export_import import ImportOptions
            options = ImportOptions(conflict_mode="auto", dry_run=True)
            result = fs.import_metadata("backup.jsonl", options=options)

            # Import and overwrite conflicts
            options = ImportOptions(conflict_mode="overwrite")
            result = fs.import_metadata("backup.jsonl", options=options)

            # Backward compatibility (old API)
            result = fs.import_metadata("backup.jsonl", overwrite=True)
            # Returns ImportResult, but behaves like old (imported, skipped) tuple
        """
        import json

        input_file = Path(input_path)
        if not input_file.exists():
            raise FileNotFoundError(f"Input file not found: {input_path}")

        # Handle backward compatibility - convert old params to ImportOptions
        if options is None:
            if overwrite:
                options = ImportOptions(conflict_mode="overwrite")
            elif skip_existing:
                options = ImportOptions(conflict_mode="skip")
            else:
                options = ImportOptions(conflict_mode="skip")

        result = ImportResult()

        with input_file.open("r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue

                try:
                    # Parse JSON line
                    metadata_dict = json.loads(line)

                    # Validate required fields
                    required_fields = ["path", "backend_name", "physical_path", "size"]
                    for field in required_fields:
                        if field not in metadata_dict:
                            raise ValueError(f"Missing required field: {field}")

                    original_path = metadata_dict["path"]
                    path = original_path

                    # Parse timestamps
                    created_at = None
                    if metadata_dict.get("created_at"):
                        created_at = datetime.fromisoformat(metadata_dict["created_at"])

                    modified_at = None
                    if metadata_dict.get("modified_at"):
                        modified_at = datetime.fromisoformat(metadata_dict["modified_at"])

                    # Check if file already exists
                    existing = self.metadata.get(path)
                    imported_etag = metadata_dict.get("etag")

                    if existing:
                        # Collision detected - determine resolution
                        existing_etag = existing.etag
                        is_same_content = existing_etag == imported_etag

                        if is_same_content:
                            # Same content, different metadata - just update
                            if options.dry_run:
                                result.updated += 1
                                continue

                            # Update metadata
                            file_meta = FileMetadata(
                                path=path,
                                backend_name=metadata_dict["backend_name"],
                                physical_path=metadata_dict["physical_path"],
                                size=metadata_dict["size"],
                                etag=imported_etag,
                                mime_type=metadata_dict.get("mime_type"),
                                created_at=created_at or existing.created_at,
                                modified_at=modified_at or existing.modified_at,
                                version=metadata_dict.get("version", existing.version),
                            )
                            self.metadata.put(file_meta)
                            self._import_custom_metadata(path, metadata_dict)
                            result.updated += 1
                            continue

                        # Different content - apply conflict mode
                        if options.conflict_mode == "skip":
                            result.skipped += 1
                            result.collisions.append(
                                CollisionDetail(
                                    path=path,
                                    existing_etag=existing_etag,
                                    imported_etag=imported_etag,
                                    resolution="skip",
                                    message="Skipped: existing file has different content",
                                )
                            )
                            continue

                        elif options.conflict_mode == "overwrite":
                            if options.dry_run:
                                result.updated += 1
                                result.collisions.append(
                                    CollisionDetail(
                                        path=path,
                                        existing_etag=existing_etag,
                                        imported_etag=imported_etag,
                                        resolution="overwrite",
                                        message="Would overwrite with imported content",
                                    )
                                )
                                continue

                            # Overwrite existing
                            file_meta = FileMetadata(
                                path=path,
                                backend_name=metadata_dict["backend_name"],
                                physical_path=metadata_dict["physical_path"],
                                size=metadata_dict["size"],
                                etag=imported_etag,
                                mime_type=metadata_dict.get("mime_type"),
                                created_at=created_at or existing.created_at,
                                modified_at=modified_at,
                                version=metadata_dict.get("version", existing.version + 1),
                            )
                            self.metadata.put(file_meta)
                            self._import_custom_metadata(path, metadata_dict)
                            result.updated += 1
                            result.collisions.append(
                                CollisionDetail(
                                    path=path,
                                    existing_etag=existing_etag,
                                    imported_etag=imported_etag,
                                    resolution="overwrite",
                                    message="Overwrote with imported content",
                                )
                            )
                            continue

                        elif options.conflict_mode == "remap":
                            # Rename imported file to avoid collision
                            suffix = 1
                            while self.metadata.exists(f"{path}_imported{suffix}"):
                                suffix += 1
                            path = f"{path}_imported{suffix}"

                            if options.dry_run:
                                result.remapped += 1
                                result.collisions.append(
                                    CollisionDetail(
                                        path=original_path,
                                        existing_etag=existing_etag,
                                        imported_etag=imported_etag,
                                        resolution="remap",
                                        message=f"Would remap to: {path}",
                                    )
                                )
                                continue

                            # Create with new path
                            file_meta = FileMetadata(
                                path=path,
                                backend_name=metadata_dict["backend_name"],
                                physical_path=metadata_dict["physical_path"],
                                size=metadata_dict["size"],
                                etag=imported_etag,
                                mime_type=metadata_dict.get("mime_type"),
                                created_at=created_at,
                                modified_at=modified_at,
                                version=metadata_dict.get("version", 1),
                            )
                            self.metadata.put(file_meta)
                            self._import_custom_metadata(path, metadata_dict)
                            result.remapped += 1
                            result.collisions.append(
                                CollisionDetail(
                                    path=original_path,
                                    existing_etag=existing_etag,
                                    imported_etag=imported_etag,
                                    resolution="remap",
                                    message=f"Remapped to: {path}",
                                )
                            )
                            continue

                        elif options.conflict_mode == "auto":
                            # Smart resolution: newer wins
                            existing_time = existing.modified_at or existing.created_at
                            imported_time = modified_at or created_at

                            # Ensure both timestamps are timezone-aware for comparison
                            if existing_time and existing_time.tzinfo is None:
                                existing_time = existing_time.replace(tzinfo=UTC)
                            if imported_time and imported_time.tzinfo is None:
                                imported_time = imported_time.replace(tzinfo=UTC)

                            if imported_time and existing_time and imported_time > existing_time:
                                # Imported is newer - overwrite
                                if options.dry_run:
                                    result.updated += 1
                                    result.collisions.append(
                                        CollisionDetail(
                                            path=path,
                                            existing_etag=existing_etag,
                                            imported_etag=imported_etag,
                                            resolution="auto_overwrite",
                                            message=f"Would overwrite: imported is newer ({imported_time} > {existing_time})",
                                        )
                                    )
                                    continue

                                file_meta = FileMetadata(
                                    path=path,
                                    backend_name=metadata_dict["backend_name"],
                                    physical_path=metadata_dict["physical_path"],
                                    size=metadata_dict["size"],
                                    etag=imported_etag,
                                    mime_type=metadata_dict.get("mime_type"),
                                    created_at=created_at or existing.created_at,
                                    modified_at=modified_at,
                                    version=metadata_dict.get("version", existing.version + 1),
                                )
                                self.metadata.put(file_meta)
                                self._import_custom_metadata(path, metadata_dict)
                                result.updated += 1
                                result.collisions.append(
                                    CollisionDetail(
                                        path=path,
                                        existing_etag=existing_etag,
                                        imported_etag=imported_etag,
                                        resolution="auto_overwrite",
                                        message=f"Overwrote: imported is newer ({imported_time} > {existing_time})",
                                    )
                                )
                            else:
                                # Existing is newer or equal - skip
                                result.skipped += 1
                                result.collisions.append(
                                    CollisionDetail(
                                        path=path,
                                        existing_etag=existing_etag,
                                        imported_etag=imported_etag,
                                        resolution="auto_skip",
                                        message="Skipped: existing is newer or equal",
                                    )
                                )
                            continue

                    # No collision - create new file
                    if options.dry_run:
                        result.created += 1
                        continue

                    # Create FileMetadata object
                    file_meta = FileMetadata(
                        path=path,
                        backend_name=metadata_dict["backend_name"],
                        physical_path=metadata_dict["physical_path"],
                        size=metadata_dict["size"],
                        etag=imported_etag,
                        mime_type=metadata_dict.get("mime_type"),
                        created_at=created_at,
                        modified_at=modified_at,
                        version=metadata_dict.get("version", 1),
                    )

                    # Store metadata
                    self.metadata.put(file_meta)
                    self._import_custom_metadata(path, metadata_dict)
                    result.created += 1

                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON at line {line_num}: {e}") from e
                except Exception as e:
                    raise ValueError(f"Error processing line {line_num}: {e}") from e

        return result

    def _import_custom_metadata(self, path: str, metadata_dict: dict[str, Any]) -> None:
        """Helper to import custom metadata for a file."""
        if "custom_metadata" in metadata_dict:
            custom_meta = metadata_dict["custom_metadata"]
            if isinstance(custom_meta, dict):
                for key, value in custom_meta.items():
                    with contextlib.suppress(Exception):
                        # Ignore errors when setting custom metadata
                        self.metadata.set_file_metadata(path, key, value)

    def batch_get_content_ids(self, paths: builtins.list[str]) -> dict[str, str | None]:
        """
        Get content IDs (hashes) for multiple paths in a single query.

        This is a convenience method that delegates to the metadata store's
        batch_get_content_ids(). Useful for CAS deduplication scenarios where
        you need to find duplicate files efficiently.

        Performance: Uses a single SQL query instead of N queries (avoids N+1 problem).

        Args:
            paths: List of virtual file paths

        Returns:
            Dictionary mapping path to content_hash (or None if file not found)

        Examples:
            # Find duplicate files
            paths = fs.list()
            hashes = fs.batch_get_content_ids(paths)

            # Group by hash to find duplicates
            from collections import defaultdict
            by_hash = defaultdict(list)
            for path, hash in hashes.items():
                if hash:
                    by_hash[hash].append(path)

            # Find duplicate groups
            duplicates = {h: paths for h, paths in by_hash.items() if len(paths) > 1}
        """
        return self.metadata.batch_get_content_ids(paths)

    # === Version Tracking (v0.3.5) ===

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
        import difflib

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

    async def parse(
        self,
        path: str,
        store_result: bool = True,
    ) -> ParseResult:
        """
        Parse a file's content using the appropriate parser.

        This method reads the file, selects a parser based on the file extension,
        and extracts structured data (text, metadata, chunks, etc.).

        Args:
            path: Virtual path to the file to parse
            store_result: If True, store parsed text as file metadata (default: True)

        Returns:
            ParseResult containing extracted text, metadata, structure, and chunks

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            ParserError: If parsing fails or no suitable parser found

        Examples:
            # Parse a PDF file
            result = await fs.parse("/documents/report.pdf")
            print(result.text)  # Extracted text
            print(result.structure)  # Document structure

            # Parse without storing metadata
            result = await fs.parse("/data/file.xlsx", store_result=False)

            # Access parsed chunks
            for chunk in result.chunks:
                print(chunk.text)
        """
        # Validate path
        path = self._validate_path(path)

        # Read file content
        content = self.read(path)

        # Type narrowing: when return_metadata=False (default), result is bytes
        assert isinstance(content, bytes), "Expected bytes from read()"

        # Get file metadata for MIME type
        meta = self.metadata.get(path)
        mime_type = meta.mime_type if meta else None

        # Get appropriate parser
        parser = self.parser_registry.get_parser(path, mime_type)

        # Parse the content
        parse_metadata = {
            "path": path,
            "mime_type": mime_type,
            "size": len(content),
        }
        result = await parser.parse(content, parse_metadata)

        # Optionally store parsed text as file metadata
        if store_result and result.text:
            # Store parsed text in custom metadata
            self.metadata.set_file_metadata(path, "parsed_text", result.text)
            self.metadata.set_file_metadata(path, "parsed_at", datetime.now(UTC).isoformat())
            self.metadata.set_file_metadata(path, "parser_name", parser.name)

        return result

    # === Permission Management (v0.3.0) ===

    def chmod(
        self,
        path: str,
        mode: int | str,
        context: OperationContext | None = None,
    ) -> None:
        """Change file mode (permissions).

        Requires the user to be the owner of the file or an admin.

        Args:
            path: Virtual file path
            mode: Permission mode (int like 0o644 or string like '755')
            context: Optional operation context (defaults to self._default_context)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            PermissionError: If user is not owner and not admin
            ValueError: If mode is invalid

        Examples:
            >>> nx.chmod("/workspace/file.txt", 0o644)
            >>> nx.chmod("/workspace/file.txt", "755")
            >>> nx.chmod("/workspace/file.txt", "rwxr-xr-x")
        """
        from nexus.core.permissions import parse_mode

        path = self._validate_path(path)

        # Get file metadata
        file_meta = self.metadata.get(path)
        if not file_meta:
            raise NexusFileNotFoundError(path)

        # Get context (use default if not provided)
        ctx = context or self._default_context

        # Check if user is owner or admin
        # Must be owner to chmod (unless admin or system)
        if (
            not ctx.is_admin
            and not ctx.is_system
            and file_meta.owner
            and file_meta.owner != ctx.user
        ):
            raise PermissionError(
                f"Access denied: Only the owner ('{file_meta.owner}') or admin "
                f"can change permissions for '{path}'"
            )

        # Parse mode (handles int, octal string, or symbolic string)
        if isinstance(mode, str):
            mode_int = parse_mode(mode)
        elif isinstance(mode, int):
            mode_int = mode
        else:
            raise ValueError(f"mode must be int or str, got {type(mode)}")

        # Update mode
        file_meta.mode = mode_int
        self.metadata.put(file_meta)

        # Invalidate cache
        if self.metadata._cache_enabled and self.metadata._cache:
            self.metadata._cache.invalidate_path(path)

    def chown(
        self,
        path: str,
        owner: str,
        context: OperationContext | None = None,
    ) -> None:
        """Change file owner.

        Requires the user to be the current owner of the file or an admin.

        Args:
            path: Virtual file path
            owner: New owner username
            context: Optional operation context (defaults to self._default_context)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            PermissionError: If user is not owner and not admin

        Examples:
            >>> nx.chown("/workspace/file.txt", "alice")
        """
        path = self._validate_path(path)

        # Get file metadata
        file_meta = self.metadata.get(path)
        if not file_meta:
            raise NexusFileNotFoundError(path)

        # Get context (use default if not provided)
        ctx = context or self._default_context

        # Check if user is owner or admin
        # Must be owner to chown (unless admin or system)
        if (
            not ctx.is_admin
            and not ctx.is_system
            and file_meta.owner
            and file_meta.owner != ctx.user
        ):
            raise PermissionError(
                f"Access denied: Only the owner ('{file_meta.owner}') or admin "
                f"can change ownership for '{path}'"
            )

        # Update owner
        file_meta.owner = owner
        self.metadata.put(file_meta)

        # Invalidate cache
        if self.metadata._cache_enabled and self.metadata._cache:
            self.metadata._cache.invalidate_path(path)

    def chgrp(
        self,
        path: str,
        group: str,
        context: OperationContext | None = None,
    ) -> None:
        """Change file group.

        Requires the user to be the owner of the file or an admin.

        Args:
            path: Virtual file path
            group: New group name
            context: Optional operation context (defaults to self._default_context)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            PermissionError: If user is not owner and not admin

        Examples:
            >>> nx.chgrp("/workspace/file.txt", "developers")
        """
        path = self._validate_path(path)

        # Get file metadata
        file_meta = self.metadata.get(path)
        if not file_meta:
            raise NexusFileNotFoundError(path)

        # Get context (use default if not provided)
        ctx = context or self._default_context

        # Check if user is owner or admin
        # Must be owner to chgrp (unless admin or system)
        if (
            not ctx.is_admin
            and not ctx.is_system
            and file_meta.owner
            and file_meta.owner != ctx.user
        ):
            raise PermissionError(
                f"Access denied: Only the owner ('{file_meta.owner}') or admin "
                f"can change group for '{path}'"
            )

        # Update group
        file_meta.group = group
        self.metadata.put(file_meta)

        # Invalidate cache
        if self.metadata._cache_enabled and self.metadata._cache:
            self.metadata._cache.invalidate_path(path)

    def close(self) -> None:
        """Close the filesystem and release resources."""
        # Wait for all parser threads to complete before closing metadata store
        # This prevents database corruption from threads writing during shutdown
        with self._parser_threads_lock:
            threads_to_join = list(self._parser_threads)

        for thread in threads_to_join:
            # Wait up to 5 seconds for each thread
            # Parser threads should complete quickly, but we don't want to hang forever
            thread.join(timeout=5.0)

        # Close metadata store after all parsers have finished
        self.metadata.close()

        # Close ReBACManager to release database connection
        if hasattr(self, "_rebac_manager"):
            self._rebac_manager.close()
