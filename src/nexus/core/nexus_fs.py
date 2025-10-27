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
from nexus.core.nexus_fs_mounts import NexusFSMountsMixin
from nexus.core.nexus_fs_rebac import NexusFSReBACMixin
from nexus.core.nexus_fs_search import NexusFSSearchMixin
from nexus.core.nexus_fs_versions import NexusFSVersionsMixin
from nexus.core.permissions import OperationContext, Permission
from nexus.core.permissions_enhanced import EnhancedOperationContext
from nexus.core.router import NamespaceConfig, PathRouter
from nexus.core.rpc_decorator import rpc_expose
from nexus.parsers import MarkItDownParser, ParserRegistry
from nexus.parsers.types import ParseResult
from nexus.storage.content_cache import ContentCache
from nexus.storage.metadata_store import SQLAlchemyMetadataStore


class NexusFS(
    NexusFSCoreMixin,
    NexusFSSearchMixin,
    NexusFSReBACMixin,
    NexusFSVersionsMixin,
    NexusFSMountsMixin,
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
        is_admin: bool = False,
        tenant_id: str | None = None,  # Default tenant ID for operations
        agent_id: str | None = None,  # Default agent ID for operations
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
        enforce_permissions: bool = True,  # P0-6: ENABLED by default for security
        inherit_permissions: bool = True,  # P0-3: Enable automatic parent tuple creation for directory inheritance
        allow_admin_bypass: bool = False,  # P0-4: Allow admin bypass (DEFAULT OFF for production security)
        audit_strict_mode: bool = True,  # P0 COMPLIANCE: Fail writes if audit logging fails (DEFAULT ON)
    ):
        """
        Initialize filesystem.

        Args:
            backend: Backend instance for storing file content (LocalBackend, GCSBackend, etc.)
            db_path: Path to SQLite metadata database (auto-generated if None)
            is_admin: Whether this instance has admin privileges (default: False)
            tenant_id: Default tenant ID for all operations (optional, for multi-tenancy)
            agent_id: Default agent ID for all operations (optional, for agent isolation)
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
            inherit_permissions: Enable automatic parent tuple creation for directory inheritance (default: True, P0-3)
            allow_admin_bypass: Allow admin users to bypass permission checks (default: False for security, P0-4)

        Note:
            When tenant_id or agent_id are provided, they set the default context for all operations.
            Individual operations can still override context by passing context parameter.
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

        # Store admin flag and auto-parse setting
        self.is_admin = is_admin
        self.auto_parse = auto_parse

        # Store default tenant/agent IDs for all operations
        self.tenant_id: str | None = tenant_id
        self.agent_id: str | None = agent_id
        self.user_id: str | None = None

        # Store allow_admin_bypass flag as public attribute for backward compatibility
        self.allow_admin_bypass = allow_admin_bypass

        # P0 COMPLIANCE: Store audit_strict_mode flag
        # When True (default): Write operations FAIL if audit logging fails
        # When False: Write operations succeed but log at CRITICAL level
        self._audit_strict_mode = audit_strict_mode

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

        # v0.6.0: Policy system removed - use ReBAC for all permissions
        self.policy_matcher = None  # type: ignore[assignment]

        # P0 Fixes: Use EnhancedOperationContext for GA features
        from nexus.core.permissions_enhanced import EnhancedOperationContext

        # Create default context using provided tenant_id/agent_id
        # If tenant_id/agent_id are None, creates an unrestricted context for backward compatibility
        self._default_context = EnhancedOperationContext(  # type: ignore[assignment]
            user="anonymous",
            groups=[],
            tenant_id=tenant_id,
            agent_id=agent_id,
            is_admin=is_admin,
            is_system=False,  # SECURITY: Prevent privilege escalation
            admin_capabilities=set(),  # No capabilities for default context
        )

        # P0 Fixes: Initialize EnhancedReBACManager with all GA features
        from nexus.core.rebac_manager_enhanced import EnhancedReBACManager

        self._rebac_manager = EnhancedReBACManager(
            engine=self.metadata.engine,  # Use SQLAlchemy engine (supports SQLite + PostgreSQL)
            cache_ttl_seconds=cache_ttl_seconds or 300,
            max_depth=10,
            enforce_tenant_isolation=True,  # P0-2: Tenant scoping
            enable_graph_limits=True,  # P0-5: DoS protection
        )

        # P0-4: Initialize AuditStore for admin bypass logging
        from nexus.core.permissions_enhanced import AuditStore

        self._audit_store = AuditStore(engine=self.metadata.engine)

        # P0 Fixes: Initialize EnhancedPermissionEnforcer with audit logging
        from nexus.core.permissions_enhanced import EnhancedPermissionEnforcer

        self._permission_enforcer = EnhancedPermissionEnforcer(  # type: ignore[assignment]
            metadata_store=self.metadata,
            rebac_manager=self._rebac_manager,
            allow_admin_bypass=allow_admin_bypass,  # P0-4: Controlled by constructor parameter
            allow_system_bypass=True,  # P0-4: System operations still allowed
            audit_store=self._audit_store,  # P0-4: Immutable audit log
            admin_bypass_paths=[],  # P0-4: Scoped bypass (empty = no bypass paths)
        )

        # Permission enforcement is opt-in for backward compatibility
        # Set enforce_permissions=True in init to enable permission checks
        self._enforce_permissions = enforce_permissions

        # P0-3: Initialize HierarchyManager for automatic parent tuple creation
        from nexus.core.hierarchy_manager import HierarchyManager

        self._hierarchy_manager = HierarchyManager(
            rebac_manager=self._rebac_manager,
            enable_inheritance=inherit_permissions,
        )

        # Initialize workspace registry for managing registered workspaces/memories
        from nexus.core.workspace_registry import WorkspaceRegistry

        self._workspace_registry = WorkspaceRegistry(metadata=self.metadata)

        # Initialize mount manager for persistent mount configurations
        from nexus.core.mount_manager import MountManager

        self.mount_manager = MountManager(self.metadata.SessionLocal)

        # Load workspace/memory configs from custom config if provided
        if custom_namespaces and hasattr(custom_namespaces, "__iter__"):
            # Check if this came from a config object with workspaces/memories
            # This is a bit hacky but works for now
            pass  # Will be handled by separate load method

        # Initialize workspace manager for snapshot/versioning
        from nexus.core.workspace_manager import WorkspaceManager

        self._workspace_manager = WorkspaceManager(
            metadata=self.metadata,
            backend=self.backend,
            rebac_manager=self._rebac_manager,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )

        # Initialize semantic search - lazy initialization
        self._semantic_search = None

        # Initialize Memory API
        # Memory operations should use subject parameter
        self._memory_api: Memory | None = None  # Lazy initialization
        self._entity_registry: EntityRegistry | None = None
        # Store config for lazy init
        self._memory_config: dict[str, str | None] = {
            "tenant_id": None,
            "user_id": None,
            "agent_id": None,
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

            except (ImportError, AttributeError, TypeError, ValueError) as e:
                # Skip parsers that fail to load due to config or import errors
                # This prevents config errors from breaking the entire system
                import logging

                parser_id = (
                    f"{module_path}.{class_name}" if module_path and class_name else "unknown"
                )
                logging.warning(f"Failed to load parser {parser_id}: {e}")

    @property
    def memory(self) -> Any:
        """Get Memory API instance for agent memory management.

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

    def _validate_path(self, path: str, allow_root: bool = False) -> str:
        """
        Validate and normalize virtual path.

        SECURITY FIX (v0.7.0): Enhanced validation to prevent cache collisions,
        database issues, and undefined behavior from whitespace and malformed paths.

        Args:
            path: Virtual path to validate
            allow_root: If True, allow "/" as a valid path (for directory operations)

        Returns:
            Normalized path (stripped, deduplicated slashes, validated)

        Raises:
            InvalidPathError: If path is invalid or malformed

        Examples:
            >>> fs._validate_path("  /foo/bar  ")  # Stripped
            '/foo/bar'
            >>> fs._validate_path("foo///bar")  # Normalized slashes
            '/foo/bar'
            >>> fs._validate_path(" ")  # Raises InvalidPathError
            InvalidPathError: Path cannot be empty or whitespace-only
        """
        # SECURITY FIX: Strip leading/trailing whitespace to prevent cache collisions
        # Before: " " → "/ " (space in path, causes cache issues)
        # After:  " " → raises InvalidPathError
        original_path = path
        path = path.strip() if isinstance(path, str) else path

        if not path:
            raise InvalidPathError(original_path, "Path cannot be empty or whitespace-only")

        # SECURITY FIX: Reject root path "/" for file operations (unless allow_root=True)
        # The root "/" is ambiguous - is it a directory or file?
        # Use list("/") for directory listings, not read("/") or write("/", ...)
        if path == "/" and not allow_root:
            raise InvalidPathError(
                "/",
                "Root path '/' not allowed for file operations. "
                "Use list('/') for directory listings.",
            )

        # Ensure path starts with /
        if not path.startswith("/"):
            path = "/" + path

        # SECURITY FIX: Normalize multiple consecutive slashes
        # Before: "///foo//bar///" → stored as-is (database issues)
        # After:  "///foo//bar///" → "/foo/bar" (normalized)
        import re

        path = re.sub(r"/+", "/", path)

        # Remove trailing slash (except for root, but we already rejected that)
        if path.endswith("/") and len(path) > 1:
            path = path.rstrip("/")

        # SECURITY FIX: Expanded invalid character list to include tab
        # Tabs are invisible and cause confusion in logs/debugging
        invalid_chars = ["\0", "\n", "\r", "\t"]
        for char in invalid_chars:
            if char in path:
                raise InvalidPathError(path, f"Path contains invalid character: {repr(char)}")

        # SECURITY FIX: Check for leading/trailing whitespace in path components
        # Prevents paths like "/foo/ bar/baz" where " bar" has leading space
        # This causes cache collisions and database query issues
        parts = path.split("/")
        for part in parts:
            if part and (part != part.strip()):
                raise InvalidPathError(
                    path,
                    f"Path component '{part}' has leading/trailing whitespace. "
                    f"Path components must not contain spaces at start/end.",
                )

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
        self, _path: str, _is_directory: bool
    ) -> tuple[str | None, str | None, int | None]:
        """
        Inherit permissions from parent directory (DEPRECATED).

        This method is deprecated. UNIX permissions are no longer used.
        Use ReBAC relationships for permission management.

        Args:
            _path: Virtual path of the new file/directory (unused)
            _is_directory: Whether the new item is a directory (unused)

        Returns:
            Always returns (None, None, None)
        """
        return (None, None, None)

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
        import logging

        logger = logging.getLogger(__name__)

        # Skip if permission enforcement is disabled
        if not self._enforce_permissions:
            return

        # Use default context if none provided
        ctx = context or self._default_context

        logger.info(
            f"_check_permission: path={path}, permission={permission.name}, user={ctx.user}, tenant={getattr(ctx, 'tenant_id', None)}"
        )

        # Check permission using enforcer
        result = self._permission_enforcer.check(path, permission, ctx)
        logger.info(f"  -> permission_enforcer.check returned: {result}")

        if not result:
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

        # Note: UNIX permissions (owner/group/mode) are deprecated.
        # All permissions are now managed through ReBAC relationships.
        # We no longer inherit or store UNIX permissions in metadata.

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
        )

        self.metadata.put(metadata)

    # === Directory Operations ===

    @rpc_expose(description="Create directory")
    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | EnhancedOperationContext | None = None,
    ) -> None:
        """
        Create a directory.

        Args:
            path: Virtual path to directory
            parents: Create parent directories if needed (like mkdir -p)
            exist_ok: Don't raise error if directory exists
            context: Operation context with user, permissions, tenant info (uses default if None)

        Raises:
            FileExistsError: If directory exists and exist_ok=False
            FileNotFoundError: If parent doesn't exist and parents=False
            InvalidPathError: If path is invalid
            BackendError: If operation fails
            AccessDeniedError: If access is denied (tenant isolation or read-only namespace)
            PermissionError: If path is read-only or user doesn't have write permission on parent
        """
        path = self._validate_path(path)

        # Use provided context or default
        ctx = context if context is not None else self._default_context

        # Check write permission on parent directory
        # Only check if parent exists and we're not creating it with --parents
        # Skip check if parent will be created as part of this mkdir operation
        parent_path = self._get_parent_path(path)
        if parent_path and self.metadata.exists(parent_path) and not parents:
            self._check_permission(parent_path, Permission.WRITE, ctx)  # type: ignore[arg-type]

        # Route to backend with write access check (mkdir requires write permission)
        route = self.router.route(
            path,
            tenant_id=ctx.tenant_id,
            agent_id=ctx.agent_id,
            is_admin=ctx.is_admin,
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
            import logging

            logger = logging.getLogger(__name__)

            for parent_dir in reversed(parents_to_create):
                self._create_directory_metadata(parent_dir)
                # P0-3: Create parent tuples for each intermediate directory
                # This ensures permission inheritance works for deeply nested paths
                if hasattr(self, "_hierarchy_manager"):
                    try:
                        ctx_inner = context or self._default_context
                        logger.info(
                            f"mkdir: Creating parent tuples for intermediate dir: {parent_dir}"
                        )
                        self._hierarchy_manager.ensure_parent_tuples(
                            parent_dir, tenant_id=ctx_inner.tenant_id
                        )
                    except Exception as e:
                        # Don't fail mkdir if parent tuple creation fails
                        logger.warning(
                            f"mkdir: Failed to create parent tuples for {parent_dir}: {e}"
                        )
                        pass

        # Create explicit metadata entry for the directory
        self._create_directory_metadata(path)

        # P0-3: Create parent relationship tuples for directory inheritance
        # This enables granting access to /workspace to automatically grant access to subdirectories
        import logging

        logger = logging.getLogger(__name__)

        logger.info(
            f"mkdir: Checking for hierarchy_manager: hasattr={hasattr(self, '_hierarchy_manager')}"
        )

        if hasattr(self, "_hierarchy_manager"):
            try:
                ctx = context or self._default_context
                logger.info(
                    f"mkdir: Calling ensure_parent_tuples for {path}, tenant_id={ctx.tenant_id}"
                )
                created_count = self._hierarchy_manager.ensure_parent_tuples(
                    path, tenant_id=ctx.tenant_id
                )
                logger.info(f"mkdir: Created {created_count} parent tuples for {path}")
                if created_count > 0:
                    logger.debug(f"Created {created_count} parent tuples for {path}")
            except Exception as e:
                # Log the error but don't fail the mkdir operation
                # This helps diagnose issues with parent tuple creation
                logger.warning(
                    f"Failed to create parent tuples for {path}: {type(e).__name__}: {e}"
                )
                import traceback

                logger.debug(traceback.format_exc())

    @rpc_expose(description="Remove directory")
    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        subject: tuple[str, str] | None = None,
        context: OperationContext | EnhancedOperationContext | None = None,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        is_admin: bool | None = None,
    ) -> None:
        """
        Remove a directory.

        Args:
            path: Virtual path to directory
            recursive: Remove non-empty directory (like rm -rf)
            subject: Subject performing the operation as (type, id) tuple
            context: Operation context (DEPRECATED, use subject instead)
            tenant_id: Legacy tenant ID (DEPRECATED)
            agent_id: Legacy agent ID (DEPRECATED)
            is_admin: Admin override flag

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

        # P0 Fixes: Create EnhancedOperationContext
        from nexus.core.permissions_enhanced import EnhancedOperationContext

        if context is not None:
            ctx = (
                context
                if isinstance(context, EnhancedOperationContext)
                else EnhancedOperationContext(
                    user=context.user,
                    groups=context.groups,
                    tenant_id=context.tenant_id or tenant_id,
                    agent_id=context.agent_id or agent_id,
                    is_admin=context.is_admin if is_admin is None else is_admin,
                    is_system=context.is_system,
                    admin_capabilities=set(),
                )
            )
        elif subject is not None:
            ctx = EnhancedOperationContext(
                user=subject[1],
                groups=[],
                tenant_id=tenant_id,
                agent_id=agent_id,
                is_admin=is_admin or False,
                is_system=False,
                admin_capabilities=set(),
            )
        else:
            ctx = (
                self._default_context
                if isinstance(self._default_context, EnhancedOperationContext)
                else EnhancedOperationContext(
                    user=self._default_context.user,
                    groups=self._default_context.groups,
                    tenant_id=tenant_id or self._default_context.tenant_id,
                    agent_id=agent_id or self._default_context.agent_id,
                    is_admin=(is_admin if is_admin is not None else self._default_context.is_admin),
                    is_system=self._default_context.is_system,
                    admin_capabilities=set(),
                )
            )

        # Check write permission on directory
        self._check_permission(path, Permission.WRITE, ctx)  # type: ignore[arg-type]

        # Route to backend with write access check (rmdir requires write permission)
        route = self.router.route(
            path,
            tenant_id=ctx.tenant_id,
            agent_id=ctx.agent_id,
            is_admin=ctx.is_admin,
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

        # Also delete the directory's own metadata entry if it exists
        # Directories can have metadata entries (created by mkdir)
        with contextlib.suppress(Exception):
            self.metadata.delete(path)

    @rpc_expose(description="Check if path is a directory")
    def is_directory(
        self,
        path: str,
        context: OperationContext | EnhancedOperationContext | None = None,
    ) -> bool:
        """
        Check if path is a directory (explicit or implicit).

        Args:
            path: Virtual path to check
            context: Operation context with user, permissions, tenant info (uses default if None)

        Returns:
            True if path is a directory, False otherwise

        Note:
            This method requires READ permission on the path when enforce_permissions=True.
            Returns False if path doesn't exist or user lacks permission.
        """
        try:
            path = self._validate_path(path)

            # Use provided context or default
            ctx = context if context is not None else self._default_context

            # Check read permission
            # Don't raise exception, just return False if no permission
            try:
                self._check_permission(path, Permission.READ, ctx)  # type: ignore[arg-type]
            except PermissionError:
                return False

            # Route with access control (read permission needed to check)
            route = self.router.route(
                path,
                tenant_id=ctx.tenant_id,  # v0.6.0: from context
                agent_id=ctx.agent_id,  # v0.6.0: from context
                is_admin=ctx.is_admin,  # v0.6.0: from context
                check_write=False,
            )
            # Check if it's an explicit directory in the backend
            if route.backend.is_directory(route.backend_path):
                return True
            # Check if it's an implicit directory (has files beneath it)
            return self.metadata.is_implicit_directory(path)
        except (InvalidPathError, Exception):
            return False

    @rpc_expose(description="Get available namespaces")
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
                except (OSError, PermissionError, TypeError):
                    # I/O, permission, or type errors - skip silently (best-effort directory listing)
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
                except (OSError, PermissionError, TypeError):
                    # I/O, permission, or type errors - skip silently (best-effort directory listing)
                    pass

        except (ValueError, AttributeError, KeyError):
            # Ignore routing errors - directory detection is best-effort
            pass

        return directories

    # === Metadata Export/Import ===

    @rpc_expose(description="Export metadata to JSONL file")
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
                except (OSError, ValueError, json.JSONDecodeError):
                    # Ignore errors when fetching custom metadata (DB errors or JSON decode issues)
                    pass

                # Write JSON line
                f.write(json.dumps(metadata_dict) + "\n")
                count += 1

        return count

    @rpc_expose(description="Import metadata from JSONL file")
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

    @rpc_expose(description="Batch get content IDs for multiple paths")
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

        # Read file content with system bypass for background parsing
        # Auto-parse is a system operation that should not be subject to user permissions
        from nexus.core.permissions_enhanced import EnhancedOperationContext

        parse_ctx = EnhancedOperationContext(
            user="system_parser", groups=[], tenant_id=None, is_system=True
        )
        content = self.read(path, context=parse_ctx)  # type: ignore[arg-type]

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

    @rpc_expose(description="Create workspace snapshot")
    def workspace_snapshot(
        self,
        workspace_path: str | None = None,
        agent_id: str | None = None,  # DEPRECATED: For backward compatibility
        description: str | None = None,
        tags: list[str] | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """Create a snapshot of a registered workspace.

        Args:
            workspace_path: Path to registered workspace (e.g., "/my-workspace")
            agent_id: DEPRECATED - Use workspace_path instead
            description: Human-readable description of snapshot
            tags: List of tags for categorization
            created_by: User/agent who created the snapshot

        Returns:
            Snapshot metadata dict

        Raises:
            ValueError: If workspace not registered or not provided
            BackendError: If snapshot cannot be created

        Example:
            >>> nx = NexusFS(backend)
            >>> nx.register_workspace("/my-workspace")
            >>> snapshot = nx.workspace_snapshot("/my-workspace", description="Initial state")
            >>> print(f"Created snapshot #{snapshot['snapshot_number']}")
        """
        # Backward compatibility: support old agent_id parameter
        if workspace_path is None and agent_id:
            import warnings

            warnings.warn(
                "agent_id parameter is deprecated. Use workspace_path parameter instead. "
                "Auto-registering workspace for backward compatibility.",
                DeprecationWarning,
                stacklevel=2,
            )
            # Auto-construct path from agent_id (simple format, no tenant in path)
            workspace_path = f"/workspace/{agent_id}"

            # Auto-register if not exists
            if not self._workspace_registry.get_workspace(workspace_path):
                self._workspace_registry.register_workspace(
                    workspace_path,
                    name=f"auto-{agent_id}",
                    description=f"Auto-registered workspace for agent {agent_id}",
                )

        if not workspace_path:
            raise ValueError("workspace_path must be provided")

        # Verify workspace is registered
        if not self._workspace_registry.get_workspace(workspace_path):
            raise ValueError(
                f"Workspace not registered: {workspace_path}. Use register_workspace() first."
            )

        return self._workspace_manager.create_snapshot(
            workspace_path=workspace_path,
            description=description,
            tags=tags,
            created_by=created_by,
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
        )

    @rpc_expose(description="Restore workspace snapshot")
    def workspace_restore(
        self,
        snapshot_number: int,
        workspace_path: str | None = None,
        agent_id: str | None = None,  # DEPRECATED: For backward compatibility
    ) -> dict[str, Any]:
        """Restore workspace to a previous snapshot.

        Args:
            snapshot_number: Snapshot version number to restore
            workspace_path: Path to registered workspace
            agent_id: DEPRECATED - Use workspace_path instead

        Returns:
            Restore operation result

        Raises:
            ValueError: If workspace not registered or not provided
            NexusFileNotFoundError: If snapshot not found

        Example:
            >>> nx = NexusFS(backend)
            >>> result = nx.workspace_restore(5, "/my-workspace")
            >>> print(f"Restored {result['files_restored']} files")
        """
        # Backward compatibility: support old agent_id parameter
        if workspace_path is None and agent_id:
            import warnings

            warnings.warn(
                "agent_id parameter is deprecated. Use workspace_path parameter instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            workspace_path = f"/workspace/{agent_id}"

        if workspace_path is None and self.agent_id:
            workspace_path = f"/workspace/{self.agent_id}"

        if not workspace_path:
            raise ValueError("workspace_path must be provided")

        # Verify workspace is registered
        if not self._workspace_registry.get_workspace(workspace_path):
            raise ValueError(f"Workspace not registered: {workspace_path}")

        return self._workspace_manager.restore_snapshot(
            workspace_path=workspace_path,
            snapshot_number=snapshot_number,
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
        )

    @rpc_expose(description="List workspace snapshots")
    def workspace_log(
        self,
        workspace_path: str | None = None,
        agent_id: str | None = None,  # DEPRECATED: For backward compatibility
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List snapshot history for workspace.

        Args:
            workspace_path: Path to registered workspace
            agent_id: DEPRECATED - Use workspace_path instead
            limit: Maximum number of snapshots to return

        Returns:
            List of snapshot metadata dicts (most recent first)

        Raises:
            ValueError: If workspace not registered or not provided

        Example:
            >>> nx = NexusFS(backend)
            >>> snapshots = nx.workspace_log("/my-workspace", limit=10)
            >>> for snap in snapshots:
            >>>     print(f"#{snap['snapshot_number']}: {snap['description']}")
        """
        # Backward compatibility: support old agent_id parameter
        if workspace_path is None and agent_id:
            import warnings

            warnings.warn(
                "agent_id parameter is deprecated. Use workspace_path parameter instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            workspace_path = f"/workspace/{agent_id}"

        if workspace_path is None and self.agent_id:
            workspace_path = f"/workspace/{self.agent_id}"

        if not workspace_path:
            raise ValueError("workspace_path must be provided")

        # Verify workspace is registered
        if not self._workspace_registry.get_workspace(workspace_path):
            raise ValueError(f"Workspace not registered: {workspace_path}")

        return self._workspace_manager.list_snapshots(
            workspace_path=workspace_path,
            limit=limit,
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
        )

    @rpc_expose(description="Compare workspace snapshots")
    def workspace_diff(
        self,
        snapshot_1: int,
        snapshot_2: int,
        workspace_path: str | None = None,
        agent_id: str | None = None,  # DEPRECATED: For backward compatibility
    ) -> dict[str, Any]:
        """Compare two workspace snapshots.

        Args:
            snapshot_1: First snapshot number
            snapshot_2: Second snapshot number
            workspace_path: Path to registered workspace
            agent_id: DEPRECATED - Use workspace_path instead

        Returns:
            Diff dict with added, removed, modified files

        Raises:
            ValueError: If workspace_path not provided
            NexusFileNotFoundError: If either snapshot not found

        Example:
            >>> nx = NexusFS(backend)
            >>> diff = nx.workspace_diff(snapshot_1=5, snapshot_2=10, workspace_path="/my-workspace")
            >>> print(f"Added: {len(diff['added'])}, Modified: {len(diff['modified'])}")
        """
        # Backward compatibility: support old agent_id parameter
        if workspace_path is None and agent_id:
            import warnings

            warnings.warn(
                "agent_id parameter is deprecated. Use workspace_path parameter instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            workspace_path = f"/workspace/{agent_id}"

        if workspace_path is None and self.agent_id:
            workspace_path = f"/workspace/{self.agent_id}"

        if not workspace_path:
            raise ValueError("workspace_path must be provided")

        # Verify workspace is registered
        if not self._workspace_registry.get_workspace(workspace_path):
            raise ValueError(
                f"Workspace not registered: {workspace_path}. Use register_workspace() first."
            )

        # Get snapshot IDs from numbers
        snapshots = self._workspace_manager.list_snapshots(
            workspace_path=workspace_path,
            limit=1000,
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
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

        return self._workspace_manager.diff_snapshots(
            snap_1_id,
            snap_2_id,
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
        )

    # ===== Workspace Registry Management =====

    @rpc_expose()
    def load_workspace_memory_config(
        self,
        workspaces: list[dict] | None = None,
        memories: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Load workspaces and memories from configuration.

        Args:
            workspaces: List of workspace config dicts with keys:
                - path (required): Workspace path
                - name (optional): Friendly name
                - description (optional): Description
                - created_by (optional): Creator
                - metadata (optional): Additional metadata dict
            memories: List of memory config dicts (same format as workspaces)

        Returns:
            Dict with registration results:
                - workspaces_registered: Number of workspaces registered
                - memories_registered: Number of memories registered
                - workspaces_skipped: Number already registered
                - memories_skipped: Number already registered

        Example YAML:
            workspaces:
              - path: /my-workspace
                name: main
                description: My main workspace
              - path: /team/project
                name: team-project

            memories:
              - path: /my-memory
                name: knowledge-base
        """
        results = {
            "workspaces_registered": 0,
            "workspaces_skipped": 0,
            "memories_registered": 0,
            "memories_skipped": 0,
        }

        # Load workspaces
        if workspaces:
            for ws_config in workspaces:
                path = ws_config.get("path")
                if not path:
                    continue

                # Skip if already registered
                if self._workspace_registry.get_workspace(path):
                    results["workspaces_skipped"] += 1
                    continue

                # Register workspace
                self._workspace_registry.register_workspace(
                    path=path,
                    name=ws_config.get("name"),
                    description=ws_config.get("description", ""),
                    created_by=ws_config.get("created_by"),
                    metadata=ws_config.get("metadata"),
                )
                results["workspaces_registered"] += 1

        # Load memories
        if memories:
            for mem_config in memories:
                path = mem_config.get("path")
                if not path:
                    continue

                # Skip if already registered
                if self._workspace_registry.get_memory(path):
                    results["memories_skipped"] += 1
                    continue

                # Register memory
                self._workspace_registry.register_memory(
                    path=path,
                    name=mem_config.get("name"),
                    description=mem_config.get("description", ""),
                    created_by=mem_config.get("created_by"),
                    metadata=mem_config.get("metadata"),
                )
                results["memories_registered"] += 1

        return results

    @rpc_expose()
    def register_workspace(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register a directory as a workspace.

        Args:
            path: Absolute path to workspace directory (e.g., "/my-workspace")
            name: Optional friendly name for the workspace
            description: Human-readable description
            created_by: User/agent who created it (for audit)
            tags: Tags for categorization (reserved for future use)
            metadata: Additional user-defined metadata

        Returns:
            Workspace configuration dict

        Raises:
            ValueError: If path already registered as workspace

        Example:
            >>> nx = NexusFS(backend)
            >>> nx.register_workspace("/my-workspace", name="main", description="My main workspace")
            >>> nx.workspace_snapshot("/my-workspace", description="Initial state")
        """
        # tags parameter reserved for future use
        _ = tags

        config = self._workspace_registry.register_workspace(
            path=path,
            name=name,
            description=description or "",
            created_by=created_by,
            metadata=metadata,
        )
        return config.to_dict()

    @rpc_expose()
    def unregister_workspace(self, path: str) -> bool:
        """Unregister a workspace (does NOT delete files).

        Args:
            path: Workspace path to unregister

        Returns:
            True if unregistered, False if not found

        Example:
            >>> nx.unregister_workspace("/my-workspace")
            True
        """
        return self._workspace_registry.unregister_workspace(path)

    @rpc_expose()
    def list_workspaces(self) -> list[dict]:
        """List all registered workspaces.

        Returns:
            List of workspace configuration dicts

        Example:
            >>> workspaces = nx.list_workspaces()
            >>> for ws in workspaces:
            ...     print(f"{ws['path']}: {ws['name']}")
        """
        configs = self._workspace_registry.list_workspaces()
        return [c.to_dict() for c in configs]

    @rpc_expose()
    def get_workspace_info(self, path: str) -> dict | None:
        """Get information about a registered workspace.

        Args:
            path: Workspace path

        Returns:
            Workspace configuration dict or None if not found

        Example:
            >>> info = nx.get_workspace_info("/my-workspace")
            >>> if info:
            ...     print(f"Workspace: {info['name']}")
        """
        config = self._workspace_registry.get_workspace(path)
        return config.to_dict() if config else None

    # ===== Memory Registry Management =====

    @rpc_expose()
    def register_memory(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register a directory as a memory.

        Args:
            path: Absolute path to memory directory (e.g., "/my-memory")
            name: Optional friendly name for the memory
            description: Human-readable description
            created_by: User/agent who created it (for audit)
            tags: Tags for categorization (reserved for future use)
            metadata: Additional user-defined metadata

        Returns:
            Memory configuration dict

        Raises:
            ValueError: If path already registered as memory

        Example:
            >>> nx = NexusFS(backend)
            >>> nx.register_memory("/my-memory", name="kb", description="Knowledge base")
        """
        # tags parameter reserved for future use
        _ = tags

        config = self._workspace_registry.register_memory(
            path=path,
            name=name,
            description=description or "",
            created_by=created_by,
            metadata=metadata,
        )
        return config.to_dict()

    @rpc_expose()
    def unregister_memory(self, path: str) -> bool:
        """Unregister a memory (does NOT delete files).

        Args:
            path: Memory path to unregister

        Returns:
            True if unregistered, False if not found

        Example:
            >>> nx.unregister_memory("/my-memory")
            True
        """
        return self._workspace_registry.unregister_memory(path)

    @rpc_expose()
    def list_memories(self) -> list[dict]:
        """List all registered memories.

        Returns:
            List of memory configuration dicts

        Example:
            >>> memories = nx.list_memories()
            >>> for mem in memories:
            ...     print(f"{mem['path']}: {mem['name']}")
        """
        configs = self._workspace_registry.list_memories()
        return [c.to_dict() for c in configs]

    @rpc_expose()
    def get_memory_info(self, path: str) -> dict | None:
        """Get information about a registered memory.

        Args:
            path: Memory path

        Returns:
            Memory configuration dict or None if not found

        Example:
            >>> info = nx.get_memory_info("/my-memory")
            >>> if info:
            ...     print(f"Memory: {info['name']}")
        """
        config = self._workspace_registry.get_memory(path)
        return config.to_dict() if config else None

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

        # Close AuditStore to release database connection
        if hasattr(self, "_audit_store"):
            self._audit_store.close()
