"""Unified filesystem implementation for Nexus."""

import builtins
import contextlib
import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import InvalidPathError, NexusFileNotFoundError
from nexus.contracts.types import OperationContext, Permission
from nexus.core.file_events import FileEvent, FileEventType
from nexus.core.hash_fast import hash_content

if TYPE_CHECKING:
    from nexus.bricks.rebac.entity_registry import EntityRegistry
    from nexus.services.memory.memory_api import Memory
from nexus.contracts.cache_store import CacheStoreABC, NullCacheStore
from nexus.contracts.filesystem.filesystem_abc import NexusFilesystemABC
from nexus.contracts.metadata import FileMetadata
from nexus.core.config import (
    BrickServices,
    CacheConfig,
    DistributedConfig,
    KernelServices,
    MemoryConfig,
    ParseConfig,
    PermissionConfig,
    SystemServices,
    WiredServices,
)
from nexus.core.metastore import MetastoreABC
from nexus.core.nexus_fs_core import NexusFSCoreMixin
from nexus.core.router import PathRouter
from nexus.lib.rpc_decorator import rpc_expose
from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


class NexusFS(  # type: ignore[misc]
    NexusFSCoreMixin,
    NexusFilesystemABC,
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

    _memory_api: Any

    def __init__(
        self,
        metadata_store: MetastoreABC,
        record_store: RecordStoreABC | None = None,
        cache_store: CacheStoreABC | None = None,
        *,
        is_admin: bool = False,
        cache: CacheConfig | None = None,
        permissions: PermissionConfig | None = None,
        distributed: DistributedConfig | None = None,
        memory: MemoryConfig | None = None,
        parsing: ParseConfig | None = None,
        kernel_services: KernelServices | None = None,
        system_services: SystemServices | None = None,
        brick_services: BrickServices | None = None,
    ):
        """Initialize NexusFS kernel.

        Kernel boots with MetastoreABC (inode layer) and an optional router
        (via KernelServices). Backends are mounted externally via
        ``router.add_mount()`` — like Linux VFS, no global backend.
        """
        # Config defaults
        cache = cache or CacheConfig()
        permissions = permissions or PermissionConfig()
        distributed = distributed or DistributedConfig()
        memory = memory or MemoryConfig()
        parsing = parsing or ParseConfig()
        ksvc = kernel_services or KernelServices()
        sys_svc = system_services or SystemServices()
        brk_svc = brick_services or BrickServices()

        self._cache_config = cache
        self._perm_config = permissions
        self._distributed_config = distributed
        self._memory_config_obj = memory
        self._parse_config = parsing
        self._kernel_services = ksvc
        self._system_services = sys_svc
        self._brick_services = brk_svc
        self._config: Any | None = None

        # Map config fields to flat attributes
        self._enable_memory_paging = memory.enable_paging
        self._memory_main_capacity = memory.main_capacity
        self._memory_recall_max_age_hours = memory.recall_max_age_hours
        self._enforce_permissions = permissions.enforce
        self._enforce_zone_isolation = permissions.enforce_zone_isolation
        self.allow_admin_bypass = permissions.allow_admin_bypass
        self.auto_parse = parsing.auto_parse
        self.is_admin = is_admin

        # Three pillars: metadata (required), record store, cache store
        # No self.backend — all I/O goes through router.route().backend
        self.metadata: MetastoreABC = metadata_store
        self._record_store = record_store
        self._sql_engine: Any = None
        self._db_session_factory: Any = None
        self.SessionLocal: Any = None
        if record_store is not None:
            self._sql_engine = record_store.engine
            self._db_session_factory = record_store.session_factory
            self.SessionLocal = self._db_session_factory

        # Initialize cache store (Task #22: Fourth Pillar)
        self.cache_store: CacheStoreABC = (
            cache_store if cache_store is not None else NullCacheStore()
        )

        # Path router (metastore-backed mount table)
        if ksvc.router is not None:
            self.router = ksvc.router
        else:
            self.router = PathRouter(metadata_store)

        # Parser registries (Issue #2134: from BrickServices, fallback for tests)
        if brk_svc.parser_registry is not None:
            self.parser_registry = brk_svc.parser_registry
        else:
            from nexus.parsers.markitdown_parser import MarkItDownParser as _MkD
            from nexus.parsers.registry import ParserRegistry as _PR

            self.parser_registry = _PR()
            self.parser_registry.register(_MkD())
        if brk_svc.provider_registry is not None:
            self.provider_registry = brk_svc.provider_registry
        else:
            from nexus.parsers.providers.registry import ProviderRegistry as _PvR

            self.provider_registry = _PvR()
            self.provider_registry.auto_discover()

        self._virtual_view_parse_fn = brk_svc.parse_fn
        self._parser_threads: list[threading.Thread] = []
        self._parser_threads_lock = threading.Lock()

        # Default context for embedded mode
        self._default_context = OperationContext(
            user_id="anonymous",
            groups=[],
            zone_id=ROOT_ZONE_ID,
            agent_id=None,
            is_admin=is_admin,
            is_system=False,
            admin_capabilities=set(),
        )

        # =====================================================================
        # Tier 1: SYSTEM services — critical + degradable (Issue #2193)
        # Moved from KernelServices to SystemServices per Liedtke's test.
        # =====================================================================
        self._rebac_manager = sys_svc.rebac_manager
        self._dir_visibility_cache = sys_svc.dir_visibility_cache
        self._audit_store = sys_svc.audit_store
        self._entity_registry = sys_svc.entity_registry
        self._permission_enforcer = sys_svc.permission_enforcer
        self._hierarchy_manager = sys_svc.hierarchy_manager
        self._deferred_permission_buffer = sys_svc.deferred_permission_buffer
        self._workspace_registry = sys_svc.workspace_registry
        self.mount_manager = sys_svc.mount_manager
        self._workspace_manager = sys_svc.workspace_manager
        # overlay_resolver removed (Issue #2034) — always None, re-add when #1264 is implemented
        self._overlay_resolver = None

        # =====================================================================
        # Tier 1: SYSTEM services (Issue #2034: from SystemServices)
        # =====================================================================
        self._agent_registry = sys_svc.agent_registry
        self._namespace_manager = sys_svc.namespace_manager
        self._async_agent_registry = sys_svc.async_agent_registry
        self._async_namespace_manager = sys_svc.async_namespace_manager
        self._context_branch_service = sys_svc.context_branch_service
        # Zone lifecycle — write gating during deprovisioning (Issue #2061)
        self._zone_lifecycle = getattr(sys_svc, "zone_lifecycle", None)

        # =====================================================================
        # Tier 2: BRICK services (Issue #2034: from BrickServices)
        # =====================================================================
        self._event_bus = brk_svc.event_bus
        self._lock_manager = brk_svc.lock_manager
        self._wallet_provisioner = brk_svc.wallet_provisioner
        self._snapshot_service = brk_svc.snapshot_service
        self._api_key_creator = brk_svc.api_key_creator
        # Version Brick (Issue #2034: moved from kernel)
        self.version_service = brk_svc.version_service

        # Lazy-init sentinels
        self._token_manager = None
        self._semantic_search = None
        self._memory_api: "Memory | None" = None
        self._memory_config: dict[str, str | None] = {
            "zone_id": None,
            "user_id": None,
            "agent_id": None,
        }
        self._sandbox_manager: Any = None
        self._coordination_client: Any = None
        self._event_client: Any = None

        # VFS lock manager (Issue #2134: from BrickServices)
        if brk_svc.vfs_lock_manager is not None:
            self._vfs_lock_manager = brk_svc.vfs_lock_manager
        else:
            from nexus.core.lock_fast import create_vfs_lock_manager

            self._vfs_lock_manager = create_vfs_lock_manager()
        logger.info("VFS lock manager initialized (%s)", type(self._vfs_lock_manager).__name__)

        # Service attributes — set to None by default.
        # Wired by factory via _bind_wired_services() after construction.
        # Issue #643/#2133: kernel never imports or creates services.
        self.rebac_service: Any = None
        self.mount_service: Any = None
        self._gateway: Any = None
        self._mount_core_service: Any = None
        self._sync_service: Any = None
        self._sync_job_service: Any = None
        self._mount_persist_service: Any = None
        self.mcp_service: Any = None
        self.llm_service: Any = None
        self._llm_subsystem: Any = None
        self.oauth_service: Any = None
        self.skill_service: Any = None
        self.skill_package_service: Any = None
        self.search_service: Any = None
        self.share_link_service: Any = None
        self.events_service: Any = None
        self.task_queue_service: Any = None

        # Kernel notification dispatch (INTERCEPT + OBSERVE).
        # Kernel owns dispatch infrastructure — creates empty callback lists.
        # Factory registers hooks at boot (KERNEL-ARCHITECTURE §3).
        from nexus.core.kernel_dispatch import KernelDispatch

        self._dispatch: KernelDispatch = KernelDispatch()

        # PermissionChecker: core module, safe to create here (Issue #2133)
        from nexus.services.permissions.checker import PermissionChecker

        self._permission_checker = PermissionChecker(
            permission_enforcer=self._permission_enforcer,
            metadata_store=self.metadata,
            default_context=self._default_context,
            enforce_permissions=self._enforce_permissions,
        )

        # Read-set-aware cache (Issue #1169)
        self._read_set_cache = None
        metadata_cache = getattr(self.metadata, "_cache", None)
        if metadata_cache is not None and self._cache_config.enable_metadata_cache:
            from nexus.storage.read_set import ReadSetRegistry
            from nexus.storage.read_set_cache import ReadSetAwareCache

            self._read_set_registry = ReadSetRegistry()
            self._read_set_cache = ReadSetAwareCache(
                base_cache=metadata_cache,
                registry=self._read_set_registry,
            )
            self._read_tracking_enabled = True

        # Issue #1519/#2034: Cache observer — created internally from read-set cache.
        # (Removed from KernelServices — NexusFS owns the cache observer lifecycle.)
        self._cache_observer = None
        if self._read_set_cache is not None:
            from nexus.storage.cache_invalidation import ReadSetCacheObserver

            self._cache_observer = ReadSetCacheObserver(self._read_set_cache)

    def _bind_wired_services(self, wired: WiredServices | dict[str, Any]) -> None:
        """Bind wired services from factory two-phase init.

        Args:
            wired: WiredServices dataclass (from _boot_wired_services).
                   Also accepts dict for backward compatibility with tests.

        Issue #2133: Accepts WiredServices frozen dataclass.
        """
        if isinstance(wired, dict):
            # Backward compat for tests that pass a dict
            self.rebac_service = wired.get("rebac_service")
            self.mount_service = wired.get("mount_service")
            self._gateway = wired.get("gateway")
            self._mount_core_service = wired.get("mount_core_service")
            self._sync_service = wired.get("sync_service")
            self._sync_job_service = wired.get("sync_job_service")
            self._mount_persist_service = wired.get("mount_persist_service")
            self.mcp_service = wired.get("mcp_service")
            self.llm_service = wired.get("llm_service")
            self._llm_subsystem = wired.get("llm_subsystem")
            self.oauth_service = wired.get("oauth_service")
            self.skill_service = wired.get("skill_service")
            self.skill_package_service = wired.get("skill_package_service")
            self.search_service = wired.get("search_service")
            self.share_link_service = wired.get("share_link_service")
            self.events_service = wired.get("events_service")
            self.task_queue_service = wired.get("task_queue_service")
            self._workspace_rpc_service = wired.get("workspace_rpc_service")
            self._agent_rpc_service = wired.get("agent_rpc_service")
            self._user_provisioning_service = wired.get("user_provisioning_service")
            self._sandbox_rpc_service = wired.get("sandbox_rpc_service")
            self._metadata_export_service = wired.get("metadata_export_service")
            self._ace_rpc_service = wired.get("ace_rpc_service")
            self._descendant_checker = wired.get("descendant_checker")
            self._memory_provider = wired.get("memory_provider")
            return
        self.rebac_service = wired.rebac_service
        self.mount_service = wired.mount_service
        self._gateway = wired.gateway
        self._mount_core_service = wired.mount_core_service
        self._sync_service = wired.sync_service
        self._sync_job_service = wired.sync_job_service
        self._mount_persist_service = wired.mount_persist_service
        self.mcp_service = wired.mcp_service
        self.llm_service = wired.llm_service
        self._llm_subsystem = wired.llm_subsystem
        self.oauth_service = wired.oauth_service
        self.skill_service = wired.skill_service
        self.skill_package_service = wired.skill_package_service
        self.search_service = wired.search_service
        self.share_link_service = wired.share_link_service
        self.events_service = wired.events_service
        self.task_queue_service = wired.task_queue_service
        self._workspace_rpc_service = wired.workspace_rpc_service
        self._agent_rpc_service = wired.agent_rpc_service
        self._user_provisioning_service = wired.user_provisioning_service
        self._sandbox_rpc_service = wired.sandbox_rpc_service
        self._metadata_export_service = wired.metadata_export_service
        self._ace_rpc_service = wired.ace_rpc_service
        self._descendant_checker = wired.descendant_checker
        self._memory_provider = wired.memory_provider

    @property
    def _service_extras(self) -> dict[str, Any]:
        """Server layer reads typed service fields as a dict interface."""
        result: dict[str, Any] = {}
        # System tier fields
        for k in ("observability_subsystem", "resiliency_manager", "delivery_worker"):
            v = getattr(self._system_services, k, None)
            if v is not None:
                result[k] = v
        # Brick tier fields
        for k in (
            "chunked_upload_service",
            "manifest_resolver",
            "rebac_circuit_breaker",
            "tool_namespace_middleware",
        ):
            v = getattr(self._brick_services, k, None)
            if v is not None:
                result[k] = v
        return result

    @property
    def read_set_cache(self) -> Any | None:
        """Public accessor for the read-set-aware cache (Issue #1169)."""
        return self._read_set_cache

    @property
    def read_set_registry(self) -> Any | None:
        """Public accessor for the ReadSetRegistry (Issue #1169)."""
        return getattr(self, "_read_set_registry", None)

    @property
    def metadata_cache(self) -> Any | None:
        """Public accessor for the underlying MetadataCache on the metadata store."""
        return getattr(self.metadata, "_cache", None)

    @property
    def namespace_manager(self) -> Any | None:
        """Public accessor for the NamespaceManager (via PermissionEnforcer)."""
        enforcer = self._permission_enforcer
        if enforcer is not None:
            return getattr(enforcer, "namespace_manager", None)
        return None

    @property
    def config(self) -> Any | None:
        """Public accessor for the runtime configuration object."""
        return self._config

    @property
    def rebac_manager(self) -> Any | None:
        """Public accessor for the ReBACManager instance."""
        return getattr(self, "_rebac_manager", None)

    @property
    def semantic_search_engine(self) -> Any | None:
        """Public accessor for the semantic search engine instance."""
        return self._semantic_search

    @property
    def memory(self) -> Any:
        """Get Memory API instance (lazy init on first access)."""
        return self._memory_provider.get_or_create()

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

    def _get_created_by(self, context: OperationContext | dict | None = None) -> str | None:
        """Get the created_by value for version history tracking."""
        from nexus.lib.context_utils import get_created_by

        return get_created_by(context, self._default_context)

    def _get_routing_params(
        self, context: OperationContext | dict | None = None
    ) -> tuple[str | None, str | None, bool]:
        """Extract (zone_id, agent_id, is_admin) from context for router.route()."""
        if context is None:
            return (
                self._default_context.zone_id,
                self._default_context.agent_id,
                self._default_context.is_admin,
            )
        if isinstance(context, dict):
            return (
                context.get("zone_id", self._default_context.zone_id),
                context.get("agent_id", self._default_context.agent_id),
                context.get("is_admin", self.is_admin),
            )
        return context.zone_id, context.agent_id, getattr(context, "is_admin", self.is_admin)

    def _check_zone_writable(self, context: OperationContext | dict | None = None) -> None:
        """Raise ZoneTerminatingError if the zone is being deprovisioned.

        Issue #2061: Write-gating during zone finalization (Decision #4A).
        """
        if self._zone_lifecycle is None:
            return
        zone_id, _, _ = self._get_routing_params(context)
        if zone_id and self._zone_lifecycle.is_zone_terminating(zone_id):
            from nexus.contracts.exceptions import ZoneTerminatingError

            raise ZoneTerminatingError(zone_id)

    @property
    def zone_id(self) -> str | None:
        """Default zone_id from the instance context."""
        return self._default_context.zone_id

    @property
    def agent_id(self) -> str | None:
        """Default agent_id from the instance context."""
        return self._default_context.agent_id

    @property
    def user_id(self) -> str | None:
        """Default user_id from the instance context."""
        return getattr(self._default_context, "user_id", None)

    def _get_memory_api(self, context: dict | None = None) -> "Memory":
        """Get Memory API instance with context-specific configuration."""
        return self._memory_provider.get_for_context(context)

    def _parse_context(self, context: OperationContext | dict | None = None) -> OperationContext:
        """Parse context dict or OperationContext into OperationContext."""
        from nexus.lib.context_utils import parse_context

        return parse_context(context)

    def _ensure_entity_registry(self) -> "EntityRegistry":
        """Lazily create and cache an EntityRegistry instance."""
        return self._memory_provider.ensure_entity_registry()

    def _validate_path(self, path: str, allow_root: bool = False) -> str:
        """Validate and normalize virtual path. Raises InvalidPathError."""
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
        """Get parent directory path, or None if root."""
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

    def _create_directory_metadata(
        self, path: str, context: OperationContext | None = None
    ) -> None:
        """
        Create metadata entry for a directory.

        Args:
            path: Virtual path to directory
            context: Operation context (for zone_id and created_by)
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

        # Route to find which backend owns this path
        route = self.router.route(path, is_admin=ctx.is_admin)

        metadata = FileMetadata(
            path=path,
            backend_name=route.backend.name,
            physical_path=empty_hash,  # Placeholder for directory
            size=0,  # Directories have size 0
            etag=empty_hash,
            mime_type="inode/directory",  # MIME type for directories
            created_at=now,
            modified_at=now,
            version=1,
            created_by=self._get_created_by(context),  # Track who created this directory
            zone_id=ctx.zone_id or ROOT_ZONE_ID,  # P0 SECURITY: Set zone_id
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
        """Create a directory (parents=True for mkdir -p)."""
        path = self._validate_path(path)

        # Use provided context or default
        ctx = context if context is not None else self._default_context

        # Block writes during zone deprovisioning (Issue #2061)
        self._check_zone_writable(ctx)

        # Check write permission on the appropriate ancestor directory
        # - parents=False: check immediate parent (must exist)
        # - parents=True: check first existing ancestor (will create missing parents)
        parent_path = self._get_parent_path(path)
        if parent_path:
            check_path: str | None = parent_path
            if parents:
                # Find the first existing ancestor to check permission on
                while check_path and check_path != "/" and not self.metadata.exists(check_path):
                    check_path = self._get_parent_path(check_path)

            # Check WRITE permission on the existing ancestor
            if check_path and self.metadata.exists(check_path):
                self._permission_checker.check(check_path, Permission.WRITE, ctx)

        # Route to backend with write access check (mkdir requires write permission)
        route = self.router.route(
            path,
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
                            parent_dir, zone_id=ctx.zone_id or ROOT_ZONE_ID
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

        logger.debug(
            f"mkdir: Checking for hierarchy_manager: hasattr={hasattr(self, '_hierarchy_manager')}"
        )

        ctx = context or self._default_context

        if hasattr(self, "_hierarchy_manager"):
            try:
                logger.debug(
                    f"mkdir: Calling ensure_parent_tuples for {path}, zone_id={ctx.zone_id or ROOT_ZONE_ID}"
                )
                created_count = self._hierarchy_manager.ensure_parent_tuples(
                    path, zone_id=ctx.zone_id or ROOT_ZONE_ID
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
        if self._rebac_manager and ctx.user_id and not ctx.is_system:
            try:
                logger.debug(f"mkdir: Granting direct_owner permission to {ctx.user_id} for {path}")
                self._rebac_manager.rebac_write(
                    subject=("user", ctx.user_id),
                    relation="direct_owner",
                    object=("file", path),
                    zone_id=ctx.zone_id or ROOT_ZONE_ID,
                )
                logger.debug(f"mkdir: Granted direct_owner permission to {ctx.user_id} for {path}")
            except Exception as e:
                logger.warning(f"Failed to grant direct_owner permission for {path}: {e}")

        # Issue #900: Unified two-phase dispatch for mkdir
        new_revision = self._increment_vfs_revision()

        from nexus.contracts.vfs_hooks import MkdirHookContext

        self._dispatch.intercept_post_mkdir(
            MkdirHookContext(
                path=path,
                context=ctx,
                zone_id=ctx.zone_id,
                agent_id=ctx.agent_id,
            )
        )
        self._dispatch.notify(
            FileEvent(
                type=FileEventType.DIR_CREATE,
                path=path,
                zone_id=ctx.zone_id or ROOT_ZONE_ID,
                revision=new_revision,
                agent_id=ctx.agent_id,
                user_id=ctx.user_id,
            )
        )

    @rpc_expose(description="Remove directory")
    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        subject: tuple[str, str] | None = None,
        context: OperationContext | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        is_admin: bool | None = None,
    ) -> None:
        """Remove a directory (recursive=True for rm -rf)."""
        import errno

        path = self._validate_path(path)

        # Block writes during zone deprovisioning (Issue #2061)
        self._check_zone_writable(context)

        # P0 Fixes: Create OperationContext
        if context is not None:
            ctx = (
                context
                if isinstance(context, OperationContext)
                else OperationContext(
                    user_id=context.user_id,
                    groups=context.groups,
                    zone_id=context.zone_id or zone_id,
                    agent_id=context.agent_id or agent_id,
                    is_admin=context.is_admin if is_admin is None else is_admin,
                    is_system=context.is_system,
                    admin_capabilities=set(),
                )
            )
        elif subject is not None:
            ctx = OperationContext(
                user_id=subject[1],
                groups=[],
                zone_id=zone_id,
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
                    user_id=self._default_context.user_id,
                    groups=self._default_context.groups,
                    zone_id=zone_id or self._default_context.zone_id,
                    agent_id=agent_id or self._default_context.agent_id,
                    is_admin=(is_admin if is_admin is not None else self._default_context.is_admin),
                    is_system=self._default_context.is_system,
                    admin_capabilities=set(),
                )
            )

        # Check write permission on directory

        logger.debug(
            f"rmdir: path={path}, recursive={recursive}, user={ctx.user_id}, is_admin={ctx.is_admin}"
        )
        self._permission_checker.check(path, Permission.WRITE, ctx)
        logger.debug(f"  -> Permission check PASSED for rmdir on {path}")

        # Route to backend with write access check (rmdir requires write permission)
        route = self.router.route(
            path,
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
            _errors: list[str] = []
            for file_meta in files_in_dir:
                if file_meta.etag:
                    try:
                        route.backend.delete_content(file_meta.etag)
                    except Exception as e:
                        if len(_errors) < 100:
                            _errors.append(f"{file_meta.path}: {e}")
            if _errors:
                logger.debug(
                    "Bulk content delete: %d error(s) (showing up to 100): %s",
                    len(_errors),
                    "; ".join(_errors),
                )

            # Batch delete from metadata store
            self.metadata.delete_batch(file_paths)

        # Remove directory in backend (if it still exists)
        # In CAS systems, the directory may no longer exist after deleting its contents
        with contextlib.suppress(NexusFileNotFoundError):
            route.backend.rmdir(route.backend_path, recursive=recursive)

        # Also delete the directory's own metadata entry if it exists
        # Directories can have metadata entries (created by mkdir)
        try:
            self.metadata.delete(path)
        except Exception as e:
            logger.debug("Failed to delete directory metadata for %s: %s", path, e)

        # Clean up sparse directory index entries (Issue: rmdir not cleaning directory index)
        # This removes entries from DirectoryEntryModel used by non-recursive list()
        if hasattr(self.metadata, "delete_directory_entries_recursive"):
            try:
                self.metadata.delete_directory_entries_recursive(path)
            except Exception as e:
                logger.debug("Failed to clean up directory index for %s: %s", path, e)

        # Issue #900: Unified two-phase dispatch for rmdir
        new_revision = self._increment_vfs_revision()

        from nexus.contracts.vfs_hooks import RmdirHookContext

        self._dispatch.intercept_post_rmdir(
            RmdirHookContext(
                path=path,
                context=ctx,
                zone_id=ctx.zone_id,
                agent_id=ctx.agent_id,
                recursive=recursive,
            )
        )
        self._dispatch.notify(
            FileEvent(
                type=FileEventType.DIR_DELETE,
                path=path,
                zone_id=ctx.zone_id or ROOT_ZONE_ID,
                revision=new_revision,
                agent_id=ctx.agent_id,
                user_id=ctx.user_id,
            )
        )

    @rpc_expose(description="Check if path is a directory")
    def is_directory(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> bool:
        """Check if path is a directory (explicit or implicit)."""
        try:
            path = self._validate_path(path)

            # Use provided context or default
            ctx = context if context is not None else self._default_context

            # Check if it's an implicit directory first (for optimization)
            is_implicit_dir = self.metadata.is_implicit_directory(path)

            # Check permission (with TRAVERSE optimization for implicit directories)
            if self._enforce_permissions:
                if is_implicit_dir:
                    # OPTIMIZATION: Try TRAVERSE permission first (O(1))
                    # Fall back to descendant access check if TRAVERSE denied
                    if not self._permission_enforcer.check(
                        path, Permission.TRAVERSE, ctx
                    ) and not self._descendant_checker.has_access(path, Permission.READ, ctx):
                        return False
                else:
                    # For explicit directories/files, use hierarchical access check
                    if not self._descendant_checker.has_access(path, Permission.READ, ctx):
                        return False

            # Route with access control (read permission needed to check)
            route = self.router.route(
                path,
                is_admin=ctx.is_admin,
                check_write=False,
            )
            # Check if it's an explicit directory in the backend
            if route.backend.is_directory(route.backend_path):
                return True
            # Return cached implicit directory status
            return is_implicit_dir
        except (InvalidPathError, Exception):
            return False

    @rpc_expose(description="Get available namespaces")
    def get_top_level_mounts(self) -> builtins.list[str]:
        """Return top-level mount names visible to the current user.

        Reads DT_MOUNT entries from metastore (kernel's single source of
        truth for mount points). Admin-only filtering uses the runtime
        mount table which carries mount options.
        """
        # Build admin_only set from runtime mount table (mount options)
        admin_only = {m.mount_point for m in self.router.list_mounts() if m.admin_only}

        names: set[str] = set()
        for meta in self.metadata.list("/"):
            if not meta.is_mount:
                continue
            top = meta.path.lstrip("/").split("/")[0]
            if not top:
                continue
            if meta.path in admin_only and not self.is_admin:
                continue
            names.add(top)
        return sorted(names)

    @rpc_expose(description="Get file metadata for FUSE operations")
    def get_metadata(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any] | None:
        """Get file metadata without reading content (FUSE getattr)."""
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
        """Get content hash for HTTP If-None-Match checks."""
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
        """Get directory entries from backend for empty directory detection."""
        directories = set()

        try:
            # For root path, try routing "/" to find the root mount's backend
            if path == "/":
                try:
                    zone_id, _agent_id, is_admin = self._get_routing_params(context)
                    root_route = self.router.route("/", is_admin=is_admin, check_write=False)
                    entries = root_route.backend.list_dir(root_route.backend_path)
                    for entry in entries:
                        if entry.endswith("/"):  # Directory marker
                            dir_name = entry.rstrip("/")
                            dir_path = "/" + dir_name
                            directories.add(dir_path)
                except (NotImplementedError, Exception):
                    # No root mount, backend doesn't support list_dir, or other error
                    pass
            else:
                # Non-root path - use router with context
                zone_id, _agent_id, is_admin = self._get_routing_params(context)
                route = self.router.route(
                    path.rstrip("/"),
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

    # ------------------------------------------------------------------
    # Internal helpers restored for backward compatibility (Issue #2033)
    # ------------------------------------------------------------------

    @property
    def _require_rebac(self) -> Any:
        """Return the ReBAC manager or raise if unavailable."""
        mgr = self._rebac_manager
        if mgr is None:
            raise RuntimeError("ReBAC manager not available")
        return mgr

    def register_observe(self, observer: Any) -> None:
        """Register a mutation observer (OBSERVE phase, Issue #900)."""
        self._dispatch.register_observe(observer)

    # ------------------------------------------------------------------
    # ReBAC delegation stubs (Issue #2033)
    # Previously on NexusFSReBACMixin, now forwarded to rebac_service.
    # Generated dynamically below via _rebac_delegate().
    # ------------------------------------------------------------------

    # Service forwarding: __getattr__ routes method calls to services (Issue #2033)

    _SERVICE_METHODS: dict[str, str] = {
        # WorkspaceRPCService
        "workspace_snapshot": "_workspace_rpc_service",
        "workspace_restore": "_workspace_rpc_service",
        "workspace_log": "_workspace_rpc_service",
        "workspace_diff": "_workspace_rpc_service",
        "snapshot_begin": "_workspace_rpc_service",
        "snapshot_commit": "_workspace_rpc_service",
        "snapshot_rollback": "_workspace_rpc_service",
        "load_workspace_memory_config": "_workspace_rpc_service",
        "register_workspace": "_workspace_rpc_service",
        "unregister_workspace": "_workspace_rpc_service",
        "update_workspace": "_workspace_rpc_service",
        "list_workspaces": "_workspace_rpc_service",
        "get_workspace_info": "_workspace_rpc_service",
        "register_memory": "_workspace_rpc_service",
        "unregister_memory": "_workspace_rpc_service",
        "list_registered_memories": "_workspace_rpc_service",
        "get_memory_info": "_workspace_rpc_service",
        # AgentRPCService
        "register_agent": "_agent_rpc_service",
        "update_agent": "_agent_rpc_service",
        "list_agents": "_agent_rpc_service",
        "get_agent": "_agent_rpc_service",
        "delete_agent": "_agent_rpc_service",
        # UserProvisioningService
        "provision_user": "_user_provisioning_service",
        "deprovision_user": "_user_provisioning_service",
        # SandboxRPCService
        "sandbox_create": "_sandbox_rpc_service",
        "sandbox_run": "_sandbox_rpc_service",
        "sandbox_validate": "_sandbox_rpc_service",
        "sandbox_pause": "_sandbox_rpc_service",
        "sandbox_resume": "_sandbox_rpc_service",
        "sandbox_stop": "_sandbox_rpc_service",
        "sandbox_list": "_sandbox_rpc_service",
        "sandbox_status": "_sandbox_rpc_service",
        "sandbox_get_or_create": "_sandbox_rpc_service",
        "sandbox_connect": "_sandbox_rpc_service",
        "sandbox_disconnect": "_sandbox_rpc_service",
        # MetadataExportService
        "export_metadata": "_metadata_export_service",
        "import_metadata": "_metadata_export_service",
        # MountCoreService
        "add_mount": "_mount_core_service",
        "remove_mount": "_mount_core_service",
        "list_connectors": "_mount_core_service",
        "list_mounts": "_mount_core_service",
        "get_mount": "_mount_core_service",
        "has_mount": "_mount_core_service",
        # MountPersistService
        "save_mount": "_mount_persist_service",
        "list_saved_mounts": "_mount_persist_service",
        "load_mount": "_mount_persist_service",
        "delete_saved_mount": "_mount_persist_service",
        # SearchService (list/glob/grep are thin forwarders, not __getattr__)
        # asemantic_search* are in _SERVICE_ALIASES (name transformation: a-prefix removed)
        "glob_batch": "search_service",
        # TaskQueueService
        "get_task": "task_queue_service",
        "cancel_task": "task_queue_service",
        # MCPService
        "mcp_list_mounts": "mcp_service",
        # OAuthService
        "oauth_list_providers": "oauth_service",
        # LLMService
        "create_llm_reader": "llm_service",
        # ReBACService direct methods (no _sync suffix)
        "set_rebac_option": "rebac_service",
        "get_rebac_option": "rebac_service",
        "register_namespace": "rebac_service",
        # EventsService (Issue #1166)
        "wait_for_changes": "events_service",
        "lock": "events_service",
        "extend_lock": "events_service",
        "unlock": "events_service",
    }

    # Special aliases where service method name differs
    _SERVICE_ALIASES: dict[str, tuple[str, str]] = {
        "list_memories": ("_workspace_rpc_service", "list_registered_memories"),
        "sandbox_available": ("_sandbox_rpc_service", "sandbox_available"),
        "get_sync_job": ("_sync_job_service", "get_job"),
        "list_sync_jobs": ("_sync_job_service", "list_jobs"),
        "load_all_saved_mounts": ("_mount_persist_service", "load_all_mounts"),
        # Dir visibility cache: NexusFS method names → cache method names
        "get_dir_visibility_cache_metrics": ("_dir_visibility_cache", "get_metrics"),
        "clear_dir_visibility_cache": ("_dir_visibility_cache", "clear"),
        # SearchService async methods: a-prefix removed when calling service
        "asemantic_search": ("search_service", "semantic_search"),
        "asemantic_search_index": ("search_service", "semantic_search_index"),
        "asemantic_search_stats": ("search_service", "semantic_search_stats"),
        # SyncService / SyncJobService (Issue #2033)
        "sync_mount": ("_sync_service", "sync_mount_flat"),
        "sync_mount_async": ("_sync_job_service", "sync_mount_async"),
        "cancel_sync_job": ("_sync_job_service", "cancel_sync_job"),
        # VersionService async methods (Issue #2033)
        "aget_version": ("version_service", "get_version"),
        "alist_versions": ("version_service", "list_versions"),
        "arollback": ("version_service", "rollback"),
        "adiff_versions": ("version_service", "diff_versions"),
        # ReBACService async methods (Issue #2033)
        "arebac_create": ("rebac_service", "rebac_create"),
        "arebac_delete": ("rebac_service", "rebac_delete"),
        "arebac_check": ("rebac_service", "rebac_check"),
        "arebac_check_batch": ("rebac_service", "rebac_check_batch"),
        "arebac_expand": ("rebac_service", "rebac_expand"),
        "arebac_explain": ("rebac_service", "rebac_explain"),
        "arebac_list_tuples": ("rebac_service", "rebac_list_tuples"),
        "aget_namespace": ("rebac_service", "get_namespace"),
        # ReBACService sync methods with _sync suffix (Issue #2033)
        "rebac_expand": ("rebac_service", "rebac_expand_sync"),
        "rebac_explain": ("rebac_service", "rebac_explain_sync"),
        "share_with_user": ("rebac_service", "share_with_user_sync"),
        "share_with_group": ("rebac_service", "share_with_group_sync"),
        "grant_consent": ("rebac_service", "grant_consent_sync"),
        "revoke_consent": ("rebac_service", "revoke_consent_sync"),
        "make_public": ("rebac_service", "make_public_sync"),
        "make_private": ("rebac_service", "make_private_sync"),
        "apply_dynamic_viewer_filter": ("rebac_service", "apply_dynamic_viewer_filter_sync"),
        "list_outgoing_shares": ("rebac_service", "list_outgoing_shares_sync"),
        "list_incoming_shares": ("rebac_service", "list_incoming_shares_sync"),
        "get_dynamic_viewer_config": ("rebac_service", "get_dynamic_viewer_config_sync"),
        "namespace_create": ("rebac_service", "namespace_create_sync"),
        "namespace_delete": ("rebac_service", "namespace_delete_sync"),
        "namespace_list": ("rebac_service", "namespace_list_sync"),
        "get_namespace": ("rebac_service", "get_namespace_sync"),
        # ReBACService direct methods (no _sync suffix)
        "rebac_expand_with_privacy": ("rebac_service", "rebac_expand_with_privacy_sync"),
        # SkillService (Issue #2035): NexusFS facade → skill_service RPC methods
        "skills_share": ("skill_service", "rpc_share"),
        "skills_discover": ("skill_service", "rpc_discover"),
        "skills_get_prompt_context": ("skill_service", "rpc_get_prompt_context"),
        # SkillPackageService (Issue #2035): NexusFS facade → skill_package_service
        "skills_import": ("skill_package_service", "import_skill"),
        "skills_validate_zip": ("skill_package_service", "validate_zip"),
    }

    def __getattr__(self, name: str) -> Any:
        """Forward extracted facade methods to their service objects.

        This enables callers to continue using nx.method_name() after
        facade methods were removed from NexusFS (Issue #2033).
        """
        # Check aliases first (method name differs on service)
        alias = NexusFS._SERVICE_ALIASES.get(name)
        if alias is not None:
            svc_attr, svc_method = alias
            svc = self.__dict__.get(svc_attr)
            if svc is not None:
                return getattr(svc, svc_method)

        # Standard forwarding (same method name on service)
        svc_attr_std = NexusFS._SERVICE_METHODS.get(name)
        if svc_attr_std is not None:
            svc = self.__dict__.get(svc_attr_std)
            if svc is not None:
                return getattr(svc, name)

        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    # ------------------------------------------------------------------
    # Abstract method forwarders (ABCMeta requires real definitions)
    # These satisfy the NexusFilesystemABC while delegating to services.
    # ------------------------------------------------------------------

    # --- Workspace Versioning (→ _workspace_rpc_service) ---

    def workspace_snapshot(
        self,
        workspace_path: str | None = None,
        description: str | None = None,
        tags: builtins.list[str] | None = None,
    ) -> dict[str, Any]:
        return self._workspace_rpc_service.workspace_snapshot(
            workspace_path=workspace_path,
            description=description,
            tags=tags,
        )

    def workspace_restore(
        self,
        snapshot_number: int,
        workspace_path: str | None = None,
    ) -> dict[str, Any]:
        return self._workspace_rpc_service.workspace_restore(
            snapshot_number=snapshot_number,
            workspace_path=workspace_path,
        )

    def workspace_log(
        self,
        workspace_path: str | None = None,
        limit: int = 100,
    ) -> builtins.list[dict[str, Any]]:
        return self._workspace_rpc_service.workspace_log(
            workspace_path=workspace_path,
            limit=limit,
        )

    def workspace_diff(
        self,
        snapshot_1: int,
        snapshot_2: int,
        workspace_path: str | None = None,
    ) -> dict[str, Any]:
        return self._workspace_rpc_service.workspace_diff(
            snapshot_1=snapshot_1,
            snapshot_2=snapshot_2,
            workspace_path=workspace_path,
        )

    # --- Workspace Registry (→ _workspace_rpc_service) ---

    def register_workspace(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        ttl: Any | None = None,
    ) -> dict[str, Any]:
        return self._workspace_rpc_service.register_workspace(
            path=path,
            name=name,
            description=description,
            created_by=created_by,
            tags=tags,
            metadata=metadata,
            session_id=session_id,
            ttl=ttl,
        )

    def unregister_workspace(self, path: str) -> bool:
        return self._workspace_rpc_service.unregister_workspace(path=path)

    def list_workspaces(self, context: Any | None = None) -> builtins.list[dict]:
        return self._workspace_rpc_service.list_workspaces(context=context)

    def get_workspace_info(self, path: str) -> dict | None:
        return self._workspace_rpc_service.get_workspace_info(path=path)

    # --- Memory Registry (→ _workspace_rpc_service) ---

    def register_memory(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        ttl: Any | None = None,
    ) -> dict[str, Any]:
        return self._workspace_rpc_service.register_memory(
            path=path,
            name=name,
            description=description,
            created_by=created_by,
            tags=tags,
            metadata=metadata,
            session_id=session_id,
            ttl=ttl,
        )

    def unregister_memory(self, path: str) -> bool:
        return self._workspace_rpc_service.unregister_memory(path=path)

    def list_memories(self) -> builtins.list[dict]:
        return self._workspace_rpc_service.list_registered_memories()

    def get_memory_info(self, path: str) -> dict | None:
        return self._workspace_rpc_service.get_memory_info(path=path)

    # --- Sandbox Operations (→ _sandbox_rpc_service) ---

    def sandbox_create(
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = "e2b",
        template_id: str | None = None,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_create(
            name=name,
            ttl_minutes=ttl_minutes,
            provider=provider,
            template_id=template_id,
            context=context,
        )

    def sandbox_get_or_create(
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        verify_status: bool = True,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_get_or_create(
            name=name,
            ttl_minutes=ttl_minutes,
            provider=provider,
            template_id=template_id,
            verify_status=verify_status,
            context=context,
        )

    def sandbox_run(
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        context: dict | None = None,
        as_script: bool = False,
    ) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_run(
            sandbox_id=sandbox_id,
            language=language,
            code=code,
            timeout=timeout,
            nexus_url=nexus_url,
            nexus_api_key=nexus_api_key,
            context=context,
            as_script=as_script,
        )

    def sandbox_pause(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_pause(sandbox_id=sandbox_id, context=context)

    def sandbox_resume(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_resume(sandbox_id=sandbox_id, context=context)

    def sandbox_stop(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_stop(sandbox_id=sandbox_id, context=context)

    def sandbox_list(
        self,
        context: dict | None = None,
        verify_status: bool = False,
        user_id: str | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_list(
            context=context,
            verify_status=verify_status,
            user_id=user_id,
            zone_id=zone_id,
            agent_id=agent_id,
            status=status,
        )

    def sandbox_status(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_status(sandbox_id=sandbox_id, context=context)

    def sandbox_connect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        mount_path: str = "/mnt/nexus",
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        agent_id: str | None = None,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_connect(
            sandbox_id=sandbox_id,
            provider=provider,
            sandbox_api_key=sandbox_api_key,
            mount_path=mount_path,
            nexus_url=nexus_url,
            nexus_api_key=nexus_api_key,
            agent_id=agent_id,
            context=context,
        )

    def sandbox_disconnect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_disconnect(
            sandbox_id=sandbox_id,
            provider=provider,
            sandbox_api_key=sandbox_api_key,
            context=context,
        )

    # --- Mount Operations (→ _mount_core_service) ---

    def add_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        readonly: bool = False,
        io_profile: str = "balanced",
        context: Any = None,
    ) -> str:
        return self._mount_core_service.add_mount(
            mount_point=mount_point,
            backend_type=backend_type,
            backend_config=backend_config,
            readonly=readonly,
            io_profile=io_profile,
            context=context,
        )

    def remove_mount(self, mount_point: str, context: Any = None) -> dict[str, Any]:
        return self._mount_core_service.remove_mount(mount_point=mount_point, context=context)

    def list_mounts(self, context: Any = None) -> builtins.list[dict[str, Any]]:
        return self._mount_core_service.list_mounts(context=context)

    def get_mount(self, mount_point: str, context: Any = None) -> dict[str, Any] | None:
        return self._mount_core_service.get_mount(mount_point=mount_point, context=context)

    def _grant_mount_owner_permission(self, mount_point: str, context: Any | None) -> None:
        """Grant direct_owner permission to the user who created the mount."""
        import logging as _logging

        _log = _logging.getLogger(__name__)
        _log.info(f"Setting up mount point: {mount_point}")

        # Create directory entry for the mount point
        try:
            self.mkdir(mount_point, parents=True, exist_ok=True)
        except Exception as e:
            _log.warning(f"Failed to create directory entry for mount {mount_point}: {e}")

        # Grant direct_owner permission to the creating user
        if context:
            from nexus.lib.context_utils import get_user_identity, get_zone_id

            subject_type, subject_id = get_user_identity(context)
            zone_id = get_zone_id(context)

            if subject_id and hasattr(self, "rebac_service"):
                try:
                    self.rebac_service.rebac_create_sync(
                        subject=(subject_type, subject_id),
                        relation="direct_owner",
                        object=("file", mount_point),
                        zone_id=zone_id,
                    )
                except Exception as e:
                    _log.warning(
                        f"Failed to grant direct_owner for {mount_point}: {type(e).__name__}: {e}"
                    )

    def _matches_patterns(
        self,
        file_path: str,
        include_patterns: builtins.list[str] | None = None,
        exclude_patterns: builtins.list[str] | None = None,
    ) -> bool:
        """Check if file path matches include/exclude patterns."""
        import fnmatch as _fnmatch

        # Check include patterns
        if include_patterns and not any(_fnmatch.fnmatch(file_path, p) for p in include_patterns):
            return False

        # Check exclude patterns
        return not (
            exclude_patterns and any(_fnmatch.fnmatch(file_path, p) for p in exclude_patterns)
        )

    # --- Search (list/glob/grep already have concrete impls below) ---

    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        show_parsed: bool = True,
        context: Any = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        if self.search_service is not None:
            return self.search_service.list(
                path=path,
                recursive=recursive,
                details=details,
                show_parsed=show_parsed,
                context=context,
                limit=limit,
                cursor=cursor,
            )
        # Kernel-only fallback: delegate directly to metadata store
        prefix = path if path != "/" else ""
        if prefix and not prefix.endswith("/"):
            prefix = prefix + "/"
        entries = self.metadata.list(prefix=prefix, recursive=recursive)
        if details:
            return [{"path": e.path, "size": e.size, "etag": e.etag} for e in entries]
        return [e.path for e in entries]

    def glob(self, pattern: str, path: str = "/", context: Any = None) -> builtins.list[str]:
        return self.search_service.glob(pattern=pattern, path=path, context=context)

    def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 100,
        search_mode: str = "auto",
        context: Any = None,
    ) -> builtins.list[dict[str, Any]]:
        return self.search_service.grep(
            pattern=pattern,
            path=path,
            file_pattern=file_pattern,
            ignore_case=ignore_case,
            max_results=max_results,
            search_mode=search_mode,
            context=context,
        )

    @staticmethod
    def _run_async(coro: Any) -> Any:
        """Run async coroutine safely, handling both running and non-running event loops.

        Uses the unified sync_bridge to avoid the ThreadPoolExecutor + asyncio.run()
        anti-pattern (Issue #1300).

        Args:
            coro: Coroutine to run

        Returns:
            Result of the coroutine
        """
        from nexus.lib.sync_bridge import run_sync

        return run_sync(coro)

    @rpc_expose(description="Backfill sparse directory index for fast listings", admin_only=True)
    def backfill_directory_index(
        self,
        prefix: str = "/",
        zone_id: str | None = None,
        _context: Any = None,  # noqa: ARG002 - RPC interface requires context param
    ) -> dict[str, Any]:
        """Backfill sparse directory index from existing files.

        Use this to populate the index for directories that existed before
        the sparse index feature was added. This improves list() performance
        from O(n) LIKE queries to O(1) index lookups.

        Args:
            prefix: Path prefix to backfill (default: "/" for all)
            zone_id: Zone ID to backfill (None for all zones)
            _context: Operation context (admin required, enforced by @rpc_expose)

        Returns:
            Dict with entries_created count
        """
        created = self.metadata.backfill_directory_index(prefix=prefix, zone_id=zone_id)
        return {"entries_created": created, "prefix": prefix}

    @rpc_expose(description="Get specific file version")
    def get_version(
        self, path: str, version: int, context: OperationContext | None = None
    ) -> bytes:
        """Get a specific version of a file."""
        return cast(
            bytes, NexusFS._run_async(self.version_service.get_version(path, version, context))
        )

    @rpc_expose(description="List file versions")
    def list_versions(
        self, path: str, context: OperationContext | None = None
    ) -> builtins.list[dict[str, Any]]:
        """List all versions of a file."""
        return cast(
            builtins.list[dict[str, Any]],
            NexusFS._run_async(self.version_service.list_versions(path, context)),
        )

    @rpc_expose(description="Rollback file to previous version")
    def rollback(self, path: str, version: int, context: OperationContext | None = None) -> None:
        """Rollback file to a previous version."""
        cast(None, NexusFS._run_async(self.version_service.rollback(path, version, context)))

    @rpc_expose(description="Compare file versions")
    def diff_versions(
        self,
        path: str,
        v1: int,
        v2: int,
        mode: str = "metadata",
        context: OperationContext | None = None,
    ) -> dict[str, Any] | str:
        """Compare two versions of a file."""
        return cast(
            dict[str, Any] | str,
            NexusFS._run_async(self.version_service.diff_versions(path, v1, v2, mode, context)),
        )

    def _get_subject_from_context(self, context: Any) -> tuple[str, str] | None:
        """Extract subject from operation context."""
        from nexus.lib.context_utils import get_subject_from_context

        return get_subject_from_context(context)

    # sync_mount, sync_mount_async, cancel_sync_job → _SERVICE_ALIASES (Issue #2033)
    async def ainitialize_semantic_search(
        self,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        api_key: str | None = None,
        chunk_size: int = 1024,
        chunk_strategy: str = "semantic",
        async_mode: bool = True,
        cache_url: str | None = None,
        embedding_cache_ttl: int = 86400 * 3,
    ) -> None:
        """Initialize semantic search engine.

        Delegates to SearchService.ainitialize_semantic_search() (Issue #1287).
        """
        if self._record_store is None:
            raise RuntimeError("Semantic search requires RecordStore (SQL engine)")

        await self.search_service.ainitialize_semantic_search(
            nx=self,
            record_store_engine=self._record_store.engine,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            api_key=api_key,
            chunk_size=chunk_size,
            chunk_strategy=chunk_strategy,
            async_mode=async_mode,
            cache_url=cache_url,
            embedding_cache_ttl=embedding_cache_ttl,
        )
        # Keep backward-compat reference on NexusFS
        self._semantic_search = self.search_service._semantic_search  # type: ignore[assignment]
        # Wire search engine into LLMService (Issue #684: DI instead of kernel access)
        if hasattr(self, "llm_service") and self._semantic_search is not None:
            self.llm_service._semantic_search_engine = self._semantic_search

    def close(self) -> None:
        """Close the filesystem and release resources."""
        # Stop DeferredPermissionBuffer first to flush pending permissions
        if hasattr(self, "_deferred_permission_buffer") and self._deferred_permission_buffer:
            self._deferred_permission_buffer.stop()

        # Wait for all parser threads to complete before closing metadata store
        # This prevents database corruption from threads writing during shutdown
        with self._parser_threads_lock:
            threads_to_join = list(self._parser_threads)

        for thread in threads_to_join:
            # Wait up to 5 seconds for each thread
            # Parser threads should complete quickly, but we don't want to hang forever
            thread.join(timeout=5.0)

        # Close Memory API session to prevent connection leak
        # The session is created lazily in the `memory` property but never closed
        if self._memory_api is not None and hasattr(self._memory_api, "session"):
            try:
                self._memory_api.session.close()
            except Exception as e:
                logger.debug("Failed to close memory API session: %s", e)

        # Close metadata store after all parsers have finished
        self.metadata.close()

        # Close record store (Services layer SQL connections)
        if self._record_store is not None:
            self._record_store.close()

        # Close ReBACManager to release database connection
        if hasattr(self, "_rebac_manager") and self._rebac_manager is not None:
            self._rebac_manager.close()

        # Close AuditStore to release database connection
        if hasattr(self, "_audit_store") and self._audit_store is not None:
            self._audit_store.close()

        # Close TokenManager to release database connection
        if hasattr(self, "_token_manager") and self._token_manager is not None:
            self._token_manager.close()

        # Close mounted backends that hold resources (e.g., OAuth connectors with SQLite)
        if hasattr(self, "router"):
            from nexus.core.protocols.connector import OAuthCapableProtocol

            for mp in self.router.get_mount_points():
                try:
                    route = self.router.route(mp, is_admin=True)
                    if isinstance(route.backend, OAuthCapableProtocol):
                        route.backend.token_manager.close()
                except Exception as e:
                    logger.debug("Failed to close backend token manager: %s", e)

    # ------------------------------------------------------------------
    # ReBAC delegation stubs (Issue #2033)
    # These delegate to rebac_service which now owns the business logic.
    # Kept on NexusFS for backward-compatibility with tests and CLI.
    # ------------------------------------------------------------------

    def rebac_create(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: Any = None,
        zone_id: str | None = None,
        context: Any = None,
        column_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a relationship tuple — delegates to rebac_service."""
        return self.rebac_service.rebac_create_sync(
            subject=subject,
            relation=relation,
            object=object,
            expires_at=expires_at,
            zone_id=zone_id,
            context=context,
            column_config=column_config,
        )

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
        context: Any = None,
    ) -> bool:
        """Check a permission — delegates to rebac_service."""
        return self.rebac_service.rebac_check_sync(
            subject=subject,
            permission=permission,
            object=object,
            zone_id=zone_id,
            context=context,
        )

    def rebac_check_batch(
        self,
        checks: builtins.list[tuple[tuple[str, str], str, tuple[str, str]]],
    ) -> builtins.list[bool]:
        """Batch check permissions — delegates to rebac_service."""
        return self.rebac_service.rebac_check_batch_sync(checks=checks)

    def rebac_delete(self, tuple_id: str) -> bool:
        """Delete a relationship tuple — delegates to rebac_service."""
        return self.rebac_service.rebac_delete_sync(tuple_id)

    def rebac_list_tuples(
        self,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
        relation_in: builtins.list[str] | None = None,
        **_kw: Any,
    ) -> builtins.list[dict[str, Any]]:
        """List relationship tuples — delegates to rebac_service."""
        return self.rebac_service.rebac_list_tuples_sync(
            subject=subject,
            relation=relation,
            object=object,
            relation_in=relation_in,
        )
