"""Unified filesystem implementation for Nexus."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select

from nexus.backends.backend import Backend
from nexus.core.exceptions import InvalidPathError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content

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
from nexus.core.nexus_fs_llm import NexusFSLLMMixin
from nexus.core.nexus_fs_mcp import NexusFSMCPMixin
from nexus.core.nexus_fs_mounts import NexusFSMountsMixin
from nexus.core.nexus_fs_oauth import NexusFSOAuthMixin
from nexus.core.nexus_fs_rebac import NexusFSReBACMixin
from nexus.core.nexus_fs_search import NexusFSSearchMixin
from nexus.core.nexus_fs_skills import NexusFSSkillsMixin
from nexus.core.nexus_fs_versions import NexusFSVersionsMixin
from nexus.core.permissions import OperationContext, Permission
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
    NexusFSOAuthMixin,
    NexusFSSkillsMixin,
    NexusFSMCPMixin,
    NexusFSLLMMixin,
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
        parse_providers: list[dict[str, Any]] | None = None,
        enforce_permissions: bool = True,  # P0-6: ENABLED by default for security
        inherit_permissions: bool = True,  # P0-3: Enable automatic parent tuple creation for directory inheritance
        allow_admin_bypass: bool = False,  # P0-4: Allow admin bypass (DEFAULT OFF for production security)
        audit_strict_mode: bool = True,  # P0 COMPLIANCE: Fail writes if audit logging fails (DEFAULT ON)
        enable_workflows: bool = True,  # v0.7.0: Enable automatic workflow triggering (DEFAULT ON)
        workflow_engine: Any
        | None = None,  # v0.7.0: Optional workflow engine (auto-created if None)
    ):
        # Store config for OAuth factory and other components that need it
        self._config: Any | None = None
        """
        Initialize filesystem.

        Args:
            backend: Backend instance for storing file content (LocalBackend, GCSBackend, etc.)
            db_path: Path to SQLite metadata database (auto-generated if None)
            is_admin: Whether this instance has admin privileges (default: False)
            tenant_id: DEPRECATED - Default tenant ID (for embedded mode only). Server mode should pass via context parameter.
            agent_id: DEPRECATED - Default agent ID (for embedded mode only). Server mode should pass via context parameter.
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
            custom_parsers: (Deprecated) Custom parser configurations. Use parse_providers instead.
            parse_providers: Parse provider configurations (list of dicts with name, priority, api_key, etc.)
                           Supports: unstructured (API), llamaparse (API), markitdown (local fallback)
            enforce_permissions: Enable permission enforcement on file operations (default: True)
            inherit_permissions: Enable automatic parent tuple creation for directory inheritance (default: True, P0-3)
            allow_admin_bypass: Allow admin users to bypass permission checks (default: False for security, P0-4)
            enable_workflows: Enable automatic workflow triggering on file operations (default: True, v0.7.0)
            workflow_engine: Optional workflow engine instance. If None and enable_workflows=True, auto-creates engine (v0.7.0)

        Note:
            When tenant_id or agent_id are provided, they set the default context for all operations.
            Individual operations can still override context by passing context parameter.

        Warning:
            Using tenant_id/agent_id in __init__ is DEPRECATED and unsafe for server mode!
            In server/multi-tenant mode, these should ALWAYS be None and passed via context parameter.

            IMPORTANT: These parameters should ONLY be used in embedded/CLI mode where a single
            NexusFS instance serves one user. In server mode, a shared NexusFS instance serves
            multiple users/tenants, so instance-level tenant_id/agent_id creates security risks!
        """
        # Warn about deprecated parameters
        if tenant_id is not None or agent_id is not None:
            import warnings

            warnings.warn(
                "tenant_id and agent_id parameters in NexusFS.__init__() are DEPRECATED. "
                "They should only be used in embedded/CLI mode where a single NexusFS instance "
                "serves one user. For server mode (shared NexusFS instance serving multiple users), "
                "these MUST be None and context must be passed to each method call instead. "
                "Using instance-level tenant_id/agent_id in server mode creates SECURITY RISKS!",
                DeprecationWarning,
                stacklevel=2,
            )

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

        # Store database path (needed for OAuth TokenManager and other components)
        self.db_path: str | None = str(db_path) if db_path else None

        # Store admin flag and auto-parse setting
        self.is_admin = is_admin
        self.auto_parse = auto_parse

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

        # Initialize parser registry with default MarkItDown parser (legacy, for auto_parse)
        self.parser_registry = ParserRegistry()
        self.parser_registry.register(MarkItDownParser())

        # Load custom parsers from config (deprecated)
        if custom_parsers:
            self._load_custom_parsers(custom_parsers)

        # Initialize new provider registry for read(parsed=True) support
        from nexus.parsers.providers import ProviderRegistry
        from nexus.parsers.providers.base import ProviderConfig

        self.provider_registry = ProviderRegistry()

        if parse_providers:
            # Use explicitly configured providers
            configs = []
            for p in parse_providers:
                configs.append(
                    ProviderConfig(
                        name=p.get("name", "unknown"),
                        enabled=p.get("enabled", True),
                        priority=p.get("priority", 50),
                        api_key=p.get("api_key"),
                        api_url=p.get("api_url"),
                        supported_formats=p.get("supported_formats"),
                    )
                )
            self.provider_registry.auto_discover(configs)
        else:
            # Auto-discover from environment
            self.provider_registry.auto_discover()

        # Track active parser threads for graceful shutdown
        self._parser_threads: list[threading.Thread] = []
        self._parser_threads_lock = threading.Lock()

        # v0.6.0: Policy system removed - use ReBAC for all permissions
        self.policy_matcher = None  # type: ignore[assignment]

        # P0 Fixes: Use OperationContext for GA features
        from nexus.core.permissions import OperationContext

        # Create default context using provided tenant_id/agent_id
        # If tenant_id is None, default to "default" for multi-tenant compatibility
        # This prevents warnings during cache warming and other internal operations
        effective_tenant_id = tenant_id if tenant_id is not None else "default"
        self._default_context = OperationContext(
            user="anonymous",
            groups=[],
            tenant_id=effective_tenant_id,
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

        # v0.5.0 ACE: Initialize EntityRegistry early for agent permission inheritance
        from nexus.core.entity_registry import EntityRegistry

        self._entity_registry: EntityRegistry | None = EntityRegistry(self.metadata.SessionLocal)

        # P0 Fixes: Initialize PermissionEnforcer with audit logging
        from nexus.core.permissions import PermissionEnforcer

        self._permission_enforcer = PermissionEnforcer(
            metadata_store=self.metadata,
            rebac_manager=self._rebac_manager,
            allow_admin_bypass=allow_admin_bypass,  # P0-4: Controlled by constructor parameter
            allow_system_bypass=True,  # P0-4: System operations still allowed
            audit_store=self._audit_store,  # P0-4: Immutable audit log
            admin_bypass_paths=[],  # P0-4: Scoped bypass (empty = no bypass paths)
            router=self.router,  # For backend object type resolution
            entity_registry=self._entity_registry,  # v0.5.0 ACE: For agent inheritance
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

        self._workspace_registry = WorkspaceRegistry(
            metadata=self.metadata,
            rebac_manager=self._rebac_manager,  # v0.5.0: Auto-grant ownership on registration
        )

        # Initialize mount manager for persistent mount configurations
        from nexus.core.mount_manager import MountManager

        self.mount_manager = MountManager(self.metadata.SessionLocal)

        # Initialize OAuth token manager (lazy initialization in mixin)
        self._token_manager = None

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
        # Note: _entity_registry initialized earlier for agent permission inheritance
        # Store config for lazy init
        self._memory_config: dict[str, str | None] = {
            "tenant_id": None,
            "user_id": None,
            "agent_id": None,
        }

        # Issue #372: Sandbox manager - lazy initialization
        from nexus.core.sandbox_manager import SandboxManager

        self._sandbox_manager: SandboxManager | None = None

        # v0.7.0: Initialize workflow engine for automatic event triggering
        self.enable_workflows = enable_workflows
        self.workflow_engine = workflow_engine

        # v0.8.0: Subscription manager for webhook notifications (set by server)
        self.subscription_manager: Any = None

        if enable_workflows and workflow_engine is None:
            # Auto-create workflow engine with persistent storage using global engine
            try:
                from nexus.workflows.engine import init_engine
                from nexus.workflows.storage import WorkflowStore

                workflow_store = WorkflowStore(
                    session_factory=self.metadata.SessionLocal,
                    tenant_id=tenant_id or "default",
                )

                # Use init_engine to set the global engine so WorkflowAPI uses the same instance
                self.workflow_engine = init_engine(
                    metadata_store=self.metadata,
                    plugin_registry=None,  # TODO: Hook up plugin registry if available
                    workflow_store=workflow_store,
                )
            except ImportError:
                # Workflow system not available, disable workflows
                self.enable_workflows = False
                self.workflow_engine = None

        # Load all saved mounts from database and activate them
        # This ensures persisted mounts are restored on server startup
        # By default, skip auto-sync for fast startup (set NEXUS_AUTO_SYNC_MOUNTS=true to enable)
        try:
            if hasattr(self, "load_all_saved_mounts"):
                import os

                # Check environment variable for auto-sync (default: False for fast startup)
                auto_sync = os.getenv("NEXUS_AUTO_SYNC_MOUNTS", "false").lower() in (
                    "true",
                    "1",
                    "yes",
                )

                mount_result = self.load_all_saved_mounts(auto_sync=auto_sync)
                if mount_result["loaded"] > 0 or mount_result["failed"] > 0:
                    import logging

                    logger = logging.getLogger(__name__)
                    sync_msg = (
                        f", {mount_result['synced']} synced" if mount_result["synced"] > 0 else ""
                    )
                    logger.info(
                        f"🔄 Mount restoration: {mount_result['loaded']} loaded{sync_msg}, {mount_result['failed']} failed"
                    )
                    if not auto_sync and mount_result["loaded"] > 0:
                        logger.info(
                            "💡 Auto-sync disabled for fast startup. Use sync_mount() to sync manually or set NEXUS_AUTO_SYNC_MOUNTS=true"
                        )
                    if mount_result["errors"]:
                        for error in mount_result["errors"]:
                            logger.error(f"  ❌ {error}")
        except Exception as e:
            # Log warning but don't fail initialization if mount loading fails
            import logging

            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to load saved mounts during initialization: {e}")

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

            # Get or create entity registry (v0.5.0: Pass SessionFactory instead of Session)
            if self._entity_registry is None:
                self._entity_registry = EntityRegistry(self.metadata.SessionLocal)

            # Create a session from SessionLocal
            session = self.metadata.SessionLocal()

            self._memory_api = Memory(
                session=session,
                backend=self.backend,
                tenant_id=self._memory_config.get("tenant_id"),
                user_id=self._memory_config.get("user_id"),
                agent_id=self._memory_config.get("agent_id"),
                entity_registry=self._entity_registry,
            )

        return self._memory_api

    def _get_created_by(self, context: OperationContext | dict | None = None) -> str | None:
        """Get the created_by value for version history tracking.

        Args:
            context: Operation context with per-request values

        Returns:
            Combined user and agent info when both are available.
            Format: 'user:alice,agent:data_analyst' or just 'user:alice' or 'agent:data_analyst'
        """
        # Extract user and agent from context
        user = None
        agent = None

        if context is None:
            user = getattr(self._default_context, "user", None)
            agent = self._default_context.agent_id
        elif hasattr(context, "agent_id"):
            user = getattr(context, "user", None) or getattr(context, "user_id", None)
            agent = context.agent_id
        elif isinstance(context, dict):
            user = context.get("user_id") or context.get("user")
            agent = context.get("agent_id")
        else:
            user = getattr(self._default_context, "user", None)
            agent = self._default_context.agent_id

        # Build combined string showing both user and agent
        parts = []
        if user:
            parts.append(f"user:{user}")
        if agent:
            parts.append(f"agent:{agent}")

        return ",".join(parts) if parts else None

    def _get_routing_params(
        self, context: OperationContext | dict | None = None
    ) -> tuple[str | None, str | None, bool]:
        """Extract tenant_id, agent_id, and is_admin from context for router.route().

        This is the critical fix for multi-tenancy: extract values from per-request context
        instead of using instance fields (which are shared across all requests in server mode).

        Args:
            context: Operation context with per-request values

        Returns:
            Tuple of (tenant_id, agent_id, is_admin)
        """
        if context is None:
            # Use default context values for embedded mode
            return (
                self._default_context.tenant_id,
                self._default_context.agent_id,
                self._default_context.is_admin,
            )

        # Extract from OperationContext object
        if not isinstance(context, dict):
            return context.tenant_id, context.agent_id, getattr(context, "is_admin", self.is_admin)

        # Extract from dict (legacy)
        if isinstance(context, dict):
            return (
                context.get("tenant_id", self._default_context.tenant_id),
                context.get("agent_id", self._default_context.agent_id),
                context.get("is_admin", self.is_admin),
            )

        # Fallback to default context
        return (
            self._default_context.tenant_id,
            self._default_context.agent_id,
            self._default_context.is_admin,
        )

    # Backward compatibility properties for deprecated instance fields
    @property
    def tenant_id(self) -> str | None:
        """DEPRECATED: Access via context parameter instead. Returns default tenant_id for embedded mode."""
        return self._default_context.tenant_id

    @property
    def agent_id(self) -> str | None:
        """DEPRECATED: Access via context parameter instead. Returns default agent_id for embedded mode."""
        return self._default_context.agent_id

    @property
    def user_id(self) -> str | None:
        """DEPRECATED: Access via context parameter instead. Returns default user_id for embedded mode."""
        return getattr(self._default_context, "user", None)

    def _get_memory_api(self, context: dict | None = None) -> Memory:
        """Get Memory API instance with context-specific configuration.

        Args:
            context: Optional context dict with tenant_id, user_id, agent_id

        Returns:
            Memory API instance
        """
        from nexus.core.entity_registry import EntityRegistry
        from nexus.core.memory_api import Memory

        # Get or create entity registry
        if self._entity_registry is None:
            self._entity_registry = EntityRegistry(self.metadata.SessionLocal)

        # Create a session
        session = self.metadata.SessionLocal()

        # Parse context properly
        ctx = self._parse_context(context)

        return Memory(
            session=session,
            backend=self.backend,
            tenant_id=ctx.tenant_id or self._default_context.tenant_id,
            user_id=ctx.user or self._default_context.user,
            agent_id=ctx.agent_id or self._default_context.agent_id,
            entity_registry=self._entity_registry,
        )

    def _parse_context(self, context: OperationContext | dict | None = None) -> OperationContext:
        """Parse context dict or OperationContext into OperationContext.

        Args:
            context: Optional context dict or OperationContext with user_id, groups, tenant_id, etc.

        Returns:
            OperationContext instance
        """
        from nexus.core.permissions import OperationContext

        # If already an OperationContext, return as-is
        if isinstance(context, OperationContext):
            return context

        if context is None:
            context = {}

        return OperationContext(
            user=context.get("user_id", "system"),
            groups=context.get("groups", []),
            tenant_id=context.get("tenant_id"),
            agent_id=context.get("agent_id"),
            is_admin=context.get("is_admin", False),
            is_system=context.get("is_system", False),
        )

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
        from nexus.core.permissions import OperationContext

        ctx_raw = context or self._default_context
        assert isinstance(ctx_raw, OperationContext), "Context must be OperationContext"
        ctx: OperationContext = ctx_raw

        logger.debug(
            f"_check_permission: path={path}, permission={permission.name}, user={ctx.user}, tenant={getattr(ctx, 'tenant_id', None)}"
        )

        # Fix #332: Virtual parsed views (e.g., report_parsed.pdf.md) should inherit
        # permissions from their original files (e.g., report.pdf)
        from nexus.core.virtual_views import parse_virtual_path

        # Use metadata.exists to avoid circular dependency with self.exists()
        def metadata_exists(check_path: str) -> bool:
            return self.metadata.exists(check_path)

        original_path, view_type = parse_virtual_path(path, metadata_exists)
        if view_type == "md":
            # This is a virtual view - check permissions on the original file instead
            logger.debug(
                f"  -> Virtual view detected: checking permissions on original file {original_path}"
            )
            permission_path = original_path
        else:
            permission_path = path

        # Check permission using enforcer
        result = self._permission_enforcer.check(permission_path, permission, ctx)
        logger.debug(f"  -> permission_enforcer.check returned: {result}")

        if not result:
            raise PermissionError(
                f"Access denied: User '{ctx.user}' does not have {permission.name} "
                f"permission for '{path}'"
            )

    def _create_directory_metadata(
        self, path: str, context: OperationContext | None = None
    ) -> None:
        """
        Create metadata entry for a directory.

        Args:
            path: Virtual path to directory
            context: Operation context (for tenant_id and created_by)
        """
        now = datetime.now(UTC)

        # Use provided context or default
        ctx = context if context is not None else self._default_context

        # Note: UNIX permissions (owner/group/mode) are deprecated.
        # All permissions are now managed through ReBAC relationships.
        # We no longer inherit or store UNIX permissions in metadata.

        # Create a marker for the directory in metadata
        # We use an empty content hash as a placeholder
        empty_hash = hash_content(b"")

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
            created_by=self._get_created_by(context),  # Track who created this directory
            tenant_id=ctx.tenant_id or "default",  # P0 SECURITY: Set tenant_id
        )

        self.metadata.put(metadata)

    # === Directory Operations ===

    @rpc_expose(description="Create directory")
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
            self._check_permission(parent_path, Permission.WRITE, ctx)

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
        route.backend.mkdir(route.backend_path, parents=parents, exist_ok=True, context=ctx)

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
                self._create_directory_metadata(parent_dir, context=ctx)
                # P0-3: Create parent tuples for each intermediate directory
                # This ensures permission inheritance works for deeply nested paths
                if hasattr(self, "_hierarchy_manager"):
                    try:
                        logger.debug(
                            f"mkdir: Creating parent tuples for intermediate dir: {parent_dir}"
                        )
                        self._hierarchy_manager.ensure_parent_tuples(
                            parent_dir, tenant_id=ctx.tenant_id or "default"
                        )
                    except Exception as e:
                        # Don't fail mkdir if parent tuple creation fails
                        logger.warning(
                            f"mkdir: Failed to create parent tuples for {parent_dir}: {e}"
                        )
                        pass

        # Create explicit metadata entry for the directory
        self._create_directory_metadata(path, context=ctx)

        # P0-3: Create parent relationship tuples for directory inheritance
        # This enables granting access to /workspace to automatically grant access to subdirectories
        import logging

        logger = logging.getLogger(__name__)

        logger.debug(
            f"mkdir: Checking for hierarchy_manager: hasattr={hasattr(self, '_hierarchy_manager')}"
        )

        ctx = context or self._default_context

        if hasattr(self, "_hierarchy_manager"):
            try:
                logger.debug(
                    f"mkdir: Calling ensure_parent_tuples for {path}, tenant_id={ctx.tenant_id or 'default'}"
                )
                created_count = self._hierarchy_manager.ensure_parent_tuples(
                    path, tenant_id=ctx.tenant_id or "default"
                )
                logger.debug(f"mkdir: Created {created_count} parent tuples for {path}")
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

        # Grant direct_owner permission to the user who created the directory
        # Note: Use 'direct_owner' (not 'owner') as the base relation.
        # 'owner' is a computed union of direct_owner + parent_owner in the ReBAC schema.
        if self._rebac_manager and ctx.user and not ctx.is_system:
            try:
                logger.debug(f"mkdir: Granting direct_owner permission to {ctx.user} for {path}")
                self._rebac_manager.rebac_write(
                    subject=("user", ctx.user),
                    relation="direct_owner",
                    object=("file", path),
                    tenant_id=ctx.tenant_id or "default",
                )
                logger.debug(f"mkdir: Granted direct_owner permission to {ctx.user} for {path}")
            except Exception as e:
                logger.warning(f"Failed to grant direct_owner permission for {path}: {e}")

    @rpc_expose(description="Remove directory")
    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        subject: tuple[str, str] | None = None,
        context: OperationContext | None = None,
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

        # P0 Fixes: Create OperationContext
        from nexus.core.permissions import OperationContext

        if context is not None:
            ctx = (
                context
                if isinstance(context, OperationContext)
                else OperationContext(
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
            ctx = OperationContext(
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
                if isinstance(self._default_context, OperationContext)
                else OperationContext(
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
        import logging

        logger = logging.getLogger(__name__)
        logger.debug(
            f"rmdir: path={path}, recursive={recursive}, user={ctx.user}, is_admin={ctx.is_admin}"
        )
        self._check_permission(path, Permission.WRITE, ctx)
        logger.debug(f"  -> Permission check PASSED for rmdir on {path}")

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

    def _has_descendant_access(
        self,
        path: str,
        permission: Permission,
        context: OperationContext,
    ) -> bool:
        """
        Check if user has access to a path OR any of its descendants.

        This enables hierarchical directory navigation: users can see parent directories
        if they have access to any child/descendant (even if deeply nested).

        Workflow:
        1. Check direct access on the path first (fast path)
        2. If no direct access, query all descendants from metadata
        3. Check ReBAC permissions on each descendant until one is accessible
        4. Early exit on first accessible descendant (performance optimization)

        Args:
            path: Path to check (e.g., "/workspace")
            permission: Permission to check (e.g., Permission.READ)
            context: User context with subject info

        Returns:
            True if user has access to path OR any descendant, False otherwise

        Performance Notes:
            - Uses prefix query on metadata for efficiency
            - Early exit after finding first accessible descendant
            - Skips descendant check if no ReBAC manager available

        Examples:
            >>> # Joe has access to /workspace/joe/file.txt
            >>> _has_descendant_access("/workspace", READ, joe_ctx)
            True  # Can access /workspace because has access to descendant

            >>> _has_descendant_access("/other", READ, joe_ctx)
            False  # No access to /other or any descendants
        """
        # Admin/system bypass
        if context.is_admin or context.is_system:
            return True

        # Check if ReBAC is available
        has_rebac = hasattr(self, "_rebac_manager") and self._rebac_manager is not None

        if not has_rebac:
            # Fallback to permission enforcer if no ReBAC
            from nexus.core.permissions import OperationContext

            assert isinstance(context, OperationContext), "Context must be OperationContext"
            return self._permission_enforcer.check(path, permission, context)

        # Validate subject_id (required for ReBAC checks)
        if context.subject_id is None:
            return False

        # Type narrowing - create local variables with explicit types
        subject_id: str = context.subject_id  # Now guaranteed non-None after check
        subject_tuple: tuple[str, str] = (context.subject_type, subject_id)

        # Map permission to ReBAC permission name
        permission_map = {
            Permission.READ: "read",
            Permission.WRITE: "write",
            Permission.EXECUTE: "execute",
        }
        rebac_permission = permission_map.get(permission, "read")

        # 1. Check direct access first (fast path)
        direct_access = self.rebac_check(
            subject=subject_tuple,
            permission=rebac_permission,
            object=("file", path),
            tenant_id=context.tenant_id,
        )
        if direct_access:
            return True

        # 2. Check if user has access to ANY descendant
        # Get all files/directories under this path (recursive)
        prefix = path if path.endswith("/") else path + "/"
        if path == "/":
            prefix = ""

        try:
            all_descendants = self.metadata.list(prefix)
        except Exception:
            # If metadata query fails, return False
            return False

        # 3. OPTIMIZATION (issue #380): Use bulk permission checking for descendants
        # Instead of checking each descendant individually (N queries), use rebac_check_bulk()
        if (
            hasattr(self, "_rebac_manager")
            and self._rebac_manager is not None
            and hasattr(self._rebac_manager, "rebac_check_bulk")
        ):
            import logging

            logger = logging.getLogger(__name__)
            logger.debug(
                f"_has_descendant_access: Using bulk check for {len(all_descendants)} descendants of {path}"
            )

            # Build list of checks for all descendants
            checks = [
                (subject_tuple, rebac_permission, ("file", meta.path)) for meta in all_descendants
            ]

            try:
                # Perform bulk permission check
                results = self._rebac_manager.rebac_check_bulk(
                    checks, tenant_id=context.tenant_id or "default"
                )

                # Check if any descendant is accessible
                for check in checks:
                    if results.get(check, False):
                        logger.debug(
                            f"_has_descendant_access: Found accessible descendant {check[2][1]}"
                        )
                        return True

                logger.debug("_has_descendant_access: No accessible descendants found")
                return False

            except Exception as e:
                logger.warning(
                    f"_has_descendant_access: Bulk check failed, falling back to individual checks: {e}"
                )
                # Fall through to original implementation

        # Fallback: Check ReBAC permissions on each descendant (with early exit)
        for meta in all_descendants:
            descendant_access = self.rebac_check(
                subject=subject_tuple,
                permission=rebac_permission,
                object=("file", meta.path),
                tenant_id=context.tenant_id,
            )
            if descendant_access:
                # Found accessible descendant! User can see this parent
                return True

        # No accessible descendants found
        return False

    def _has_descendant_access_bulk(
        self,
        paths: list[str],
        permission: Permission,
        context: OperationContext,
    ) -> dict[str, bool]:
        """Check if user has access to any descendant for multiple paths in bulk.

        This is an optimization for list() operations that need to check many backend directories.
        Instead of calling _has_descendant_access() for each directory (N separate bulk queries),
        this method batches all directories + all their descendants into ONE bulk query.

        Args:
            paths: List of directory paths to check
            permission: Permission to check (READ, WRITE, or EXECUTE)
            context: Operation context with user/agent identity

        Returns:
            Dict mapping each path to True (has access) or False (no access)

        Performance:
            - Before: N directories × 1 bulk query = N bulk queries
            - After: 1 bulk query for all directories + all descendants
            - 10x improvement for 10 backend directories
        """
        import logging

        logger = logging.getLogger(__name__)

        # Admin/system bypass
        if context.is_admin or context.is_system:
            return dict.fromkeys(paths, True)

        # Check if ReBAC bulk checking is available
        if not (
            hasattr(self, "_rebac_manager")
            and self._rebac_manager is not None
            and hasattr(self._rebac_manager, "rebac_check_bulk")
        ):
            # Fallback to individual checks
            return {path: self._has_descendant_access(path, permission, context) for path in paths}

        # Validate subject_id
        if context.subject_id is None:
            return dict.fromkeys(paths, False)

        subject_tuple: tuple[str, str] = (context.subject_type, context.subject_id)

        # Map permission to ReBAC name
        permission_map = {
            Permission.READ: "read",
            Permission.WRITE: "write",
            Permission.EXECUTE: "execute",
        }
        rebac_permission = permission_map.get(permission, "read")

        # PHASE 1: Collect all descendants for all paths
        all_checks = []
        path_to_descendants: dict[str, list[str]] = {}

        for path in paths:
            # Check direct access to the directory itself
            all_checks.append((subject_tuple, rebac_permission, ("file", path)))

            # Get all descendants
            prefix = path if path.endswith("/") else path + "/"
            if path == "/":
                prefix = ""

            try:
                descendants = self.metadata.list(prefix)
                descendant_paths = [meta.path for meta in descendants]
                path_to_descendants[path] = descendant_paths

                # Add checks for all descendants
                for desc_path in descendant_paths:
                    all_checks.append((subject_tuple, rebac_permission, ("file", desc_path)))
            except Exception as e:
                logger.warning(f"_has_descendant_access_bulk: Failed to list {path}: {e}")
                path_to_descendants[path] = []

        logger.debug(
            f"_has_descendant_access_bulk: Checking {len(all_checks)} paths for {len(paths)} directories"
        )

        # PHASE 2: Perform ONE bulk permission check for everything
        try:
            results = self._rebac_manager.rebac_check_bulk(
                all_checks, tenant_id=context.tenant_id or "default"
            )
        except Exception as e:
            logger.warning(f"_has_descendant_access_bulk: Bulk check failed, falling back: {e}")
            # Fallback to individual checks
            return {path: self._has_descendant_access(path, permission, context) for path in paths}

        # PHASE 3: Map results back to each directory
        result_map = {}
        for path in paths:
            # Check if user has access to directory itself
            direct_check = (subject_tuple, rebac_permission, ("file", path))
            if results.get(direct_check, False):
                result_map[path] = True
                continue

            # Check if user has access to any descendant
            has_access = False
            for desc_path in path_to_descendants.get(path, []):
                desc_check = (subject_tuple, rebac_permission, ("file", desc_path))
                if results.get(desc_check, False):
                    has_access = True
                    break

            result_map[path] = has_access

        logger.debug(
            f"_has_descendant_access_bulk: {sum(result_map.values())}/{len(paths)} directories accessible"
        )
        return result_map

    @rpc_expose(description="Check if path is a directory")
    def is_directory(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> bool:
        """
        Check if path is a directory (explicit or implicit).

        Args:
            path: Virtual path to check
            context: Operation context with user, permissions, tenant info (uses default if None)

        Returns:
            True if path is a directory, False otherwise

        Note:
            This method requires READ permission on the path OR any descendant when
            enforce_permissions=True. Returns True if user has access to the directory
            or any child/descendant (enables hierarchical navigation).
            Returns False if path doesn't exist or user lacks permission to path and all descendants.
        """
        try:
            path = self._validate_path(path)

            # Use provided context or default
            ctx = context if context is not None else self._default_context

            # Check read permission (with hierarchical descendant access)
            # Use hierarchical access check: return True if user has access to path OR any descendant
            if self._enforce_permissions and not self._has_descendant_access(
                path, Permission.READ, ctx
            ):
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
        import logging
        import time

        logger = logging.getLogger(__name__)

        start = time.time()
        logger.warning(
            f"[PERF-IMPL] get_available_namespaces: START, is_admin={self.is_admin}, namespace_count={len(self.router._namespaces)}"
        )

        namespaces = []

        for name, config in self.router._namespaces.items():
            # Include namespace if it's not admin-only OR user is admin
            # Note: We show all namespaces regardless of tenant_id.
            # Tenant filtering happens when accessing files within the namespace.
            if not config.admin_only or self.is_admin:
                namespaces.append(name)

        result = sorted(namespaces)
        elapsed = time.time() - start
        logger.warning(
            f"[PERF-IMPL] get_available_namespaces: DONE in {elapsed:.3f}s, returned {len(result)} namespaces: {result}"
        )
        return result

    @rpc_expose(description="Get file metadata for FUSE operations")
    def get_metadata(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any] | None:
        """
        Get file metadata (permissions, ownership, size, etc.) for FUSE operations.

        This method retrieves metadata without reading the file content,
        used primarily by FUSE getattr() operations.

        Args:
            path: Virtual file path
            context: Operation context with user, permissions, tenant info

        Returns:
            Metadata dict with keys: path, size, mime_type, created_at, modified_at,
            is_directory, owner, mode. Returns None if file doesn't exist.

        Examples:
            >>> metadata = fs.get_metadata("/workspace/file.txt")
            >>> print(f"Size: {metadata['size']} bytes")
        """
        ctx = context or self._default_context
        normalized = self._validate_path(path, allow_root=True)

        # Check if it's a directory first
        is_dir = self.is_directory(normalized, context=ctx)

        if is_dir:
            # Return directory metadata
            return {
                "path": normalized,
                "size": 4096,  # Standard directory size
                "mime_type": "inode/directory",
                "created_at": None,
                "modified_at": None,
                "is_directory": True,
                "owner": ctx.user_id,
                "group": ctx.user_id,
                "mode": 0o755,  # drwxr-xr-x
            }

        # Try to get file metadata from store
        file_meta = self.metadata.get(normalized)
        if file_meta is None:
            return None

        return {
            "path": file_meta.path,
            "size": file_meta.size or 0,
            "mime_type": file_meta.mime_type or "application/octet-stream",
            "created_at": file_meta.created_at.isoformat() if file_meta.created_at else None,
            "modified_at": file_meta.modified_at.isoformat() if file_meta.modified_at else None,
            "is_directory": False,
            "owner": ctx.user_id,
            "group": ctx.user_id,
            "mode": 0o644,  # -rw-r--r--
        }

    @rpc_expose(description="Get ETag (content hash) for HTTP caching")
    def get_etag(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> str | None:
        """Get the ETag (content hash) for a file without reading content.

        This method is optimized for HTTP caching - it retrieves only the
        content hash from metadata, not the actual content. Use this for
        efficient If-None-Match / 304 Not Modified checks.

        For local backend: Returns content_hash from file_paths table.
        For connectors: Returns content_hash from content_cache table (if cached).

        Args:
            path: Virtual file path
            context: Operation context

        Returns:
            Content hash (ETag) if available, None otherwise

        Examples:
            >>> etag = fs.get_etag("/workspace/file.txt")
            >>> if etag == request.headers.get("If-None-Match"):
            ...     return Response(status_code=304)
        """
        _ = context  # Reserved for future permission checks
        normalized = self._validate_path(path, allow_root=False)

        # Get file metadata (lightweight - doesn't read content)
        file_meta = self.metadata.get(normalized)
        if file_meta is None:
            return None

        # Return the etag (content_hash) from metadata
        return file_meta.etag

    def _get_backend_directory_entries(
        self, path: str, context: OperationContext | None = None
    ) -> set[str]:
        """
        Get directory entries from backend for empty directory detection.

        This helper method queries the backend's list_dir() to find directories
        that don't contain any files (empty directories). It handles routing
        and error cases gracefully.

        Args:
            path: Virtual path to list (e.g., "/", "/workspace")
            context: Optional operation context for routing (uses default if not provided)

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
                # Non-root path - use router with context
                tenant_id, agent_id, is_admin = self._get_routing_params(context)
                route = self.router.route(
                    path.rstrip("/"),
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    is_admin=is_admin,
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
                                created_by=self._get_created_by(),  # Track who imported this version
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
                                created_by=self._get_created_by(),  # Track who imported this version
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
                                created_by=self._get_created_by(),  # Track who imported this version
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
                                    created_by=self._get_created_by(),  # Track who imported this version
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
                        created_by=self._get_created_by(),  # Track who imported this version
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
        from nexus.core.permissions import OperationContext

        parse_ctx = OperationContext(
            user="system_parser", groups=[], tenant_id=None, is_system=True
        )
        content = self.read(path, context=parse_ctx)

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
        context: dict | None = None,  # v0.5.0: RPC context with user_id
    ) -> dict[str, Any]:
        """Create a snapshot of a registered workspace.

        Args:
            workspace_path: Path to registered workspace (e.g., "/my-workspace")
            agent_id: DEPRECATED - Use workspace_path instead
            description: Human-readable description of snapshot
            tags: List of tags for categorization
            created_by: User/agent who created the snapshot
            context: Operation context (v0.5.0)

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

        # v0.5.0: Extract user_id, agent_id, and tenant_id from context (set by RPC authentication)
        ctx = self._parse_context(context)

        return self._workspace_manager.create_snapshot(
            workspace_path=workspace_path,
            description=description,
            tags=tags,
            created_by=created_by,
            user_id=ctx.user
            or self._default_context.user,  # v0.5.0: Pass user_id for permission check
            agent_id=ctx.agent_id or self._default_context.agent_id,
            tenant_id=ctx.tenant_id
            or self._default_context.tenant_id,  # v0.5.0: Use context tenant_id
        )

    @rpc_expose(description="Restore workspace snapshot")
    def workspace_restore(
        self,
        snapshot_number: int,
        workspace_path: str | None = None,
        agent_id: str | None = None,  # DEPRECATED: For backward compatibility
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Restore workspace to a previous snapshot.

        Args:
            snapshot_number: Snapshot version number to restore
            workspace_path: Path to registered workspace
            agent_id: DEPRECATED - Use workspace_path instead
            context: Operation context with user, permissions, tenant info (uses default if None)

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
        # Use provided context or default
        ctx = context if context is not None else self._default_context

        # Backward compatibility: support old agent_id parameter
        if workspace_path is None and agent_id:
            import warnings

            warnings.warn(
                "agent_id parameter is deprecated. Use workspace_path parameter instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            workspace_path = f"/workspace/{agent_id}"

        if workspace_path is None:
            # Fallback to context agent_id, then default context
            fallback_agent_id = ctx.agent_id or self._default_context.agent_id
            if fallback_agent_id:
                workspace_path = f"/workspace/{fallback_agent_id}"

        if not workspace_path:
            raise ValueError("workspace_path must be provided")

        # Verify workspace is registered
        if not self._workspace_registry.get_workspace(workspace_path):
            raise ValueError(f"Workspace not registered: {workspace_path}")

        return self._workspace_manager.restore_snapshot(
            workspace_path=workspace_path,
            snapshot_number=snapshot_number,
            user_id=ctx.user,  # v0.5.0: Pass user_id from context
            agent_id=ctx.agent_id or self._default_context.agent_id,
            tenant_id=ctx.tenant_id or self._default_context.tenant_id,
        )

    @rpc_expose(description="List workspace snapshots")
    def workspace_log(
        self,
        workspace_path: str | None = None,
        agent_id: str | None = None,  # DEPRECATED: For backward compatibility
        limit: int = 100,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List snapshot history for workspace.

        Args:
            workspace_path: Path to registered workspace
            agent_id: DEPRECATED - Use workspace_path instead
            limit: Maximum number of snapshots to return
            context: Operation context with user, permissions, tenant info (uses default if None)

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
        # Parse context properly
        ctx = self._parse_context(context)

        # Backward compatibility: support old agent_id parameter
        if workspace_path is None and agent_id:
            import warnings

            warnings.warn(
                "agent_id parameter is deprecated. Use workspace_path parameter instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            workspace_path = f"/workspace/{agent_id}"

        if workspace_path is None:
            # Fallback to context agent_id, then default context
            fallback_agent_id = ctx.agent_id or self._default_context.agent_id
            if fallback_agent_id:
                workspace_path = f"/workspace/{fallback_agent_id}"

        if not workspace_path:
            raise ValueError("workspace_path must be provided")

        # Verify workspace is registered
        if not self._workspace_registry.get_workspace(workspace_path):
            raise ValueError(f"Workspace not registered: {workspace_path}")

        return self._workspace_manager.list_snapshots(
            workspace_path=workspace_path,
            limit=limit,
            user_id=ctx.user or self._default_context.user,  # v0.5.0: Pass user_id from context
            agent_id=ctx.agent_id or self._default_context.agent_id,
            tenant_id=ctx.tenant_id or self._default_context.tenant_id,
        )

    @rpc_expose(description="Compare workspace snapshots")
    def workspace_diff(
        self,
        snapshot_1: int,
        snapshot_2: int,
        workspace_path: str | None = None,
        agent_id: str | None = None,  # DEPRECATED: For backward compatibility
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Compare two workspace snapshots.

        Args:
            snapshot_1: First snapshot number
            snapshot_2: Second snapshot number
            workspace_path: Path to registered workspace
            agent_id: DEPRECATED - Use workspace_path instead
            context: Operation context with user, permissions, tenant info (uses default if None)

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
        # Parse context properly
        ctx = self._parse_context(context)

        # Backward compatibility: support old agent_id parameter
        if workspace_path is None and agent_id:
            import warnings

            warnings.warn(
                "agent_id parameter is deprecated. Use workspace_path parameter instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            workspace_path = f"/workspace/{agent_id}"

        if workspace_path is None:
            # Fallback to context agent_id, then default context
            fallback_agent_id = ctx.agent_id or self._default_context.agent_id
            if fallback_agent_id:
                workspace_path = f"/workspace/{fallback_agent_id}"

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
            user_id=ctx.user or self._default_context.user,  # v0.5.0: Pass user_id from context
            agent_id=ctx.agent_id or self._default_context.agent_id,
            tenant_id=ctx.tenant_id or self._default_context.tenant_id,
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
            user_id=ctx.user or self._default_context.user,  # v0.5.0: Pass user_id from context
            agent_id=ctx.agent_id or self._default_context.agent_id,
            tenant_id=ctx.tenant_id or self._default_context.tenant_id,
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
        session_id: str
        | None = None,  # v0.5.0: If provided, workspace is session-scoped (temporary)
        ttl: timedelta | None = None,  # v0.5.0: Time-to-live for auto-expiry
        context: Any | None = None,  # v0.5.0: OperationContext (passed by RPC server)
    ) -> dict[str, Any]:
        """Register a directory as a workspace.

        Args:
            path: Absolute path to workspace directory (e.g., "/my-workspace")
            name: Optional friendly name for the workspace
            description: Human-readable description
            created_by: User/agent who created it (for audit)
            tags: Tags for categorization (reserved for future use)
            metadata: Additional user-defined metadata
            session_id: If provided, workspace is session-scoped (temporary). If None, persistent. (v0.5.0)
            ttl: Time-to-live as timedelta for auto-expiry (v0.5.0)

        Returns:
            Workspace configuration dict

        Raises:
            ValueError: If path already registered as workspace

        Examples:
            >>> # Persistent workspace (traditional)
            >>> nx = NexusFS(backend)
            >>> nx.register_workspace("/my-workspace", name="main", description="My main workspace")

            >>> # v0.5.0: Temporary 8-hour notebook workspace
            >>> from datetime import timedelta
            >>> nx.register_workspace(
            ...     "/tmp/jupyter",
            ...     session_id=session.session_id,  # session_id = session-scoped
            ...     ttl=timedelta(hours=8)
            ... )
        """
        # tags parameter reserved for future use
        _ = tags

        # v0.5.0: Use provided context, or fall back to instance context
        if context is None and hasattr(self, "_operation_context"):
            context = self._operation_context

        # Create the directory if it doesn't exist
        # Workspaces must exist as directories before they can be registered
        if not self.exists(path, context=context):
            self.mkdir(path, parents=True, exist_ok=True, context=context)

        config = self._workspace_registry.register_workspace(
            path=path,
            name=name,
            description=description or "",
            created_by=created_by,
            metadata=metadata,
            context=context,  # v0.5.0
            session_id=session_id,  # v0.5.0
            ttl=ttl,  # v0.5.0
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
        session_id: str | None = None,  # v0.5.0: If provided, memory is session-scoped (temporary)
        ttl: timedelta | None = None,  # v0.5.0: Time-to-live for auto-expiry
        context: Any | None = None,  # v0.5.0: OperationContext (passed by RPC server)
    ) -> dict[str, Any]:
        """Register a directory as a memory.

        Args:
            path: Absolute path to memory directory (e.g., "/my-memory")
            name: Optional friendly name for the memory
            description: Human-readable description
            created_by: User/agent who created it (for audit)
            tags: Tags for categorization (reserved for future use)
            metadata: Additional user-defined metadata
            session_id: If provided, memory is session-scoped (temporary). If None, persistent. (v0.5.0)
            ttl: Time-to-live as timedelta for auto-expiry (v0.5.0)

        Returns:
            Memory configuration dict

        Raises:
            ValueError: If path already registered as memory

        Examples:
            >>> # Persistent memory (traditional)
            >>> nx = NexusFS(backend)
            >>> nx.register_memory("/my-memory", name="kb", description="Knowledge base")

            >>> # v0.5.0: Temporary agent memory (auto-expire after task)
            >>> from datetime import timedelta
            >>> nx.register_memory(
            ...     "/tmp/agent-context",
            ...     session_id=session.session_id,  # session_id = session-scoped
            ...     ttl=timedelta(hours=2)
            ... )
        """
        # tags parameter reserved for future use
        _ = tags

        # v0.5.0: Use provided context, or fall back to instance context
        if context is None and hasattr(self, "_operation_context"):
            context = self._operation_context

        config = self._workspace_registry.register_memory(
            path=path,
            name=name,
            description=description or "",
            created_by=created_by,
            metadata=metadata,
            context=context,  # v0.5.0
            session_id=session_id,  # v0.5.0
            ttl=ttl,  # v0.5.0
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
    def list_registered_memories(self) -> list[dict]:
        """List all registered memory paths.

        Returns:
            List of memory configuration dicts

        Example:
            >>> memories = nx.list_registered_memories()
            >>> for mem in memories:
            ...     print(f"{mem['path']}: {mem['name']}")

        Note:
            RPC: This method is exposed as "list_registered_memories".
            The RPC endpoint "list_memories" calls memory.list() for memory records.
        """
        configs = self._workspace_registry.list_memories()
        return [c.to_dict() for c in configs]

    def list_memories(self) -> list[dict]:
        """Alias for list_registered_memories() for backward compatibility."""
        return self.list_registered_memories()

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

    # ===== Agent Management (v0.5.0) =====

    def _extract_tenant_id(self, context: dict | Any | None) -> str | None:
        """Extract tenant_id from context (dict or OperationContext)."""
        if not context:
            return None
        if isinstance(context, dict):
            return context.get("tenant_id")
        return getattr(context, "tenant_id", None)

    def _extract_user_id(self, context: dict | Any | None) -> str | None:
        """Extract user_id from context (dict or OperationContext)."""
        if not context:
            return None
        if isinstance(context, dict):
            return context.get("user_id") or context.get("user")
        return getattr(context, "user_id", None) or getattr(context, "user", None)

    def _create_agent_config_data(
        self,
        agent_id: str,
        name: str,
        user_id: str,
        description: str | None,
        created_at: str | None,
        metadata: dict | None = None,
        api_key: str | None = None,
        inherit_permissions: bool | None = None,
    ) -> dict[str, Any]:
        """Create agent config.yaml data structure."""
        config_data: dict[str, Any] = {
            "agent_id": agent_id,
            "name": name,
            "user_id": user_id,
            "description": description,
            "created_at": created_at,
        }

        if metadata:
            config_data["metadata"] = metadata.copy()

        if api_key is not None:
            config_data["api_key"] = api_key

        if inherit_permissions is not None:
            config_data["inherit_permissions"] = inherit_permissions

        return config_data

    def _write_agent_config(
        self,
        config_path: str,
        config_data: dict[str, Any],
        context: dict | Any | None,
    ) -> None:
        """Write agent config.yaml file."""
        import yaml

        config_yaml = yaml.dump(config_data, default_flow_style=False, sort_keys=False)
        ctx = self._parse_context(context)
        self.write(config_path, config_yaml.encode("utf-8"), context=ctx)

    def _create_agent_directory(
        self,
        agent_id: str,
        user_id: str,
        agent_dir: str,
        config_path: str,
        config_data: dict[str, Any],
        context: dict | Any | None,
    ) -> None:
        """Create agent directory, config file, and grant ReBAC permissions."""
        import logging

        logger = logging.getLogger(__name__)

        try:
            # Parse context to OperationContext
            ctx = self._parse_context(context)

            # Create agent directory
            self.mkdir(agent_dir, parents=True, exist_ok=True, context=ctx)

            # Write config.yaml
            self._write_agent_config(config_path, config_data, context)

            # Grant ReBAC permissions
            if self._rebac_manager:
                tenant_id = self._extract_tenant_id(context) or "default"

                # Grant direct_owner to the agent itself
                try:
                    logger.debug(
                        f"register_agent: Granting direct_owner to agent {agent_id} for {agent_dir}"
                    )
                    self._rebac_manager.rebac_write(
                        subject=("agent", agent_id),
                        relation="direct_owner",
                        object=("file", agent_dir),
                        tenant_id=tenant_id,
                    )
                    logger.debug(f"register_agent: Granted direct_owner to agent {agent_id}")
                except Exception as e:
                    logger.warning(f"Failed to grant direct_owner to agent for {agent_dir}: {e}")

                # Grant user permissions to access agent's directory
                # Use direct_owner relation for full access
                try:
                    self._rebac_manager.rebac_write(
                        subject=("user", user_id),
                        relation="direct_owner",
                        object=("file", agent_dir),
                        tenant_id=tenant_id,
                    )
                except Exception as e:
                    logger.warning(f"Failed to grant owner permission to user for {agent_dir}: {e}")

        except Exception as e:
            logger.warning(f"Failed to create agent directory or config: {e}")

    def _determine_agent_key_expiration(
        self,
        user_id: str,
        session: Any,
    ) -> datetime:
        """Determine expiration date for agent API key based on owner's key."""
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import select

        from nexus.storage.models import APIKeyModel

        # Find the owner's active API key (exclude agent keys)
        stmt = (
            select(APIKeyModel)
            .where(
                APIKeyModel.user_id == user_id,
                APIKeyModel.revoked == 0,  # Active keys only
                APIKeyModel.subject_type != "agent",  # Only user keys, not agent keys
            )
            .order_by(APIKeyModel.created_at.desc())
        )  # Get most recent key

        owner_key = session.scalar(stmt)

        # Determine expiration for agent key
        if owner_key and owner_key.expires_at:
            # Use owner's key expiration as maximum
            now = datetime.now(UTC)
            owner_expires: datetime = owner_key.expires_at
            if owner_expires.tzinfo is None:
                owner_expires = owner_expires.replace(tzinfo=UTC)

            if owner_expires > now:
                return owner_expires  # Agent key expires with owner's key
            else:
                # Owner's key is expired, cannot create agent API key
                raise ValueError(
                    f"Cannot generate API key for agent: Your API key has expired on {owner_expires.isoformat()}. "
                    "Please renew your API key before creating agent API keys."
                )
        else:
            # No expiration on owner's key or no key found, use default 365 days
            return datetime.now(UTC) + timedelta(days=365)

    def _create_agent_api_key(
        self,
        agent_id: str,
        user_id: str,
        inherit_permissions: bool,
        context: dict | Any | None,
    ) -> str:
        """Create API key for agent and return the raw key."""
        from nexus.server.auth.database_key import DatabaseAPIKeyAuth

        tenant_id = self._extract_tenant_id(context)
        session = self.metadata.SessionLocal()

        try:
            # Determine expiration based on owner's key
            expires_at = self._determine_agent_key_expiration(user_id, session)

            # Create the API key
            _key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id=user_id,
                name=agent_id,  # Use agent_id format: <user_id>,<agent_name>
                subject_type="agent",
                subject_id=agent_id,
                tenant_id=tenant_id,
                expires_at=expires_at,
                inherit_permissions=inherit_permissions,
            )
            session.commit()
            return raw_key
        finally:
            session.close()

    @rpc_expose(description="Register an AI agent")
    def register_agent(
        self,
        agent_id: str,
        name: str,
        description: str | None = None,
        generate_api_key: bool = False,
        inherit_permissions: bool = False,  # v0.5.1: Default False (zero permissions)
        metadata: dict | None = None,  # v0.5.1: Optional metadata (platform, endpoint_url, etc.)
        context: dict | None = None,
    ) -> dict:
        """Register an AI agent (v0.5.0).

        Agents are persistent identities owned by users. They do NOT have session_id
        or expiry - they live forever until explicitly deleted.

        v0.5.1: Added inherit_permissions for controlling agent permission inheritance.

        Args:
            agent_id: Unique agent identifier
            name: Human-readable name
            description: Optional description
            generate_api_key: If True, create API key for agent (not recommended)
            inherit_permissions: Whether agent inherits owner's permissions (v0.5.1)
                                Default False (zero permissions, principle of least privilege)
            metadata: Optional metadata dict (platform, endpoint_url, agent_id, etc.)
                     Stored in agent's config.yaml for agent configuration
            context: Operation context (user_id extracted from here)

        Returns:
            Agent info dict with agent_id, user_id, name, etc.

        Example:
            >>> # Recommended: No API key (uses user's auth + X-Agent-ID)
            >>> agent = nx.register_agent("data_analyst", "Data Analyst")
            >>> # Agent uses owner's credentials + X-Agent-ID header
            >>>
            >>> # With API key but no inheritance (zero permissions)
            >>> agent = nx.register_agent("secure_agent", "Secure Agent",
            ...                          generate_api_key=True, inherit_permissions=False)
            >>> # Agent starts with 0 permissions, needs explicit ReBAC grants
            >>>
            >>> # With API key and inheritance (full permissions)
            >>> agent = nx.register_agent("trusted_agent", "Trusted Agent",
            ...                          generate_api_key=True, inherit_permissions=True)
            >>> # Agent inherits all owner's permissions
        """
        import logging

        from nexus.core.agents import register_agent

        logger = logging.getLogger(__name__)

        # Extract user_id and tenant_id from context
        user_id = self._extract_user_id(context)
        if not user_id:
            raise ValueError("user_id required in context to register agent")

        tenant_id = self._extract_tenant_id(context) or "default"

        # Ensure EntityRegistry is initialized
        if not self._entity_registry:
            from nexus.core.entity_registry import EntityRegistry

            self._entity_registry = EntityRegistry(self.metadata.SessionLocal)

        # Register agent entity (always without API key first)
        agent = register_agent(
            user_id=user_id,
            agent_id=agent_id,
            name=name,
            tenant_id=tenant_id,
            metadata={"description": description} if description else None,
            entity_registry=self._entity_registry,
        )

        # Create agent directory structure
        # Extract agent name from agent_id (format: user_id,agent_name)
        # Use new namespace convention: /tenant:<tenant_id>/user:<user_id>/agent/<agent_id>
        agent_name_part = agent_id.split(",", 1)[1] if "," in agent_id else agent_id
        agent_dir = f"/tenant:{tenant_id}/user:{user_id}/agent/{agent_name_part}"
        config_path = f"{agent_dir}/config.yaml"

        # Create initial config data
        config_data = self._create_agent_config_data(
            agent_id=agent_id,
            name=name,
            user_id=user_id,
            description=description,
            created_at=agent.get("created_at"),
            metadata=metadata,
        )

        # Create directory, config file, and grant ReBAC permissions
        self._create_agent_directory(
            agent_id=agent_id,
            user_id=user_id,
            agent_dir=agent_dir,
            config_path=config_path,
            config_data=config_data,
            context=context,
        )
        agent["config_path"] = config_path

        # Optionally generate API key
        if generate_api_key:
            try:
                raw_key = self._create_agent_api_key(
                    agent_id=agent_id,
                    user_id=user_id,
                    inherit_permissions=inherit_permissions,
                    context=context,
                )
                agent["api_key"] = raw_key
                agent["has_api_key"] = True

                # Update config.yaml with API key information
                try:
                    updated_config_data = self._create_agent_config_data(
                        agent_id=agent_id,
                        name=name,
                        user_id=user_id,
                        description=description,
                        created_at=agent.get("created_at"),
                        metadata=metadata,
                        api_key=raw_key,
                        inherit_permissions=inherit_permissions,
                    )
                    self._write_agent_config(config_path, updated_config_data, context)
                except Exception as e:
                    logger.warning(f"Failed to update config with API key: {e}")
            except Exception as e:
                logger.error(f"Failed to create API key for agent: {e}")
                raise
        else:
            agent["has_api_key"] = False

        return agent

    @rpc_expose(description="List all registered agents")
    def list_agents(self, _context: dict | None = None) -> list[dict]:
        """List all registered agents (v0.5.0).

        Returns:
            List of agent info dicts

        Example:
            >>> agents = nx.list_agents()
            >>> for agent in agents:
            ...     print(f"{agent['agent_id']}: {agent['name']}")
        """
        if not self._entity_registry:
            from nexus.core.entity_registry import EntityRegistry

            self._entity_registry = EntityRegistry(self.metadata.SessionLocal)

        entities = self._entity_registry.get_entities_by_type("agent")
        result = []

        # Query API keys for all agents in one go for efficiency
        from sqlalchemy import select

        from nexus.storage.models import APIKeyModel

        session = self.metadata.SessionLocal()
        try:
            # Get all agent API keys
            agent_keys_stmt = select(APIKeyModel).where(
                APIKeyModel.subject_type == "agent",
                APIKeyModel.revoked == 0,  # Only active keys
            )
            agent_keys = {key.subject_id: key for key in session.scalars(agent_keys_stmt).all()}
        finally:
            session.close()

        for e in entities:
            import json

            # Parse metadata if available
            metadata = {}
            if e.entity_metadata:
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    metadata = json.loads(e.entity_metadata)

            agent_info = {
                "agent_id": e.entity_id,
                "user_id": e.parent_id,
                "name": metadata.get(
                    "name", e.entity_id
                ),  # Use display name or fallback to entity_id
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }

            # Add description if available
            if "description" in metadata:
                agent_info["description"] = metadata["description"]

            # Check if agent has an API key
            agent_key = agent_keys.get(e.entity_id)
            if agent_key:
                agent_info["has_api_key"] = True
                agent_info["inherit_permissions"] = bool(agent_key.inherit_permissions)
            else:
                agent_info["has_api_key"] = False
                # If no API key, try to read from config.yaml or use default True
                # Agents without API keys typically inherit permissions by default
                inherit_perms = None
                try:
                    # Extract user_id and agent_name from agent_id (format: user_id,agent_name)
                    if "," in e.entity_id:
                        user_id, agent_name = e.entity_id.split(",", 1)
                        # Try to read from config.yaml (use default tenant for now)
                        config_path = (
                            f"/tenant:default/user:{user_id}/agent/{agent_name}/config.yaml"
                        )
                        try:
                            config_content = self.read(
                                config_path, context=self._parse_context(_context)
                            )
                            import yaml

                            if isinstance(config_content, bytes):
                                config_data = yaml.safe_load(config_content.decode("utf-8"))
                                inherit_perms = config_data.get("inherit_permissions")
                        except Exception:
                            pass  # If can't read config, will use default
                except Exception:
                    pass

                # Default to True if not found (agents without API keys inherit by default)
                agent_info["inherit_permissions"] = (
                    bool(inherit_perms) if inherit_perms is not None else True
                )

            result.append(agent_info)

        return result

    @rpc_expose(description="Get agent information")
    def get_agent(self, agent_id: str, _context: dict | None = None) -> dict | None:
        """Get information about a registered agent (v0.5.0).

        Args:
            agent_id: Agent identifier
            context: Operation context (optional)

        Returns:
            Agent info dict with all fields (same as list_agents) plus api_key if available, or None if not found

        Example:
            >>> agent = nx.get_agent("data_analyst")
            >>> if agent:
            ...     print(f"Owner: {agent['user_id']}")
            ...     if agent.get('api_key'):
            ...         print(f"Has API key: {agent['api_key'][:10]}...")
        """
        if not self._entity_registry:
            from nexus.core.entity_registry import EntityRegistry

            self._entity_registry = EntityRegistry(self.metadata.SessionLocal)

        entity = self._entity_registry.get_entity("agent", agent_id)
        if not entity:
            return None

        import json

        # Parse metadata if available
        metadata = {}
        if entity.entity_metadata:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                metadata = json.loads(entity.entity_metadata)

        agent_info = {
            "agent_id": entity.entity_id,
            "user_id": entity.parent_id,
            "name": metadata.get(
                "name", entity.entity_id
            ),  # Use display name or fallback to entity_id
            "created_at": entity.created_at.isoformat() if entity.created_at else None,
        }

        # Add description if available
        if "description" in metadata:
            agent_info["description"] = metadata["description"]

        # Check if agent has an API key (same logic as list_agents)
        from sqlalchemy import select

        from nexus.storage.models import APIKeyModel

        session = self.metadata.SessionLocal()
        try:
            # Check if agent has an API key in database
            agent_key_stmt = select(APIKeyModel).where(
                APIKeyModel.subject_type == "agent",
                APIKeyModel.subject_id == agent_id,
                APIKeyModel.revoked == 0,  # Only active keys
            )
            agent_key = session.scalar(agent_key_stmt)

            if agent_key:
                agent_info["has_api_key"] = True
                agent_info["inherit_permissions"] = bool(agent_key.inherit_permissions)

                # Read config.yaml file to get API key and other config fields
                try:
                    # Extract user_id and agent_name from agent_id (format: user_id,agent_name)
                    if "," in entity.entity_id:
                        user_id, agent_name = entity.entity_id.split(",", 1)
                        # Get tenant_id from context
                        ctx = self._parse_context(_context)
                        tenant_id = self._extract_tenant_id(_context) or "default"
                        config_path = (
                            f"/tenant:{tenant_id}/user:{user_id}/agent/{agent_name}/config.yaml"
                        )
                        try:
                            config_content = self.read(config_path, context=ctx)
                            import yaml

                            if isinstance(config_content, bytes):
                                config_data = yaml.safe_load(config_content.decode("utf-8"))
                                # Return API key from config if available
                                if config_data.get("api_key"):
                                    agent_info["api_key"] = config_data["api_key"]

                                # Check metadata first, then top-level for config fields
                                # Config fields can be in metadata (from provision script) or at top-level
                                metadata = config_data.get("metadata", {})
                                if isinstance(metadata, dict):
                                    # Platform and endpoint_url are often in metadata
                                    if metadata.get("platform"):
                                        agent_info["platform"] = metadata["platform"]
                                    if metadata.get("endpoint_url"):
                                        agent_info["endpoint_url"] = metadata["endpoint_url"]
                                    # agent_id in metadata is the LangGraph graph/assistant ID (e.g., "agent")
                                    if metadata.get("agent_id"):
                                        agent_info["config_agent_id"] = metadata["agent_id"]

                                # Fall back to top-level if not in metadata
                                if not agent_info.get("platform") and config_data.get("platform"):
                                    agent_info["platform"] = config_data["platform"]
                                if not agent_info.get("endpoint_url") and config_data.get(
                                    "endpoint_url"
                                ):
                                    agent_info["endpoint_url"] = config_data["endpoint_url"]
                                # Only use top-level agent_id if config_agent_id not set and it's different from full agent_id
                                if (
                                    not agent_info.get("config_agent_id")
                                    and config_data.get("agent_id")
                                    and config_data["agent_id"] != entity.entity_id
                                ):
                                    # Only use if it's actually a LangGraph graph ID, not the full agent_id
                                    agent_info["config_agent_id"] = config_data["agent_id"]

                            if config_data.get("system_prompt"):
                                agent_info["system_prompt"] = config_data["system_prompt"]
                            if config_data.get("tools"):
                                agent_info["tools"] = config_data["tools"]
                        except Exception:
                            # If can't read config, that's okay - agent might not have config file yet
                            pass
                except Exception:
                    pass
            else:
                agent_info["has_api_key"] = False
                # If no API key, try to read from config.yaml or use default True
                inherit_perms = None
                try:
                    # Extract user_id and agent_name from agent_id (format: user_id,agent_name)
                    if "," in entity.entity_id:
                        user_id, agent_name = entity.entity_id.split(",", 1)
                        ctx = self._parse_context(_context)
                        tenant_id = self._extract_tenant_id(_context) or "default"
                        config_path = (
                            f"/tenant:{tenant_id}/user:{user_id}/agent/{agent_name}/config.yaml"
                        )
                        try:
                            config_content = self.read(config_path, context=ctx)
                            import yaml

                            if isinstance(config_content, bytes):
                                config_data = yaml.safe_load(config_content.decode("utf-8"))
                                inherit_perms = config_data.get("inherit_permissions")

                                # Check metadata first, then top-level for config fields
                                metadata = config_data.get("metadata", {})
                                if isinstance(metadata, dict):
                                    if metadata.get("platform"):
                                        agent_info["platform"] = metadata["platform"]
                                    if metadata.get("endpoint_url"):
                                        agent_info["endpoint_url"] = metadata["endpoint_url"]
                                    # agent_id in metadata is the LangGraph graph/assistant ID
                                    if metadata.get("agent_id"):
                                        agent_info["config_agent_id"] = metadata["agent_id"]

                                # Fall back to top-level if not in metadata
                                if not agent_info.get("platform") and config_data.get("platform"):
                                    agent_info["platform"] = config_data["platform"]
                                if not agent_info.get("endpoint_url") and config_data.get(
                                    "endpoint_url"
                                ):
                                    agent_info["endpoint_url"] = config_data["endpoint_url"]
                                if (
                                    not agent_info.get("config_agent_id")
                                    and config_data.get("agent_id")
                                    and config_data["agent_id"] != entity.entity_id
                                ):
                                    agent_info["config_agent_id"] = config_data["agent_id"]

                                if config_data.get("system_prompt"):
                                    agent_info["system_prompt"] = config_data["system_prompt"]
                                if config_data.get("tools"):
                                    agent_info["tools"] = config_data["tools"]
                        except Exception:
                            pass  # If can't read config, will use default
                except Exception:
                    pass

                # Default to True if not found (agents without API keys inherit by default)
                agent_info["inherit_permissions"] = (
                    bool(inherit_perms) if inherit_perms is not None else True
                )
        finally:
            session.close()

        return agent_info

    @rpc_expose(description="Delete an agent")
    def delete_agent(self, agent_id: str, _context: dict | None = None) -> bool:
        """Delete a registered agent (v0.5.0).

        Args:
            agent_id: Agent identifier
            context: Operation context (optional)

        Returns:
            True if deleted, False if not found

        Example:
            >>> deleted = nx.delete_agent("data_analyst")
            >>> if deleted:
            ...     print("Agent deleted")
        """
        if not self._entity_registry:
            from nexus.core.entity_registry import EntityRegistry

            self._entity_registry = EntityRegistry(self.metadata.SessionLocal)

        # Get agent info before deletion to extract user_id and tenant_id
        try:
            # Agent ID format: user_id,agent_name
            if "," in agent_id:
                user_id, agent_name_part = agent_id.split(",", 1)
                # Get tenant_id from context or use default
                tenant_id = self._extract_tenant_id(_context) or "default"
                # Use new namespace convention: /tenant:<tenant_id>/user:<user_id>/agent/<agent_id>
                agent_dir = f"/tenant:{tenant_id}/user:{user_id}/agent/{agent_name_part}"

                # Delete agent directory and config
                try:
                    ctx = self._parse_context(_context)
                    if self.exists(agent_dir, context=ctx):
                        # Use admin override for cleanup during agent deletion
                        self.rmdir(agent_dir, recursive=True, context=ctx, is_admin=True)
                except Exception as e:
                    import logging

                    logger = logging.getLogger(__name__)
                    logger.warning(f"Failed to delete agent directory {agent_dir}: {e}")

                # Delete ALL API keys associated with this agent
                session = self.metadata.SessionLocal()
                try:
                    from sqlalchemy import update

                    from nexus.storage.models import APIKeyModel

                    # Revoke (soft delete) all API keys for this agent
                    stmt = (
                        update(APIKeyModel)
                        .where(
                            APIKeyModel.subject_type == "agent",
                            APIKeyModel.subject_id == agent_id,
                            APIKeyModel.revoked == 0,  # Only active keys
                        )
                        .values(revoked=1)  # Mark as revoked
                    )
                    result = session.execute(stmt)
                    session.commit()

                    # Get rowcount from result (SQLAlchemy 2.0+)
                    rowcount = result.rowcount if hasattr(result, "rowcount") else 0
                    if rowcount > 0:
                        import logging

                        logger = logging.getLogger(__name__)
                        logger.info(f"Revoked {rowcount} API key(s) for agent {agent_id}")
                except Exception as e:
                    import logging

                    logger = logging.getLogger(__name__)
                    logger.warning(f"Failed to revoke API keys for agent {agent_id}: {e}")
                    session.rollback()
                finally:
                    session.close()

                # Delete ALL ReBAC permissions for this agent
                if self._rebac_manager:
                    import logging

                    logger = logging.getLogger(__name__)

                    # List all ReBAC tuples for this agent using nexus_fs method
                    try:
                        tuples = self.rebac_list_tuples(
                            subject=("agent", agent_id),
                        )

                        # Delete each tuple by tuple_id
                        deleted_count = 0
                        for tuple_data in tuples:
                            try:
                                tuple_id = tuple_data.get("tuple_id")
                                if tuple_id:
                                    self.rebac_delete(tuple_id=tuple_id)
                                    deleted_count += 1
                            except Exception as e:
                                logger.warning(f"Failed to delete ReBAC tuple: {e}")

                        if deleted_count > 0:
                            logger.info(
                                f"Deleted {deleted_count} ReBAC tuple(s) for agent {agent_id}"
                            )
                    except Exception as e:
                        logger.warning(f"Failed to delete ReBAC tuples for agent {agent_id}: {e}")

                    # Revoke user's permissions on agent directory
                    # List tuples for user on agent directory and delete them
                    try:
                        user_tuples = self.rebac_list_tuples(
                            subject=("user", user_id),
                            object=("file", agent_dir),
                        )
                        for tuple_data in user_tuples:
                            tuple_id = tuple_data.get("tuple_id")
                            if tuple_id:
                                try:
                                    self.rebac_delete(tuple_id=tuple_id)
                                except Exception as e:
                                    logger.warning(f"Failed to delete user permission tuple: {e}")
                    except Exception as e:
                        logger.warning(
                            f"Failed to revoke user permissions for agent directory: {e}"
                        )
        except Exception as e:
            import logging

            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to cleanup agent resources: {e}")

        return self._entity_registry.delete_entity("agent", agent_id)

    # ===== ACE (Agentic Context Engineering) Integration (v0.5.0) =====

    @rpc_expose(description="Start a new execution trajectory")
    def ace_start_trajectory(
        self,
        task_description: str,
        task_type: str | None = None,
        context: dict | None = None,
    ) -> dict:
        """Start tracking a new execution trajectory for ACE learning.

        Args:
            task_description: Description of the task being executed
            task_type: Optional task type ('api_call', 'data_processing', etc.)
            context: Operation context

        Returns:
            Dict with trajectory_id

        Example:
            >>> result = nx.ace_start_trajectory("Deploy caching strategy")
            >>> traj_id = result['trajectory_id']
        """
        memory_api = self._get_memory_api(context)
        trajectory_id = memory_api.start_trajectory(task_description, task_type)
        return {"trajectory_id": trajectory_id}

    @rpc_expose(description="Log a step in a trajectory")
    def ace_log_trajectory_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        result: Any = None,
        context: dict | None = None,
    ) -> dict:
        """Log a step in an execution trajectory.

        Args:
            trajectory_id: Trajectory ID
            step_type: Type of step ('action', 'decision', 'observation')
            description: Step description
            result: Optional result data
            context: Operation context

        Returns:
            Success status

        Example:
            >>> nx.ace_log_trajectory_step(
            ...     traj_id,
            ...     "action",
            ...     "Configured cache with 5min TTL"
            ... )
        """
        memory_api = self._get_memory_api(context)
        memory_api.log_trajectory_step(trajectory_id, step_type, description, result)
        return {"success": True}

    @rpc_expose(description="Complete a trajectory")
    def ace_complete_trajectory(
        self,
        trajectory_id: str,
        status: str,
        success_score: float | None = None,
        error_message: str | None = None,
        context: dict | None = None,
    ) -> dict:
        """Complete a trajectory with outcome.

        Args:
            trajectory_id: Trajectory ID
            status: Status ('success', 'failure', 'partial')
            success_score: Success score (0.0-1.0)
            error_message: Error message if failed
            context: Operation context

        Returns:
            Dict with trajectory_id

        Example:
            >>> nx.ace_complete_trajectory(traj_id, "success", success_score=0.95)
        """
        memory_api = self._get_memory_api(context)
        completed_id = memory_api.complete_trajectory(
            trajectory_id, status, success_score, error_message
        )
        return {"trajectory_id": completed_id}

    @rpc_expose(description="Add feedback to a trajectory")
    def ace_add_feedback(
        self,
        trajectory_id: str,
        feedback_type: str,
        score: float | None = None,
        source: str | None = None,
        message: str | None = None,
        metrics: dict | None = None,
        context: dict | None = None,
    ) -> dict:
        """Add feedback to a completed trajectory.

        Args:
            trajectory_id: Trajectory ID
            feedback_type: Type of feedback
            score: Revised score (0.0-1.0)
            source: Feedback source
            message: Human-readable message
            metrics: Additional metrics
            context: Operation context

        Returns:
            Dict with feedback_id

        Example:
            >>> nx.ace_add_feedback(
            ...     traj_id,
            ...     "monitoring_alert",
            ...     score=0.3,
            ...     message="Error rate spiked"
            ... )
        """
        memory_api = self._get_memory_api(context)
        feedback_id = memory_api.add_feedback(
            trajectory_id, feedback_type, score, source, message, metrics
        )
        return {"feedback_id": feedback_id}

    @rpc_expose(description="Get feedback for a trajectory")
    def ace_get_trajectory_feedback(
        self, trajectory_id: str, context: dict | None = None
    ) -> list[dict[str, Any]]:
        """Get all feedback for a trajectory.

        Args:
            trajectory_id: Trajectory ID
            context: Operation context

        Returns:
            List of feedback dicts
        """
        memory_api = self._get_memory_api(context)
        return memory_api.get_trajectory_feedback(trajectory_id)

    @rpc_expose(description="Get effective score for a trajectory")
    def ace_get_effective_score(
        self,
        trajectory_id: str,
        strategy: Literal["latest", "average", "weighted"] = "latest",
        context: dict | None = None,
    ) -> dict:
        """Get effective score for a trajectory.

        Args:
            trajectory_id: Trajectory ID
            strategy: Scoring strategy ('latest', 'average', 'weighted')
            context: Operation context

        Returns:
            Dict with effective_score
        """
        memory_api = self._get_memory_api(context)
        score = memory_api.get_effective_score(trajectory_id, strategy)
        return {"effective_score": score}

    @rpc_expose(description="Mark trajectory for re-learning")
    def ace_mark_for_relearning(
        self,
        trajectory_id: str,
        reason: str,
        priority: int = 5,
        context: dict | None = None,
    ) -> dict:
        """Mark trajectory for re-learning.

        Args:
            trajectory_id: Trajectory ID
            reason: Reason for re-learning
            priority: Priority (1-10)
            context: Operation context

        Returns:
            Success status
        """
        memory_api = self._get_memory_api(context)
        memory_api.mark_for_relearning(trajectory_id, reason, priority)
        return {"success": True}

    @rpc_expose(description="Query trajectories")
    def ace_query_trajectories(
        self,
        task_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
        context: dict | None = None,
    ) -> list[dict]:
        """Query execution trajectories.

        Args:
            task_type: Filter by task type
            status: Filter by status
            limit: Maximum results
            context: Operation context

        Returns:
            List of trajectory summaries
        """
        from nexus.core.ace.trajectory import TrajectoryManager

        session = self.metadata.SessionLocal()
        try:
            ctx = self._parse_context(context)
            traj_mgr = TrajectoryManager(
                session,
                self.backend,
                ctx.user or "system",
                ctx.agent_id or self._default_context.agent_id,
                ctx.tenant_id or self._default_context.tenant_id,
            )
            return traj_mgr.query_trajectories(
                agent_id=ctx.agent_id or self._default_context.agent_id,
                task_type=task_type,
                status=status,
                limit=limit,
            )
        finally:
            session.close()

    @rpc_expose(description="Create a new playbook")
    def ace_create_playbook(
        self,
        name: str,
        description: str | None = None,
        scope: str = "agent",
        context: dict | None = None,
    ) -> dict:
        """Create a new playbook.

        Args:
            name: Playbook name
            description: Optional description
            scope: Scope level ('agent', 'user', 'tenant', 'global')
            context: Operation context

        Returns:
            Dict with playbook_id
        """
        from nexus.core.ace.playbook import PlaybookManager

        session = self.metadata.SessionLocal()
        try:
            ctx = self._parse_context(context)
            playbook_mgr = PlaybookManager(
                session,
                self.backend,
                ctx.user or "system",
                ctx.agent_id or self._default_context.agent_id,
                ctx.tenant_id or self._default_context.tenant_id,
            )
            playbook_id = playbook_mgr.create_playbook(name, description, scope)  # type: ignore
            return {"playbook_id": playbook_id}
        finally:
            session.close()

    @rpc_expose(description="Get playbook details")
    def ace_get_playbook(self, playbook_id: str, context: dict | None = None) -> dict | None:
        """Get playbook details.

        Args:
            playbook_id: Playbook ID
            context: Operation context

        Returns:
            Playbook dict or None
        """
        from nexus.core.ace.playbook import PlaybookManager

        session = self.metadata.SessionLocal()
        try:
            ctx = self._parse_context(context)
            playbook_mgr = PlaybookManager(
                session,
                self.backend,
                ctx.user or "system",
                ctx.agent_id or self._default_context.agent_id,
                ctx.tenant_id or self._default_context.tenant_id,
            )
            return playbook_mgr.get_playbook(playbook_id)
        finally:
            session.close()

    @rpc_expose(description="Query playbooks")
    def ace_query_playbooks(
        self,
        scope: str | None = None,
        limit: int = 50,
        context: dict | None = None,
    ) -> list[dict]:
        """Query playbooks.

        Args:
            scope: Filter by scope
            limit: Maximum results
            context: Operation context

        Returns:
            List of playbook summaries
        """
        from nexus.core.ace.playbook import PlaybookManager

        session = self.metadata.SessionLocal()
        try:
            ctx = self._parse_context(context)
            playbook_mgr = PlaybookManager(
                session,
                self.backend,
                ctx.user or "system",
                ctx.agent_id or self._default_context.agent_id,
                ctx.tenant_id or self._default_context.tenant_id,
            )
            return playbook_mgr.query_playbooks(
                agent_id=ctx.agent_id or self._default_context.agent_id,
                scope=scope,
                limit=limit,
            )
        finally:
            session.close()

    # ========================================================================
    # Sandbox Management (Issue #372)
    # ========================================================================

    def _ensure_sandbox_manager(self) -> None:
        """Ensure sandbox manager is initialized (lazy initialization)."""
        if not hasattr(self, "_sandbox_manager") or self._sandbox_manager is None:
            import os

            from nexus.core.sandbox_manager import SandboxManager

            # Initialize sandbox manager with E2B credentials and config for Docker provider
            session = self.metadata.SessionLocal()
            # Pass config if available (needed for Docker provider initialization)
            config = getattr(self, "_config", None)
            self._sandbox_manager = SandboxManager(
                db_session=session,
                e2b_api_key=os.getenv("E2B_API_KEY"),
                e2b_team_id=os.getenv("E2B_TEAM_ID"),
                e2b_template_id=os.getenv("E2B_TEMPLATE_ID"),
                config=config,  # Pass config for Docker provider
            )

    @staticmethod
    def _get_event_loop() -> asyncio.AbstractEventLoop:
        """Get or create event loop (thread-safe for ThreadingHTTPServer).

        This handles the case where async code needs to run in a thread that
        doesn't have an event loop (e.g., worker threads in ThreadingHTTPServer).
        """
        try:
            # Try to get existing event loop for this thread
            loop = asyncio.get_event_loop()
            # Check if it's closed and create new one if needed
            if loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
        except RuntimeError:
            # No event loop in this thread, create one
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop

    @staticmethod
    def _run_async(coro: Any) -> Any:
        """Run async coroutine safely, handling both running and non-running event loops.

        This method handles the case where we're already in a running event loop
        (e.g., from background tasks) and need to run async code.

        Args:
            coro: Coroutine to run

        Returns:
            Result of the coroutine
        """
        try:
            # Check if we're already in a running event loop
            asyncio.get_running_loop()
            # We're in a running loop - can't use run_until_complete
            # Run in a thread pool to avoid "loop is already running" error
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(asyncio.run, coro)
                return future.result()
        except RuntimeError:
            # No running loop - safe to use run_until_complete
            loop = NexusFS._get_event_loop()
            return loop.run_until_complete(coro)

    @rpc_expose(description="Create a new sandbox")
    async def sandbox_create(  # type: ignore[override]
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        context: dict | None = None,
    ) -> dict:
        """Create a new code execution sandbox.

        Args:
            name: User-friendly sandbox name (unique per user)
            ttl_minutes: Idle timeout in minutes (default: 10)
            provider: Sandbox provider ("docker", "e2b", etc.). If None, auto-selects based on environment.
            template_id: Provider template ID (optional)
            context: Operation context with user/agent/tenant info

        Returns:
            Sandbox metadata dict with sandbox_id, name, status, etc.
        """
        ctx = self._parse_context(context)

        # Ensure sandbox manager is initialized
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        # Create sandbox (provider auto-selection happens in sandbox_manager)
        result: dict[Any, Any] = await self._sandbox_manager.create_sandbox(
            name=name,
            user_id=ctx.user or "system",
            tenant_id=ctx.tenant_id or self._default_context.tenant_id or "default",
            agent_id=ctx.agent_id,
            ttl_minutes=ttl_minutes,
            provider=provider,
            template_id=template_id,
        )
        return result

    @rpc_expose(description="Run code in sandbox")
    async def sandbox_run(  # type: ignore[override]
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        context: dict | None = None,
    ) -> dict:
        """Run code in a sandbox.

        Args:
            sandbox_id: Sandbox ID
            language: Programming language ("python", "javascript", "bash")
            code: Code to execute
            timeout: Execution timeout in seconds (default: 300)
            nexus_url: Nexus server URL (auto-injected as env var if provided)
            nexus_api_key: Nexus API key (auto-injected as env var if provided)
            context: Operation context (used to get api_key if nexus_api_key not provided)

        Returns:
            Dict with stdout, stderr, exit_code, execution_time
        """
        # Ensure sandbox manager is initialized
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        # Get Nexus credentials from context if not provided
        if not nexus_api_key and context:
            ctx = self._parse_context(context)
            nexus_api_key = getattr(ctx, "api_key", None)

        # Auto-detect nexus_url if not provided
        if not nexus_url:
            import os

            nexus_url = os.getenv("NEXUS_SERVER_URL") or os.getenv("NEXUS_URL")

        # Inject Nexus credentials as environment variables in the code
        if nexus_url or nexus_api_key:
            env_prefix = ""
            if language == "bash":
                if nexus_url:
                    env_prefix += f'export NEXUS_URL="{nexus_url}"\n'
                if nexus_api_key:
                    env_prefix += f'export NEXUS_API_KEY="{nexus_api_key}"\n'
                code = env_prefix + code
            elif language == "python":
                env_lines = ["import os"]
                if nexus_url:
                    env_lines.append(f'os.environ["NEXUS_URL"] = "{nexus_url}"')
                if nexus_api_key:
                    env_lines.append(f'os.environ["NEXUS_API_KEY"] = "{nexus_api_key}"')
                env_prefix = "\n".join(env_lines) + "\n"
                code = env_prefix + code
            elif language in ("javascript", "js"):
                env_lines = []
                if nexus_url:
                    env_lines.append(f'process.env.NEXUS_URL = "{nexus_url}";')
                if nexus_api_key:
                    env_lines.append(f'process.env.NEXUS_API_KEY = "{nexus_api_key}";')
                env_prefix = "\n".join(env_lines) + "\n"
                code = env_prefix + code

        result: dict[Any, Any] = await self._sandbox_manager.run_code(
            sandbox_id, language, code, timeout
        )
        return result

    @rpc_expose(description="Pause sandbox")
    async def sandbox_pause(self, sandbox_id: str, context: dict | None = None) -> dict:  # type: ignore[override]  # noqa: ARG002
        """Pause sandbox to save costs.

        Args:
            sandbox_id: Sandbox ID
            context: Operation context

        Returns:
            Updated sandbox metadata
        """
        # Ensure sandbox manager is initialized
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        result: dict[Any, Any] = await self._sandbox_manager.pause_sandbox(sandbox_id)
        return result

    @rpc_expose(description="Resume paused sandbox")
    async def sandbox_resume(self, sandbox_id: str, context: dict | None = None) -> dict:  # type: ignore[override]  # noqa: ARG002
        """Resume a paused sandbox.

        Args:
            sandbox_id: Sandbox ID
            context: Operation context

        Returns:
            Updated sandbox metadata
        """
        # Ensure sandbox manager is initialized
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        result: dict[Any, Any] = await self._sandbox_manager.resume_sandbox(sandbox_id)
        return result

    @rpc_expose(description="Stop and destroy sandbox")
    async def sandbox_stop(self, sandbox_id: str, context: dict | None = None) -> dict:  # type: ignore[override]  # noqa: ARG002
        """Stop and destroy sandbox.

        Args:
            sandbox_id: Sandbox ID
            context: Operation context

        Returns:
            Updated sandbox metadata
        """
        # Ensure sandbox manager is initialized
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        result: dict[Any, Any] = await self._sandbox_manager.stop_sandbox(sandbox_id)
        return result

    @rpc_expose(description="List sandboxes")
    async def sandbox_list(  # type: ignore[override]
        self,
        context: dict | None = None,
        verify_status: bool = False,
        user_id: str | None = None,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> dict:
        """List user's sandboxes.

        Args:
            context: Operation context
            verify_status: If True, verify status with provider (slower but accurate)
            user_id: Filter by user_id (admin only)
            tenant_id: Filter by tenant_id (admin only)
            agent_id: Filter by agent_id
            status: Filter by status (e.g., 'active', 'stopped', 'paused')

        Returns:
            Dict with list of sandboxes
        """
        # Ensure sandbox manager is initialized
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        ctx = self._parse_context(context)

        # Determine filter values
        # If explicit filter parameters are provided and user is admin, use them
        # Otherwise filter by authenticated user
        filter_user_id = user_id if (user_id is not None and ctx.is_admin) else ctx.user
        filter_tenant_id = tenant_id if (tenant_id is not None and ctx.is_admin) else ctx.tenant_id
        filter_agent_id = agent_id if agent_id is not None else ctx.agent_id

        sandboxes = await self._sandbox_manager.list_sandboxes(
            user_id=filter_user_id,
            tenant_id=filter_tenant_id,
            agent_id=filter_agent_id,
            status=status,
            verify_status=verify_status,
        )
        return {"sandboxes": sandboxes}

    @rpc_expose(description="Get sandbox status")
    async def sandbox_status(self, sandbox_id: str, context: dict | None = None) -> dict:  # type: ignore[override]  # noqa: ARG002
        """Get sandbox status and metadata.

        Args:
            sandbox_id: Sandbox ID
            context: Operation context

        Returns:
            Sandbox metadata dict
        """
        # Ensure sandbox manager is initialized
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        result: dict[Any, Any] = await self._sandbox_manager.get_sandbox_status(sandbox_id)
        return result

    @rpc_expose(description="Get or create sandbox")
    async def sandbox_get_or_create(  # type: ignore[override]
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        verify_status: bool = True,
        context: dict | None = None,
    ) -> dict:
        """Get existing active sandbox or create a new one.

        This handles the common pattern where you want to reuse an existing
        sandbox if it exists and is still running, or create a new one if not.
        Perfect for agent workflows where each user+agent pair should have
        one persistent sandbox.

        Args:
            name: Sandbox name (e.g., "user_id,agent_id")
            ttl_minutes: Idle timeout in minutes (default: 10)
            provider: Sandbox provider ("docker", "e2b", etc.)
            template_id: Provider template ID (optional)
            verify_status: If True, verify with provider that sandbox is running (default: True)
            context: Operation context with user/agent/tenant info

        Returns:
            Sandbox metadata dict (either existing or newly created)

        Example:
            # Agent workflow: always get valid sandbox for user+agent
            sandbox = nx.sandbox_get_or_create(
                name=f"{user_id},{agent_id}",
                context={"user": user_id, "agent_id": agent_id}
            )
            sandbox_id = sandbox["sandbox_id"]  # Always valid!
        """
        ctx = self._parse_context(context)

        # Ensure sandbox manager is initialized
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        result: dict[Any, Any] = await self._sandbox_manager.get_or_create_sandbox(
            name=name,
            user_id=ctx.user or "system",
            tenant_id=ctx.tenant_id or self._default_context.tenant_id or "default",
            agent_id=ctx.agent_id,
            ttl_minutes=ttl_minutes,
            provider=provider,
            template_id=template_id,
            verify_status=verify_status,
        )
        return result

    @rpc_expose(description="Connect to user-managed sandbox")
    async def sandbox_connect(  # type: ignore[override]
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        mount_path: str = "/mnt/nexus",
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        agent_id: str | None = None,
        context: dict | None = None,
    ) -> dict:
        """Connect and mount Nexus to a sandbox (Nexus-managed or user-managed).

        Works for both:
        - Nexus-managed sandboxes (created via sandbox_create) - no sandbox_api_key needed
        - User-managed sandboxes (external) - requires sandbox_api_key

        Args:
            sandbox_id: Sandbox ID (Nexus-managed or external)
            provider: Sandbox provider ("e2b", etc.). Default: "e2b"
            sandbox_api_key: Provider API key (optional, only for user-managed sandboxes)
            mount_path: Path where Nexus will be mounted in sandbox (default: /mnt/nexus)
            nexus_url: Nexus server URL (auto-detected if not provided)
            nexus_api_key: Nexus API key (from context if not provided)
            agent_id: Agent ID for version attribution (issue #418).
                When set, file modifications will be attributed to this agent.
            context: Operation context

        Returns:
            Dict with connection details (sandbox_id, provider, mount_path, mounted_at, mount_status)

        Raises:
            ValueError: If provider not supported or required credentials missing
            RuntimeError: If connection/mount fails
        """
        # Ensure sandbox manager is initialized
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        # Get Nexus URL - should be provided by client
        # Falls back to localhost only for direct server-side calls
        if not nexus_url:
            import os

            # Check NEXUS_SERVER_URL first (for Docker deployments), then NEXUS_URL
            nexus_url = os.getenv("NEXUS_SERVER_URL") or os.getenv(
                "NEXUS_URL", "http://localhost:8080"
            )

        # Get Nexus API key from context if not provided
        if not nexus_api_key:
            ctx = self._parse_context(context)
            nexus_api_key = getattr(ctx, "api_key", None)

        if not nexus_api_key:
            raise ValueError(
                "Nexus API key required for mounting. Pass nexus_api_key or provide in context."
            )

        result: dict[Any, Any] = await self._sandbox_manager.connect_sandbox(
            sandbox_id=sandbox_id,
            provider=provider,
            sandbox_api_key=sandbox_api_key,
            mount_path=mount_path,
            nexus_url=nexus_url,
            nexus_api_key=nexus_api_key,
            agent_id=agent_id,
        )
        return result

    @rpc_expose(description="Disconnect from user-managed sandbox")
    async def sandbox_disconnect(  # type: ignore[override]
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        context: dict | None = None,  # noqa: ARG002
    ) -> dict:
        """Disconnect and unmount Nexus from a user-managed sandbox.

        Args:
            sandbox_id: External sandbox ID
            provider: Sandbox provider ("e2b", etc.). Default: "e2b"
            sandbox_api_key: Provider API key for authentication
            context: Operation context

        Returns:
            Dict with disconnection details (sandbox_id, provider, unmounted_at)

        Raises:
            ValueError: If provider not supported or API key missing
            RuntimeError: If disconnection/unmount fails
        """
        # Ensure sandbox manager is initialized
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        result: dict[Any, Any] = await self._sandbox_manager.disconnect_sandbox(
            sandbox_id=sandbox_id,
            provider=provider,
            sandbox_api_key=sandbox_api_key,
        )
        return result

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
        if hasattr(self, "_rebac_manager") and self._rebac_manager is not None:
            self._rebac_manager.close()

        # Close AuditStore to release database connection
        if hasattr(self, "_audit_store"):
            self._audit_store.close()

        # Close TokenManager to release database connection
        if hasattr(self, "_token_manager") and self._token_manager is not None:
            self._token_manager.close()
