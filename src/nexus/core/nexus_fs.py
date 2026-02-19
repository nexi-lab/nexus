"""Unified filesystem implementation for Nexus."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from nexus.backends.backend import Backend
from nexus.core.exceptions import InvalidPathError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content
from nexus.raft.zone_manager import ROOT_ZONE_ID

if TYPE_CHECKING:
    from nexus.rebac.entity_registry import EntityRegistry
    from nexus.services.memory.memory_api import Memory
from nexus.core.cache_store import CacheStoreABC, NullCacheStore
from nexus.core.config import (
    CacheConfig,
    DistributedConfig,
    KernelServices,
    MemoryConfig,
    ParseConfig,
    PermissionConfig,
)
from nexus.core.filesystem import NexusFilesystem
from nexus.core.metadata import FileMetadata
from nexus.core.metastore import MetastoreABC
from nexus.core.nexus_fs_core import NexusFSCoreMixin
from nexus.core.permissions import OperationContext, Permission
from nexus.core.router import NamespaceConfig, PathRouter
from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.parsers.registry import ParserRegistry
    from nexus.parsers.types import ParseResult

# Phase 2: Service imports moved to _wire_services() as lazy imports (Issue #1519)
# NexusFSReBACMixin import removed (Issue #1387)
from nexus.storage.content_cache import ContentCache
from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


class NexusFS(  # type: ignore[misc]
    NexusFSCoreMixin,
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
        metadata_store: MetastoreABC,
        record_store: RecordStoreABC | None = None,
        cache_store: CacheStoreABC | None = None,
        *,
        is_admin: bool = False,
        custom_namespaces: list[NamespaceConfig] | None = None,
        cache: CacheConfig | None = None,
        permissions: PermissionConfig | None = None,
        distributed: DistributedConfig | None = None,
        memory: MemoryConfig | None = None,
        parsing: ParseConfig | None = None,
        services: KernelServices | None = None,
        parse_fn: Any | None = None,
        content_cache: Any | None = None,
        parser_registry: ParserRegistry | None = None,
        provider_registry: Any | None = None,
        vfs_lock_manager: Any | None = None,
    ):
        """Initialize NexusFS kernel."""
        # Config defaults
        cache = cache or CacheConfig()
        permissions = permissions or PermissionConfig()
        distributed = distributed or DistributedConfig()
        memory = memory or MemoryConfig()
        parsing = parsing or ParseConfig()
        svc = services or KernelServices()

        self._cache_config = cache
        self._perm_config = permissions
        self._distributed_config = distributed
        self._memory_config_obj = memory
        self._parse_config = parsing
        self._services = svc
        self._config: Any | None = None

        # Map config fields to flat attributes
        self._enable_memory_paging = memory.enable_paging
        self._memory_main_capacity = memory.main_capacity
        self._memory_recall_max_age_hours = memory.recall_max_age_hours
        self._enforce_permissions = permissions.enforce
        self._enforce_zone_isolation = permissions.enforce_zone_isolation
        self._audit_strict_mode = permissions.audit_strict_mode
        self.allow_admin_bypass = permissions.allow_admin_bypass
        self.auto_parse = parsing.auto_parse
        self.is_admin = is_admin

        # Content cache
        if content_cache is not None:
            backend.content_cache = content_cache
        elif cache.enable_content_cache and backend.has_root_path is True:
            backend.content_cache = ContentCache(max_size_mb=cache.content_cache_size_mb)

        # Four pillars: backend, metadata, record store, cache store
        self.backend = backend
        self.metadata: MetastoreABC = metadata_store
        self._record_store = record_store
        if record_store is not None:
            self._sql_engine = record_store.engine
            self._db_session_factory = record_store.session_factory
            self.SessionLocal = self._db_session_factory
        else:
            self._sql_engine = None
            self._db_session_factory = None
            self.SessionLocal = None
        self.cache_store: CacheStoreABC = cache_store if cache_store is not None else NullCacheStore()

        # Path router
        if svc.router is not None:
            self.router = svc.router
        else:
            self.router = PathRouter()
            if custom_namespaces:
                for ns_config in custom_namespaces:
                    self.router.register_namespace(ns_config)
        self.router.add_mount("/", self.backend, priority=0)

        # Parser registries (injected by ParsersBrick, fallback for tests)
        if parser_registry is not None:
            self.parser_registry = parser_registry
        else:
            from nexus.parsers.markitdown_parser import MarkItDownParser as _MkD
            from nexus.parsers.registry import ParserRegistry as _PR
            self.parser_registry = _PR()
            self.parser_registry.register(_MkD())
        if provider_registry is not None:
            self.provider_registry = provider_registry
        else:
            from nexus.parsers.providers.registry import ProviderRegistry as _PvR
            self.provider_registry = _PvR()
            self.provider_registry.auto_discover()

        self._virtual_view_parse_fn = parse_fn
        self._parser_threads: list[threading.Thread] = []
        self._parser_threads_lock = threading.Lock()

        # Default context for embedded mode
        self._default_context = OperationContext(
            user_id="anonymous", groups=[], zone_id=ROOT_ZONE_ID,
            agent_id=None, is_admin=is_admin, is_system=False,
            admin_capabilities=set(),
        )

        # Injected services (KernelServices)
        self._rebac_manager = svc.rebac_manager
        self._dir_visibility_cache = svc.dir_visibility_cache
        self._audit_store = svc.audit_store
        self._entity_registry = svc.entity_registry
        self._permission_enforcer = svc.permission_enforcer
        self._hierarchy_manager = svc.hierarchy_manager
        self._deferred_permission_buffer = svc.deferred_permission_buffer
        self._workspace_registry = svc.workspace_registry
        self.mount_manager = svc.mount_manager
        self._workspace_manager = svc.workspace_manager
        self._write_observer = svc.write_observer
        self._overlay_resolver = svc.overlay_resolver
        self._wallet_provisioner = svc.wallet_provisioner
        self._snapshot_service = getattr(svc, "snapshot_service", None)
        self._agent_registry = svc.agent_registry
        self._namespace_manager = svc.namespace_manager
        self._async_agent_registry = svc.async_agent_registry
        self._async_namespace_manager = svc.async_namespace_manager
        self._async_vfs_router = svc.async_vfs_router
        self._event_bus = svc.event_bus
        self._lock_manager = svc.lock_manager
        self.enable_workflows = distributed.enable_workflows
        self.workflow_engine = svc.workflow_engine
        self._api_key_creator = svc.api_key_creator

        # Lazy-init sentinels
        self._token_manager = None
        self._semantic_search = None
        self._memory_api: Memory | None = None
        self._memory_config: dict[str, str | None] = {"zone_id": None, "user_id": None, "agent_id": None}
        self._sandbox_manager: Any = None
        self.subscription_manager: Any = None
        self._coordination_client: Any = None
        self._event_client: Any = None

        # VFS lock manager
        if vfs_lock_manager is not None:
            self._vfs_lock_manager = vfs_lock_manager
        else:
            from nexus.core.lock_fast import create_vfs_lock_manager
            self._vfs_lock_manager = create_vfs_lock_manager()
        logger.info("VFS lock manager initialized (%s)", type(self._vfs_lock_manager).__name__)

        # VFS Hook Pipeline
        from nexus.core.vfs_hooks import VFSHookPipeline
        self._hook_pipeline: VFSHookPipeline = svc.hook_pipeline or VFSHookPipeline()

        # Wire self-dependent services, then register hooks
        self._wire_services()
        self._register_vfs_hooks()

        # Read-set-aware cache (Issue #1169)
        self._read_set_cache = None
        metadata_cache = getattr(self.metadata, "_cache", None)
        if metadata_cache is not None and self._cache_config.enable_metadata_cache:
            from nexus.core.read_set import ReadSetRegistry
            from nexus.storage.read_set_cache import ReadSetAwareCache
            self._read_set_registry = ReadSetRegistry()
            self._read_set_cache = ReadSetAwareCache(
                base_cache=metadata_cache, registry=self._read_set_registry,
            )
            self._read_tracking_enabled = True

        # Cache observer
        self._cache_observer = svc.cache_observer
        if self._cache_observer is None and self._read_set_cache is not None:
            from nexus.core.cache_invalidation import ReadSetCacheObserver
            self._cache_observer = ReadSetCacheObserver(self._read_set_cache)

        # Tiger Cache
        from nexus.services.tiger_cache_manager import TigerCacheManager
        self._tiger_cache_manager = TigerCacheManager(
            rebac_manager=self._rebac_manager, metadata_store=self.metadata,
            default_zone_id=self._default_context.zone_id or "root",
            process_queue_fn=getattr(self, "process_tiger_cache_queue", None),
            warm_cache_fn=getattr(self, "warm_tiger_cache", None),
        )
        self._tiger_cache_manager.initialize()

    def _wire_services(self) -> None:
        """Wire services that require a reference to self (NexusFS).

        Delegates to nexus.core.service_wiring.wire_services() (Issue #2033).
        """
        from nexus.core.service_wiring import wire_services

        wire_services(self)

    @property
    def _service_extras(self) -> dict[str, Any]:
        """Server layer reads typed service fields as a dict interface."""
        _fields = (
            "observability_subsystem",
            "chunked_upload_service",
            "manifest_resolver",
            "manifest_metrics",
            "rebac_circuit_breaker",
            "tool_namespace_middleware",
            "resiliency_manager",
            "delivery_worker",
        )
        return {
            k: getattr(self._services, k) for k in _fields if getattr(self._services, k) is not None
        }

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

    def _get_created_by(self, context: OperationContext | dict | None = None) -> str | None:
        """Get the created_by value for version history tracking."""
        from nexus.core.context_utils import get_created_by
        return get_created_by(context, self._default_context)

    def _get_routing_params(
        self, context: OperationContext | dict | None = None
    ) -> tuple[str | None, str | None, bool]:
        """Extract (zone_id, agent_id, is_admin) from context for router.route()."""
        if context is None:
            return self._default_context.zone_id, self._default_context.agent_id, self._default_context.is_admin
        if isinstance(context, dict):
            return (
                context.get("zone_id", self._default_context.zone_id),
                context.get("agent_id", self._default_context.agent_id),
                context.get("is_admin", self.is_admin),
            )
        return context.zone_id, context.agent_id, getattr(context, "is_admin", self.is_admin)

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

    def _get_memory_api(self, context: dict | None = None) -> Memory:
        """Get Memory API instance with context-specific configuration."""
        return self._memory_provider.get_for_context(context)

    def _parse_context(self, context: OperationContext | dict | None = None) -> OperationContext:
        """Parse context dict or OperationContext into OperationContext."""
        from nexus.core.context_utils import parse_context
        return parse_context(context)

    def _ensure_entity_registry(self) -> EntityRegistry:
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
            zone_id=ctx.zone_id or "root",  # P0 SECURITY: Set zone_id
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
            zone_id=ctx.zone_id,
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
        route.backend.mkdir(
            route.backend_path, parents=parents, exist_ok=True, context=ctx
        ).unwrap()

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
                            parent_dir, zone_id=ctx.zone_id or "root"
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
                    f"mkdir: Calling ensure_parent_tuples for {path}, zone_id={ctx.zone_id or 'default'}"
                )
                created_count = self._hierarchy_manager.ensure_parent_tuples(
                    path, zone_id=ctx.zone_id or "root"
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
                    zone_id=ctx.zone_id or "root",
                )
                logger.debug(f"mkdir: Granted direct_owner permission to {ctx.user_id} for {path}")
            except Exception as e:
                logger.warning(f"Failed to grant direct_owner permission for {path}: {e}")

        # Issue #1331: Publish dir_create event to event bus
        self._publish_file_event(
            event_type="dir_create",
            path=path,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
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
            zone_id=ctx.zone_id,
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
                        route.backend.delete_content(file_meta.etag).unwrap()
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
            route.backend.rmdir(route.backend_path, recursive=recursive).unwrap()

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
                zone_id=ctx.zone_id,
                is_admin=ctx.is_admin,
                check_write=False,
            )
            # Check if it's an explicit directory in the backend
            if route.backend.is_directory(route.backend_path).unwrap():
                return True
            # Return cached implicit directory status
            return is_implicit_dir
        except (InvalidPathError, Exception):
            return False

    @rpc_expose(description="Get available namespaces")
    def get_available_namespaces(self) -> builtins.list[str]:
        """Get list of available namespace directories."""
        import time

        start = time.time()
        logger.warning(
            f"[PERF-IMPL] get_available_namespaces: START, is_admin={self.is_admin}, namespace_count={len(self.router._namespaces)}"
        )

        namespaces = []

        for name, config in self.router._namespaces.items():
            # Include namespace if it's not admin-only OR user is admin
            # Note: We show all namespaces regardless of zone_id.
            # Zone filtering happens when accessing files within the namespace.
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
                zone_id, _agent_id, is_admin = self._get_routing_params(context)
                route = self.router.route(
                    path.rstrip("/"),
                    zone_id=zone_id,
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

    # Service forwarding: __getattr__ routes method calls to services (Issue #2033)

    _SERVICE_METHODS: dict[str, str] = {
        # WorkspaceRPCService
        'workspace_snapshot': '_workspace_rpc_service',
        'workspace_restore': '_workspace_rpc_service',
        'workspace_log': '_workspace_rpc_service',
        'workspace_diff': '_workspace_rpc_service',
        'snapshot_begin': '_workspace_rpc_service',
        'snapshot_commit': '_workspace_rpc_service',
        'snapshot_rollback': '_workspace_rpc_service',
        'load_workspace_memory_config': '_workspace_rpc_service',
        'register_workspace': '_workspace_rpc_service',
        'unregister_workspace': '_workspace_rpc_service',
        'update_workspace': '_workspace_rpc_service',
        'list_workspaces': '_workspace_rpc_service',
        'get_workspace_info': '_workspace_rpc_service',
        'register_memory': '_workspace_rpc_service',
        'unregister_memory': '_workspace_rpc_service',
        'list_registered_memories': '_workspace_rpc_service',
        'get_memory_info': '_workspace_rpc_service',
        # AgentRPCService
        'register_agent': '_agent_rpc_service',
        'update_agent': '_agent_rpc_service',
        'list_agents': '_agent_rpc_service',
        'get_agent': '_agent_rpc_service',
        'delete_agent': '_agent_rpc_service',
        # UserProvisioningService
        'provision_user': '_user_provisioning_service',
        'deprovision_user': '_user_provisioning_service',
        # SandboxRPCService
        'sandbox_create': '_sandbox_rpc_service',
        'sandbox_run': '_sandbox_rpc_service',
        'sandbox_validate': '_sandbox_rpc_service',
        'sandbox_pause': '_sandbox_rpc_service',
        'sandbox_resume': '_sandbox_rpc_service',
        'sandbox_stop': '_sandbox_rpc_service',
        'sandbox_list': '_sandbox_rpc_service',
        'sandbox_status': '_sandbox_rpc_service',
        'sandbox_get_or_create': '_sandbox_rpc_service',
        'sandbox_connect': '_sandbox_rpc_service',
        'sandbox_disconnect': '_sandbox_rpc_service',
        # MetadataExportService
        'export_metadata': '_metadata_export_service',
        'import_metadata': '_metadata_export_service',
        # MountCoreService
        'add_mount': '_mount_core_service',
        'remove_mount': '_mount_core_service',
        'list_connectors': '_mount_core_service',
        'list_mounts': '_mount_core_service',
        'get_mount': '_mount_core_service',
        'has_mount': '_mount_core_service',
        # MountPersistService
        'save_mount': '_mount_persist_service',
        'list_saved_mounts': '_mount_persist_service',
        'load_mount': '_mount_persist_service',
        'delete_saved_mount': '_mount_persist_service',
        # SearchService (list/glob/grep are thin forwarders, not __getattr__)
        # asemantic_search* are in _SERVICE_ALIASES (name transformation: a-prefix removed)
        'glob_batch': 'search_service',
        # TaskQueueService
        'get_task': 'task_queue_service',
        'cancel_task': 'task_queue_service',
    }

    # Special aliases where service method name differs
    _SERVICE_ALIASES: dict[str, tuple[str, str]] = {
        'list_memories': ('_workspace_rpc_service', 'list_registered_memories'),
        'sandbox_available': ('_sandbox_rpc_service', 'sandbox_available'),
        'get_sync_job': ('_sync_job_service', 'get_job'),
        'list_sync_jobs': ('_sync_job_service', 'list_jobs'),
        'load_all_saved_mounts': ('_mount_persist_service', 'load_all_mounts'),
        # Dir visibility cache: NexusFS method names → cache method names
        'get_dir_visibility_cache_metrics': ('_dir_visibility_cache', 'get_metrics'),
        'clear_dir_visibility_cache': ('_dir_visibility_cache', 'clear'),
        # SearchService async methods: a-prefix removed when calling service
        'asemantic_search': ('search_service', 'semantic_search'),
        'asemantic_search_index': ('search_service', 'semantic_search_index'),
        'asemantic_search_stats': ('search_service', 'semantic_search_stats'),
        # SyncService / SyncJobService (Issue #2033)
        'sync_mount': ('_sync_service', 'sync_mount_flat'),
        'sync_mount_async': ('_sync_job_service', 'sync_mount_async'),
        'cancel_sync_job': ('_sync_job_service', 'cancel_sync_job'),
        # VersionService async methods (Issue #2033)
        'aget_version': ('version_service', 'get_version'),
        'alist_versions': ('version_service', 'list_versions'),
        'arollback': ('version_service', 'rollback'),
        'adiff_versions': ('version_service', 'diff_versions'),
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
        svc_attr = NexusFS._SERVICE_METHODS.get(name)
        if svc_attr is not None:
            svc = self.__dict__.get(svc_attr)
            if svc is not None:
                return getattr(svc, name)

        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    # Abstract method forwarders (ABCMeta requires real definitions)

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
        return self.search_service.list(
            path=path, recursive=recursive, details=details,
            show_parsed=show_parsed, context=context,
            limit=limit, cursor=cursor,
        )

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
            pattern=pattern, path=path, file_pattern=file_pattern,
            ignore_case=ignore_case, max_results=max_results,
            search_mode=search_mode, context=context,
        )

    @rpc_expose(description="Batch get content IDs for multiple paths")
    def batch_get_content_ids(self, paths: builtins.list[str]) -> dict[str, str | None]:
        """Get content hashes for multiple paths in a single query."""
        return self.metadata.batch_get_content_ids(paths)

    async def parse(
        self,
        path: str,
        store_result: bool = True,
    ) -> ParseResult:
        """Parse a file using the appropriate parser."""
        # Validate path
        path = self._validate_path(path)

        # Read file content with system bypass for background parsing
        # Auto-parse is a system operation that should not be subject to user permissions
        parse_ctx = OperationContext(
            user_id="system_parser", groups=[], zone_id=None, is_system=True
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
            await asyncio.to_thread(
                self.metadata.set_file_metadata, path, "parsed_text", result.text
            )
            await asyncio.to_thread(
                self.metadata.set_file_metadata, path, "parsed_at", datetime.now(UTC).isoformat()
            )
            await asyncio.to_thread(
                self.metadata.set_file_metadata, path, "parser_name", parser.name
            )

        return result

    @staticmethod
    def _run_async(coro: Any) -> Any:
        """Run async coroutine safely (Issue #1300)."""
        from nexus.core.sync_bridge import run_sync

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
    def get_version(self, path: str, version: int, context: OperationContext | None = None) -> bytes:
        """Get a specific version of a file."""
        return cast(bytes, NexusFS._run_async(self.version_service.get_version(path, version, context)))

    @rpc_expose(description="List file versions")
    def list_versions(self, path: str, context: OperationContext | None = None) -> list[dict[str, Any]]:
        """List all versions of a file."""
        return cast(list[dict[str, Any]], NexusFS._run_async(self.version_service.list_versions(path, context)))

    @rpc_expose(description="Rollback file to previous version")
    def rollback(self, path: str, version: int, context: OperationContext | None = None) -> None:
        """Rollback file to a previous version."""
        cast(None, NexusFS._run_async(self.version_service.rollback(path, version, context)))

    @rpc_expose(description="Compare file versions")
    def diff_versions(self, path: str, v1: int, v2: int, mode: str = "metadata", context: OperationContext | None = None) -> dict[str, Any] | str:
        """Compare two versions of a file."""
        return cast(dict[str, Any] | str, NexusFS._run_async(self.version_service.diff_versions(path, v1, v2, mode, context)))

    def _get_subject_from_context(self, context: Any) -> tuple[str, str] | None:
        """Extract subject from operation context."""
        from nexus.core.context_utils import get_subject_from_context
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

        # Stop Tiger Cache background worker first
        if hasattr(self, "_tiger_cache_manager"):
            self._tiger_cache_manager.stop_worker()

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

            for mount in self.router.list_mounts():
                try:
                    if isinstance(mount.backend, OAuthCapableProtocol):
                        mount.backend.token_manager.close()
                except Exception as e:
                    logger.debug("Failed to close backend token manager: %s", e)
