"""Unified filesystem implementation for Nexus."""

from __future__ import annotations

import builtins
import contextlib
import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from nexus.backends.backend import Backend
from nexus.core.exceptions import InvalidPathError, NexusFileNotFoundError

if TYPE_CHECKING:
    from nexus.core.entity_registry import EntityRegistry
    from nexus.core.memory_api import Memory
from nexus.core.export_import import (
    CollisionDetail,
    ExportFilter,
    ImportOptions,
    ImportResult,
)
from nexus.core.filesystem import NexusFilesystem
from nexus.core.metadata import FileMetadata
from nexus.core.nexus_fs_core import NexusFSCoreMixin
from nexus.core.nexus_fs_permissions import NexusFSPermissionsMixin
from nexus.core.nexus_fs_search import NexusFSSearchMixin
from nexus.core.nexus_fs_versions import NexusFSVersionsMixin
from nexus.core.permissions import OperationContext, Permission
from nexus.core.router import NamespaceConfig, PathRouter
from nexus.parsers import MarkItDownParser, ParserRegistry
from nexus.parsers.types import ParseResult
from nexus.storage.content_cache import ContentCache
from nexus.storage.metadata_store import SQLAlchemyMetadataStore


class NexusFS(
    NexusFSCoreMixin,
    NexusFSSearchMixin,
    NexusFSPermissionsMixin,
    NexusFSVersionsMixin,
    NexusFilesystem,
):
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
        user_id: str | None = None,
        agent_id: str | None = None,
        is_admin: bool = False,
        custom_namespaces: list[NamespaceConfig] | None = None,
        enable_metadata_cache: bool = True,
        cache_path_size: int = 512,
        cache_list_size: int = 128,
        cache_kv_size: int = 256,
        cache_exists_size: int = 1024,
        cache_ttl_seconds: int | None = 300,
        enable_content_cache: bool = True,
        content_cache_size_mb: int = 256,
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
            user_id: User identifier for identity-based memory system (v0.4.0, optional)
            agent_id: Agent identifier for agent-level isolation in /workspace (optional)
            is_admin: Whether this instance has admin privileges (default: False)
            custom_namespaces: Additional custom namespace configurations (optional)
            enable_metadata_cache: Enable in-memory metadata caching (default: True)
            cache_path_size: Max entries for path metadata cache (default: 512)
            cache_list_size: Max entries for directory listing cache (default: 128)
            cache_kv_size: Max entries for file metadata KV cache (default: 256)
            cache_exists_size: Max entries for existence check cache (default: 1024)
            cache_ttl_seconds: Cache TTL in seconds, None = no expiry (default: 300)
            enable_content_cache: Enable in-memory content caching for faster reads (default: True)
            content_cache_size_mb: Maximum content cache size in megabytes (default: 256)
            auto_parse: Automatically parse files on write (default: True)
            custom_parsers: Custom parser configurations from config (optional)
            enforce_permissions: Enable permission enforcement on file operations (default: True)
        """
        # Initialize content cache if enabled and backend supports it
        if enable_content_cache:
            # Import here to avoid circular import
            from nexus.backends.local import LocalBackend

            if isinstance(backend, LocalBackend):
                # Create content cache and attach to LocalBackend
                content_cache = ContentCache(max_size_mb=content_cache_size_mb)
                backend.content_cache = content_cache

        # Store backend
        self.backend = backend

        # Store tenant, user, and agent context
        self.tenant_id = tenant_id
        self.user_id = user_id  # v0.4.0: Identity-based memory
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

        # Initialize workspace manager for snapshot/versioning
        from nexus.core.workspace_manager import WorkspaceManager

        self._workspace_manager = WorkspaceManager(metadata=self.metadata, backend=self.backend)

        # Initialize semantic search (v0.4.0) - lazy initialization
        # Call initialize_semantic_search() to enable semantic search features
        self._semantic_search = None

        # Initialize Memory API (v0.4.0)

        self._memory_api: Memory | None = None  # Lazy initialization
        self._entity_registry: EntityRegistry | None = None
        # Store config for lazy init
        self._memory_config = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "agent_id": agent_id,
        }

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

    @property
    def memory(self) -> Any:
        """Get Memory API instance for agent memory management (v0.4.0).

        Lazy initialization on first access.

        Returns:
            Memory API instance.

        Example:
            >>> nx = nexus.connect()
            >>> memory_id = nx.memory.store("User prefers Python", scope="user")
            >>> results = nx.memory.query(memory_type="preference")
        """
        if self._memory_api is None:
            from nexus.core.entity_registry import EntityRegistry
            from nexus.core.memory_api import Memory

            # Create a session from SessionLocal
            session = self.metadata.SessionLocal()

            # Get or create entity registry
            if self._entity_registry is None:
                self._entity_registry = EntityRegistry(session)

            self._memory_api = Memory(
                session=session,
                backend=self.backend,
                tenant_id=self._memory_config.get("tenant_id"),
                user_id=self._memory_config.get("user_id"),
                agent_id=self._memory_config.get("agent_id"),
                entity_registry=self._entity_registry,
            )

        return self._memory_api

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

    # === Workspace Snapshot Operations ===

    def workspace_snapshot(
        self,
        agent_id: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a snapshot of the current agent's workspace.

        Args:
            agent_id: Agent identifier (uses self.agent_id if not provided)
            description: Human-readable description of snapshot
            tags: List of tags for categorization

        Returns:
            Snapshot metadata dict

        Raises:
            ValueError: If agent_id not provided and self.agent_id is None
            BackendError: If snapshot cannot be created

        Example:
            >>> nx = NexusFS(backend, agent_id="agent1")
            >>> snapshot = nx.workspace_snapshot(description="Before major refactor")
            >>> print(f"Created snapshot #{snapshot['snapshot_number']}")
        """
        agent_id = agent_id or self.agent_id
        if not agent_id:
            raise ValueError("agent_id must be provided or set in NexusFS init")

        return self._workspace_manager.create_snapshot(
            agent_id=agent_id,
            tenant_id=self.tenant_id,
            description=description,
            tags=tags,
            created_by=agent_id,
        )

    def workspace_restore(
        self,
        snapshot_number: int,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore workspace to a previous snapshot.

        Args:
            snapshot_number: Snapshot version number to restore
            agent_id: Agent identifier (uses self.agent_id if not provided)

        Returns:
            Restore operation result

        Raises:
            ValueError: If agent_id not provided and self.agent_id is None
            NexusFileNotFoundError: If snapshot not found

        Example:
            >>> nx = NexusFS(backend, agent_id="agent1")
            >>> result = nx.workspace_restore(snapshot_number=5)
            >>> print(f"Restored {result['files_restored']} files")
        """
        agent_id = agent_id or self.agent_id
        if not agent_id:
            raise ValueError("agent_id must be provided or set in NexusFS init")

        return self._workspace_manager.restore_snapshot(
            snapshot_number=snapshot_number,
            agent_id=agent_id,
            tenant_id=self.tenant_id,
        )

    def workspace_log(
        self,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List snapshot history for workspace.

        Args:
            agent_id: Agent identifier (uses self.agent_id if not provided)
            limit: Maximum number of snapshots to return

        Returns:
            List of snapshot metadata dicts (most recent first)

        Raises:
            ValueError: If agent_id not provided and self.agent_id is None

        Example:
            >>> nx = NexusFS(backend, agent_id="agent1")
            >>> snapshots = nx.workspace_log(limit=10)
            >>> for snap in snapshots:
            >>>     print(f"#{snap['snapshot_number']}: {snap['description']}")
        """
        agent_id = agent_id or self.agent_id
        if not agent_id:
            raise ValueError("agent_id must be provided or set in NexusFS init")

        return self._workspace_manager.list_snapshots(
            agent_id=agent_id,
            tenant_id=self.tenant_id,
            limit=limit,
        )

    def workspace_diff(
        self,
        snapshot_1: int,
        snapshot_2: int,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Compare two workspace snapshots.

        Args:
            snapshot_1: First snapshot number
            snapshot_2: Second snapshot number
            agent_id: Agent identifier (uses self.agent_id if not provided)

        Returns:
            Diff dict with added, removed, modified files

        Raises:
            ValueError: If agent_id not provided and self.agent_id is None
            NexusFileNotFoundError: If either snapshot not found

        Example:
            >>> nx = NexusFS(backend, agent_id="agent1")
            >>> diff = nx.workspace_diff(snapshot_1=5, snapshot_2=10)
            >>> print(f"Added: {len(diff['added'])}, Modified: {len(diff['modified'])}")
        """
        agent_id = agent_id or self.agent_id
        if not agent_id:
            raise ValueError("agent_id must be provided or set in NexusFS init")

        # Get snapshot IDs from numbers
        snapshots = self._workspace_manager.list_snapshots(
            agent_id=agent_id,
            tenant_id=self.tenant_id,
            limit=1000,
        )

        snap_1_id = None
        snap_2_id = None
        for snap in snapshots:
            if snap["snapshot_number"] == snapshot_1:
                snap_1_id = snap["snapshot_id"]
            if snap["snapshot_number"] == snapshot_2:
                snap_2_id = snap["snapshot_id"]

        if not snap_1_id:
            raise NexusFileNotFoundError(
                path=f"snapshot:{snapshot_1}",
                message=f"Snapshot #{snapshot_1} not found",
            )
        if not snap_2_id:
            raise NexusFileNotFoundError(
                path=f"snapshot:{snapshot_2}",
                message=f"Snapshot #{snapshot_2} not found",
            )

        return self._workspace_manager.diff_snapshots(snap_1_id, snap_2_id)

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
