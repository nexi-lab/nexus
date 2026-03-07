"""Unified filesystem implementation for Nexus."""

import builtins
import contextlib
import logging
import time
from collections.abc import Callable, Generator, Iterator
from datetime import UTC, datetime
from typing import Any

from nexus.contracts.cache_store import CacheStoreABC, NullCacheStore
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import (
    BackendError,
    ConflictError,
    InvalidPathError,
    NexusFileNotFoundError,
)
from nexus.contracts.filesystem.filesystem_abc import NexusFilesystemABC
from nexus.contracts.metadata import FileMetadata
from nexus.contracts.types import OperationContext, Permission
from nexus.core.config import (
    BrickServices,
    CacheConfig,
    DistributedConfig,
    KernelServices,
    MemoryConfig,
    ParseConfig,
    PermissionConfig,
    SystemServices,
)
from nexus.core.file_events import FileEvent, FileEventType
from nexus.core.hash_fast import hash_content
from nexus.core.metastore import MetastoreABC
from nexus.core.router import PathRouter
from nexus.lib.path_utils import validate_path
from nexus.lib.rpc_decorator import rpc_expose
from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


class NexusFS(  # type: ignore[misc]
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
            from nexus.bricks.parsers.markitdown_parser import MarkItDownParser as _MkD
            from nexus.bricks.parsers.registry import ParserRegistry as _PR

            self.parser_registry = _PR()
            self.parser_registry.register(_MkD())
        if brk_svc.provider_registry is not None:
            self.provider_registry = brk_svc.provider_registry
        else:
            from nexus.bricks.parsers.providers.registry import ProviderRegistry as _PvR

            self.provider_registry = _PvR()
            self.provider_registry.auto_discover()

        self._virtual_view_parse_fn = brk_svc.parse_fn

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
        self._sandbox_manager: Any = None
        self._coordination_client: Any = None
        self._event_client: Any = None

        # VFS lock manager — kernel-internal, NOT injected via DI.
        # Analogous to Linux i_rwsem: always present, created by kernel at init.
        # Protects same-process coroutine concurrency on sys_write/sys_read.
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
        self.oauth_service: Any = None
        self.search_service: Any = None
        self.share_link_service: Any = None
        self.events_service: Any = None
        # Kernel notification dispatch (INTERCEPT + OBSERVE).
        # Kernel owns dispatch infrastructure — creates empty callback lists.
        # Factory registers hooks at boot (KERNEL-ARCHITECTURE §3).
        from nexus.core.kernel_dispatch import KernelDispatch

        self._dispatch: KernelDispatch = KernelDispatch()

    # Services wired by factory via bind_wired_services() (Issue #1381).
    # See nexus.factory.service_routing.bind_wired_services().

    @property
    def config(self) -> Any | None:
        """Public accessor for the runtime configuration object."""
        return self._config

    @property
    def rebac_manager(self) -> Any | None:
        """Public accessor for the ReBACManager instance."""
        return getattr(self, "_rebac_manager", None)

    @property
    def memory(self) -> Any:
        """Get Memory API instance (lazy init on first access)."""
        return self._memory_provider.get_or_create()

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

    def _parse_context(self, context: OperationContext | dict | None = None) -> OperationContext:
        """Parse context dict or OperationContext into OperationContext."""
        from nexus.lib.context_utils import parse_context

        return parse_context(context)

    def _validate_path(self, path: str, allow_root: bool = False) -> str:
        """Validate and normalize virtual path. Delegates to lib/path_utils."""
        return validate_path(path, allow_root=allow_root)

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

    def _ensure_parent_directories(self, path: str, ctx: OperationContext) -> None:
        """Create metadata entries for all parent directories that don't exist.

        Walks from the immediate parent of *path* upward toward ``/``, collecting
        every path that has no metastore entry, then creates directory metadata
        entries from top to bottom (shallowest first) so that ``sys_readdir``
        lists them correctly.

        This is factored out of ``sys_mkdir`` so it can be called both on the
        normal code-path *and* on the early-return path when the target path
        already exists (e.g. a DT_MOUNT entry written by ``PathRouter.add_mount``).
        """
        parent_path = self._get_parent_path(path)
        parents_to_create: list[str] = []

        while parent_path and parent_path != "/":
            if not self.metadata.exists(parent_path):
                parents_to_create.append(parent_path)
            else:
                break
            parent_path = self._get_parent_path(parent_path)

        for parent_dir in reversed(parents_to_create):
            self._create_directory_metadata(parent_dir, context=ctx)
            if hasattr(self, "_hierarchy_manager") and self._hierarchy_manager is not None:
                try:
                    logger.debug(
                        f"mkdir: Creating parent tuples for intermediate dir: {parent_dir}"
                    )
                    self._hierarchy_manager.ensure_parent_tuples(
                        parent_dir, zone_id=ctx.zone_id or ROOT_ZONE_ID
                    )
                except Exception as e:
                    logger.warning(f"mkdir: Failed to create parent tuples for {parent_dir}: {e}")

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
    def sys_mkdir(
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

        # PRE-INTERCEPT: pre-mkdir hooks (Issue #899)
        from nexus.contracts.vfs_hooks import MkdirHookContext

        self._dispatch.intercept_pre_mkdir(MkdirHookContext(path=path, context=ctx))

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
            # If exist_ok=True (or parents=True) and directory exists, we still
            # need to create parent directory metadata entries.  DT_MOUNT entries
            # are created by PathRouter.add_mount() *before* sys_mkdir is called,
            # so the target path already exists in metastore but the parent
            # directories (e.g. /mnt for /mnt/test) have no metadata yet.
            if existing is not None:
                if parents:
                    self._ensure_parent_directories(path, ctx)
                return

        # Create directory in backend
        route.backend.mkdir(route.backend_path, parents=parents, exist_ok=True, context=ctx)

        # Create metadata entries for parent directories if parents=True
        if parents:
            self._ensure_parent_directories(path, ctx)

        # Create explicit metadata entry for the directory
        self._create_directory_metadata(path, context=ctx)

        # P0-3: Create parent relationship tuples for directory inheritance
        # This enables granting access to /workspace to automatically grant access to subdirectories

        logger.debug(
            f"mkdir: Checking for hierarchy_manager: hasattr={hasattr(self, '_hierarchy_manager')}"
        )

        ctx = context or self._default_context

        if hasattr(self, "_hierarchy_manager") and self._hierarchy_manager is not None:
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
                agent_id=ctx.agent_id,
                user_id=ctx.user_id,
            )
        )

    @rpc_expose(description="Remove directory")
    def sys_rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | None = None,
        *,
        subject: tuple[str, str] | None = None,
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
        from nexus.contracts.vfs_hooks import RmdirHookContext

        self._dispatch.intercept_pre_rmdir(RmdirHookContext(path=path, context=ctx))
        logger.debug(f"  -> PRE-INTERCEPT passed for rmdir on {path}")

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
                agent_id=ctx.agent_id,
                user_id=ctx.user_id,
            )
        )

    @rpc_expose(description="Check if path is a directory")
    def sys_is_directory(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> bool:
        """Check if path is a directory (explicit or implicit).

        A path is considered a directory if any of the following hold:
        - It is an implicit directory (has children in metastore)
        - Its metastore entry has ``entry_type`` DT_DIR or DT_MOUNT
        - The backend reports it as a directory
        """
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

            # Check metastore entry_type: DT_DIR and DT_MOUNT are directories.
            # This is a fast ~5 us redb read that avoids calling into the backend.
            meta = self.metadata.get(path)
            if meta is not None and (meta.is_dir or meta.is_mount):
                return True

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
    def sys_stat(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any] | None:
        """Get file metadata without reading content (FUSE getattr)."""
        ctx = context or self._default_context
        normalized = self._validate_path(path, allow_root=True)

        # Check if it's a directory first
        is_dir = self.sys_is_directory(normalized, context=ctx)

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
            "etag": file_meta.etag,
            "mime_type": file_meta.mime_type or "application/octet-stream",
            "created_at": file_meta.created_at.isoformat() if file_meta.created_at else None,
            "modified_at": file_meta.modified_at.isoformat() if file_meta.modified_at else None,
            "is_directory": False,
            "owner": ctx.user_id,
            "group": ctx.user_id,
            "mode": 0o644,  # -rw-r--r--
            "version": file_meta.version,
            "zone_id": file_meta.zone_id,
        }

    @rpc_expose(description="Update file metadata attributes")
    def sys_setattr(
        self,
        path: str,
        context: OperationContext | None = None,
        **attrs: Any,
    ) -> dict[str, Any]:
        """Update file metadata attributes (chmod/chown/utimensat analog).

        Args:
            path: Virtual file path.
            context: Operation context.
            **attrs: Metadata attributes to update (e.g., mime_type, owner_id).

        Returns:
            Dict with path and list of updated attribute names.

        Raises:
            NexusFileNotFoundError: If file does not exist.
        """
        path = self._validate_path(path)
        ctx = context or self._default_context

        # Block writes during zone deprovisioning
        self._check_zone_writable(ctx)

        meta = self.metadata.get(path)
        if meta is None:
            raise NexusFileNotFoundError(path)

        from dataclasses import fields, replace

        valid_fields = {f.name for f in fields(meta)}
        valid_attrs = {k: v for k, v in attrs.items() if k in valid_fields}
        if not valid_attrs:
            return {"path": path, "updated": []}

        new_meta = replace(meta, **valid_attrs)
        self.metadata.put(new_meta)
        return {"path": path, "updated": list(valid_attrs.keys())}

    @rpc_expose(description="Get ETag (content hash) for HTTP caching")
    def get_etag(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> str | None:
        """Get content hash for HTTP If-None-Match checks."""
        _ = context  # Reserved for future use
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

    # =================================================================
    # Core VFS File Operations (Issue #899)
    # =================================================================

    def _get_overlay_config(self, path: str) -> Any:
        """Get overlay config for a path, if overlay is active.

        Issue #1264: Looks up the workspace containing this path and returns
        its OverlayConfig if overlay is enabled.

        Args:
            path: File path to check

        Returns:
            OverlayConfig if overlay active for this path, None otherwise
        """
        registry = getattr(self, "_workspace_registry", None)
        if registry is None:
            return None

        ws_config = registry.find_workspace_for_path(path)
        if ws_config is None:
            return None

        # Check if workspace has overlay metadata
        overlay_data = ws_config.metadata.get("overlay_config")
        if overlay_data is None:
            return None

        from nexus.contracts.overlay_config import OverlayConfig

        return OverlayConfig(
            enabled=overlay_data.get("enabled", False),
            base_manifest_hash=overlay_data.get("base_manifest_hash"),
            workspace_path=ws_config.path,
            agent_id=overlay_data.get("agent_id"),
        )

    # =========================================================================
    # VFS I/O Lock — kernel-internal path-level read/write protection
    # =========================================================================

    _VFS_LOCK_TIMEOUT_MS = 5000  # 5s — generous for kernel I/O serialization

    def _vfs_acquire(self, path: str, mode: str) -> int:
        """Acquire VFS I/O lock, raising LockTimeout on failure.

        Args:
            path: Virtual path to lock.
            mode: "read" (shared) or "write" (exclusive).

        Returns:
            Lock handle (positive int) for release.

        Raises:
            LockTimeout: If lock cannot be acquired within timeout.
        """
        handle = self._vfs_lock_manager.acquire(path, mode, timeout_ms=self._VFS_LOCK_TIMEOUT_MS)
        if handle == 0:
            from nexus.contracts.exceptions import LockTimeout

            raise LockTimeout(
                path=path,
                timeout=self._VFS_LOCK_TIMEOUT_MS / 1000,
                message=f"VFS {mode} lock timeout on {path}",
            )
        return handle

    @contextlib.contextmanager
    def _vfs_locked(self, path: str, mode: str) -> Generator[int, None, None]:
        """Context manager for VFS I/O lock — symmetric acquire/release.

        Usage::

            with self._vfs_locked(path, "write"):
                backend.write_content(...)
                metadata.put(...)
            # Event emission AFTER lock release (like Linux inotify after i_rwsem)
        """
        handle = self._vfs_acquire(path, mode)
        try:
            yield handle
        finally:
            self._vfs_lock_manager.release(handle)

    @rpc_expose(description="Read file content")
    def sys_read(
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> bytes:
        """Read file content as bytes (POSIX pread(2)).

        Kernel primitive — always returns raw bytes. For metadata or parsed
        content, use the convenience ``read()`` method.

        Args:
            path: Virtual path to read (supports memory virtual paths).
            count: Max bytes to read (None = entire file).
            offset: Byte offset to start reading from.
            context: Optional operation context for permission checks.

        Returns:
            File content as bytes.

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If read operation fails
            AccessDeniedError: If access is denied based on zone isolation
            PermissionError: If user doesn't have read permission
        """
        path = self._validate_path(path)
        # Normalize context dict to OperationContext dataclass (CLI passes dicts)
        context = self._parse_context(context)

        # PRE-DISPATCH: virtual path resolvers (Issue #889)
        _handled, _resolve_hint = self._dispatch.resolve_read(
            path, return_metadata=False, context=context
        )
        if _handled:
            # Normalize resolver results to bytes
            if isinstance(_resolve_hint, dict):
                _resolve_hint = _resolve_hint.get("content", b"")
            if isinstance(_resolve_hint, str):
                _resolve_hint = _resolve_hint.encode("utf-8")
            content = _resolve_hint
            if offset or count is not None:
                content = (
                    content[offset : offset + count] if count is not None else content[offset:]
                )
            return content

        # PRE-INTERCEPT: pre-read hooks (Issue #899)
        perm_check_start = time.time()
        from nexus.contracts.vfs_hooks import ReadHookContext as _RHC

        self._dispatch.intercept_pre_read(_RHC(path=path, context=context))
        perm_check_elapsed = time.time() - perm_check_start

        # Log slow pre-intercept
        if perm_check_elapsed > 0.010:  # >10ms
            logger.warning(
                f"[READ-PERF] SLOW pre-intercept for {path}: {perm_check_elapsed * 1000:.1f}ms"
            )

        # Normal file path - proceed with regular read
        zone_id, agent_id, is_admin = self._get_routing_params(context)
        route = self.router.route(
            path,
            is_admin=is_admin,
            check_write=False,
        )

        # Add backend_path to context for path-based connectors
        from dataclasses import replace

        if context:
            read_context = replace(context, backend_path=route.backend_path, virtual_path=path)
        else:
            # Create minimal context with just backend_path for connectors
            from nexus.contracts.types import OperationContext

            read_context = OperationContext(
                user_id="anonymous", groups=[], backend_path=route.backend_path, virtual_path=path
            )

        # Check if backend is a dynamic API-backed connector or external content source.
        # TODO(#899): Move this bypass logic out of kernel into service/composition layer.
        _caps: frozenset[str] = getattr(route.backend, "capabilities", frozenset())
        is_dynamic_connector = (
            route.backend.user_scoped is True and route.backend.has_token_manager is True
        ) or "external_content" in _caps

        if is_dynamic_connector:
            # Dynamic connector - read directly from backend without metadata check
            # The backend handles authentication and API calls (no VFS lock needed)
            content = route.backend.read_content("", context=read_context)

            if offset or count is not None:
                content = (
                    content[offset : offset + count] if count is not None else content[offset:]
                )
            return content

        # VFS I/O Lock: shared read lock around metadata check + backend read.
        # Prevents reading while a concurrent writer mutates the same path.
        # Like Linux i_rwsem: held for I/O duration only, released before observers.
        with self._vfs_locked(path, "read"):
            # Check if file exists in metadata (for regular backends)
            # _resolve_hint may carry prefetched metadata from a resolver
            meta = _resolve_hint if _resolve_hint is not None else self.metadata.get(path)

            # Issue #1264: Overlay resolution — check base layer if upper layer has no entry
            if (meta is None or meta.etag is None) and getattr(self, "_overlay_resolver", None):
                overlay_config = self._get_overlay_config(path)
                if overlay_config:
                    meta = self._overlay_resolver.resolve_read(path, overlay_config)

            if meta is None or meta.etag is None:
                raise NexusFileNotFoundError(path)

            # Issue #1264: Reject whiteout markers (file was deleted in overlay)
            if getattr(self, "_overlay_resolver", None) and self._overlay_resolver.is_whiteout(
                meta
            ):
                raise NexusFileNotFoundError(path)

            content = route.backend.read_content(meta.etag, context=read_context)

        # --- Lock released — post-read processing (like Linux inotify after i_rwsem) ---

        # Issue #900: Unified INTERCEPT for read (dynamic viewer, tracking, etc.)
        if self._dispatch.read_hook_count > 0:
            from nexus.contracts.vfs_hooks import ReadHookContext

            _read_ctx = ReadHookContext(
                path=path,
                context=context,
                zone_id=zone_id,
                agent_id=agent_id,
                metadata=meta,
                content=content,
                content_hash=meta.etag,
            )
            self._dispatch.intercept_post_read(_read_ctx)
            content = _read_ctx.content or content  # hooks may have filtered content

        # Apply count/offset slicing (POSIX pread semantics)
        if offset or count is not None:
            content = content[offset : offset + count] if count is not None else content[offset:]

        return content

    @rpc_expose(description="Read multiple files in a single RPC call")
    def read_bulk(
        self,
        paths: list[str],
        context: OperationContext | None = None,
        return_metadata: bool = False,
        skip_errors: bool = True,
    ) -> dict[str, bytes | dict[str, Any] | None]:
        """
        Read multiple files in a single RPC call for improved performance.

        This method is optimized for bulk operations like grep, where many files
        need to be read. It batches permission checks and reduces RPC overhead.

        Args:
            paths: List of virtual paths to read
            context: Optional operation context for permission checks
            return_metadata: If True, return dicts with content and metadata
            skip_errors: If True, skip files that can't be read and return None.
                        If False, raise exception on first error.

        Returns:
            Dict mapping path -> content (or None if skip_errors=True and read failed)
            If return_metadata=False: {path: bytes}
            If return_metadata=True: {path: {content, etag, version, ...}}

        Performance:
            - Single RPC call instead of N calls
            - Batch permission checks (one DB query instead of N)
            - Reduced network round trips
            - Expected speedup: 2-5x for 50+ files

        Examples:
            >>> # Read multiple files at once
            >>> results = nx.read_bulk(["/file1.txt", "/file2.txt", "/file3.txt"])
            >>> print(results["/file1.txt"])  # b'content'
            >>> print(results["/file2.txt"])  # b'content' or None if failed

            >>> # With metadata
            >>> results = nx.read_bulk(["/file1.txt"], return_metadata=True)
            >>> print(results["/file1.txt"]["content"])
            >>> print(results["/file1.txt"]["etag"])
        """
        import time

        bulk_start = time.time()
        results: dict[str, bytes | dict[str, Any] | None] = {}

        # Validate all paths
        validated_paths = []
        for path in paths:
            try:
                validated_path = self._validate_path(path)
                validated_paths.append(validated_path)
            except Exception as exc:
                logger.debug("Path validation failed in read_bulk for %s: %s", path, exc)
                if skip_errors:
                    results[path] = None
                    continue
                raise

        if not validated_paths:
            return results

        # Batch permission check using filter_list
        perm_start = time.time()
        allowed_set: set[str]
        if not self._enforce_permissions:  # type: ignore[attr-defined]  # allowed
            # Skip permission check if permissions are disabled
            allowed_set = set(validated_paths)
        else:
            try:
                # Use the existing bulk permission check from list()
                # Note: filter_list assumes READ permission, which is what we want
                from nexus.contracts.types import OperationContext

                ctx = context if context is not None else self._default_context
                assert isinstance(ctx, OperationContext), "Context must be OperationContext"
                allowed_paths = self._permission_enforcer.filter_list(validated_paths, ctx)
                allowed_set = set(allowed_paths)
            except Exception as e:
                logger.error(f"[READ-BULK] Permission check failed: {e}")
                if not skip_errors:
                    raise
                # If skip_errors, assume no files are allowed
                allowed_set = set()

        perm_elapsed = time.time() - perm_start
        logger.info(
            f"[READ-BULK] Permission check: {len(allowed_set)}/{len(validated_paths)} allowed in {perm_elapsed * 1000:.1f}ms"
        )

        # Mark denied files
        for path in validated_paths:
            if path not in allowed_set:
                results[path] = None

        # Read allowed files
        read_start = time.time()
        zone_id, agent_id, is_admin = self._get_routing_params(context)

        # Group paths by backend for potential bulk optimization
        # Use get_batch for metadata lookup (single query instead of N queries)
        path_info: dict[str, tuple[FileMetadata, Any]] = {}  # path -> (meta, route)
        backend_paths: dict[Any, list[str]] = {}  # backend -> [paths]

        # Batch metadata lookup
        meta_start = time.time()
        batch_meta = self.metadata.get_batch(list(allowed_set))
        meta_elapsed = (time.time() - meta_start) * 1000
        logger.info(
            f"[READ-BULK] Batch metadata lookup: {len(batch_meta)} paths in {meta_elapsed:.1f}ms"
        )

        # Process metadata and group by backend
        route_start = time.time()
        for path in allowed_set:
            try:
                meta = batch_meta.get(path)
                if meta is None or meta.etag is None:
                    if skip_errors:
                        results[path] = None
                        continue
                    raise NexusFileNotFoundError(path)

                route = self.router.route(
                    path,
                    is_admin=is_admin,
                    check_write=False,
                )
                path_info[path] = (meta, route)

                # Group by backend
                backend = route.backend
                if backend not in backend_paths:
                    backend_paths[backend] = []
                backend_paths[backend].append(path)
            except Exception as e:
                logger.warning(f"[READ-BULK] Failed to route {path}: {type(e).__name__}: {e}")
                if skip_errors:
                    results[path] = None
                else:
                    raise

        route_elapsed = (time.time() - route_start) * 1000
        logger.info(f"[READ-BULK] Routing: {len(path_info)} paths in {route_elapsed:.1f}ms")

        # Try bulk read for backends that support it (CacheConnectorMixin)
        for backend, paths_for_backend in backend_paths.items():
            if hasattr(backend, "read_bulk_from_cache") and len(paths_for_backend) > 1:
                # Use bulk cache lookup
                logger.info(
                    f"[READ-BULK] Using bulk cache for {len(paths_for_backend)} files on {type(backend).__name__}"
                )
                try:
                    cache_entries = backend.read_bulk_from_cache(paths_for_backend, original=True)

                    # Process cache hits
                    paths_needing_backend: list[str] = []
                    for path in paths_for_backend:
                        entry = cache_entries.get(path)
                        if entry and not entry.stale and entry.content_binary:
                            content = entry.content_binary
                            meta, route = path_info[path]
                            assert meta.etag is not None  # Guaranteed by check above
                            if return_metadata:
                                results[path] = {
                                    "content": content,
                                    "etag": meta.etag,
                                    "version": meta.version,
                                    "modified_at": meta.modified_at,
                                    "size": len(content),
                                }
                            else:
                                results[path] = content
                        else:
                            paths_needing_backend.append(path)

                    # Fall back to individual reads for cache misses
                    for path in paths_needing_backend:
                        try:
                            meta, route = path_info[path]
                            assert meta.etag is not None  # Guaranteed by check above
                            read_context = context
                            if context:
                                from dataclasses import replace

                                read_context = replace(context, backend_path=route.backend_path)
                            content = route.backend.read_content(meta.etag, context=read_context)
                            if return_metadata:
                                results[path] = {
                                    "content": content,
                                    "etag": meta.etag,
                                    "version": meta.version,
                                    "modified_at": meta.modified_at,
                                    "size": len(content),
                                }
                            else:
                                results[path] = content
                        except Exception as e:
                            logger.warning(
                                f"[READ-BULK] Failed to read {path}: {type(e).__name__}: {e}"
                            )
                            if skip_errors:
                                results[path] = None
                            else:
                                raise
                except Exception as e:
                    logger.warning(
                        f"[READ-BULK] Bulk cache failed, falling back to individual reads: {e}"
                    )
                    # Fall back to individual reads
                    for path in paths_for_backend:
                        try:
                            meta, route = path_info[path]
                            assert meta.etag is not None  # Guaranteed by check above
                            read_context = context
                            if context:
                                from dataclasses import replace

                                read_context = replace(context, backend_path=route.backend_path)
                            content = route.backend.read_content(meta.etag, context=read_context)
                            if return_metadata:
                                results[path] = {
                                    "content": content,
                                    "etag": meta.etag,
                                    "version": meta.version,
                                    "modified_at": meta.modified_at,
                                    "size": len(content),
                                }
                            else:
                                results[path] = content
                        except Exception as e:
                            logger.warning(
                                f"[READ-BULK] Failed to read {path}: {type(e).__name__}: {e}"
                            )
                            if skip_errors:
                                results[path] = None
                            else:
                                raise
            else:
                # Try parallel I/O for CASLocalBackend using nexus_fast
                if backend.supports_parallel_mmap_read is True and len(paths_for_backend) > 1:
                    # Use Rust parallel mmap reads for CASLocalBackend
                    try:
                        from nexus_fast import read_files_bulk

                        # Build mapping: disk_path -> (virtual_path, meta)
                        disk_to_virtual: dict[str, tuple[str, Any]] = {}
                        disk_paths: list[str] = []
                        for path in paths_for_backend:
                            meta, route = path_info[path]
                            assert meta.etag is not None
                            disk_path = str(backend.root_path / backend._blob_key(meta.etag))
                            disk_to_virtual[disk_path] = (path, meta)
                            disk_paths.append(disk_path)

                        # Parallel mmap read
                        logger.info(
                            f"[READ-BULK] Using parallel mmap for {len(disk_paths)} CASLocalBackend files"
                        )
                        disk_contents = read_files_bulk(disk_paths)

                        # Map results back to virtual paths
                        for disk_path, content in disk_contents.items():
                            vpath, meta = disk_to_virtual[disk_path]
                            assert meta is not None  # Guaranteed by check above
                            if return_metadata:
                                results[vpath] = {
                                    "content": content,
                                    "etag": meta.etag,
                                    "version": meta.version,
                                    "modified_at": meta.modified_at,
                                    "size": len(content),
                                }
                            else:
                                results[vpath] = content

                        # Mark missing files as None if skip_errors
                        for path in paths_for_backend:
                            if path not in results:
                                if skip_errors:
                                    results[path] = None
                                else:
                                    raise NexusFileNotFoundError(path)
                    except ImportError:
                        logger.warning(
                            "[READ-BULK] nexus_fast not available, falling back to sequential"
                        )
                        # Fall through to sequential reads below
                        for path in paths_for_backend:
                            if path in results:
                                continue
                            try:
                                meta, route = path_info[path]
                                assert meta.etag is not None
                                content = route.backend.read_content(meta.etag, context=None)
                                results[path] = (
                                    content
                                    if not return_metadata
                                    else {
                                        "content": content,
                                        "etag": meta.etag,
                                        "version": meta.version,
                                        "modified_at": meta.modified_at,
                                        "size": len(content),
                                    }
                                )
                            except Exception as exc:
                                logger.debug(
                                    "Failed to read content for %s during batch read: %s", path, exc
                                )
                                if skip_errors:
                                    results[path] = None
                                else:
                                    raise
                else:
                    # Sequential reads for other backends or single files
                    for path in paths_for_backend:
                        try:
                            meta, route = path_info[path]
                            assert meta.etag is not None  # Guaranteed by check above
                            read_context = context
                            if context:
                                from dataclasses import replace

                                read_context = replace(context, backend_path=route.backend_path)
                            content = route.backend.read_content(meta.etag, context=read_context)
                            if return_metadata:
                                results[path] = {
                                    "content": content,
                                    "etag": meta.etag,
                                    "version": meta.version,
                                    "modified_at": meta.modified_at,
                                    "size": len(content),
                                }
                            else:
                                results[path] = content
                        except Exception as e:
                            logger.warning(
                                f"[READ-BULK] Failed to read {path}: {type(e).__name__}: {e}"
                            )
                            if skip_errors:
                                results[path] = None
                            else:
                                raise

        read_elapsed = time.time() - read_start
        bulk_elapsed = time.time() - bulk_start

        logger.info(
            f"[READ-BULK] Completed: {len(results)} files in {bulk_elapsed * 1000:.1f}ms "
            f"(perm={perm_elapsed * 1000:.0f}ms, read={read_elapsed * 1000:.0f}ms)"
        )

        return results

    @rpc_expose(description="Read a byte range from a file")
    def read_range(
        self,
        path: str,
        start: int,
        end: int,
        context: OperationContext | None = None,
    ) -> bytes:
        """
        Read a specific byte range from a file.

        This method enables memory-efficient streaming by allowing clients to
        fetch file content in chunks without loading the entire file into memory.

        Args:
            path: Virtual path to read
            start: Start byte offset (inclusive, 0-indexed)
            end: End byte offset (exclusive)
            context: Optional operation context for permission checks

        Returns:
            bytes: Content from start to end (exclusive)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If read operation fails
            AccessDeniedError: If access is denied
            PermissionError: If user doesn't have read permission
            ValueError: If start/end are invalid (negative, start > end, etc.)

        Example:
            >>> # Read first 1MB of a large file
            >>> chunk = nx.read_range("/workspace/large.bin", 0, 1024 * 1024)

            >>> # Stream a file in chunks
            >>> offset = 0
            >>> chunk_size = 65536
            >>> while True:
            ...     chunk = nx.read_range("/workspace/large.bin", offset, offset + chunk_size)
            ...     if not chunk:
            ...         break
            ...     process(chunk)
            ...     offset += len(chunk)
        """
        # Validate range parameters
        if start < 0:
            raise ValueError(f"start must be non-negative, got {start}")
        if end < start:
            raise ValueError(f"end ({end}) must be >= start ({start})")

        path = self._validate_path(path)

        # PRE-INTERCEPT: pre-read hooks (Issue #899)
        from nexus.contracts.vfs_hooks import ReadHookContext as _RHC

        self._dispatch.intercept_pre_read(_RHC(path=path, context=context))

        # Route to backend with access control
        zone_id, agent_id, is_admin = self._get_routing_params(context)
        route = self.router.route(
            path,
            is_admin=is_admin,
            check_write=False,
        )

        # Check if file exists in metadata
        meta = self.metadata.get(path)
        if meta is None or meta.etag is None:
            raise NexusFileNotFoundError(path)

        # Add backend_path to context for path-based connectors
        read_context = context
        if context:
            from dataclasses import replace

            read_context = replace(context, backend_path=route.backend_path)

        # Read the full content and slice (backends can override for efficiency)
        # Note: For true efficiency, backends could implement read_range() natively
        content = route.backend.read_content(meta.etag, context=read_context)

        # Apply range
        return content[start:end]

    @rpc_expose(description="Stream file content in chunks")
    def stream(
        self, path: str, chunk_size: int = 65536, context: OperationContext | None = None
    ) -> Any:
        """
        Stream file content in chunks without loading entire file into memory.

        This is a memory-efficient alternative to read() for large files.
        Yields chunks as an iterator, allowing processing of files larger than RAM.

        Args:
            path: Virtual path to stream
            chunk_size: Size of each chunk in bytes (default: 8KB)
            context: Optional operation context for permission checks

        Yields:
            bytes: Chunks of file content

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If stream operation fails
            AccessDeniedError: If access is denied
            PermissionError: If user doesn't have read permission

        Example:
            >>> # Stream large file efficiently
            >>> for chunk in nx.stream("/workspace/large_file.bin"):
            ...     process(chunk)  # Memory usage = chunk_size, not file_size

            >>> # Stream to output
            >>> import sys
            >>> for chunk in nx.stream("/workspace/video.mp4", chunk_size=1024*1024):  # 1MB chunks
            ...     sys.stdout.buffer.write(chunk)
        """
        path = self._validate_path(path)

        # PRE-INTERCEPT: pre-read hooks (Issue #899)
        from nexus.contracts.vfs_hooks import ReadHookContext as _RHC

        self._dispatch.intercept_pre_read(_RHC(path=path, context=context))

        # Route to backend with access control
        zone_id, agent_id, is_admin = self._get_routing_params(context)
        route = self.router.route(
            path,
            is_admin=is_admin,
            check_write=False,
        )

        # Check if file exists in metadata
        meta = self.metadata.get(path)
        if meta is None or meta.etag is None:
            raise NexusFileNotFoundError(path)

        # Stream from routed backend using content hash
        yield from route.backend.stream_content(meta.etag, chunk_size=chunk_size, context=context)

    @rpc_expose(description="Stream a byte range of file content")
    def stream_range(
        self,
        path: str,
        start: int,
        end: int,
        chunk_size: int = 65536,
        context: OperationContext | None = None,
    ) -> Any:
        """Stream a byte range [start, end] of file content.

        This is the kernel-level range streaming method.  HTTP routers use
        this (via ``build_range_response``) to implement RFC 9110 Range
        requests without bypassing the ObjectStore abstraction.

        Args:
            path: Virtual path to stream
            start: Start byte offset (inclusive)
            end: End byte offset (inclusive)
            chunk_size: Size of each chunk in bytes (default: 8KB)
            context: Optional operation context for permission checks

        Yields:
            bytes: Chunks of file content within the requested range
        """
        path = self._validate_path(path)
        from nexus.contracts.vfs_hooks import ReadHookContext as _RHC

        self._dispatch.intercept_pre_read(_RHC(path=path, context=context))

        zone_id, agent_id, is_admin = self._get_routing_params(context)
        route = self.router.route(
            path,
            is_admin=is_admin,
            check_write=False,
        )

        meta = self.metadata.get(path)
        if meta is None or meta.etag is None:
            raise NexusFileNotFoundError(path)

        yield from route.backend.stream_range(
            meta.etag, start, end, chunk_size=chunk_size, context=context
        )

    @rpc_expose(description="Write file content from stream")
    def write_stream(
        self,
        path: str,
        chunks: Iterator[bytes],
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """
        Write file content from an iterator of chunks.

        This is a memory-efficient alternative to write() for large files.
        Accepts chunks as an iterator, computing hash incrementally.

        Args:
            path: Virtual path to write
            chunks: Iterator yielding byte chunks
            context: Optional operation context for permission checks

        Returns:
            Dict with metadata about the written file:
                - etag: Content hash of the written content
                - version: New version number
                - modified_at: Modification timestamp
                - size: File size in bytes

        Raises:
            InvalidPathError: If path is invalid
            BackendError: If write operation fails
            AccessDeniedError: If access is denied
            PermissionError: If path is read-only or user doesn't have write permission

        Example:
            >>> # Stream large file without loading into memory
            >>> def file_chunks(path, chunk_size=8192):
            ...     with open(path, 'rb') as f:
            ...         while chunk := f.read(chunk_size):
            ...             yield chunk
            >>> result = nx.write_stream("/workspace/large.bin", file_chunks("/tmp/large.bin"))
        """
        path = self._validate_path(path)
        self._check_zone_writable(context)  # Issue #2061: write-gating

        # Route to backend with write access check
        zone_id, agent_id, is_admin = self._get_routing_params(context)
        route = self.router.route(
            path,
            is_admin=is_admin,
            check_write=True,
        )

        # Check if path is read-only
        if route.readonly:
            raise PermissionError(f"Path is read-only: {path}")

        # PRE-INTERCEPT: pre-write hooks (Issue #899)
        from nexus.contracts.vfs_hooks import WriteHookContext as _WHC

        self._dispatch.intercept_pre_write(_WHC(path=path, content=b"", context=context))

        # Get existing metadata for version tracking
        now = datetime.now(UTC)
        meta = self.metadata.get(path)

        # Write content via streaming
        write_result = route.backend.write_stream(chunks, context=context)
        content_hash = write_result.content_hash

        # WriteResult carries the byte count to avoid a redundant
        # get_content_size() round-trip after streaming writes.
        size = write_result.size
        if size <= 0:
            try:
                size = route.backend.get_content_size(content_hash, context=context)
            except Exception as e:
                logger.debug("Failed to get content size for %s: %s", content_hash, e)

        # Update metadata
        new_version = (meta.version + 1) if meta else 1
        new_meta = FileMetadata(
            path=path,
            backend_name=route.backend.name,
            physical_path=content_hash,  # CAS: hash is the "physical" location
            etag=content_hash,
            size=size,
            version=new_version,
            created_at=meta.created_at if meta else now,
            modified_at=now,
            created_by=self._get_created_by(context),
            zone_id=zone_id or "root",  # Issue #904, #773: Store zone_id for PREWHERE filtering
        )

        self.metadata.put(new_meta)

        # Issue #900: Unified INTERCEPT for write_stream
        from nexus.contracts.vfs_hooks import WriteHookContext

        _ws_ctx = WriteHookContext(
            path=path,
            content=b"",  # stream — content not available in single buffer
            context=None,
            zone_id=zone_id,
            is_new_file=(meta is None),
            metadata=new_meta,
        )
        self._dispatch.intercept_post_write(_ws_ctx)

        return {
            "etag": content_hash,
            "version": new_version,
            "modified_at": now.isoformat(),
            "size": size,
        }

    @rpc_expose(description="Write file content")
    def sys_write(
        self,
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> int:
        """Write content to a file (POSIX pwrite(2)).

        Kernel primitive — content-only. CAS, locking, and OCC are
        driver/application concerns. For metadata return or locking,
        use the convenience ``write()`` method.

        Phase A: Still updates metadata as side effect (will be separated
        into sys_write + sys_setattr in Phase B).

        Args:
            path: Virtual path to write.
            buf: File content as bytes or str (str will be UTF-8 encoded).
            count: Max bytes to write (None = len(buf)).
            offset: Byte offset to start writing at (currently ignored — whole-file).
            context: Optional operation context for permission checks.

        Returns:
            Number of bytes written.

        Raises:
            InvalidPathError: If path is invalid
            BackendError: If write operation fails
            AccessDeniedError: If access is denied (zone isolation or read-only namespace)
            PermissionError: If path is read-only or user doesn't have write permission
        """
        # Auto-convert str to bytes for convenience
        if isinstance(buf, str):
            buf = buf.encode("utf-8")

        # Apply count slicing if specified
        if count is not None:
            buf = buf[:count]

        path = self._validate_path(path)
        self._check_zone_writable(context)  # Issue #2061: write-gating

        # PRE-DISPATCH: virtual path resolvers (Issue #889)
        _handled, _result = self._dispatch.resolve_write(path, buf)
        if _handled:
            return len(buf)

        self._write_internal(path=path, content=buf, context=context)
        return len(buf)

    # ── Tier 2 overrides (NexusFS-specific) ───────────────────────

    @rpc_expose(description="Read file with optional metadata")
    def read(
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
        return_metadata: bool = False,
    ) -> bytes | dict[str, Any]:
        """Read with optional metadata (VFS convenience).

        Composes sys_stat + sys_read.  POSIX pread semantics.

        Args:
            path: Virtual file path.
            count: Max bytes to read (None = entire file).
            offset: Byte offset to start reading from.
            context: Operation context.
            return_metadata: If True, return dict with content + metadata.

        Returns:
            bytes if return_metadata=False, else dict with content + metadata.
        """
        content = self.sys_read(path, count=count, offset=offset, context=context)

        if not return_metadata:
            return content

        # Compose with sys_stat for metadata
        meta_dict = self.sys_stat(path, context=context)
        result: dict[str, Any] = {"content": content}
        if meta_dict:
            result.update(
                {
                    "etag": meta_dict.get("etag"),
                    "version": meta_dict.get("version"),
                    "modified_at": meta_dict.get("modified_at"),
                    "size": len(content),
                }
            )
        return result

    @rpc_expose(description="Write file with metadata return")
    def write(
        self,
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Write with metadata return (Tier 2 convenience).

        Overrides ABC default. Returns dict with metadata
        (etag, version, modified_at, size).

        OCC (if_match, if_none_match) is NOT here — use ``lib.occ.occ_write()``
        to compose OCC + write at the caller level (RPC handler, CLI, SDK).

        Distributed locking is NOT here — use ``lock()``/``unlock()`` or
        ``async with locked(path)`` to compose locking at the caller level.
        See Issue #1323.

        Args:
            path: Virtual file path.
            buf: File content as bytes or str.
            count: Max bytes to write (None = len(buf)).
            offset: Byte offset (currently ignored — whole-file).
            context: Operation context.

        Returns:
            Dict with metadata (etag, version, modified_at, size).
        """
        if isinstance(buf, str):
            buf = buf.encode("utf-8")
        if count is not None:
            buf = buf[:count]

        path = self._validate_path(path)
        self._check_zone_writable(context)

        # PRE-DISPATCH: virtual path resolvers
        _handled, _result = self._dispatch.resolve_write(path, buf)
        if _handled:
            return _result

        return self._write_internal(path=path, content=buf, context=context)

    def _write_internal(
        self,
        path: str,
        content: bytes,
        context: OperationContext | None,
    ) -> dict[str, Any]:
        """Kernel write implementation — OCC-free.

        Performs content write + metadata update + event dispatch.
        OCC checks (if_match, if_none_match) are done by callers
        (write() convenience method or RPC handlers) BEFORE calling this.

        Used by both sys_write (returns int) and write() (returns dict).

        Issue #1323: OCC params removed from kernel write path.
        """
        # Normalize context dict to OperationContext dataclass (CLI passes dicts)
        context = self._parse_context(context)

        # Route to backend with write access check FIRST (to check zone/agent isolation)
        # This must happen before permission check so AccessDeniedError is raised before PermissionError
        zone_id, agent_id, is_admin = self._get_routing_params(context)

        route = self.router.route(
            path,
            is_admin=is_admin,
            check_write=True,
        )

        # Check if path is read-only
        if route.readonly:
            raise PermissionError(f"Path is read-only: {path}")

        # Get existing metadata for permission check and update detection (single query)
        now = datetime.now(UTC)
        meta = self.metadata.get(path)

        # Capture snapshot before operation for undo capability
        snapshot_hash = meta.etag if meta else None
        metadata_snapshot = None
        if meta:
            metadata_snapshot = {
                "size": meta.size,
                "version": meta.version,
                "modified_at": meta.modified_at.isoformat() if meta.modified_at else None,
            }

        # PRE-INTERCEPT: pre-write hooks (Issue #899)
        # Hook handles existing-file (owner fast-path) vs new-file (parent check)
        from nexus.contracts.vfs_hooks import WriteHookContext as _WHC

        self._dispatch.intercept_pre_write(
            _WHC(
                path=path,
                content=content,
                context=context,
                old_metadata=meta,
            )
        )

        # Add backend_path to context for path-based connectors
        from dataclasses import replace

        if context:
            context = replace(context, backend_path=route.backend_path, virtual_path=path)
        else:
            from nexus.contracts.types import OperationContext

            context = OperationContext(
                user_id="anonymous", groups=[], backend_path=route.backend_path, virtual_path=path
            )

        # VFS I/O Lock: exclusive write lock around backend write + metadata put.
        # Like Linux i_rwsem: held for I/O duration only, released before observers.
        with self._vfs_locked(path, "write"):
            content_hash = route.backend.write_content(content, context=context).content_hash

            # NOTE: sys_write does NOT release old content on overwrite.
            # HDFS/GFS pattern: content cleanup is async via background GC.
            # See: docs/architecture/federation-memo.md §7f Caveat 4.

            # Calculate new version number (increment if updating)
            new_version = (meta.version + 1) if meta else 1

            # Store metadata with content hash as both etag and physical_path
            # Issue #920: Set owner_id for O(1) permission checks (only on new files)
            ctx = context if context is not None else self._default_context
            owner_id = meta.owner_id if meta else (ctx.subject_id or ctx.user_id)

            metadata = FileMetadata(
                path=path,
                backend_name=route.backend.name,
                physical_path=content_hash,
                size=len(content),
                etag=content_hash,
                created_at=meta.created_at if meta else now,
                modified_at=now,
                version=new_version,
                created_by=self._get_created_by(context),
                zone_id=zone_id or "root",  # Issue #904, #773: pre-existing default
                owner_id=owner_id,
            )

            self.metadata.put(metadata)

        # --- Lock released — event dispatch + side effects (like Linux inotify after i_rwsem) ---

        # Issue #900: Unified two-phase dispatch — OBSERVE (fire-and-forget)
        self._dispatch.notify(
            FileEvent(
                type=FileEventType.FILE_WRITE,
                path=path,
                zone_id=zone_id or ROOT_ZONE_ID,
                agent_id=agent_id,
                etag=content_hash,
                size=len(content),
                version=new_version,
                is_new=(meta is None),
            )
        )

        # P0-3: Create parent relationship tuples for file inheritance
        # This enables permission inheritance from parent directories
        # Issue #1071: Use deferred buffer for async permission operations if available
        # This reduces single-file write latency from ~36ms to ~10ms by batching
        # permission operations in the background. Owner access is guaranteed by
        # owner_id in metadata (fast-path check).
        ctx = context if context is not None else self._default_context
        deferred_buffer = getattr(self, "_deferred_permission_buffer", None)

        if deferred_buffer is not None:
            # DEFERRED PATH: Queue permission operations for background batch processing
            # Owner can still access file immediately via owner_id fast-path
            try:
                deferred_buffer.queue_hierarchy(path, ctx.zone_id or "root")
                if meta is None and ctx.user_id and not ctx.is_system:
                    deferred_buffer.queue_owner_grant(ctx.user_id, path, ctx.zone_id or "root")
            except Exception as e:
                logger.warning(f"write: Failed to queue deferred permissions for {path}: {e}")
        else:
            # SYNC PATH: Execute permission operations immediately (original behavior)
            if hasattr(self, "_hierarchy_manager") and self._hierarchy_manager is not None:
                try:
                    logger.info(
                        f"write: Calling ensure_parent_tuples for {path}, zone_id={ctx.zone_id or ROOT_ZONE_ID}"
                    )
                    created_count = self._hierarchy_manager.ensure_parent_tuples(
                        path, zone_id=ctx.zone_id or "root"
                    )
                    logger.info(f"write: Created {created_count} parent tuples for {path}")
                except Exception as e:
                    logger.warning(
                        f"write: Failed to create parent tuples for {path}: {type(e).__name__}: {e}"
                    )

            # Issue #548: Grant direct_owner permission to the user who created the file
            if meta is None and hasattr(self, "_rebac_manager") and self._rebac_manager:
                try:
                    if ctx.user_id and not ctx.is_system:
                        logger.debug(
                            f"write: Granting direct_owner permission to {ctx.user_id} for {path}"
                        )
                        self._rebac_manager.rebac_write(
                            subject=("user", ctx.user_id),
                            relation="direct_owner",
                            object=("file", path),
                            zone_id=ctx.zone_id or "root",
                        )
                        logger.debug(
                            f"write: Granted direct_owner permission to {ctx.user_id} for {path}"
                        )
                except Exception as e:
                    logger.warning(
                        f"write: Failed to grant direct_owner permission for {path}: {e}"
                    )

        # Issue #1752: Auto-track write in active transaction (snapshot for rollback)
        # Issue #2131 (14A): Direct attribute access (set in __init__ via BrickServices)
        if self._snapshot_service is not None:
            _txn_id = self._snapshot_service.is_tracked(path)
            if _txn_id is not None:
                self._snapshot_service.track_write(
                    _txn_id, path, snapshot_hash, metadata_snapshot, content_hash
                )

        # Issue #900: Unified two-phase dispatch — INTERCEPT (observer + hooks)
        from nexus.contracts.vfs_hooks import WriteHookContext

        _write_ctx = WriteHookContext(
            path=path,
            content=content,
            context=context,
            zone_id=zone_id,
            agent_id=agent_id,
            is_new_file=(meta is None),
            content_hash=content_hash,
            metadata=metadata,
            old_metadata=meta,
            new_version=new_version,
        )
        self._dispatch.intercept_post_write(_write_ctx)

        # Return metadata for optimistic concurrency control
        return {
            "etag": content_hash,
            "version": new_version,
            "modified_at": now,
            "size": len(content),
        }

    async def atomic_update(
        self,
        path: str,
        update_fn: Callable[[bytes], bytes],
        context: OperationContext | None = None,
        timeout: float = 30.0,
        ttl: float = 30.0,
    ) -> dict[str, Any]:
        """Atomically read-modify-write a file with distributed locking.

        This is the recommended API for concurrent file updates where you need
        to read existing content, modify it, and write back atomically.

        The operation:
        1. Acquires distributed lock on the path
        2. Reads current file content
        3. Applies your update function
        4. Writes modified content
        5. Releases lock (even on failure)

        For multiple operations within one lock, use `async with locked()` instead.

        Args:
            path: Virtual path to update
            update_fn: Function that transforms content (bytes -> bytes).
                      Receives current file content, returns new content.
            context: Operation context (optional)
            timeout: Maximum time to wait for lock in seconds (default: 30.0)
            ttl: Lock TTL in seconds (default: 30.0)

        Returns:
            Dict with metadata about the written file:
                - etag: Content hash of the new content
                - version: New version number
                - modified_at: Modification timestamp
                - size: File size in bytes

        Raises:
            LockTimeout: If lock cannot be acquired within timeout
            NexusFileNotFoundError: If file doesn't exist
            BackendError: If read or write operation fails

        Example:
            >>> # Increment a counter atomically
            >>> import json
            >>> await nx.atomic_update(
            ...     "/counters/visits.json",
            ...     lambda c: json.dumps({"count": json.loads(c)["count"] + 1}).encode()
            ... )

            >>> # Append to a log file atomically
            >>> await nx.atomic_update(
            ...     "/logs/access.log",
            ...     lambda c: c + b"New log entry\\n"
            ... )

            >>> # Update config safely across multiple agents
            >>> await nx.atomic_update(
            ...     "/shared/config.json",
            ...     lambda c: json.dumps({**json.loads(c), "version": 2}).encode()
            ... )
        """
        # Check if lock manager is available
        if not hasattr(self, "_lock_manager") or self._lock_manager is None:
            raise RuntimeError(
                "atomic_update() requires distributed lock manager. "
                "Set NEXUS_REDIS_URL environment variable "
                "or pass coordination_url to NexusFS constructor."
            )

        async with self.events_service.locked(path, timeout=timeout, ttl=ttl, _context=context):
            # Read current content — sys_read (Tier 1) always returns bytes
            content = self.sys_read(path, context=context)

            # Apply update function
            new_content = update_fn(content)

            # Write back via Tier 2 write() (no lock since we already hold it)
            return self.write(path, new_content, context=context)

    @rpc_expose(description="Append content to an existing file or create if it doesn't exist")
    def append(
        self,
        path: str,
        content: bytes | str,
        *,
        context: OperationContext | None = None,
        if_match: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        Append content to an existing file or create a new file if it doesn't exist.

        This is an efficient way to add content to files without reading the entire
        file separately, particularly useful for:
        - Writing JSONL (JSON Lines) logs incrementally
        - Appending to log files
        - Building append-only data structures
        - Streaming data collection

        Args:
            path: Virtual path to append to
            content: Content to append as bytes or str (str will be UTF-8 encoded)
            context: Optional operation context for permission checks (uses default if not provided)
            if_match: Optional etag for optimistic concurrency control.
                     If provided, append only succeeds if current file etag matches this value.
                     Prevents concurrent modification conflicts.
            force: If True, skip version check and append unconditionally (dangerous!)

        Returns:
            Dict with metadata about the written file:
                - etag: Content hash (SHA-256) of the final content (after append)
                - version: New version number
                - modified_at: Modification timestamp
                - size: Final file size in bytes

        Raises:
            InvalidPathError: If path is invalid
            BackendError: If append operation fails
            AccessDeniedError: If access is denied (zone isolation or read-only namespace)
            PermissionError: If path is read-only or user doesn't have write permission
            ConflictError: If if_match is provided and doesn't match current etag
            NexusFileNotFoundError: If file doesn't exist during read (should not happen in normal flow)

        Examples:
            >>> # Append to a log file
            >>> nx.append("/workspace/app.log", "New log entry\\n")

            >>> # Build JSONL file incrementally
            >>> import json
            >>> for record in records:
            ...     line = json.dumps(record) + "\\n"
            ...     nx.append("/workspace/data.jsonl", line)

            >>> # Append with optimistic concurrency control
            >>> result = nx.read("/workspace/log.txt", return_metadata=True)
            >>> try:
            ...     nx.append("/workspace/log.txt", "New entry\\n", if_match=result['etag'])
            ... except ConflictError:
            ...     print("File was modified by another process!")

            >>> # Create new file if doesn't exist
            >>> nx.append("/workspace/new.txt", "First line\\n")
        """
        # Auto-convert str to bytes for convenience
        if isinstance(content, str):
            content = content.encode("utf-8")

        path = self._validate_path(path)

        # Try to read existing content if file exists
        # For non-existent files, we'll create them (existing_content stays empty)
        existing_content = b""
        try:
            result = self.read(path, context=context, return_metadata=True)
            # Tier 2 read(return_metadata=True) always returns dict
            assert isinstance(result, dict), "Expected dict when return_metadata=True"

            existing_content = result["content"]

            # If if_match is provided, verify it matches current etag
            # (the write call will also check, but we check here to fail fast)
            if if_match is not None and not force:
                current_etag = result.get("etag")
                if current_etag != if_match:
                    from nexus.contracts.exceptions import ConflictError

                    raise ConflictError(
                        path=path,
                        expected_etag=if_match,
                        current_etag=current_etag or "(no etag)",
                    )
        except Exception as e:
            # If file doesn't exist, treat as empty (will create new file)
            # Permission errors on non-existent files are OK - write() will check parent permissions
            from nexus.contracts.exceptions import NexusFileNotFoundError

            if not isinstance(e, NexusFileNotFoundError | PermissionError):
                # Re-raise unexpected errors
                raise
            # For FileNotFoundError or PermissionError, continue with empty content
            # write() will check if user has permission to create the file

        # Combine existing content with new content
        final_content = existing_content + content

        # Use the existing write method to handle all the complexity:
        # - Permission checking
        # - Version management
        # - Audit logging
        # - Workflow triggers
        # - Parent tuple creation
        # OCC check already done above (line 2985-2996), so just write.
        return self.write(
            path,
            final_content,
            context=context,
        )

    @rpc_expose(description="Apply surgical search/replace edits to a file")
    def edit(
        self,
        path: str,
        edits: list[tuple[str, str]] | list[dict[str, Any]] | list[Any],
        *,
        context: OperationContext | None = None,
        if_match: str | None = None,
        fuzzy_threshold: float = 0.85,
        preview: bool = False,
    ) -> dict[str, Any]:
        """
        Apply surgical search/replace edits to a file.

        This enables precise file modifications without rewriting entire files,
        reducing token cost and errors when used with LLMs.

        Issue #800: Add edit engine with search/replace for surgical file edits.

        Uses a layered matching strategy:
        1. Exact match (fast path)
        2. Whitespace-normalized match
        3. Fuzzy match (Levenshtein similarity)

        Args:
            path: Virtual path to edit
            edits: List of edit operations. Each edit can be:
                - Tuple: (old_str, new_str) - simple search/replace
                - Dict: {"old_str": str, "new_str": str, "hint_line": int | None,
                         "allow_multiple": bool} - full control
                - EditOperation: Direct EditOperation instance
            context: Optional operation context for permission checks
            if_match: Optional etag for optimistic concurrency control.
                If provided, edit fails if file changed since read.
            fuzzy_threshold: Similarity threshold (0.0-1.0) for fuzzy matching.
                Default 0.85. Use 1.0 for exact matching only.
            preview: If True, return preview without writing. Default False.

        Returns:
            Dict containing:
                - success: bool - True if all edits applied
                - diff: str - Unified diff of changes
                - matches: list[dict] - Info about each match (type, line, similarity)
                - applied_count: int - Number of edits applied
                - etag: str - New etag (if not preview)
                - version: int - New version (if not preview)
                - errors: list[str] - Error messages if any edits failed

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            AccessDeniedError: If access is denied
            PermissionError: If path is read-only
            ConflictError: If if_match doesn't match current etag

        Examples:
            >>> # Simple search/replace
            >>> result = nx.edit("/code/main.py", [
            ...     ("def foo():", "def bar():"),
            ...     ("return x", "return x + 1"),
            ... ])
            >>> print(result['diff'])

            >>> # With optimistic concurrency
            >>> content = nx.read("/code/main.py", return_metadata=True)
            >>> result = nx.edit(
            ...     "/code/main.py",
            ...     [("old_text", "new_text")],
            ...     if_match=content['etag']
            ... )

            >>> # Preview without writing
            >>> result = nx.edit("/code/main.py", edits, preview=True)
            >>> if result['success']:
            ...     print(result['diff'])

            >>> # With fuzzy matching
            >>> result = nx.edit("/code/main.py", [
            ...     {"old_str": "def foo():", "new_str": "def bar():", "hint_line": 42}
            ... ], fuzzy_threshold=0.8)
        """
        from nexus.utils.edit_engine import EditEngine
        from nexus.utils.edit_engine import EditOperation as EditOp

        path = self._validate_path(path)

        # Read current content with metadata (via Tier 2 convenience)
        result = self.read(path, context=context, return_metadata=True)
        assert isinstance(result, dict), "Expected dict when return_metadata=True"

        content_bytes: bytes = result["content"]
        current_etag = result.get("etag")

        # Check etag if provided (optimistic concurrency control)
        if if_match is not None and current_etag != if_match:
            raise ConflictError(
                path=path,
                expected_etag=if_match,
                current_etag=current_etag or "(no etag)",
            )

        # Decode content to string for editing
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            return {
                "success": False,
                "diff": "",
                "matches": [],
                "applied_count": 0,
                "errors": [f"File is not valid UTF-8 text: {e}"],
            }

        # Convert edits to EditOperation instances
        edit_operations: list[EditOp] = []
        for edit in edits:
            if isinstance(edit, EditOp):
                edit_operations.append(edit)
            elif isinstance(edit, tuple | list) and len(edit) >= 2:
                # Handle both tuple and list (JSON deserializes tuples as lists)
                edit_operations.append(EditOp(old_str=edit[0], new_str=edit[1]))
            elif isinstance(edit, dict):
                edit_operations.append(
                    EditOp(
                        old_str=edit["old_str"],
                        new_str=edit["new_str"],
                        hint_line=edit.get("hint_line"),
                        allow_multiple=edit.get("allow_multiple", False),
                    )
                )
            else:
                return {
                    "success": False,
                    "diff": "",
                    "matches": [],
                    "applied_count": 0,
                    "errors": [
                        f"Invalid edit format: expected tuple (old, new), dict, or EditOperation, got {type(edit)}"
                    ],
                }

        # Apply edits
        engine = EditEngine(
            fuzzy_threshold=fuzzy_threshold,
            enable_fuzzy=fuzzy_threshold < 1.0,
        )
        edit_result = engine.apply_edits(content, edit_operations)

        # Convert matches to serializable dicts
        matches_list = [
            {
                "edit_index": m.edit_index,
                "match_type": m.match_type,
                "similarity": m.similarity,
                "line_start": m.line_start,
                "line_end": m.line_end,
                "original_text": m.original_text[:200] if m.original_text else "",
                "search_strategy": m.search_strategy,
                "match_count": m.match_count,
            }
            for m in edit_result.matches
        ]

        # If edits failed, return error without writing
        if not edit_result.success:
            return {
                "success": False,
                "diff": edit_result.diff,
                "matches": matches_list,
                "applied_count": edit_result.applied_count,
                "errors": edit_result.errors,
            }

        # If preview mode, return without writing
        if preview:
            return {
                "success": True,
                "diff": edit_result.diff,
                "matches": matches_list,
                "applied_count": edit_result.applied_count,
                "preview": True,
                "new_content": edit_result.content,
            }

        # Write the edited content. OCC check already done above (line 3117-3123).
        new_content_bytes = edit_result.content.encode("utf-8")
        write_result = self.write(
            path,
            new_content_bytes,
            context=context,
        )

        return {
            "success": True,
            "diff": edit_result.diff,
            "matches": matches_list,
            "applied_count": edit_result.applied_count,
            "etag": write_result.get("etag"),
            "version": write_result.get("version"),
            "size": write_result.get("size"),
            "modified_at": write_result.get("modified_at"),
        }

    @rpc_expose(description="Write multiple files in a single transaction")
    def write_batch(
        self, files: list[tuple[str, bytes]], context: OperationContext | None = None
    ) -> list[dict[str, Any]]:
        """
        Write multiple files in a single transaction for improved performance.

        This is 13x faster than calling write() multiple times for small files
        because it uses a single database transaction instead of N transactions.

        All files are written atomically - either all succeed or all fail.

        Args:
            files: List of (path, content) tuples to write
            context: Optional operation context for permission checks (uses default if not provided)

        Returns:
            List of metadata dicts for each file (in same order as input):
                - etag: Content hash (SHA-256) of the written content
                - version: New version number
                - modified_at: Modification timestamp
                - size: File size in bytes

        Raises:
            InvalidPathError: If any path is invalid
            BackendError: If write operation fails
            AccessDeniedError: If access is denied (zone isolation or read-only namespace)
            PermissionError: If any path is read-only or user doesn't have write permission

        Examples:
            >>> # Write 100 small files in a single batch (13x faster!)
            >>> files = [(f"/logs/file_{i}.txt", b"log data") for i in range(100)]
            >>> results = nx.write_batch(files)
            >>> print(f"Wrote {len(results)} files")

            >>> # Atomic batch write - all or nothing
            >>> files = [
            ...     ("/config/setting1.json", b'{"enabled": true}'),
            ...     ("/config/setting2.json", b'{"timeout": 30}'),
            ... ]
            >>> nx.write_batch(files)
        """
        if not files:
            return []

        self._check_zone_writable(context)  # Issue #2061: write-gating

        # Validate all paths first
        validated_files: list[tuple[str, bytes]] = []
        for path, content in files:
            validated_path = self._validate_path(path)
            validated_files.append((validated_path, content))

        # Route all paths and check write access
        zone_id, agent_id, is_admin = self._get_routing_params(context)
        routes = []
        for path, _ in validated_files:
            route = self.router.route(
                path,
                is_admin=is_admin,
                check_write=True,
            )
            # Check if path is read-only
            if route.readonly:
                raise PermissionError(f"Path is read-only: {path}")
            routes.append(route)

        # Get existing metadata for all paths (single query)
        paths = [path for path, _ in validated_files]
        existing_metadata = self.metadata.get_batch(paths)

        # PRE-INTERCEPT: pre-write hooks per file in batch
        from nexus.contracts.vfs_hooks import WriteHookContext as _WHC

        for path in paths:
            meta = existing_metadata.get(path)
            self._dispatch.intercept_pre_write(
                _WHC(
                    path=path,
                    content=b"",
                    context=context,
                    old_metadata=meta,
                )
            )

        now = datetime.now(UTC)
        metadata_list: list[FileMetadata] = []
        results: list[dict[str, Any]] = []

        # Write all content to backend CAS (deduplicated automatically)
        for (path, content), route in zip(validated_files, routes, strict=False):
            # Write to backend - returns content hash
            content_hash = route.backend.write_content(content, context=context).content_hash

            # Get existing metadata for this file
            meta = existing_metadata.get(path)

            # UNIX permissions removed - all access control via ReBAC

            # Calculate new version number (increment if updating)
            new_version = (meta.version + 1) if meta else 1

            # Build metadata for batch insert
            # Note: UNIX permissions (owner/group/mode) removed - use ReBAC instead
            metadata = FileMetadata(
                path=path,
                backend_name=route.backend.name,  # FIX: Use routed backend name, not default backend
                physical_path=content_hash,  # CAS: hash is the "physical" location
                size=len(content),
                etag=content_hash,  # SHA-256 hash for integrity
                created_at=meta.created_at if meta else now,
                modified_at=now,
                version=new_version,
                created_by=getattr(self, "agent_id", None)
                or getattr(self, "user_id", None),  # Track who created/modified this version
                zone_id=zone_id or "root",  # Issue #904, #773: Store zone_id for PREWHERE filtering
            )
            metadata_list.append(metadata)

            # Build result dict
            results.append(
                {
                    "etag": content_hash,
                    "version": new_version,
                    "modified_at": now,
                    "size": len(content),
                }
            )

        # Store all metadata in a single transaction (with version history)
        self.metadata.put_batch(metadata_list)

        # Issue #900: Unified two-phase dispatch — INTERCEPT (observer + hooks)
        items = [
            (metadata, existing_metadata.get(metadata.path) is None) for metadata in metadata_list
        ]
        self._dispatch.intercept_post_write_batch(
            items,
            zone_id=zone_id,
            agent_id=agent_id,
        )

        # Issue #900: Unified two-phase dispatch — OBSERVE (fire-and-forget)
        for metadata in metadata_list:
            is_new = existing_metadata.get(metadata.path) is None
            self._dispatch.notify(
                FileEvent(
                    type=FileEventType.FILE_WRITE,
                    path=metadata.path,
                    zone_id=zone_id or ROOT_ZONE_ID,
                    agent_id=agent_id,
                    etag=metadata.etag,
                    size=metadata.size,
                    version=metadata.version,
                    is_new=is_new,
                )
            )

        # Issue #548: Create parent tuples and grant direct_owner for new files
        # This ensures agents can read files they create (via user inheritance)
        # PERF OPTIMIZATION: Use batch operations instead of individual calls (20x faster)
        ctx = context if context is not None else self._default_context
        zone_id_for_perms = ctx.zone_id or "root"

        # PERF: Batch hierarchy tuple creation (single transaction instead of N)
        _hierarchy_start = time.perf_counter()
        all_paths = [path for path, _ in validated_files]
        if (
            hasattr(self, "_hierarchy_manager")
            and self._hierarchy_manager is not None
            and hasattr(self._hierarchy_manager, "ensure_parent_tuples_batch")
        ):
            try:
                created_count = self._hierarchy_manager.ensure_parent_tuples_batch(
                    all_paths, zone_id=zone_id_for_perms
                )
                logger.info(
                    f"write_batch: Batch created {created_count} parent tuples for {len(all_paths)} files"
                )
            except Exception as e:
                logger.warning(
                    f"write_batch: Batch parent tuples failed, falling back to individual: {e}"
                )
                # Fallback to individual calls if batch fails
                for path in all_paths:
                    try:
                        self._hierarchy_manager.ensure_parent_tuples(
                            path, zone_id=zone_id_for_perms
                        )
                    except Exception as e2:
                        logger.warning(
                            f"write_batch: Failed to create parent tuples for {path}: {e2}"
                        )
        elif hasattr(self, "_hierarchy_manager") and self._hierarchy_manager is not None:
            # No batch method available, use individual calls
            for path in all_paths:
                try:
                    self._hierarchy_manager.ensure_parent_tuples(path, zone_id=zone_id_for_perms)
                except Exception as e:
                    logger.warning(f"write_batch: Failed to create parent tuples for {path}: {e}")
        _hierarchy_elapsed = (time.perf_counter() - _hierarchy_start) * 1000

        # PERF: Batch direct_owner grants (single transaction instead of N)
        _rebac_start = time.perf_counter()
        if (
            hasattr(self, "_rebac_manager")
            and self._rebac_manager
            and ctx.user_id
            and not ctx.is_system
        ):
            # Collect all owner grants needed for new files
            owner_grants = []
            for (path, _), _meta in zip(validated_files, metadata_list, strict=False):
                is_new_file = existing_metadata.get(path) is None
                if is_new_file:
                    owner_grants.append(
                        {
                            "subject": ("user", ctx.user_id),
                            "relation": "direct_owner",
                            "object": ("file", path),
                            "zone_id": zone_id_for_perms,
                        }
                    )

            if owner_grants and hasattr(self._rebac_manager, "rebac_write_batch"):
                try:
                    grant_count = self._rebac_manager.rebac_write_batch(owner_grants)
                    logger.info(f"write_batch: Batch granted direct_owner to {grant_count} files")
                except Exception as e:
                    logger.warning(
                        f"write_batch: Batch rebac_write failed, falling back to individual: {e}"
                    )
                    # Fallback to individual calls
                    for grant in owner_grants:
                        try:
                            self._rebac_manager.rebac_write(
                                subject=grant["subject"],
                                relation=grant["relation"],
                                object=grant["object"],
                                zone_id=grant["zone_id"],
                            )
                        except Exception as e2:
                            logger.warning(f"write_batch: Failed to grant direct_owner: {e2}")
            elif owner_grants:
                # No batch method available, use individual calls
                for grant in owner_grants:
                    try:
                        self._rebac_manager.rebac_write(
                            subject=grant["subject"],
                            relation=grant["relation"],
                            object=grant["object"],
                            zone_id=grant["zone_id"],
                        )
                    except Exception as e:
                        logger.warning(f"write_batch: Failed to grant direct_owner: {e}")
        _rebac_elapsed = (time.perf_counter() - _rebac_start) * 1000

        # Log detailed timing breakdown for performance analysis
        logger.warning(
            f"[WRITE-BATCH-PERF] files={len(validated_files)}, "
            f"hierarchy={_hierarchy_elapsed:.1f}ms, rebac={_rebac_elapsed:.1f}ms, "
            f"per_file_avg={(_hierarchy_elapsed + _rebac_elapsed) / len(validated_files):.1f}ms"
        )

        return results

    @rpc_expose(description="Delete file")
    def sys_unlink(self, path: str, context: OperationContext | None = None) -> dict[str, Any]:
        """
        Delete a file or memory.

        Removes file from backend and metadata store.
        Decrements reference count in CAS (only deletes when ref_count=0).

        Supports memory virtual paths.

        Args:
            path: Virtual path to delete (supports memory paths)
            context: Optional operation context for permission checks (uses default if not provided)

        Returns:
            Empty dict on success.

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If delete operation fails
            AccessDeniedError: If access is denied (zone isolation or read-only namespace)
            PermissionError: If path is read-only or user doesn't have write permission
        """
        path = self._validate_path(path)
        self._check_zone_writable(context)  # Issue #2061: write-gating

        # PRE-DISPATCH: virtual path resolvers (Issue #889)
        _handled, _result = self._dispatch.resolve_delete(path, context=context)
        if _handled:
            return _result

        # Route to backend with write access check FIRST (to check zone/agent isolation)
        # This must happen before permission check so AccessDeniedError is raised before PermissionError
        zone_id, agent_id, is_admin = self._get_routing_params(context)
        route = self.router.route(
            path,
            is_admin=is_admin,
            check_write=True,
        )

        # Check if path is read-only
        if route.readonly:
            raise PermissionError(f"Cannot delete from read-only path: {path}")

        # Check if file exists in metadata.
        # Use prefetched hint from resolve_delete() if available (#1311)
        meta = _result if _result is not None else self.metadata.get(path)

        # Issue #1264: If file exists only in base layer, create whiteout instead of deleting
        if meta is None and getattr(self, "_overlay_resolver", None):
            overlay_config = self._get_overlay_config(path)
            if overlay_config:
                base_meta = self._overlay_resolver.resolve_read(path, overlay_config)
                if base_meta is not None and not self._overlay_resolver.is_whiteout(base_meta):
                    self._overlay_resolver.create_whiteout(path, overlay_config)
                    return {"deleted": path, "overlay_whiteout": True}

        if meta is None:
            raise NexusFileNotFoundError(path)

        # Capture snapshot before operation for undo capability
        snapshot_hash = meta.etag
        metadata_snapshot = {
            "size": meta.size,
            "version": meta.version,
            "modified_at": meta.modified_at.isoformat() if meta.modified_at else None,
            "backend_name": meta.backend_name,
            "physical_path": meta.physical_path,
        }

        # PRE-INTERCEPT: pre-delete hooks (Issue #899)
        from nexus.contracts.vfs_hooks import DeleteHookContext as _DHC

        self._dispatch.intercept_pre_delete(_DHC(path=path, context=context))

        # Issue #1752: Auto-track delete in active transaction (snapshot for rollback)
        # Issue #2131 (14A): Direct attribute access (set in __init__ via BrickServices)
        if self._snapshot_service is not None:
            _txn_id = self._snapshot_service.is_tracked(path)
            if _txn_id is not None:
                self._snapshot_service.track_delete(_txn_id, path, snapshot_hash, metadata_snapshot)

        # Issue #900: Unified two-phase dispatch — INTERCEPT (observer + hooks)
        # Placed BEFORE physical content delete to preserve audit integrity.
        from nexus.contracts.vfs_hooks import DeleteHookContext

        _delete_ctx = DeleteHookContext(
            path=path,
            context=context,
            zone_id=zone_id,
            agent_id=agent_id,
            metadata=meta,
        )
        self._dispatch.intercept_post_delete(_delete_ctx)

        # VFS I/O Lock: exclusive write lock around CAS delete + metadata delete.
        # Like Linux i_rwsem: held for I/O duration only, released before observers.
        with self._vfs_locked(path, "write"):
            # Delete from routed backend CAS (decrements ref count)
            # Content is only physically deleted when ref_count reaches 0
            # Skip content deletion for directories (no CAS entry)
            if meta.etag and meta.mime_type != "inode/directory":
                route.backend.delete_content(meta.etag, context=context)

            # Remove from metadata
            self.metadata.delete(path)

        # --- Lock released — event dispatch (like Linux inotify after i_rwsem) ---

        # Issue #900: Unified two-phase dispatch — OBSERVE (fire-and-forget)
        self._dispatch.notify(
            FileEvent(
                type=FileEventType.FILE_DELETE,
                path=path,
                zone_id=zone_id or ROOT_ZONE_ID,
                agent_id=agent_id,
                etag=meta.etag,
                size=meta.size,
            )
        )

        return {}

    @rpc_expose(description="Rename/move file")
    def sys_rename(
        self, old_path: str, new_path: str, context: OperationContext | None = None
    ) -> dict[str, Any]:
        """
        Rename/move a file by updating its path in metadata.

        This is a metadata-only operation that does NOT copy file content.
        The file's content remains in the same location in CAS storage,
        only the virtual path is updated in the metadata database.

        This makes rename/move operations instant, regardless of file size.

        Args:
            old_path: Current virtual path
            new_path: New virtual path
            context: Optional operation context for permission checks (uses default if not provided)

        Returns:
            Empty dict on success.

        Raises:
            NexusFileNotFoundError: If source file doesn't exist
            FileExistsError: If destination path already exists
            InvalidPathError: If either path is invalid
            PermissionError: If either path is read-only
            AccessDeniedError: If access is denied (zone isolation)

        Example:
            >>> nx.sys_rename('/workspace/old.txt', '/workspace/new.txt')
            >>> nx.sys_rename('/folder-a/file.txt', '/shared/folder-a/file.txt')
        """
        old_path = self._validate_path(old_path)
        new_path = self._validate_path(new_path)
        # Normalize context dict to OperationContext dataclass (CLI passes dicts)
        context = self._parse_context(context)
        self._check_zone_writable(context)  # Issue #2061: write-gating

        # Route both paths
        zone_id, agent_id, is_admin = self._get_routing_params(context)
        old_route = self.router.route(
            old_path,
            is_admin=is_admin,
            check_write=True,  # Need write access to source
        )
        new_route = self.router.route(
            new_path,
            is_admin=is_admin,
            check_write=True,  # Need write access to destination
        )

        # Check if paths are read-only
        if old_route.readonly:
            raise PermissionError(f"Cannot rename from read-only path: {old_path}")
        if new_route.readonly:
            raise PermissionError(f"Cannot rename to read-only path: {new_path}")

        # Check if source exists (explicit metadata or implicit directory)
        is_implicit_dir = not self.metadata.exists(
            old_path
        ) and self.metadata.is_implicit_directory(old_path)
        if not self.metadata.exists(old_path) and not is_implicit_dir:
            raise NexusFileNotFoundError(old_path)

        meta = self.metadata.get(old_path)

        # Check if destination already exists
        # For connector backends, also verify the file exists in backend storage
        # (metadata might be stale if previous operations failed)
        if self.metadata.exists(new_path):
            if new_route.backend.supports_rename is True:
                # Connector backend - verify file actually exists in storage
                # If metadata says it exists but storage doesn't, clean up stale metadata
                try:
                    # Check if this is a GCS connector backend (has bucket attribute)
                    # NOTE: bucket/blob access is GCS-specific, kept as hasattr for now
                    if (
                        hasattr(new_route.backend, "bucket")
                        and hasattr(new_route.backend, "_get_blob_path")
                        and new_route.backend.name == "path_gcs"
                    ):
                        # GCS-specific attributes (dynamically checked with hasattr above)
                        dest_blob = new_route.backend.bucket.blob(
                            new_route.backend._get_blob_path(new_route.backend_path)
                        )
                        if not dest_blob.exists():
                            # Stale metadata - clean it up
                            import logging

                            log = logging.getLogger(__name__)
                            log.warning(
                                f"Cleaning up stale metadata for {new_path} (file not in backend storage)"
                            )
                            self.metadata.delete(new_path)
                        else:
                            # File really exists
                            raise FileExistsError(f"Destination path already exists: {new_path}")
                    else:
                        # Not a GCS connector backend, just check metadata
                        raise FileExistsError(f"Destination path already exists: {new_path}")
                except AttributeError:
                    # Not a GCS connector backend, just check metadata
                    raise FileExistsError(f"Destination path already exists: {new_path}") from None
            else:
                # CAS backend - metadata is source of truth
                raise FileExistsError(f"Destination path already exists: {new_path}")

        # Check if this is a directory BEFORE renaming (important!)
        # After rename, the old path won't have children anymore
        # is_implicit_dir was already computed above - also check for explicit directory
        is_directory = is_implicit_dir or (meta and meta.mime_type == "inode/directory")

        # PRE-INTERCEPT: pre-rename hooks (Issue #900 / #1312)
        from nexus.contracts.vfs_hooks import RenameHookContext

        _rename_ctx = RenameHookContext(
            old_path=old_path,
            new_path=new_path,
            context=context,
            zone_id=zone_id,
            agent_id=agent_id,
            is_directory=bool(is_directory),
            metadata=meta,
        )
        self._dispatch.intercept_pre_rename(_rename_ctx)

        # VFS I/O Lock: exclusive write lock on BOTH paths (sorted order = deadlock-free).
        # Like Linux i_rwsem on both source and destination inodes.
        _first, _second = sorted([old_path, new_path])
        _h1 = self._vfs_acquire(_first, "write")
        try:
            _h2 = self._vfs_acquire(_second, "write") if _first != _second else 0
            try:
                # For path-based connector backends, move actual file in storage
                if old_route.backend.supports_rename is True:
                    try:
                        old_route.backend.rename_file(
                            old_route.backend_path, new_route.backend_path
                        )
                    except FileExistsError:
                        raise
                    except Exception as e:
                        raise BackendError(
                            f"Failed to rename file in backend: {e}",
                            backend=old_route.backend.name,
                        ) from e

                # Perform metadata rename
                self.metadata.rename_path(old_path, new_path)
            finally:
                if _h2:
                    self._vfs_lock_manager.release(_h2)
        finally:
            self._vfs_lock_manager.release(_h1)

        # --- Lock released — event dispatch + side effects (like Linux inotify after i_rwsem) ---

        # Issue #900: Unified two-phase dispatch — OBSERVE (fire-and-forget)
        self._dispatch.notify(
            FileEvent(
                type=FileEventType.FILE_RENAME,
                path=old_path,
                zone_id=zone_id or ROOT_ZONE_ID,
                agent_id=agent_id,
                new_path=new_path,
            )
        )

        # Update ReBAC permissions to follow the renamed file/directory
        # This ensures permissions are preserved when files are moved
        logger.warning(f"[RENAME-REBAC] Starting ReBAC update: {old_path} -> {new_path}")
        logger.warning(
            f"[RENAME-REBAC] has _rebac_manager: {hasattr(self, '_rebac_manager')}, is truthy: {bool(getattr(self, '_rebac_manager', None))}"
        )

        if hasattr(self, "_rebac_manager") and self._rebac_manager:
            try:
                logger.warning(
                    f"[RENAME-REBAC] Calling update_object_path: old={old_path}, new={new_path}, is_dir={is_directory}"
                )

                # Update all ReBAC tuples that reference this path
                updated_count = self._rebac_manager.update_object_path(
                    old_path=old_path,
                    new_path=new_path,
                    object_type="file",
                    is_directory=is_directory,
                )

                # Log if any permissions were updated
                logger.warning(
                    f"[RENAME-REBAC] update_object_path returned: {updated_count} tuples updated"
                )
            except Exception as e:
                # Don't fail the rename operation if ReBAC update fails
                # The file is already renamed in metadata, we just couldn't update permissions
                logger.error(
                    f"[RENAME-REBAC] FAILED to update ReBAC permissions: {e}", exc_info=True
                )
        else:
            logger.warning("[RENAME-REBAC] SKIPPED - no _rebac_manager available")

        # POST-INTERCEPT: post-rename hooks (Issue #900)
        self._dispatch.intercept_post_rename(_rename_ctx)

        return {}

    @rpc_expose(description="Get file metadata without reading content")
    def stat(self, path: str, context: OperationContext | None = None) -> dict[str, Any]:
        """
        Get file metadata without reading the file content.

        This is useful for getting file size before streaming, or checking
        file properties without the overhead of reading large files.

        Args:
            path: Virtual path to stat
            context: Optional operation context for permission checks

        Returns:
            Dict with file metadata:
                - size: File size in bytes
                - etag: Content hash
                - version: Version number
                - modified_at: Last modification timestamp
                - is_directory: Whether path is a directory

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            AccessDeniedError: If access is denied
            PermissionError: If user doesn't have read permission

        Example:
            >>> info = nx.stat("/workspace/large_file.bin")
            >>> print(f"File size: {info['size']} bytes")
        """
        path = self._validate_path(path)

        # Check if it's an implicit directory first (for permission check optimization)
        is_implicit_dir = self.metadata.is_implicit_directory(path)

        # Check permission: TRAVERSE for implicit directories, READ for files
        # This enables `stat /skills` to work for authenticated users (TRAVERSE is auto-allowed)
        ctx = context if context is not None else self._default_context
        if is_implicit_dir:
            # Only check permissions if enforcement is enabled
            if self._enforce_permissions:  # type: ignore[attr-defined]  # allowed
                # Try TRAVERSE permission first (O(1))
                # Fall back to descendant access check if TRAVERSE denied (Unix-like behavior)
                has_permission = self._permission_enforcer.check(path, Permission.TRAVERSE, ctx)
                if not has_permission:
                    has_permission = self._descendant_checker.has_access(path, Permission.READ, ctx)  # type: ignore[attr-defined]  # allowed
                if not has_permission:
                    raise PermissionError(
                        f"Access denied: User '{ctx.user_id}' does not have TRAVERSE "
                        f"permission for '{path}'"
                    )
        else:
            from nexus.contracts.vfs_hooks import ReadHookContext as _RHC

            self._dispatch.intercept_pre_read(_RHC(path=path, context=context))

        # Return directory info for implicit directories
        if is_implicit_dir:
            return {
                "size": 0,
                "etag": None,
                "version": None,
                "modified_at": None,
                "is_directory": True,
            }

        # Get file metadata
        meta = self.metadata.get(path)
        if meta is None:
            raise NexusFileNotFoundError(path)

        # Get size from backend if not in metadata
        size = meta.size
        if size is None and meta.etag:
            # Try to get size from backend
            zone_id, agent_id, is_admin = self._get_routing_params(context)
            route = self.router.route(
                path,
                is_admin=is_admin,
                check_write=False,
            )
            try:
                # Add backend_path to context for path-based connectors
                size_context = context
                if context:
                    from dataclasses import replace

                    size_context = replace(context, backend_path=route.backend_path)
                size = route.backend.get_content_size(meta.etag, context=size_context)
            except Exception as exc:
                logger.debug("Failed to get content size for %s: %s", path, exc)
                size = None

        # Convert datetime to ISO string for wire compatibility with Rust FUSE client
        # The client expects a plain string, not the wrapped {"__type__": "datetime", ...} format
        modified_at_str = meta.modified_at.isoformat() if meta.modified_at else None

        return {
            "size": size,
            "etag": meta.etag,
            "version": meta.version,
            "modified_at": modified_at_str,
            "is_directory": False,
        }

    @rpc_expose(description="Get metadata for multiple files in bulk")
    def stat_bulk(
        self,
        paths: list[str],
        context: OperationContext | None = None,
        skip_errors: bool = True,
    ) -> dict[str, dict[str, Any] | None]:
        """
        Get metadata for multiple files in a single RPC call.

        This is optimized for bulk operations where many file stats are needed.
        It batches permission checks and metadata lookups for better performance.

        Args:
            paths: List of virtual paths to stat
            context: Optional operation context for permission checks
            skip_errors: If True, skip files that can't be stat'd and return None.
                        If False, raise exception on first error.

        Returns:
            Dict mapping path -> stat dict (or None if skip_errors=True and stat failed)
            Each stat dict contains: size, etag, version, modified_at, is_directory

        Performance:
            - Single RPC call instead of N calls
            - Batch permission checks (one DB query instead of N)
            - Batch metadata lookups
            - Expected speedup: 10-50x for 100+ files
        """
        import time

        bulk_start = time.time()
        results: dict[str, dict[str, Any] | None] = {}

        # Validate all paths
        validated_paths = []
        for path in paths:
            try:
                validated_path = self._validate_path(path)
                validated_paths.append(validated_path)
            except Exception as exc:
                logger.debug("Path validation failed in metadata_bulk for %s: %s", path, exc)
                if skip_errors:
                    results[path] = None
                    continue
                raise

        if not validated_paths:
            return results

        # Batch permission check using filter_list
        perm_start = time.time()
        allowed_set: set[str]
        if not self._enforce_permissions:  # type: ignore[attr-defined]  # allowed
            allowed_set = set(validated_paths)
        else:
            try:
                from nexus.contracts.types import OperationContext

                ctx = context if context is not None else self._default_context
                assert isinstance(ctx, OperationContext), "Context must be OperationContext"
                allowed_paths = self._permission_enforcer.filter_list(validated_paths, ctx)
                allowed_set = set(allowed_paths)
            except Exception as e:
                logger.error(f"[STAT-BULK] Permission check failed: {e}")
                if not skip_errors:
                    raise
                allowed_set = set()

        perm_elapsed = time.time() - perm_start
        logger.info(
            f"[STAT-BULK] Permission check: {len(allowed_set)}/{len(validated_paths)} allowed in {perm_elapsed * 1000:.1f}ms"
        )

        # Mark denied files
        for path in validated_paths:
            if path not in allowed_set:
                results[path] = None

        # Batch metadata lookup - single SQL query for all paths
        meta_start = time.time()

        # Batch fetch metadata for all files in single query
        # Note: We assume paths are files (not implicit directories) since stat_bulk
        # is typically called on paths returned by list(). If a path isn't found,
        # we check if it's an implicit directory as a fallback.
        try:
            batch_meta = self.metadata.get_batch(list(allowed_set))
            for path, meta in batch_meta.items():
                if meta is None:
                    # Path not found in metadata - check if it's an implicit directory
                    if self.metadata.is_implicit_directory(path):
                        results[path] = {
                            "size": 0,
                            "etag": None,
                            "version": None,
                            "modified_at": None,
                            "is_directory": True,
                        }
                    elif skip_errors:
                        results[path] = None
                    else:
                        raise NexusFileNotFoundError(path)
                else:
                    modified_at_str = meta.modified_at.isoformat() if meta.modified_at else None
                    results[path] = {
                        "size": meta.size,
                        "etag": meta.etag,
                        "version": meta.version,
                        "modified_at": modified_at_str,
                        "is_directory": False,
                    }
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            logger.warning(f"[STAT-BULK] Batch metadata failed: {type(e).__name__}: {e}")
            if not skip_errors:
                raise

        meta_elapsed = time.time() - meta_start
        bulk_elapsed = time.time() - bulk_start

        logger.info(
            f"[STAT-BULK] Completed: {len(results)} files in {bulk_elapsed * 1000:.1f}ms "
            f"(perm={perm_elapsed * 1000:.0f}ms, meta={meta_elapsed * 1000:.0f}ms)"
        )

        return results

    @rpc_expose(description="Check if file exists")
    def sys_access(self, path: str, context: OperationContext | None = None) -> bool:
        """
        Check if a file or directory exists.

        Args:
            path: Virtual path to check
            context: Operation context for permission checks (uses default if None)

        Returns:
            True if file or implicit directory exists AND user has read permission on it
            OR any descendant (enables hierarchical navigation), False otherwise

        Note:
            With permissions enabled, directories are visible if user has access to ANY
            descendant, even if they don't have direct access to the directory itself.
            This enables hierarchical navigation (e.g., /workspace visible if user has
            access to /workspace/joe/file.txt).

        Performance:
            For implicit directories (directories without explicit files, like /zones),
            uses TRAVERSE permission check (O(1)) instead of descendant access check (O(n)).
            This is a major optimization for FUSE path resolution operations.
        """
        try:
            path = self._validate_path(path)

            # Check if it's an implicit directory first (before permission check for optimization)
            is_implicit_dir = self.metadata.is_implicit_directory(path)

            # Check permission if enforcement enabled
            if self._enforce_permissions:  # type: ignore[attr-defined]  # allowed
                ctx = context if context is not None else self._default_context

                # OPTIMIZATION: For implicit directories, use TRAVERSE permission (O(1))
                # instead of expensive descendant access check (O(n))
                # TRAVERSE is granted on root-level implicit directories like /zones, /sessions, /skills
                if is_implicit_dir:
                    # Try TRAVERSE permission first (O(1) check)
                    if self._permission_enforcer.check(path, Permission.TRAVERSE, ctx):
                        return True
                    # Fall back to descendant access check for non-root implicit dirs
                    # (e.g., /zones/zone_1 where user may have access to children)
                    if not self._has_descendant_access(path, Permission.READ, ctx):  # type: ignore[attr-defined]  # allowed
                        return False
                else:
                    # Issue #1147: OPTIMIZATION for real files - use direct permission check (O(1))
                    # instead of _has_descendant_access (O(n) fallback).
                    # Real files have no descendants, so descendant check is unnecessary.
                    # This reduces exists() latency from 300-500ms to 10-20ms.
                    if not self._permission_enforcer.check(path, Permission.READ, ctx):
                        # No direct READ permission = treat as non-existent for security
                        return False

            # Check if file exists explicitly
            if self.metadata.exists(path):
                return True
            # Return implicit directory status (already computed above)
            return is_implicit_dir
        except Exception:  # InvalidPathError
            return False

    @rpc_expose(description="Check existence of multiple paths in single call")
    def exists_batch(
        self, paths: list[str], context: OperationContext | None = None
    ) -> dict[str, bool]:
        """
        Check existence of multiple paths in a single call (Issue #859).

        This reduces network round trips when checking many paths at once.
        Processing 10 paths requires 1 round trip instead of 10.

        Args:
            paths: List of virtual paths to check
            context: Operation context for permission checks (uses default if None)

        Returns:
            Dictionary mapping each path to its existence status (True/False)

        Performance:
            - Single RPC call instead of N calls
            - 10x fewer round trips for multi-path operations
            - Each path is checked independently (errors don't affect others)

        Examples:
            >>> results = nx.exists_batch(["/file1.txt", "/file2.txt", "/missing.txt"])
            >>> print(results)
            {"/file1.txt": True, "/file2.txt": True, "/missing.txt": False}
        """
        results: dict[str, bool] = {}
        for path in paths:
            try:
                results[path] = self.sys_access(path, context=context)
            except Exception as exc:
                # Any error means file doesn't exist or isn't accessible
                logger.debug("Exists check failed for %s: %s", path, exc)
                results[path] = False
        return results

    @rpc_expose(description="Get metadata for multiple paths in single call")
    def metadata_batch(
        self, paths: list[str], context: OperationContext | None = None
    ) -> dict[str, dict[str, Any] | None]:
        """
        Get metadata for multiple paths in a single call (Issue #859).

        This reduces network round trips when fetching metadata for many files.
        Processing 10 paths requires 1 round trip instead of 10.

        Args:
            paths: List of virtual paths to get metadata for
            context: Operation context for permission checks (uses default if None)

        Returns:
            Dictionary mapping each path to its metadata dict or None if not found.
            Metadata includes: path, size, etag, mime_type, created_at, modified_at,
            version, zone_id, is_directory.

        Performance:
            - Single RPC call instead of N calls
            - 10x fewer round trips for multi-path operations
            - Leverages batch metadata fetch from database

        Examples:
            >>> results = nx.metadata_batch(["/file1.txt", "/missing.txt"])
            >>> print(results["/file1.txt"]["size"])
            1024
            >>> print(results["/missing.txt"])
            None
        """
        results: dict[str, dict[str, Any] | None] = {}

        # Validate paths and collect valid ones
        valid_paths: list[str] = []
        for path in paths:
            try:
                validated = self._validate_path(path)
                valid_paths.append(validated)
            except Exception as exc:
                logger.debug("Path validation failed in metadata_batch for %s: %s", path, exc)
                results[path] = None

        # Batch fetch metadata from database
        if valid_paths and hasattr(self.metadata, "get_batch"):
            batch_metadata = self.metadata.get_batch(valid_paths)
        else:
            # Fallback to individual fetches if get_batch not available
            batch_metadata = {p: self.metadata.get(p) for p in valid_paths}

        # Process results with permission checks
        for path in valid_paths:
            try:
                meta = batch_metadata.get(path)

                if meta is None:
                    results[path] = None
                    continue

                # Check permission if enforcement enabled
                if self._enforce_permissions:  # type: ignore[attr-defined]  # allowed
                    ctx = context if context is not None else self._default_context
                    if not self._has_descendant_access(path, Permission.READ, ctx):  # type: ignore[attr-defined]  # allowed
                        results[path] = None
                        continue

                # Check if it's a directory
                is_dir = self.sys_is_directory(path, context=context)  # type: ignore[attr-defined]  # allowed

                results[path] = {
                    "path": meta.path,
                    "backend_name": meta.backend_name,
                    "physical_path": meta.physical_path,
                    "size": meta.size,
                    "etag": meta.etag,
                    "mime_type": meta.mime_type,
                    "created_at": meta.created_at,
                    "modified_at": meta.modified_at,
                    "version": meta.version,
                    "zone_id": meta.zone_id,
                    "is_directory": is_dir,
                }
            except Exception as exc:
                logger.debug("Failed to build metadata result for %s: %s", path, exc)
                results[path] = None

        return results

    @rpc_expose(description="Delete multiple files/directories")
    def delete_bulk(
        self,
        paths: list[str],
        recursive: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, dict]:
        """
        Delete multiple files or directories in a single operation.

        Each path is processed independently - failures on one path don't affect others.
        Directories require recursive=True to delete non-empty directories.

        Args:
            paths: List of virtual paths to delete
            recursive: If True, delete non-empty directories (like rm -rf)
            context: Optional operation context for permission checks

        Returns:
            Dictionary mapping each path to its result:
                {"success": True} or {"success": False, "error": "error message"}

        Example:
            >>> results = nx.delete_bulk(['/a.txt', '/b.txt', '/folder'])
            >>> for path, result in results.items():
            ...     if result['success']:
            ...         print(f"Deleted {path}")
            ...     else:
            ...         print(f"Failed {path}: {result['error']}")
        """
        self._check_zone_writable(context)  # Issue #2061: write-gating
        results = {}
        for path in paths:
            try:
                path = self._validate_path(path)
                meta = self.metadata.get(path)

                # Check for implicit directory (exists because it has files beneath it)
                is_implicit_dir = meta is None and self.metadata.is_implicit_directory(path)

                if meta is None and not is_implicit_dir:
                    results[path] = {"success": False, "error": "File not found"}
                    continue

                # Check if this is a directory (explicit or implicit)
                is_dir = is_implicit_dir or (meta and meta.mime_type == "inode/directory")

                if is_dir:
                    # Use rmdir for directories
                    self._rmdir_internal(
                        path, recursive=recursive, context=context, is_implicit=is_implicit_dir
                    )
                else:
                    # Use delete for files
                    self.sys_unlink(path, context=context)

                results[path] = {"success": True}
            except Exception as e:
                results[path] = {"success": False, "error": str(e)}

        return results

    def _rmdir_internal(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | None = None,
        is_implicit: bool | None = None,
    ) -> None:
        """Internal rmdir implementation without RPC decoration.

        Args:
            path: Directory path to remove
            recursive: If True, delete non-empty directories
            context: Operation context for permission checks
            is_implicit: If True, directory is implicit (no metadata, exists due to child files).
                        If None, will be auto-detected.
        """
        import errno

        path = self._validate_path(path)
        zone_id, agent_id, is_admin = self._get_routing_params(context)

        route = self.router.route(
            path,
            is_admin=is_admin,
            check_write=True,
        )

        if route.readonly:
            raise PermissionError(f"Cannot remove read-only directory: {path}")

        # PRE-INTERCEPT: pre-write hooks (Issue #899)
        from nexus.contracts.vfs_hooks import WriteHookContext as _WHC

        self._dispatch.intercept_pre_write(_WHC(path=path, content=b"", context=context))

        # Check if path exists (explicit or implicit)
        meta = self.metadata.get(path)
        if is_implicit is None:
            is_implicit = meta is None and self.metadata.is_implicit_directory(path)

        if meta is None and not is_implicit:
            raise NexusFileNotFoundError(path)

        # Check if it's a directory (skip for implicit dirs - they're always directories)
        if meta is not None and meta.mime_type != "inode/directory":
            raise OSError(errno.ENOTDIR, "Not a directory", path)

        # Get files in directory
        dir_path = path if path.endswith("/") else path + "/"
        files_in_dir = self.metadata.list(dir_path)

        if files_in_dir and not recursive:
            raise OSError(errno.ENOTEMPTY, "Directory not empty", path)

        if recursive and files_in_dir:
            # Delete content from backend for each file
            _errors: list[str] = []
            for file_meta in files_in_dir:
                if file_meta.etag and file_meta.mime_type != "inode/directory":
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
            file_paths = [file_meta.path for file_meta in files_in_dir]
            self.metadata.delete_batch(file_paths)

        # Remove directory in backend
        with contextlib.suppress(NexusFileNotFoundError):
            route.backend.rmdir(route.backend_path, recursive=recursive)

        # Delete the directory metadata (only if explicit directory)
        if not is_implicit:
            self.metadata.delete(path)

    @rpc_expose(description="Rename/move multiple files")
    def rename_bulk(
        self,
        renames: list[tuple[str, str]],
        context: OperationContext | None = None,
    ) -> dict[str, dict]:
        """
        Rename/move multiple files in a single operation.

        Each rename is processed independently - failures on one don't affect others.
        This is a metadata-only operation (instant, regardless of file size).

        Args:
            renames: List of (old_path, new_path) tuples
            context: Optional operation context for permission checks

        Returns:
            Dictionary mapping each old_path to its result:
                {"success": True, "new_path": "..."} or {"success": False, "error": "..."}

        Example:
            >>> results = nx.rename_bulk([
            ...     ('/old1.txt', '/new1.txt'),
            ...     ('/old2.txt', '/new2.txt'),
            ... ])
            >>> for old_path, result in results.items():
            ...     if result['success']:
            ...         print(f"Renamed {old_path} -> {result['new_path']}")
        """
        results = {}
        for old_path, new_path in renames:
            try:
                self.sys_rename(old_path, new_path, context=context)
                results[old_path] = {"success": True, "new_path": new_path}
            except Exception as e:
                results[old_path] = {"success": False, "error": str(e)}

        return results

    def register_observe(self, observer: Any) -> None:
        """Register a mutation observer (OBSERVE phase, Issue #900)."""
        self._dispatch.register_observe(observer)

    # ------------------------------------------------------------------
    # ReBAC delegation stubs (Issue #2033)
    # Previously on NexusFSReBACMixin, now forwarded to rebac_service.
    # Generated dynamically below via _rebac_delegate().
    # ------------------------------------------------------------------

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

    # --- Search (sys_readdir/glob/grep) ---

    @rpc_expose(description="List directory entries")
    def sys_readdir(
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

    # _run_async: replaced by direct run_sync() calls (Issue #1381)

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

    def close(self) -> None:
        """Close the filesystem and release resources."""
        # Stop DeferredPermissionBuffer first to flush pending permissions
        if hasattr(self, "_deferred_permission_buffer") and self._deferred_permission_buffer:
            self._deferred_permission_buffer.stop()

        # Flush write observer pre-buffer (CLI mode: events buffered in memory
        # because PipeManager was never injected). Must happen before
        # record_store.close() since flush needs the DB connection.
        write_observer = (
            getattr(self._system_services, "write_observer", None)
            if self._system_services
            else None
        )
        if write_observer is not None and hasattr(write_observer, "flush_sync"):
            try:
                count = write_observer.flush_sync()
                if count:
                    import logging

                    logging.getLogger(__name__).debug(
                        "NexusFS.close: flushed %d write observer events", count
                    )
            except Exception as exc:
                import logging

                logging.getLogger(__name__).debug(
                    "NexusFS.close: flush_sync failed (best-effort): %s", exc
                )

        # Close metadata store
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
