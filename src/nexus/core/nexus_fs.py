"""Unified filesystem implementation for Nexus."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import json
import logging
import threading
from datetime import UTC, datetime, timedelta
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
from nexus.core.export_import import (
    ExportFilter,
    ImportOptions,
    ImportResult,
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
        """Initialize NexusFS kernel.

        Args:
            backend: Backend instance for file storage (LocalBackend, GCSBackend, etc.)
            metadata_store: MetastoreABC instance (RaftMetadataStore or custom)
            record_store: Optional RecordStoreABC for Services layer (ReBAC, Audit, etc.)
            cache_store: Optional CacheStoreABC for ephemeral KV+PubSub. Defaults to NullCacheStore.
            is_admin: Whether this instance has admin privileges (default: False)
            custom_namespaces: Additional custom namespace configurations
            cache: Cache configuration (LRU sizes, content cache). Defaults to CacheConfig().
            permissions: Permission enforcement config. Defaults to PermissionConfig().
            distributed: Distributed coordination config. Defaults to DistributedConfig().
            memory: Memory paging config. Defaults to MemoryConfig().
            parsing: File parsing config. Defaults to ParseConfig().
            services: Injected service dependencies. Defaults to KernelServices().
            parse_fn: Pre-built parse callback ``(bytes, str) -> bytes | None``
                for virtual views. Created by factory.py via ParsersBrick.create_parse_fn().
            parser_registry: Injected ParserRegistry from ParsersBrick (Issue #1523).
            provider_registry: Injected ProviderRegistry from ParsersBrick (Issue #1523).
        """
        # Apply defaults — config dataclasses are SSOT for default values
        cache = cache or CacheConfig()
        permissions = permissions or PermissionConfig()
        distributed = distributed or DistributedConfig()
        memory = memory or MemoryConfig()
        parsing = parsing or ParseConfig()
        svc = services or KernelServices()

        # Store config objects for introspection
        self._cache_config = cache
        self._perm_config = permissions
        self._distributed_config = distributed
        self._memory_config_obj = memory
        self._parse_config = parsing
        self._services = svc

        # Store config for OAuth factory and other components that need it
        self._config: Any | None = None

        # Map config fields to internal attributes used throughout codebase
        self._enable_memory_paging = memory.enable_paging
        self._memory_main_capacity = memory.main_capacity
        self._memory_recall_max_age_hours = memory.recall_max_age_hours
        self._enforce_permissions = permissions.enforce
        self._enforce_zone_isolation = permissions.enforce_zone_isolation
        self._audit_strict_mode = permissions.audit_strict_mode
        self.allow_admin_bypass = permissions.allow_admin_bypass
        self.auto_parse = parsing.auto_parse
        self.is_admin = is_admin

        # Initialize content cache — accept pre-built or create (Issue #657)
        if content_cache is not None:
            backend.content_cache = content_cache
        elif cache.enable_content_cache and backend.has_root_path is True:
            backend.content_cache = ContentCache(max_size_mb=cache.content_cache_size_mb)

        # Store backend
        self.backend = backend

        # Initialize metadata store (Task #14: Dependency Injection)
        self.metadata: MetastoreABC = metadata_store

        # Initialize record store (Task #14: Four Pillars)
        self._record_store = record_store
        if record_store is not None:
            self._sql_engine = record_store.engine
            self._db_session_factory = record_store.session_factory
            self.SessionLocal = self._db_session_factory
        else:
            self._sql_engine = None
            self._db_session_factory = None
            self.SessionLocal = None

        # Initialize cache store (Task #22: Fourth Pillar)
        self.cache_store: CacheStoreABC = (
            cache_store if cache_store is not None else NullCacheStore()
        )

        # Initialize path router (Task #23: injectable)
        if svc.router is not None:
            self.router = svc.router
        else:
            self.router = PathRouter()
            if custom_namespaces:
                for ns_config in custom_namespaces:
                    self.router.register_namespace(ns_config)

        # Mount backend
        self.router.add_mount("/", self.backend, priority=0)

        # Parser registries — injected by factory via ParsersBrick (Issue #1523)
        if parser_registry is not None:
            self.parser_registry = parser_registry
        else:
            # Fallback: create default registry for direct construction (tests, etc.)
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

        # Parse callback for virtual views — injected by factory.py (Issue #668)
        self._virtual_view_parse_fn = parse_fn

        # Track active parser threads for graceful shutdown
        self._parser_threads: list[threading.Thread] = []
        self._parser_threads_lock = threading.Lock()

        # Create default context
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
        # Services layer (Task #23: pure dependency injection via KernelServices)
        # =====================================================================
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

        # Issue #1752: Transactional snapshot service (optional)
        self._snapshot_service = getattr(svc, "snapshot_service", None)

        # Kernel protocol services + async wrappers (Issue #1502)
        self._agent_registry = svc.agent_registry
        self._namespace_manager = svc.namespace_manager
        self._async_agent_registry = svc.async_agent_registry
        self._async_namespace_manager = svc.async_namespace_manager
        self._async_vfs_router = svc.async_vfs_router

        # Infrastructure services (previously created inline, now injected)
        self._event_bus = svc.event_bus
        self._lock_manager = svc.lock_manager
        self.enable_workflows = distributed.enable_workflows
        self.workflow_engine = svc.workflow_engine

        # Auth services — injected from server layer (Issue #1519, 3A)
        self._api_key_creator = svc.api_key_creator

        # Initialize OAuth token manager (lazy initialization in mixin)
        self._token_manager = None

        # Initialize semantic search - lazy initialization
        self._semantic_search = None

        # Initialize Memory API
        self._memory_api: Memory | None = None
        self._memory_config: dict[str, str | None] = {
            "zone_id": None,
            "user_id": None,
            "agent_id": None,
        }

        # Issue #372: Sandbox manager - lazy initialization
        from nexus.sandbox.sandbox_manager import SandboxManager

        self._sandbox_manager: SandboxManager | None = None

        # v0.8.0: Subscription manager for webhook notifications (set by server)
        self.subscription_manager: Any = None

        # Distributed coordination clients (may be set by factory)
        self._coordination_client: Any = None
        self._event_client: Any = None

        # VFS lock manager — accept pre-built or create (Issue #657)
        if vfs_lock_manager is not None:
            self._vfs_lock_manager = vfs_lock_manager
        else:
            from nexus.core.lock_fast import create_vfs_lock_manager

            self._vfs_lock_manager = create_vfs_lock_manager()
        logger.info("VFS lock manager initialized (%s)", type(self._vfs_lock_manager).__name__)

        # VFS Hook Pipeline (Issue #2033, Phase 4/5)
        from nexus.core.vfs_hooks import VFSHookPipeline

        self._hook_pipeline: VFSHookPipeline = svc.hook_pipeline or VFSHookPipeline()

        # Wire self-dependent services (require self reference)
        self._wire_services()

        # Issue #2033 Phase 5: Register concrete hook implementations on the pipeline.
        # Hooks are wired AFTER _wire_services() so all dependencies are available.
        self._register_vfs_hooks()

        # Issue #1169: Read Set-Aware Cache for precise invalidation
        # Wraps the metadata cache with read-set-aware invalidation.
        # Falls back to path-based invalidation for entries without read sets.
        self._read_set_cache = None
        metadata_cache = None
        if hasattr(self.metadata, "_cache"):
            metadata_cache = self.metadata._cache

        if metadata_cache is not None and self._cache_config.enable_metadata_cache:
            from nexus.core.read_set import ReadSetRegistry
            from nexus.storage.read_set_cache import ReadSetAwareCache

            self._read_set_registry = ReadSetRegistry()
            self._read_set_cache = ReadSetAwareCache(
                base_cache=metadata_cache,
                registry=self._read_set_registry,
            )
            self._read_tracking_enabled = True

        # Issue #1519: Cache observer — decouples kernel from ReadSetAwareCache
        self._cache_observer = svc.cache_observer
        if self._cache_observer is None and self._read_set_cache is not None:
            from nexus.core.cache_invalidation import ReadSetCacheObserver

            self._cache_observer = ReadSetCacheObserver(self._read_set_cache)

        # OPTIMIZATION: Initialize TRAVERSE permissions and Tiger Cache
        self._init_performance_optimizations()

    def _wire_services(self) -> None:
        """Wire services that require a reference to self (NexusFS).

        Called at end of __init__. Services follow "accept or build" pattern
        (Issue #1519, 4B): if pre-built via KernelServices, use that instance;
        otherwise build internally. This enables factory pre-wiring and
        test-time mock injection.
        """
        svc = self._services

        # VersionService: injected by factory (Task #45)
        self.version_service = svc.version_service

        # Lazy-import services to avoid core/ → services/ top-level coupling (#1519)
        from nexus.services.llm_service import LLMService
        from nexus.services.mcp_service import MCPService
        from nexus.services.mount_service import MountService
        from nexus.services.oauth_service import OAuthService
        from nexus.services.search_service import SearchService

        # WorkspaceRPCService: Replaces NexusFS workspace/memory/snapshot facades
        from nexus.services.workspace_rpc_service import WorkspaceRPCService

        self._workspace_rpc_service = WorkspaceRPCService(
            workspace_manager=self._workspace_manager,
            workspace_registry=self._workspace_registry,
            vfs=self,
            default_context=self._default_context,
            snapshot_service=self._snapshot_service,
        )

        # AgentRPCService: Replaces NexusFS agent management/lifecycle facades
        from nexus.services.agents.agent_rpc_service import AgentRPCService

        self._agent_rpc_service = AgentRPCService(
            vfs=self,
            metastore=self.metadata,
            session_factory=self.SessionLocal,
            agent_registry=self._agent_registry,
            entity_registry=self._entity_registry,
            rebac_manager=self._rebac_manager,
            wallet_provisioner=self._wallet_provisioner,
            api_key_creator=self._api_key_creator,
            key_service=getattr(self, "_key_service", None),
            rmdir_fn=self.rmdir,
            rebac_create_fn=self.rebac_create,
            rebac_list_tuples_fn=self.rebac_list_tuples,
            rebac_delete_fn=self.rebac_delete,
        )

        # UserProvisioningService: Replaces NexusFS provision/deprovision facades
        from nexus.services.user_provisioning import UserProvisioningService

        self._user_provisioning_service = UserProvisioningService(
            vfs=self,
            session_factory=self.SessionLocal,
            entity_registry=self._entity_registry,
            api_key_creator=self._api_key_creator,
            backend=getattr(self, "backend", None),
            rebac_manager=self._rebac_manager,
            rmdir_fn=self.rmdir,
            rebac_create_fn=self.rebac_create,
            rebac_delete_fn=self.rebac_delete,
            register_workspace_fn=self.register_workspace,
            register_agent_fn=self.register_agent,
            skills_import_fn=getattr(self, "skills_import", None),
            list_cache=getattr(self, "_list_cache", None),
            exists_cache=getattr(self, "_exists_cache", None),
        )

        # SandboxRPCService: Replaces NexusFS sandbox management facades
        from nexus.sandbox.sandbox_rpc_service import SandboxRPCService

        self._sandbox_rpc_service = SandboxRPCService(
            session_factory=self.SessionLocal,
            default_context=self._default_context,
            config=getattr(self, "_config", None),
        )

        # MetadataExportService: Replaces NexusFS export/import facades
        from nexus.services.metadata_export import MetadataExportService

        self._metadata_export_service = MetadataExportService(
            metastore=self.metadata,
            default_context=self._default_context,
        )

        # ACERPCService: Replaces NexusFS ACE trajectory/playbook facades
        from nexus.services.ace_rpc_service import ACERPCService

        self._ace_rpc_service = ACERPCService(
            session_factory=self.SessionLocal,
            backend=self.backend,
            default_context=self._default_context,
            entity_registry=self._entity_registry,
            ensure_entity_registry_fn=self._ensure_entity_registry,
        )

        # ReBACService: Permission and access control operations
        if svc.rebac_service is not None:
            self.rebac_service = svc.rebac_service
        else:
            from nexus.services.rebac_service import ReBACService

            self.rebac_service = ReBACService(
                rebac_manager=self._rebac_manager,
                enforce_permissions=self._enforce_permissions,
                enable_audit_logging=True,
                circuit_breaker=self._services.rebac_circuit_breaker,
            )

        # MountService: Dynamic backend mounting operations
        self.mount_service = MountService(
            router=self.router,
            mount_manager=self.mount_manager,
            nexus_fs=self,
        )

        # MCPService: Model Context Protocol operations
        self.mcp_service = MCPService(nexus_fs=self)

        # LLMService: LLM integration operations
        self.llm_service = LLMService(nexus_fs=self)
        from nexus.services.subsystems.llm_subsystem import LLMSubsystem

        self._llm_subsystem = LLMSubsystem(llm_service=self.llm_service)

        # OAuthService: OAuth authentication operations
        self.oauth_service = OAuthService(
            oauth_factory=None,
            token_manager=None,
            nexus_fs=self,
        )

        # Shared gateway for all extracted services (Issue #1287)
        from nexus.services.gateway import NexusFSGateway

        self._gateway = NexusFSGateway(self)

        # Mount/sync services: accept pre-built or create (Issue #655)
        if svc.mount_core_service is not None:
            self._mount_core_service = svc.mount_core_service
        else:
            from nexus.services.mount_core_service import MountCoreService

            self._mount_core_service = MountCoreService(self._gateway)

        if svc.sync_service is not None:
            self._sync_service = svc.sync_service
        else:
            from nexus.services.sync_service import SyncService

            self._sync_service = SyncService(self._gateway)

        if svc.sync_job_service is not None:
            self._sync_job_service = svc.sync_job_service
        else:
            from nexus.services.sync_job_service import SyncJobService

            self._sync_job_service = SyncJobService(self._gateway, self._sync_service)

        if svc.mount_persist_service is not None:
            self._mount_persist_service = svc.mount_persist_service
        else:
            from nexus.services.mount_persist_service import MountPersistService

            self._mount_persist_service = MountPersistService(
                mount_manager=getattr(self, "mount_manager", None),
                mount_service=self._mount_core_service,
                sync_service=self._sync_service,
            )

        # TaskQueueService: accept pre-built (Issue #655)
        if svc.task_queue_service is not None:
            self.task_queue_service = svc.task_queue_service

        # SkillService: Skill management
        from nexus.services.skill_service import SkillService as _SkillService

        self.skill_service = _SkillService(gateway=self._gateway)

        # SearchService: Search operations
        if svc.search_service is not None:
            self.search_service = svc.search_service
        else:
            self.search_service = SearchService(
                metadata_store=self.metadata,
                permission_enforcer=self._permission_enforcer,
                router=self.router,
                rebac_manager=self._rebac_manager,
                enforce_permissions=self._enforce_permissions,
                default_context=self._default_context,
                record_store=self._record_store,
                gateway=self._gateway,
            )

        # ShareLinkService: Share link operations
        from nexus.services.share_link_service import ShareLinkService

        self.share_link_service = ShareLinkService(
            gateway=self._gateway,
            enforce_permissions=self._enforce_permissions,
        )

        # EventsService: File watching + advisory locking
        if svc.events_service is not None:
            self.events_service = svc.events_service
        else:
            from nexus.services.events_service import EventsService

            metadata_cache = None
            if hasattr(self.metadata, "_cache"):
                metadata_cache = self.metadata._cache

            self.events_service = EventsService(
                backend=self.backend,
                event_bus=self._event_bus,
                lock_manager=self._lock_manager,
                zone_id=None,
                metadata_cache=metadata_cache,
            )

    def _register_vfs_hooks(self) -> None:
        """Register concrete hook implementations on the VFS pipeline.

        Called after _wire_services() so all dependencies (ReBAC manager,
        parser registry, tiger cache) are available for injection.

        Issue #2033 Phase 5: hooks run via _hook_pipeline.run_post_*() in
        nexus_fs_core.py read/write/delete/rename paths.
        """
        from nexus.core.vfs_hook_impls import (
            AutoParseWriteHook,
            DynamicViewerReadHook,
            TigerCacheRenameHook,
        )

        pipeline = self._hook_pipeline

        # --- DynamicViewerReadHook (post-read: column-level CSV filtering) ---
        rebac_mgr = getattr(self, "_rebac_manager", None)
        has_viewer = (
            rebac_mgr is not None
            and hasattr(self, "_get_subject_from_context")
            and hasattr(self, "get_dynamic_viewer_config")
            and hasattr(self, "apply_dynamic_viewer_filter")
        )
        if has_viewer:
            pipeline.register_read_hook(
                DynamicViewerReadHook(
                    get_subject=self._get_subject_from_context,
                    get_viewer_config=self.get_dynamic_viewer_config,  # type: ignore[attr-defined]
                    apply_filter=self.apply_dynamic_viewer_filter,  # type: ignore[attr-defined]
                )
            )

        # --- AutoParseWriteHook (post-write: fire-and-forget background parsing) ---
        parser_reg = getattr(self, "parser_registry", None)
        if parser_reg is not None and getattr(self, "auto_parse", False):
            pipeline.register_write_hook(
                AutoParseWriteHook(
                    get_parser=parser_reg.get_parser,
                    parse_fn=self.parse,  # type: ignore[attr-defined]
                )
            )

        # --- TigerCacheRenameHook (post-rename: bitmap updates on move) ---
        tiger_cache = getattr(rebac_mgr, "_tiger_cache", None) if rebac_mgr else None
        if tiger_cache is not None:

            def _metadata_list_iter(
                prefix: str, recursive: bool = True, zone_id: str = "root"
            ) -> Any:
                return self.metadata.list(prefix=prefix, recursive=recursive)

            pipeline.register_rename_hook(
                TigerCacheRenameHook(
                    tiger_cache=tiger_cache,
                    metadata_list_iter=_metadata_list_iter,
                )
            )

        logger.info(
            "[VFS-HOOKS] Registered: read=%d, write=%d, delete=%d, rename=%d",
            pipeline.read_hook_count,
            pipeline.write_hook_count,
            pipeline.delete_hook_count,
            pipeline.rename_hook_count,
        )

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

    def _init_performance_optimizations(self) -> None:
        """Initialize performance optimizations for permission checks.

        This method:
        1. Syncs tiger_resource_map from existing metadata (Issue #934)
        2. Grants TRAVERSE permission on implicit directories (enables O(1) stat)
        3. Warms the Tiger Cache for faster subsequent permission checks
        4. Starts background worker for Tiger Cache queue processing

        Called automatically during __init__. Can be called manually to refresh.
        """
        import os

        # Check if optimizations are enabled (default: True)
        # Set NEXUS_DISABLE_PERF_OPTIMIZATIONS=true to disable
        if os.getenv("NEXUS_DISABLE_PERF_OPTIMIZATIONS", "false").lower() in ("true", "1", "yes"):
            logger.debug("Performance optimizations disabled via environment variable")
            return

        try:
            # 1. Sync tiger_resource_map from existing metadata (Issue #934)
            # This MUST happen BEFORE cache warming so Tiger Cache can find resources
            # Fixes chicken-and-egg: resources only added during check_access(),
            # but check_access() returns cache miss because map is empty
            if os.getenv("NEXUS_SYNC_TIGER_RESOURCE_MAP", "true").lower() in (
                "true",
                "1",
                "yes",
            ):
                synced = self._sync_resource_map_from_metadata()
                if synced > 0:
                    logger.info(f"Synced {synced} resources to Tiger resource map")

            # 2. TRAVERSE on implicit directories is now AUTOMATIC
            # The permission check auto-allows TRAVERSE for any implicit directory
            # when the user is authenticated. No manual grants needed!
            # See: permissions.py _check_rebac() TRAVERSE handling

            # 3. Warm Tiger Cache (optional, can be slow for large systems)
            # Only warm if explicitly enabled via environment variable
            if os.getenv("NEXUS_WARM_TIGER_CACHE", "false").lower() in (
                "true",
                "1",
                "yes",
            ) and hasattr(self, "warm_tiger_cache"):
                entries = self.warm_tiger_cache(zone_id=self._default_context.zone_id)
                if entries > 0:
                    logger.info(f"Warmed Tiger Cache with {entries} entries")

            # 4. Start Tiger Cache background worker
            # This processes permission change queue to keep Tiger Cache up-to-date
            self._start_tiger_cache_worker()

        except Exception as e:
            # Don't fail initialization if optimizations fail
            logger.warning(f"Failed to initialize performance optimizations: {e}")

    def _sync_resource_map_from_metadata(self) -> int:
        """Populate tiger_resource_map from existing metadata.

        Issue #934: Enables Tiger Cache to work for pre-existing files by
        ensuring all files have integer IDs in the resource map.

        This fixes the chicken-and-egg problem where:
        - Tiger Cache needs resource IDs to check access
        - Resource IDs were only created during permission checks
        - Permission checks returned cache miss → never populated

        Returns:
            Number of resources synced to the map

        Performance:
            ~5 seconds for 6,000 files (one-time startup cost)

        Environment:
            NEXUS_SYNC_TIGER_RESOURCE_MAP: Set to "false" to disable (default: true)
        """


        # Check if Tiger Cache is available
        if not hasattr(self, "_rebac_manager"):
            logger.debug("No ReBAC manager - skipping resource map sync")
            return 0

        tiger_cache = getattr(self._rebac_manager, "_tiger_cache", None)
        if not tiger_cache:
            logger.debug("Tiger Cache disabled - skipping resource map sync")
            return 0

        resource_map = getattr(tiger_cache, "_resource_map", None)
        if not resource_map:
            logger.debug("No resource map in Tiger Cache - skipping sync")
            return 0

        try:
            # Stream files from metadata store instead of materializing full list
            count = 0
            log_interval = 1000

            for meta in self.metadata.list_iter("/", recursive=True):
                # Register resource in the map (idempotent operation)
                # Note: zone_id removed from resource map (Issue #xyz)
                resource_map.get_or_create_int_id(
                    resource_type="file",
                    resource_id=meta.path,
                )
                count += 1

                # Log progress for large datasets
                if count % log_interval == 0:
                    logger.debug(f"Tiger resource map sync progress: {count} resources...")

            logger.info(f"Tiger resource map sync complete: {count} resources")
            return count

        except Exception as e:
            logger.warning(f"Failed to sync resource map from metadata: {e}")
            return 0

    def _start_tiger_cache_worker(self) -> None:
        """Start background thread for Tiger Cache queue processing.

        NOTE: With write-through implemented, automatic queue processing is
        DISABLED by default. Write-through handles grants/revokes immediately.

        Queue processing is only needed for:
        - Cold start cache warming (use warm_tiger_cache() explicitly)
        - Bulk migrations
        - Group permission inheritance changes

        To enable automatic queue processing, set:
            NEXUS_ENABLE_TIGER_WORKER=true
        """
        import os
        import threading

        # Queue processor is DISABLED by default (write-through handles normal ops)
        # Enable explicitly with NEXUS_ENABLE_TIGER_WORKER=true
        if os.getenv("NEXUS_ENABLE_TIGER_WORKER", "false").lower() not in ("true", "1", "yes"):
            logger.debug("Tiger Cache queue worker disabled (write-through handles grants)")
            return

        # Don't start if already running
        worker_thread = getattr(self, "_tiger_worker_thread", None)
        if worker_thread is not None and worker_thread.is_alive():
            return

        # Worker interval in seconds (default: 1 second)
        interval = float(os.getenv("NEXUS_TIGER_WORKER_INTERVAL", "1.0"))

        # Shutdown flag
        self._tiger_worker_stop = threading.Event()

        def worker_loop() -> None:
            """Background worker loop for Tiger Cache queue processing.

            NOTE: With write-through implemented, this worker is mainly for legacy
            queue entries. New permission grants are handled immediately by
            persist_single_grant() in rebac_write.
            """
            while not self._tiger_worker_stop.is_set():
                try:
                    if hasattr(self, "process_tiger_cache_queue"):
                        # Process only 1 entry at a time to avoid blocking
                        # Each entry can take 10-40 seconds due to _compute_accessible_resources
                        processed = self.process_tiger_cache_queue(batch_size=1)
                        if processed > 0:
                            logger.debug(f"Tiger Cache worker processed {processed} updates")
                except Exception as e:
                    logger.warning(f"Tiger Cache worker error: {e}")

                # Sleep longer since write-through handles new grants
                # This worker is just for legacy queue cleanup
                self._tiger_worker_stop.wait(timeout=interval * 10)

            logger.debug("Tiger Cache worker stopped")

        # Start worker thread
        self._tiger_worker_thread = threading.Thread(
            target=worker_loop,
            name="tiger-cache-worker",
            daemon=True,  # Daemon thread - exits when main program exits
        )
        self._tiger_worker_thread.start()
        logger.debug(f"Tiger Cache worker started (interval={interval}s)")

    def stop_tiger_cache_worker(self) -> None:
        """Stop the Tiger Cache background worker.

        Call this during graceful shutdown to stop the worker thread.
        """
        if hasattr(self, "_tiger_worker_stop"):
            self._tiger_worker_stop.set()
        if hasattr(self, "_tiger_worker_thread") and self._tiger_worker_thread is not None:
            # Wait longer in test environments (check if pytest is running)
            import sys

            is_test = "pytest" in sys.modules
            timeout = 15.0 if is_test else 5.0
            self._tiger_worker_thread.join(timeout=timeout)

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
            # Get or create entity registry (v0.5.0: Pass SessionFactory instead of Session)
            self._ensure_entity_registry()

            # Create a session from SessionLocal
            session = self.SessionLocal()

            # Issue #1258: Create MemoryWithPaging if enabled, else standard Memory
            if self._enable_memory_paging:
                from nexus.services.memory.memory_with_paging import MemoryWithPaging

                # Try to get engine for VectorDatabase integration
                engine = None
                if self.SessionLocal is not None:
                    engine = self.SessionLocal.kw.get("bind")

                self._memory_api = MemoryWithPaging(
                    session=session,
                    backend=self.backend,
                    zone_id=self._memory_config.get("zone_id"),
                    user_id=self._memory_config.get("user_id"),
                    agent_id=self._memory_config.get("agent_id"),
                    entity_registry=self._entity_registry,
                    enable_paging=True,
                    main_capacity=self._memory_main_capacity,
                    recall_max_age_hours=self._memory_recall_max_age_hours,
                    engine=engine,
                    session_factory=self.SessionLocal,
                )
            else:
                from nexus.services.memory.memory_api import Memory

                self._memory_api = Memory(
                    session=session,
                    backend=self.backend,
                    zone_id=self._memory_config.get("zone_id"),
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
            user = getattr(self._default_context, "user_id", None)
            agent = self._default_context.agent_id
        elif hasattr(context, "agent_id"):
            user = getattr(context, "user_id", None)
            agent = context.agent_id
        elif isinstance(context, dict):
            user = context.get("user_id")
            agent = context.get("agent_id")
        else:
            user = getattr(self._default_context, "user_id", None)
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
        """Extract zone_id, agent_id, and is_admin from context for router.route().

        This is the critical fix for multi-tenancy: extract values from per-request context
        instead of using instance fields (which are shared across all requests in server mode).

        Args:
            context: Operation context with per-request values

        Returns:
            Tuple of (zone_id, agent_id, is_admin)
        """
        if context is None:
            # Use default context values for embedded mode
            return (
                self._default_context.zone_id,
                self._default_context.agent_id,
                self._default_context.is_admin,
            )

        # Extract from OperationContext object
        if not isinstance(context, dict):
            return context.zone_id, context.agent_id, getattr(context, "is_admin", self.is_admin)

        # Extract from dict (legacy)
        if isinstance(context, dict):
            return (
                context.get("zone_id", self._default_context.zone_id),
                context.get("agent_id", self._default_context.agent_id),
                context.get("is_admin", self.is_admin),
            )

        # Fallback to default context
        return (
            self._default_context.zone_id,
            self._default_context.agent_id,
            self._default_context.is_admin,
        )

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
        """Get Memory API instance with context-specific configuration.

        Args:
            context: Optional context dict with zone_id, user_id, agent_id

        Returns:
            Memory API instance
        """
        from nexus.services.memory.memory_api import Memory

        # Get or create entity registry
        self._ensure_entity_registry()

        # Create a session
        session = self.SessionLocal()

        # Parse context properly
        ctx = self._parse_context(context)

        return Memory(
            session=session,
            backend=self.backend,
            zone_id=ctx.zone_id or self._default_context.zone_id,
            user_id=ctx.user_id or self._default_context.user_id,
            agent_id=ctx.agent_id or self._default_context.agent_id,
            entity_registry=self._entity_registry,
        )

    def _parse_context(self, context: OperationContext | dict | None = None) -> OperationContext:
        """Parse context dict or OperationContext into OperationContext.

        Args:
            context: Optional context dict or OperationContext with user_id, groups, zone_id, etc.

        Returns:
            OperationContext instance
        """
        # If already an OperationContext, return as-is
        if isinstance(context, OperationContext):
            return context

        if context is None:
            context = {}

        return OperationContext(
            user_id=context.get("user_id", "system"),
            groups=context.get("groups", []),
            zone_id=context.get("zone_id"),
            agent_id=context.get("agent_id"),
            is_admin=context.get("is_admin", False),
            is_system=context.get("is_system", False),
        )

    def _ensure_entity_registry(self) -> EntityRegistry:
        """Lazily create and cache an EntityRegistry instance.

        Consolidates 7 deferred import sites (Issue #1291).
        """
        if self._entity_registry is None:
            from nexus.rebac.entity_registry import EntityRegistry

            self._entity_registry = EntityRegistry(self.SessionLocal)
        return self._entity_registry

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

    def _check_permission(
        self,
        path: str,
        permission: Permission,
        context: OperationContext | None = None,
        file_metadata: FileMetadata | None = None,
    ) -> None:
        """Check if operation is permitted.

        Args:
            path: Virtual file path
            permission: Permission to check (READ, WRITE, EXECUTE)
            context: Optional operation context (defaults to self._default_context)
            file_metadata: Pre-fetched metadata for owner fast-path (avoids redundant
                metadata lookup when caller already has it)

        Raises:
            PermissionError: If access is denied
        """


        # Skip if permission enforcement is disabled
        if not self._enforce_permissions:
            return

        # Use default context if none provided
        ctx_raw = context or self._default_context
        assert isinstance(ctx_raw, OperationContext), "Context must be OperationContext"
        ctx: OperationContext = ctx_raw

        # P0-4: Zone boundary security check (Issue #819)
        # Even admins need zone boundary checks (unless they have MANAGE_ZONES capability)
        if ctx.is_admin and self._permission_enforcer:
            from nexus.rebac.permissions_enhanced import AdminCapability

            # Extract zone from path (format: /zone/{zone_id}/...)
            path_zone_id = None
            if path.startswith("/zone/"):
                parts = path[6:].split("/", 1)  # Remove "/zone/" prefix
                if parts:
                    path_zone_id = parts[0]

            # Check if admin is attempting cross-zone access without MANAGE_ZONES
            if (
                path_zone_id
                and ctx.zone_id
                and path_zone_id != ctx.zone_id
                and AdminCapability.MANAGE_ZONES not in ctx.admin_capabilities
            ):
                # Cross-zone access requires MANAGE_ZONES capability
                raise PermissionError(
                    f"Access denied: Cross-zone access requires MANAGE_ZONES capability. "
                    f"Context zone: {ctx.zone_id}, Path zone: {path_zone_id}"
                )

        # Skip permission checks for admin/system users during provisioning
        # This significantly speeds up operations like skill imports (82s -> ~10s)
        if ctx.is_admin or ctx.is_system:
            logger.debug(
                f"_check_permission: SKIPPED (admin/system bypass) - path={path}, permission={permission.name}, user={ctx.user_id}"
            )
            return

        logger.debug(
            f"_check_permission: path={path}, permission={permission.name}, user={ctx.user_id}, zone={getattr(ctx, 'zone_id', None)}"
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

        # Issue #920: O(1) owner fast-path check
        # If the file has posix_uid set and it matches the requesting user, skip ReBAC
        # This avoids expensive graph traversal for owner accessing their own files
        # Use pre-fetched metadata when available (avoids redundant FFI call)
        # Use pre-fetched metadata when path wasn't redirected to a virtual view's original
        file_meta = (
            file_metadata
            if (file_metadata is not None and permission_path == path)
            else self.metadata.get(permission_path)
        )
        if file_meta and file_meta.owner_id:
            subject_id = ctx.subject_id or ctx.user_id
            if file_meta.owner_id == subject_id:
                logger.debug(
                    f"  -> OWNER FAST-PATH: {subject_id} owns {permission_path}, skipping ReBAC"
                )
                return  # Owner has all permissions

        # Check permission using enforcer (ReBAC graph traversal)
        result = self._permission_enforcer.check(permission_path, permission, ctx)
        logger.debug(f"  -> permission_enforcer.check returned: {result}")

        if not result:
            raise PermissionError(
                f"Access denied: User '{ctx.user_id}' does not have {permission.name} "
                f"permission for '{path}'"
            )

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
        """
        Create a directory.

        Args:
            path: Virtual path to directory
            parents: Create parent directories if needed (like mkdir -p)
            exist_ok: Don't raise error if directory exists
            context: Operation context with user, permissions, zone info (uses default if None)

        Raises:
            FileExistsError: If directory exists and exist_ok=False
            FileNotFoundError: If parent doesn't exist and parents=False
            InvalidPathError: If path is invalid
            BackendError: If operation fails
            AccessDeniedError: If access is denied (zone isolation or read-only namespace)
            PermissionError: If path is read-only or user doesn't have write permission on parent
        """
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
                self._check_permission(check_path, Permission.WRITE, ctx)

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
        """
        Remove a directory.

        Args:
            path: Virtual path to directory
            recursive: Remove non-empty directory (like rm -rf)
            subject: Subject performing the operation as (type, id) tuple
            context: Operation context (DEPRECATED, use subject instead)
            zone_id: Legacy zone ID (DEPRECATED)
            agent_id: Legacy agent ID (DEPRECATED)
            is_admin: Admin override flag

        Raises:
            OSError: If directory not empty and recursive=False
            NexusFileNotFoundError: If directory doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If operation fails
            AccessDeniedError: If access is denied (zone isolation or read-only namespace)
            PermissionError: If path is read-only
        """
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
        self._check_permission(path, Permission.WRITE, ctx)
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

        Workflow (Issue #919 optimization):
        1. Check DirectoryVisibilityCache first (O(1) cache hit)
        2. Check Tiger Cache direct access (O(1) bitmap lookup)
        3. If cache miss, compute from Tiger bitmap (O(bitmap) - no descendant enumeration!)
        4. Only fall back to slow O(n) path if Tiger Cache unavailable

        Args:
            path: Path to check (e.g., "/workspace")
            permission: Permission to check (e.g., Permission.READ)
            context: User context with subject info

        Returns:
            True if user has access to path OR any descendant, False otherwise

        Performance Notes:
            - Issue #919: Uses DirectoryVisibilityCache for O(1) lookups
            - Uses Tiger bitmap scan instead of N descendant queries
            - /workspace with 10K files: ~2000ms -> ~5ms
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
            Permission.TRAVERSE: "traverse",
        }
        rebac_permission = permission_map.get(permission, "read")
        zone_id = context.zone_id or "root"

        # =============================================================
        # Issue #919 OPTIMIZATION 1: Check DirectoryVisibilityCache (O(1))
        # =============================================================
        if hasattr(self, "_dir_visibility_cache") and self._dir_visibility_cache is not None:
            cached_visible = self._dir_visibility_cache.is_visible(
                zone_id=zone_id,
                subject_type=context.subject_type,
                subject_id=subject_id,
                dir_path=path,
            )
            if cached_visible is not None:
                logger.debug(
                    f"_has_descendant_access: DirVisCache HIT for {path} = {cached_visible}"
                )
                return cached_visible

        # =============================================================
        # OPTIMIZATION 2: Try Tiger Cache direct access (O(1) lookup)
        # =============================================================
        if hasattr(self._rebac_manager, "tiger_check_access"):
            tiger_result = self._rebac_manager.tiger_check_access(
                subject=subject_tuple,
                permission=rebac_permission,
                object=("file", path),
            )
            if tiger_result is True:
                # Cache this positive result
                if (
                    hasattr(self, "_dir_visibility_cache")
                    and self._dir_visibility_cache is not None
                ):
                    self._dir_visibility_cache.set_visible(
                        zone_id,
                        context.subject_type,
                        subject_id,
                        path,
                        True,
                        "direct_tiger_access",
                    )
                return True
            # If tiger_result is None, cache miss - continue with normal check
            # If tiger_result is False, explicitly denied - but still check descendants

        # =============================================================
        # OPTIMIZATION 3: Check direct access via rebac_check (fast path)
        # =============================================================
        direct_access = self.rebac_check(
            subject=subject_tuple,
            permission=rebac_permission,
            object=("file", path),
            zone_id=context.zone_id,
        )
        if direct_access:
            # Cache this positive result
            if hasattr(self, "_dir_visibility_cache") and self._dir_visibility_cache is not None:
                self._dir_visibility_cache.set_visible(
                    zone_id, context.subject_type, subject_id, path, True, "direct_rebac_access"
                )
            return True

        # =============================================================
        # Issue #919 OPTIMIZATION 4: Compute from Tiger bitmap (O(bitmap))
        # This is the KEY optimization - no descendant enumeration!
        # Instead of querying N descendants from metadata, scan the Tiger
        # bitmap of accessible resources for prefix matches.
        # =============================================================
        if hasattr(self, "_dir_visibility_cache") and self._dir_visibility_cache is not None:
            bitmap_result = self._dir_visibility_cache.compute_from_tiger_bitmap(
                zone_id=zone_id,
                subject_type=context.subject_type,
                subject_id=subject_id,
                dir_path=path,
                permission=rebac_permission,
            )
            if bitmap_result is not None:
                logger.debug(
                    f"_has_descendant_access: Tiger bitmap compute for {path} = {bitmap_result}"
                )
                return bitmap_result

        # =============================================================
        # SLOW PATH FALLBACK: Only reached if Tiger Cache unavailable
        # Query all descendants from metadata and check permissions
        # =============================================================
        logger.debug(f"_has_descendant_access: Falling back to slow path for {path}")

        # Get all files/directories under this path (recursive)
        prefix = path if path.endswith("/") else path + "/"
        if path == "/":
            prefix = ""

        try:
            all_descendants = self.metadata.list(prefix)
        except Exception as exc:
            # If metadata query fails, return False
            logger.debug("Metadata query failed for prefix %s: %s", prefix, exc)
            return False

        # OPTIMIZATION 5 (legacy): Use Tiger Cache for batch descendant check
        if hasattr(self._rebac_manager, "tiger_get_accessible_resources"):
            try:
                # Get all accessible resources for this subject
                accessible_ids = self._rebac_manager.tiger_get_accessible_resources(
                    subject=subject_tuple,
                    permission=rebac_permission,
                    resource_type="file",
                    zone_id=zone_id,
                )
                if accessible_ids:
                    # Check if any descendant is in the accessible set
                    # Note: This requires Tiger resource map integration
                    logger.debug(
                        f"_has_descendant_access: Tiger Cache has {len(accessible_ids)} accessible resources"
                    )
            except Exception as e:
                logger.debug(f"_has_descendant_access: Tiger Cache lookup failed: {e}")

        # 4. OPTIMIZATION (issue #380): Use bulk permission checking for descendants
        # Instead of checking each descendant individually (N queries), use rebac_check_bulk()
        if (
            hasattr(self, "_rebac_manager")
            and self._rebac_manager is not None
            and hasattr(self._rebac_manager, "rebac_check_bulk")
        ):
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
                    checks, zone_id=context.zone_id or "root"
                )

                # OPTIMIZATION 5: Early exit on first accessible descendant
                for check in checks:
                    if results.get(check, False):
                        logger.debug(
                            f"_has_descendant_access: Found accessible descendant {check[2][1]}"
                        )
                        # Cache positive result from slow path
                        if (
                            hasattr(self, "_dir_visibility_cache")
                            and self._dir_visibility_cache is not None
                        ):
                            self._dir_visibility_cache.set_visible(
                                zone_id,
                                context.subject_type,
                                subject_id,
                                path,
                                True,
                                f"slow_path:{check[2][1]}",
                            )
                        return True

                logger.debug("_has_descendant_access: No accessible descendants found")
                # Cache negative result from slow path
                if (
                    hasattr(self, "_dir_visibility_cache")
                    and self._dir_visibility_cache is not None
                ):
                    self._dir_visibility_cache.set_visible(
                        zone_id,
                        context.subject_type,
                        subject_id,
                        path,
                        False,
                        "slow_path:no_descendants",
                    )
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
                zone_id=context.zone_id,
            )
            if descendant_access:
                # Found accessible descendant! User can see this parent
                # Cache positive result
                if (
                    hasattr(self, "_dir_visibility_cache")
                    and self._dir_visibility_cache is not None
                ):
                    self._dir_visibility_cache.set_visible(
                        zone_id,
                        context.subject_type,
                        subject_id,
                        path,
                        True,
                        f"fallback:{meta.path}",
                    )
                return True

        # No accessible descendants found - cache negative result
        if hasattr(self, "_dir_visibility_cache") and self._dir_visibility_cache is not None:
            self._dir_visibility_cache.set_visible(
                zone_id, context.subject_type, subject_id, path, False, "fallback:no_descendants"
            )
        return False

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
            context: Operation context with user, permissions, zone info (uses default if None)

        Returns:
            True if path is a directory, False otherwise

        Note:
            This method requires READ permission on the path OR any descendant when
            enforce_permissions=True. Returns True if user has access to the directory
            or any child/descendant (enables hierarchical navigation).
            Returns False if path doesn't exist or user lacks permission to path and all descendants.

        Performance:
            For implicit directories, uses TRAVERSE permission check (O(1)) instead of
            descendant access check (O(n)). This optimizes FUSE path resolution.
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
                    ) and not self._has_descendant_access(path, Permission.READ, ctx):
                        return False
                else:
                    # For explicit directories/files, use hierarchical access check
                    if not self._has_descendant_access(path, Permission.READ, ctx):
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
        """
        Get list of available namespace directories.

        Returns the built-in namespaces that should appear at root level.
        Filters based on admin context only - zone filtering happens
        when accessing files within namespaces, not for listing directories.

        Returns:
            List of namespace names (e.g., ["workspace", "shared", "external"])

        Examples:
            # Get namespaces for current user context
            namespaces = fs.get_available_namespaces()
            # Returns: ["archives", "external", "shared", "workspace"]
            # (excludes "system" if not admin)
        """
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
        """
        Get file metadata (permissions, ownership, size, etc.) for FUSE operations.

        This method retrieves metadata without reading the file content,
        used primarily by FUSE getattr() operations.

        Args:
            path: Virtual file path
            context: Operation context with user, permissions, zone info

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

    # === Metadata Export/Import (delegated to MetadataExportService) ===

    def export_metadata(
        self,
        output_path: str | Path,
        filter: ExportFilter | None = None,
        prefix: str = "",
    ) -> int:
        """Export metadata to JSONL file. Delegates to MetadataExportService."""
        return self._metadata_export_service.export_metadata(output_path, filter, prefix)

    def import_metadata(
        self,
        input_path: str | Path,
        options: ImportOptions | None = None,
        overwrite: bool = False,
        skip_existing: bool = True,
    ) -> ImportResult:
        """Import metadata from JSONL file. Delegates to MetadataExportService."""
        return self._metadata_export_service.import_metadata(input_path, options, overwrite, skip_existing)

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

    # === Workspace / Memory / Snapshot — delegated to WorkspaceRPCService ===
    # RPC discovery uses register_service(self._workspace_rpc_service).
    # These thin stubs exist for ScopedFilesystem and internal callers.

    def workspace_snapshot(
        self, workspace_path: str | None = None, description: str | None = None,
        tags: list[str] | None = None, created_by: str | None = None,
        context: dict | None = None,
    ) -> dict[str, Any]:
        """Create workspace snapshot (delegates to WorkspaceRPCService)."""
        return self._workspace_rpc_service.workspace_snapshot(
            workspace_path, description, tags, created_by, context,
        )

    def workspace_restore(
        self, snapshot_number: int, workspace_path: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Restore workspace snapshot (delegates to WorkspaceRPCService)."""
        return self._workspace_rpc_service.workspace_restore(snapshot_number, workspace_path, context)

    def workspace_log(
        self, workspace_path: str | None = None, limit: int = 100,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List workspace snapshots (delegates to WorkspaceRPCService)."""
        return self._workspace_rpc_service.workspace_log(workspace_path, limit, context)

    def workspace_diff(
        self, snapshot_1: int, snapshot_2: int,
        workspace_path: str | None = None, context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Compare workspace snapshots (delegates to WorkspaceRPCService)."""
        return self._workspace_rpc_service.workspace_diff(snapshot_1, snapshot_2, workspace_path, context)

    def snapshot_begin(
        self, paths: list[str], agent_id: str | None = None,
        zone_id: str = "root", context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Begin transactional snapshot (delegates to WorkspaceRPCService)."""
        return self._workspace_rpc_service.snapshot_begin(paths, agent_id, zone_id, context)

    def snapshot_commit(self, snapshot_id: str, context: OperationContext | None = None) -> dict[str, str]:
        """Commit transactional snapshot (delegates to WorkspaceRPCService)."""
        return self._workspace_rpc_service.snapshot_commit(snapshot_id, context)

    def snapshot_rollback(self, snapshot_id: str, context: OperationContext | None = None) -> dict[str, Any]:
        """Rollback transactional snapshot (delegates to WorkspaceRPCService)."""
        return self._workspace_rpc_service.snapshot_rollback(snapshot_id, context)

    def load_workspace_memory_config(
        self, workspaces: list[dict] | None = None, memories: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Load workspace/memory config (delegates to WorkspaceRPCService)."""
        return self._workspace_rpc_service.load_workspace_memory_config(workspaces, memories)

    def register_workspace(
        self, path: str, name: str | None = None, description: str | None = None,
        created_by: str | None = None, tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None, session_id: str | None = None,
        ttl: timedelta | None = None, context: Any | None = None,
    ) -> dict[str, Any]:
        """Register workspace (delegates to WorkspaceRPCService)."""
        return self._workspace_rpc_service.register_workspace(
            path, name, description, created_by, tags, metadata, session_id, ttl, context,
        )

    def unregister_workspace(self, path: str) -> bool:
        """Unregister workspace (delegates to WorkspaceRPCService)."""
        return self._workspace_rpc_service.unregister_workspace(path)

    def update_workspace(
        self, path: str, name: str | None = None,
        description: str | None = None, metadata: dict | None = None,
    ) -> dict:
        """Update workspace (delegates to WorkspaceRPCService)."""
        return self._workspace_rpc_service.update_workspace(path, name, description, metadata)

    def list_workspaces(self, context: Any | None = None) -> list[dict]:
        """List workspaces (delegates to WorkspaceRPCService)."""
        return self._workspace_rpc_service.list_workspaces(context)

    def get_workspace_info(self, path: str) -> dict | None:
        """Get workspace info (delegates to WorkspaceRPCService)."""
        return self._workspace_rpc_service.get_workspace_info(path)

    def register_memory(
        self, path: str, name: str | None = None, description: str | None = None,
        created_by: str | None = None, tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None, session_id: str | None = None,
        ttl: timedelta | None = None, context: Any | None = None,
    ) -> dict[str, Any]:
        """Register memory (delegates to WorkspaceRPCService)."""
        return self._workspace_rpc_service.register_memory(
            path, name, description, created_by, tags, metadata, session_id, ttl, context,
        )

    def unregister_memory(self, path: str) -> bool:
        """Unregister memory (delegates to WorkspaceRPCService)."""
        return self._workspace_rpc_service.unregister_memory(path)

    def list_registered_memories(self) -> list[dict]:
        """List registered memories (delegates to WorkspaceRPCService)."""
        return self._workspace_rpc_service.list_registered_memories()

    def list_memories(self) -> list[dict]:
        """Alias for list_registered_memories()."""
        return self.list_registered_memories()

    def get_memory_info(self, path: str) -> dict | None:
        """Get memory info (delegates to WorkspaceRPCService)."""
        return self._workspace_rpc_service.get_memory_info(path)

    # ===== Agent Management (v0.5.0) — delegates to AgentRPCService =====

    def register_agent(
        self,
        agent_id: str,
        name: str,
        description: str | None = None,
        generate_api_key: bool = False,
        metadata: dict | None = None,
        capabilities: list[str] | None = None,
        context: dict | None = None,
    ) -> dict:
        """Register an AI agent (delegates to AgentRPCService)."""
        return self._agent_rpc_service.register_agent(
            agent_id, name, description, generate_api_key, metadata, capabilities, context,
        )

    def update_agent(
        self,
        agent_id: str,
        name: str | None = None,
        description: str | None = None,
        metadata: dict | None = None,
        context: dict | None = None,
    ) -> dict:
        """Update agent configuration (delegates to AgentRPCService)."""
        return self._agent_rpc_service.update_agent(agent_id, name, description, metadata, context)

    def list_agents(self, _context: dict | None = None) -> list[dict]:
        """List all registered agents (delegates to AgentRPCService)."""
        return self._agent_rpc_service.list_agents(_context)

    def get_agent(self, agent_id: str, _context: dict | None = None) -> dict | None:
        """Get agent information (delegates to AgentRPCService)."""
        return self._agent_rpc_service.get_agent(agent_id, _context)

    def delete_agent(self, agent_id: str, _context: dict | None = None) -> bool:
        """Delete a registered agent (delegates to AgentRPCService)."""
        return self._agent_rpc_service.delete_agent(agent_id, _context)

    # ===== User Provisioning API (Issue #820) — delegates to UserProvisioningService =====

    def provision_user(
        self,
        user_id: str,
        email: str,
        display_name: str | None = None,
        zone_id: str | None = None,
        zone_name: str | None = None,
        create_api_key: bool = True,
        api_key_name: str | None = None,
        api_key_expires_at: datetime | None = None,
        create_agents: bool = True,
        import_skills: bool = True,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Provision a new user (delegates to UserProvisioningService)."""
        return self._user_provisioning_service.provision_user(
            user_id, email, display_name, zone_id, zone_name,
            create_api_key, api_key_name, api_key_expires_at,
            create_agents, import_skills, context,
        )

    def deprovision_user(
        self,
        user_id: str,
        zone_id: str | None = None,
        delete_user_record: bool = False,
        force: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Deprovision a user (delegates to UserProvisioningService)."""
        return self._user_provisioning_service.deprovision_user(
            user_id, zone_id, delete_user_record, force, context,
        )

    # ========================================================================
    # Sandbox Management (Issue #372) — delegates to SandboxRPCService
    # ========================================================================

    @property
    def sandbox_available(self) -> bool:
        """Whether sandbox execution is available."""
        return self._sandbox_rpc_service.sandbox_available

    @staticmethod
    def _run_async(coro: Any) -> Any:
        """Run async coroutine safely (Issue #1300)."""
        from nexus.core.sync_bridge import run_sync

        return run_sync(coro)

    async def sandbox_create(self, name: str, ttl_minutes: int = 10, provider: str | None = None,
                             template_id: str | None = None, context: dict | None = None) -> dict:
        """Create a new sandbox (delegates to SandboxRPCService)."""
        return await self._sandbox_rpc_service.sandbox_create(name, ttl_minutes, provider, template_id, context)

    async def sandbox_run(self, sandbox_id: str, language: str, code: str, timeout: int = 300,
                          nexus_url: str | None = None, nexus_api_key: str | None = None,
                          context: dict | None = None, as_script: bool = False) -> dict:
        """Run code in sandbox (delegates to SandboxRPCService)."""
        return await self._sandbox_rpc_service.sandbox_run(sandbox_id, language, code, timeout, nexus_url, nexus_api_key, context, as_script)

    async def sandbox_validate(self, sandbox_id: str, workspace_path: str = "/workspace",
                               context: dict | None = None) -> dict:
        """Validate code in sandbox (delegates to SandboxRPCService)."""
        return await self._sandbox_rpc_service.sandbox_validate(sandbox_id, workspace_path, context)

    async def sandbox_pause(self, sandbox_id: str, context: dict | None = None) -> dict:
        """Pause sandbox (delegates to SandboxRPCService)."""
        return await self._sandbox_rpc_service.sandbox_pause(sandbox_id, context)

    async def sandbox_resume(self, sandbox_id: str, context: dict | None = None) -> dict:
        """Resume sandbox (delegates to SandboxRPCService)."""
        return await self._sandbox_rpc_service.sandbox_resume(sandbox_id, context)

    async def sandbox_stop(self, sandbox_id: str, context: dict | None = None) -> dict:
        """Stop sandbox (delegates to SandboxRPCService)."""
        return await self._sandbox_rpc_service.sandbox_stop(sandbox_id, context)

    async def sandbox_list(self, context: dict | None = None, verify_status: bool = False,
                           user_id: str | None = None, zone_id: str | None = None,
                           agent_id: str | None = None, status: str | None = None) -> dict:
        """List sandboxes (delegates to SandboxRPCService)."""
        return await self._sandbox_rpc_service.sandbox_list(context, verify_status, user_id, zone_id, agent_id, status)

    async def sandbox_status(self, sandbox_id: str, context: dict | None = None) -> dict:
        """Get sandbox status (delegates to SandboxRPCService)."""
        return await self._sandbox_rpc_service.sandbox_status(sandbox_id, context)

    async def sandbox_get_or_create(self, name: str, ttl_minutes: int = 10,
                                    provider: str | None = None, template_id: str | None = None,
                                    verify_status: bool = True, context: dict | None = None) -> dict:
        """Get or create sandbox (delegates to SandboxRPCService)."""
        return await self._sandbox_rpc_service.sandbox_get_or_create(name, ttl_minutes, provider, template_id, verify_status, context)

    async def sandbox_connect(self, sandbox_id: str, provider: str = "e2b",
                              sandbox_api_key: str | None = None, mount_path: str = "/mnt/nexus",
                              nexus_url: str | None = None, nexus_api_key: str | None = None,
                              agent_id: str | None = None, context: dict | None = None) -> dict:
        """Connect to sandbox (delegates to SandboxRPCService)."""
        return await self._sandbox_rpc_service.sandbox_connect(sandbox_id, provider, sandbox_api_key, mount_path, nexus_url, nexus_api_key, agent_id, context)

    async def sandbox_disconnect(self, sandbox_id: str, provider: str = "e2b",
                                 sandbox_api_key: str | None = None,
                                 context: dict | None = None) -> dict:
        """Disconnect from sandbox (delegates to SandboxRPCService)."""
        return await self._sandbox_rpc_service.sandbox_disconnect(sandbox_id, provider, sandbox_api_key, context)

    # =========================================================================
    # Issue #919: Directory Visibility Cache Metrics
    # =========================================================================

    def get_dir_visibility_cache_metrics(self) -> dict:
        """Get directory visibility cache metrics for monitoring.

        Returns:
            Dict with cache performance metrics:
            - hits: Number of cache hits (O(1) lookups)
            - misses: Number of cache misses
            - hit_rate: Cache hit rate (0.0-1.0)
            - bitmap_computes: Number of Tiger bitmap computations
            - cache_size: Current number of cached entries
            - max_entries: Maximum cache capacity
            - ttl: Cache entry TTL in seconds

        Example:
            >>> metrics = nx.get_dir_visibility_cache_metrics()
            >>> print(f"Hit rate: {metrics['hit_rate']:.2%}")
            Hit rate: 85.23%
        """
        if hasattr(self, "_dir_visibility_cache") and self._dir_visibility_cache is not None:
            return self._dir_visibility_cache.get_metrics()
        return {
            "hits": 0,
            "misses": 0,
            "hit_rate": 0.0,
            "bitmap_computes": 0,
            "cache_size": 0,
            "max_entries": 0,
            "ttl": 0,
        }

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

    # =========================================================================
    # Phase 2.2: Service Delegation Methods
    # =========================================================================
    # These methods delegate to independent service instances for better
    # separation of concerns, testability, and maintainability.
    # Eventually, the mixin methods will be removed in favor of these.
    # =========================================================================

    # -------------------------------------------------------------------------
    # VersionService Delegation Methods (4 methods)
    # Replaces NexusFSVersionsMixin (Phase 2.3)
    # Sync methods wrap async methods for backward compatibility
    # -------------------------------------------------------------------------

    async def aget_version(
        self,
        path: str,
        version: int,
        context: OperationContext | None = None,
    ) -> bytes:
        """Async version of get_version. Delegates to VersionService."""
        return await self.version_service.get_version(path, version, context)

    @rpc_expose(description="Get specific file version")
    def get_version(
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
            version: Version number to retrieve
            context: Operation context for permission checks (uses default if None)

        Returns:
            File content as bytes for the specified version

        Raises:
            NexusFileNotFoundError: If file or version doesn't exist
            InvalidPathError: If path is invalid
            PermissionError: If user doesn't have READ permission
        """
        return cast(bytes, NexusFS._run_async(self.aget_version(path, version, context)))

    async def alist_versions(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """Async version of list_versions. Delegates to VersionService."""
        return await self.version_service.list_versions(path, context)

    @rpc_expose(description="List file versions")
    def list_versions(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List all versions of a file.

        Returns version history with metadata for each version.

        Args:
            path: Virtual file path
            context: Operation context for permission checks (uses default if None)

        Returns:
            List of version info dicts ordered by version number (newest first)

        Raises:
            InvalidPathError: If path is invalid
            PermissionError: If user doesn't have READ permission
        """
        return cast(list[dict[str, Any]], NexusFS._run_async(self.alist_versions(path, context)))

    async def arollback(
        self,
        path: str,
        version: int,
        context: OperationContext | None = None,
    ) -> None:
        """Async version of rollback. Delegates to VersionService."""
        return await self.version_service.rollback(path, version, context)

    @rpc_expose(description="Rollback file to previous version")
    def rollback(
        self,
        path: str,
        version: int,
        context: OperationContext | None = None,
    ) -> None:
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
        """
        cast(None, NexusFS._run_async(self.arollback(path, version, context)))

    async def adiff_versions(
        self,
        path: str,
        v1: int,
        v2: int,
        mode: str = "metadata",
        context: OperationContext | None = None,
    ) -> dict[str, Any] | str:
        """Async version of diff_versions. Delegates to VersionService."""
        return await self.version_service.diff_versions(path, v1, v2, mode, context)

    @rpc_expose(description="Compare file versions")
    def diff_versions(
        self,
        path: str,
        v1: int,
        v2: int,
        mode: str = "metadata",
        context: OperationContext | None = None,
    ) -> dict[str, Any] | str:
        """Compare two versions of a file.

        Args:
            path: Virtual file path
            v1: First version number
            v2: Second version number
            mode: Diff mode - "metadata" (default) or "content"
            context: Operation context for permission checks (uses default if None)

        Returns:
            For "metadata" mode: Dict with metadata differences
            For "content" mode: Unified diff string

        Raises:
            NexusFileNotFoundError: If file or version doesn't exist
            InvalidPathError: If path is invalid
            ValueError: If mode is invalid
            PermissionError: If user doesn't have READ permission
        """
        return cast(
            dict[str, Any] | str,
            NexusFS._run_async(self.adiff_versions(path, v1, v2, mode, context)),
        )

    # Async ReBAC methods (arebac_*) removed — served via register_service(rebac_service).

    # -------------------------------------------------------------------------
    # ReBACService Sync Methods (replaces NexusFSReBACMixin, Issue #1387)
    # -------------------------------------------------------------------------

    @property
    def _require_rebac(self) -> Any:
        """Get the ReBAC manager, raising if not initialized."""
        mgr = self._rebac_manager
        if mgr is None:
            raise RuntimeError("ReBAC manager not available (record_store not configured)")
        return mgr

    def _get_subject_from_context(self, context: Any) -> tuple[str, str] | None:
        """Extract subject from operation context.

        Args:
            context: Operation context (OperationContext or dict)

        Returns:
            Subject tuple (type, id) or None if not found

        Examples:
            >>> context = {"subject": ("user", "alice")}
            >>> self._get_subject_from_context(context)
            ('user', 'alice')

            >>> context = OperationContext(user_id="alice", groups=[])
            >>> self._get_subject_from_context(context)
            ('user', 'alice')
        """
        if not context:
            return None

        # Handle dict format (used by RPC server and tests)
        if isinstance(context, dict):
            subject = context.get("subject")
            if subject and isinstance(subject, tuple) and len(subject) == 2:
                return (str(subject[0]), str(subject[1]))

            # Construct from subject_type + subject_id
            subject_type = context.get("subject_type", "user")
            subject_id = context.get("subject_id") or context.get("user_id")
            if subject_id:
                return (subject_type, subject_id)

            return None

        # Handle OperationContext format - use get_subject() method
        if hasattr(context, "get_subject") and callable(context.get_subject):
            result = context.get_subject()
            if result is not None:
                return (str(result[0]), str(result[1]))
            return None

        # Fallback: construct from attributes
        if hasattr(context, "subject_type") and hasattr(context, "subject_id"):
            subject_type = getattr(context, "subject_type", "user")
            subject_id = getattr(context, "subject_id", None) or getattr(context, "user_id", None)
            if subject_id:
                return (subject_type, subject_id)

        # Last resort: use user field
        if hasattr(context, "user_id") and context.user_id:
            return ("user", context.user_id)

        return None


        This helper centralizes the permission check logic used by rebac_create,
        share_with_user, and share_with_group to prevent code duplication.

        Args:
            resource: Resource tuple (object_type, object_id)
            context: Operation context (OperationContext or dict)
            required_permission: Permission level required (default: "execute" for ownership)

        Raises:
            PermissionError: If caller lacks required permission to manage the resource

        Examples:
            >>> self._check_share_permission(
            ...     resource=("file", "/path/doc.txt"),
            ...     context=operation_context
            ... )
        """
        if not context:
            return

        from nexus.core.permissions import OperationContext, Permission

        # Extract OperationContext from context parameter
        op_context: OperationContext | None = None
        if isinstance(context, OperationContext):
            op_context = context
        elif isinstance(context, dict):
            # Create OperationContext from dict
            op_context = OperationContext(
                user_id=context.get("user_id", "unknown"),
                groups=context.get("groups", []),
                zone_id=context.get("zone_id"),
                is_admin=context.get("is_admin", False),
                is_system=context.get("is_system", False),
            )

        # Skip permission check for admin and system contexts
        if not op_context or not self._enforce_permissions:
            return
        if op_context.is_admin or op_context.is_system:
            return

        # Check if caller has required permission on the resource
        # Map string permission to Permission enum
        permission_map = {
            "execute": Permission.EXECUTE,
            "write": Permission.WRITE,
            "read": Permission.READ,
        }
        perm_enum = permission_map.get(required_permission, Permission.EXECUTE)

        # For file resources, use the path directly
        if resource[0] == "file":
            resource_path = resource[1]
        else:
            # For non-file resources, we need to check ReBAC permissions
            # This ensures groups, workspaces, and other resources are also protected
            # Check if user has ownership (execute permission) via ReBAC
            has_permission = self.rebac_check(
                subject=self._get_subject_from_context(context) or ("user", op_context.user_id),
                permission="owner",  # Only owners can manage permissions
                object=resource,
                context=context,
            )
            if not has_permission:
                raise PermissionError(
                    f"Access denied: User '{op_context.user_id}' does not have owner "
                    f"permission to manage {resource[0]} '{resource[1]}'"
                )
            return

        # Use permission enforcer to check permission for file resources
        if hasattr(self, "_permission_enforcer"):
            has_permission = self._permission_enforcer.check(resource_path, perm_enum, op_context)

            # If user is not owner, check if they are zone admin
            if not has_permission:
                # Extract zone from resource path (format: /zone/{zone_id}/...)
                zone_id = None
                if resource_path.startswith("/zone/"):
                    parts = resource_path[6:].split("/", 1)  # Remove "/zone/" prefix
                    if parts:
                        zone_id = parts[0]

                # Fallback to zone_id from operation context
                if not zone_id and hasattr(op_context, "zone_id"):
                    zone_id = op_context.zone_id

                # Check if user is zone admin for this resource's zone
                if zone_id and op_context.user_id:
                    from nexus.core.zone_helpers import is_zone_admin

                    if is_zone_admin(self._rebac_manager, op_context.user_id, zone_id):
                        # Zone admin can share resources in their zone
                        return

                # Neither owner nor zone admin - deny
                perm_name = required_permission.upper()
                raise PermissionError(
                    f"Access denied: User '{op_context.user_id}' does not have {perm_name} "
                    f"permission to manage permissions on '{resource_path}'. "
                    f"Only owners or zone admins can share resources."
                )

    @rpc_expose(description="Create ReBAC relationship tuple")
    def rebac_create(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
        zone_id: str | None = None,
        context: Any = None,  # Accept OperationContext or dict
        column_config: dict[str, Any] | None = None,  # Column-level permissions for dynamic_viewer
    ) -> dict[str, Any]:
        """Create a relationship tuple in ReBAC system.

        Args:
            subject: (subject_type, subject_id) tuple (e.g., ('agent', 'alice'))
            relation: Relation type (e.g., 'member-of', 'owner-of', 'viewer-of', 'dynamic_viewer')
            object: (object_type, object_id) tuple (e.g., ('group', 'developers'))
            expires_at: Optional expiration datetime for temporary relationships
            zone_id: Optional zone ID for multi-zone isolation. If None, uses
                       zone_id from operation context.
            context: Operation context (automatically provided by RPC server)
            column_config: Optional column-level permissions config for dynamic_viewer relation.
                          Only applies to CSV files.
                          Structure: {
                              "hidden_columns": ["password", "ssn"],  # Completely hide these columns
                              "aggregations": {"age": "mean", "salary": "sum"},  # Show aggregated values
                              "visible_columns": ["name", "email"]  # Show raw data (optional, auto-calculated if empty)
                          }
                          Note: A column can only appear in one category (hidden, aggregations, or visible)

        Returns:
            Tuple ID of created relationship

        Raises:
            ValueError: If subject or object tuples are invalid, or column_config is invalid
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Alice is member of developers group
            >>> nx.rebac_create(
            ...     subject=("agent", "alice"),
            ...     relation="member-of",
            ...     object=("group", "developers")
            ... )
            'uuid-string'

            >>> # Developers group owns file
            >>> nx.rebac_create(
            ...     subject=("group", "developers"),
            ...     relation="owner-of",
            ...     object=("file", "/workspace/project.txt")
            ... )
            'uuid-string'

            >>> # Temporary viewer access (expires in 1 hour)
            >>> from datetime import timedelta
            >>> nx.rebac_create(
            ...     subject=("agent", "bob"),
            ...     relation="viewer-of",
            ...     object=("file", "/workspace/secret.txt"),
            ...     expires_at=datetime.now(UTC) + timedelta(hours=1)
            ... )
            'uuid-string'

            >>> # Dynamic viewer with column-level permissions for CSV files
            >>> nx.rebac_create(
            ...     subject=("agent", "alice"),
            ...     relation="dynamic_viewer",
            ...     object=("file", "/data/users.csv"),
            ...     column_config={
            ...         "hidden_columns": ["password", "ssn"],
            ...         "aggregations": {"age": "mean", "salary": "sum"},
            ...         "visible_columns": ["name", "email"]
            ...     }
            ... )
            'uuid-string'
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # Validate tuples (support 2-tuple and 3-tuple for subject to support userset-as-subject)
        if not isinstance(subject, tuple) or len(subject) not in (2, 3):
            raise ValueError(
                f"subject must be (type, id) or (type, id, relation) tuple, got {subject}"
            )
        if not isinstance(object, tuple) or len(object) != 2:
            raise ValueError(f"object must be (type, id) tuple, got {object}")

        # Normalize file paths by removing trailing slashes for proper parent traversal
        # Special case: Keep root path "/" as-is to avoid empty string
        if (
            object[0] == "file"
            and isinstance(object[1], str)
            and object[1].endswith("/")
            and object[1] != "/"
        ):
            object = (object[0], object[1].rstrip("/"))

        # Use zone_id from context if not explicitly provided
        effective_zone_id = zone_id
        if effective_zone_id is None and context:
            # Handle both dict and OperationContext
            if isinstance(context, dict):
                effective_zone_id = context.get("zone")
            elif hasattr(context, "zone_id"):
                effective_zone_id = context.zone_id

        # SECURITY: Check execute permission before allowing permission management
        # Only owners (those with execute permission) can grant/manage permissions on resources
        # Now applies to ALL resource types, not just files
        self._check_share_permission(resource=object, context=context)

        # Validate column_config for dynamic_viewer relation
        conditions = None
        if relation == "dynamic_viewer":
            # Check if object is a CSV file
            if object[0] == "file" and not object[1].lower().endswith(".csv"):
                raise ValueError(
                    f"dynamic_viewer relation only supports CSV files. "
                    f"File '{object[1]}' does not have .csv extension."
                )

            if column_config is None:
                raise ValueError(
                    "column_config is required when relation is 'dynamic_viewer'. "
                    "Provide configuration with hidden_columns, aggregations, and/or visible_columns."
                )

            # Validate column_config structure
            if not isinstance(column_config, dict):
                raise ValueError("column_config must be a dictionary")

            # Get all column categories
            hidden_columns = column_config.get("hidden_columns", [])
            aggregations = column_config.get("aggregations", {})
            visible_columns = column_config.get("visible_columns", [])

            # Validate types
            if not isinstance(hidden_columns, list):
                raise ValueError("column_config.hidden_columns must be a list")
            if not isinstance(aggregations, dict):
                raise ValueError("column_config.aggregations must be a dictionary")
            if not isinstance(visible_columns, list):
                raise ValueError("column_config.visible_columns must be a list")

            # Validate columns against actual CSV file
            file_path = object[1]
            if hasattr(self, "read") and hasattr(self, "exists"):
                try:
                    # Check if file exists
                    if self.exists(file_path):
                        # Read file to get actual columns
                        raw = self.read(file_path)
                        text_content: str = (
                            raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                        )

                        try:
                            import io

                            import pandas as pd

                            df = pd.read_csv(io.StringIO(text_content))
                            actual_columns = set(df.columns)

                            # Collect all configured columns
                            configured_columns = (
                                set(hidden_columns)
                                | set(aggregations.keys())
                                | set(visible_columns)
                            )

                            # Check for invalid columns
                            invalid_columns = configured_columns - actual_columns
                            if invalid_columns:
                                raise ValueError(
                                    f"Column config contains invalid columns: {sorted(invalid_columns)}. "
                                    f"Available columns in CSV: {sorted(actual_columns)}"
                                )
                        except ValueError:
                            # Re-raise ValueError (validation error)
                            raise
                        except ImportError:
                            # pandas not available, skip validation
                            pass
                        except (RuntimeError, pd.errors.ParserError) as e:
                            # If CSV parsing fails (non-validation error), provide warning but allow creation
                            logger.warning(
                                f"Could not validate CSV columns for {file_path}: {e}. "
                                f"Column config will be created without validation."
                            )
                except ValueError:
                    # Re-raise validation errors
                    raise
                except OSError as e:
                    # If file read fails, skip validation (file might not exist yet)
                    logger.debug(f"Could not read file {file_path} for column validation: {e}")

            # Check that a column only appears in one category
            all_columns = set()
            for col in hidden_columns:
                if col in all_columns:
                    raise ValueError(
                        f"Column '{col}' appears in multiple categories. "
                        f"Each column can only be in hidden_columns, aggregations, or visible_columns."
                    )
                all_columns.add(col)

            for col in aggregations:
                if col in all_columns:
                    raise ValueError(
                        f"Column '{col}' appears in multiple categories. "
                        f"Each column can only be in hidden_columns, aggregations, or visible_columns."
                    )
                all_columns.add(col)

            for col in visible_columns:
                if col in all_columns:
                    raise ValueError(
                        f"Column '{col}' appears in multiple categories. "
                        f"Each column can only be in hidden_columns, aggregations, or visible_columns."
                    )
                all_columns.add(col)

            # Validate aggregation operations (single value per column)
            valid_ops = {"mean", "sum", "min", "max", "std", "median", "count"}
            for col, op in aggregations.items():
                if not isinstance(op, str):
                    raise ValueError(
                        f"column_config.aggregations['{col}'] must be a string (one of: {', '.join(valid_ops)}). "
                        f"Got: {type(op).__name__}"
                    )
                if op not in valid_ops:
                    raise ValueError(
                        f"Invalid aggregation operation '{op}' for column '{col}'. "
                        f"Valid operations: {', '.join(sorted(valid_ops))}"
                    )

            # Store column_config as conditions
            conditions = {"type": "dynamic_viewer", "column_config": column_config}
        elif column_config is not None:
            # column_config provided but relation is not dynamic_viewer
            raise ValueError("column_config can only be provided when relation is 'dynamic_viewer'")

        # Create relationship
        result = self._require_rebac.rebac_write(
            subject=subject,
            relation=relation,
            object=object,
            expires_at=expires_at,
            zone_id=effective_zone_id,
            conditions=conditions,
        )

        # NOTE: Tiger Cache queue update is now handled in EnhancedReBACManager.rebac_write()
        # This ensures ALL write paths (rebac_create, share_with_user, etc.) get Tiger Cache updates

        # Convert WriteResult to dict for JSON serialization.
        # WriteResult uses slots=True so it has no __dict__ and can't be
        # auto-serialized by RPCEncoder/_prepare_for_orjson.
        return {
            "tuple_id": result.tuple_id,
            "revision": result.revision,
            "consistency_token": result.consistency_token,
        }

    def _has_descendant_access_for_traverse(
        self,
        path: str,
        subject: tuple[str, str],
        zone_id: str | None = None,
    ) -> bool:
        """Check if user has READ access to any descendant of path.

        This enables Unix-like TRAVERSE behavior: users can traverse parent
        directories if they have READ permission on any file inside.

        This method queries the ReBAC tuples directly to find files under
        the target path, avoiding sync metadata queries that can block.

        Args:
            path: Directory path to check descendants of
            subject: (subject_type, subject_id) tuple
            zone_id: Zone ID for multi-zone isolation

        Returns:
            True if user has READ on any descendant, False otherwise
        """
        from nexus.services.permissions.utils.zone import normalize_zone_id

        # Normalize path prefix for matching
        prefix = path if path.endswith("/") else path + "/"
        if path == "/":
            prefix = "/"

        # Query ReBAC tuples directly to find files under this path
        # that the user has READ access to. This avoids the blocking
        # metadata.list() call.
        try:
            # Get all tuples for this subject in this zone
            effective_zone = normalize_zone_id(zone_id)

            # Use the _fetch_zone_tuples_from_db method to get cached tuples
            # or fall back to checking the in-memory graph
            if hasattr(self._rebac_manager, "_get_cached_zone_tuples"):
                tuples = self._require_rebac._get_cached_zone_tuples(effective_zone)
                if tuples is None:
                    tuples = self._require_rebac.get_zone_tuples(effective_zone)
            else:
                tuples = []

            # Find any file objects under our path that this subject can read
            for t in tuples:
                # Check if this tuple grants read-like permission to our subject
                if t.get("subject_type") != subject[0] or t.get("subject_id") != subject[1]:
                    continue

                # Check if the relation grants read permission
                relation = t.get("relation", "")
                if relation not in (
                    "direct_viewer",
                    "direct_editor",
                    "direct_owner",
                    "viewer",
                    "editor",
                    "owner",
                ):
                    continue

                # Check if the object is a file under our path
                obj_type = t.get("object_type", "")
                obj_id = t.get("object_id", "")
                if obj_type == "file" and obj_id.startswith(prefix):
                    logger.debug(f"_has_descendant_access_for_traverse: GRANTED via {obj_id}")
                    return True

            return False
        except (RuntimeError, ValueError) as e:
            logger.debug(f"_has_descendant_access_for_traverse: check failed: {e}")
            return False

    @rpc_expose(description="Check ReBAC permission")
    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: Any = None,  # Accept OperationContext or dict
        zone_id: str | None = None,
    ) -> bool:
        """Check if subject has permission on object via ReBAC.

        Uses graph traversal to check both direct relationships and
        inherited permissions through group membership and hierarchies.

        Supports ABAC-style contextual conditions (time windows, IP allowlists, etc.).

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., 'read', 'write', 'owner')
            object: (object_type, object_id) tuple
            context: Optional ABAC context for condition evaluation (time, ip, device, attributes)
            zone_id: Optional zone ID for multi-zone isolation (defaults to "root")

        Returns:
            True if permission is granted, False otherwise

        Raises:
            ValueError: If subject or object tuples are invalid
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Basic check
            >>> nx.rebac_check(
            ...     subject=("agent", "alice"),
            ...     permission="read",
            ...     object=("file", "/workspace/doc.txt"),
            ...     zone_id="org_acme"
            ... )
            True

            >>> # ABAC check with time window
            >>> nx.rebac_check(
            ...     subject=("agent", "contractor"),
            ...     permission="read",
            ...     object=("file", "/sensitive.txt"),
            ...     context={"time": "14:30", "ip": "10.0.1.5"},
            ...     zone_id="org_acme"
            ... )
            True  # Allowed during business hours

            >>> # Check after hours
            >>> nx.rebac_check(
            ...     subject=("agent", "contractor"),
            ...     permission="read",
            ...     object=("file", "/sensitive.txt"),
            ...     context={"time": "20:00", "ip": "10.0.1.5"},
            ...     zone_id="org_acme"
            ... )
            False  # Denied outside time window
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # Validate tuples
        if not isinstance(subject, tuple) or len(subject) != 2:
            raise ValueError(f"subject must be (type, id) tuple, got {subject}")
        if not isinstance(object, tuple) or len(object) != 2:
            raise ValueError(f"object must be (type, id) tuple, got {object}")

        # P0-4: Pass zone_id for multi-zone isolation
        # Use zone_id from operation context if not explicitly provided
        effective_zone_id = zone_id
        if effective_zone_id is None and context:
            # Handle both dict and OperationContext
            if isinstance(context, dict):
                effective_zone_id = context.get("zone")
            elif hasattr(context, "zone_id"):
                effective_zone_id = context.zone_id
        # BUGFIX: Don't default to "root" - let ReBAC manager handle None
        # This allows proper zone isolation testing

        # Check permission with optional context
        result = self._require_rebac.rebac_check(
            subject=subject,
            permission=permission,
            object=object,
            context=context,
            zone_id=effective_zone_id,
        )

        # Unix-like TRAVERSE behavior: if user has READ on any descendant,
        # they can TRAVERSE the parent directory (like Unix x permission on dirs).
        # This fallback uses rebac_check_bulk directly to avoid infinite recursion
        # (since _has_descendant_access calls self.rebac_check internally).
        if not result and permission == "traverse" and object[0] == "file":
            result = self._has_descendant_access_for_traverse(
                path=object[1],
                subject=subject,
                zone_id=effective_zone_id,
            )

        return result

    @rpc_expose(description="Expand ReBAC permissions to find all subjects")
    def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
    ) -> list[tuple[str, str]]:
        """Find all subjects that have a given permission on an object.

        Uses recursive graph expansion to find both direct and inherited permissions.

        Args:
            permission: Permission to check (e.g., 'read', 'write', 'owner')
            object: (object_type, object_id) tuple

        Returns:
            List of (subject_type, subject_id) tuples that have the permission

        Raises:
            ValueError: If object tuple is invalid
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Who can read this file?
            >>> nx.rebac_expand(
            ...     permission="read",
            ...     object=("file", "/workspace/doc.txt")
            ... )
            [('agent', 'alice'), ('agent', 'bob'), ('group', 'developers')]

            >>> # Who owns this workspace?
            >>> nx.rebac_expand(
            ...     permission="owner",
            ...     object=("workspace", "/workspace")
            ... )
            [('group', 'admins')]
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # Validate tuple
        if not isinstance(object, tuple) or len(object) != 2:
            raise ValueError(f"object must be (type, id) tuple, got {object}")

        # Expand permission
        return self._require_rebac.rebac_expand(permission=permission, object=object)

    @rpc_expose(description="Explain ReBAC permission check")
    def rebac_explain(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
        context: Any = None,  # Accept OperationContext or dict
    ) -> dict:
        """Explain why a subject has or doesn't have permission on an object.

        This debugging API traces through the permission graph to show exactly
        why a permission check succeeded or failed.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., 'read', 'write', 'owner')
            object: (object_type, object_id) tuple
            zone_id: Optional zone ID for multi-zone isolation. If None, uses
                       zone_id from operation context.
            context: Operation context (automatically provided by RPC server)

        Returns:
            Dictionary with:
            - result: bool - whether permission is granted
            - cached: bool - whether result came from cache
            - reason: str - human-readable explanation
            - paths: list[dict] - all checked paths through the graph
            - successful_path: dict | None - the path that granted access (if any)

        Raises:
            ValueError: If subject or object tuples are invalid
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Why does alice have read permission?
            >>> explanation = nx.rebac_explain(
            ...     subject=("agent", "alice"),
            ...     permission="read",
            ...     object=("file", "/workspace/doc.txt"),
            ...     zone_id="org_acme"
            ... )
            >>> print(explanation["reason"])
            'alice has 'read' on file:/workspace/doc.txt via parent inheritance'

            >>> # Why doesn't bob have write permission?
            >>> explanation = nx.rebac_explain(
            ...     subject=("agent", "bob"),
            ...     permission="write",
            ...     object=("workspace", "/workspace")
            ... )
            >>> print(explanation["result"])
            False
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # Validate tuples
        if not isinstance(subject, tuple) or len(subject) != 2:
            raise ValueError(f"subject must be (type, id) tuple, got {subject}")
        if not isinstance(object, tuple) or len(object) != 2:
            raise ValueError(f"object must be (type, id) tuple, got {object}")

        # Use zone_id from context if not explicitly provided
        effective_zone_id = zone_id
        if effective_zone_id is None and context:
            # Handle both dict and OperationContext
            if isinstance(context, dict):
                effective_zone_id = context.get("zone")
            elif hasattr(context, "zone_id"):
                effective_zone_id = context.zone_id

        # Get explanation
        return self._require_rebac.rebac_explain(
            subject=subject, permission=permission, object=object, zone_id=effective_zone_id
        )

    def rebac_check_batch(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
    ) -> list[bool]:
        """Batch permission checks for efficiency.

        Performs multiple permission checks in a single call, using shared cache lookups
        and optimized database queries. More efficient than individual checks when checking
        multiple permissions.

        Args:
            checks: List of (subject, permission, object) tuples to check

        Returns:
            List of boolean results in the same order as input

        Raises:
            ValueError: If any check tuple is invalid
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Check multiple permissions at once
            >>> results = nx.rebac_check_batch([
            ...     (("agent", "alice"), "read", ("file", "/workspace/doc1.txt")),
            ...     (("agent", "alice"), "read", ("file", "/workspace/doc2.txt")),
            ...     (("agent", "bob"), "write", ("file", "/workspace/doc3.txt")),
            ... ])
            >>> # Returns: [True, False, True]
            >>>
            >>> # Check if user has multiple permissions on same object
            >>> results = nx.rebac_check_batch([
            ...     (("agent", "alice"), "read", ("file", "/project")),
            ...     (("agent", "alice"), "write", ("file", "/project")),
            ...     (("agent", "alice"), "owner", ("file", "/project")),
            ... ])
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # Validate all checks
        for i, check in enumerate(checks):
            if not isinstance(check, tuple) or len(check) != 3:
                raise ValueError(f"Check {i} must be (subject, permission, object) tuple")
            subject, permission, obj = check
            if not isinstance(subject, tuple) or len(subject) != 2:
                raise ValueError(f"Check {i}: subject must be (type, id) tuple, got {subject}")
            if not isinstance(obj, tuple) or len(obj) != 2:
                raise ValueError(f"Check {i}: object must be (type, id) tuple, got {obj}")

        # Perform batch check with Rust acceleration
        return self._require_rebac.rebac_check_batch_fast(checks=checks)

    @rpc_expose(description="Delete ReBAC relationship tuple")
    def rebac_delete(self, tuple_id: str) -> bool:
        """Delete a relationship tuple by ID.

        Args:
            tuple_id: ID of the tuple to delete (returned from rebac_create)

        Returns:
            True if tuple was deleted, False if not found

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> tuple_id = nx.rebac_create(
            ...     subject=("agent", "alice"),
            ...     relation="viewer-of",
            ...     object=("file", "/workspace/doc.txt")
            ... )
            >>> nx.rebac_delete(tuple_id)
            True
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # Delete tuple - the enhanced rebac_delete already handles Tiger Cache invalidation
        # No need to fetch tuple info here; the manager does it efficiently by tuple_id
        return self._require_rebac.rebac_delete(tuple_id=tuple_id)

    @rpc_expose(description="List ReBAC relationship tuples")
    def rebac_list_tuples(
        self,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
        relation_in: list[str] | None = None,
    ) -> list[dict]:
        """List relationship tuples matching filters.

        Args:
            subject: Optional (subject_type, subject_id) filter
            relation: Optional relation type filter (mutually exclusive with relation_in)
            object: Optional (object_type, object_id) filter
            relation_in: Optional list of relation types to filter (mutually exclusive with relation)

        Returns:
            List of tuple dictionaries with keys:
                - tuple_id: Tuple ID
                - subject_type, subject_id: Subject
                - relation: Relation type
                - object_type, object_id: Object
                - created_at: Creation timestamp
                - expires_at: Optional expiration timestamp

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # List all relationships for alice
            >>> nx.rebac_list_tuples(subject=("agent", "alice"))
            [
                {
                    'tuple_id': 'uuid-1',
                    'subject_type': 'agent',
                    'subject_id': 'alice',
                    'relation': 'member-of',
                    'object_type': 'group',
                    'object_id': 'developers',
                    'created_at': datetime(...),
                    'expires_at': None
                }
            ]

            >>> # List tuples with multiple relation types (single query)
            >>> nx.rebac_list_tuples(
            ...     subject=("user", "alice"),
            ...     relation_in=["shared-viewer", "shared-editor", "shared-owner"]
            ... )
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # Build query
        conn = self._require_rebac._get_connection()
        try:
            query = "SELECT * FROM rebac_tuples WHERE 1=1"
            params: list = []

            if subject:
                query += " AND subject_type = ? AND subject_id = ?"
                params.extend([subject[0], subject[1]])

            if relation:
                query += " AND relation = ?"
                params.append(relation)
            elif relation_in:
                # N+1 FIX: Support multiple relations in a single query
                placeholders = ", ".join("?" * len(relation_in))
                query += f" AND relation IN ({placeholders})"
                params.extend(relation_in)

            if object:
                query += " AND object_type = ? AND object_id = ?"
                params.extend([object[0], object[1]])

            # Fix SQL placeholders for PostgreSQL
            query = self._require_rebac._fix_sql_placeholders(query)

            cursor = self._require_rebac._create_cursor(conn)
            cursor.execute(query, params)

            results = []
            for row in cursor.fetchall():
                # Both SQLite and PostgreSQL now return dict-like rows
                # Note: sqlite3.Row doesn't have .get() method, so use try/except for optional fields
                try:
                    zone_id = row["zone_id"]
                except (KeyError, IndexError):
                    zone_id = None

                results.append(
                    {
                        "tuple_id": row["tuple_id"],
                        "subject_type": row["subject_type"],
                        "subject_id": row["subject_id"],
                        "relation": row["relation"],
                        "object_type": row["object_type"],
                        "object_id": row["object_id"],
                        "created_at": row["created_at"],
                        "expires_at": row["expires_at"],
                        "zone_id": zone_id,
                    }
                )

            return results
        finally:
            self._require_rebac._close_connection(conn)

    # =========================================================================
    # Public API Wrappers for Configuration (P1 - Should Do)
    # =========================================================================

    @rpc_expose(description="Set ReBAC configuration option")
    def set_rebac_option(self, key: str, value: Any) -> None:
        """Set a ReBAC configuration option.

        Provides public access to ReBAC configuration without using internal APIs.

        Args:
            key: Configuration key (e.g., "max_depth", "cache_ttl")
            value: Configuration value

        Raises:
            ValueError: If key is invalid
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Set maximum graph traversal depth
            >>> nx.set_rebac_option("max_depth", 15)

            >>> # Set cache TTL
            >>> nx.set_rebac_option("cache_ttl", 600)
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        if key == "max_depth":
            if not isinstance(value, int) or value < 1:
                raise ValueError("max_depth must be a positive integer")
            self._require_rebac.max_depth = value
        elif key == "cache_ttl":
            if not isinstance(value, int) or value < 0:
                raise ValueError("cache_ttl must be a non-negative integer")
            self._require_rebac.cache_ttl_seconds = value
        else:
            raise ValueError(f"Unknown ReBAC option: {key}. Valid options: max_depth, cache_ttl")

    @rpc_expose(description="Get ReBAC configuration option")
    def get_rebac_option(self, key: str) -> Any:
        """Get a ReBAC configuration option.

        Args:
            key: Configuration key (e.g., "max_depth", "cache_ttl")

        Returns:
            Current value of the configuration option

        Raises:
            ValueError: If key is invalid
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Get current max depth
            >>> depth = nx.get_rebac_option("max_depth")
            >>> print(f"Max traversal depth: {depth}")
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        if key == "max_depth":
            return self._require_rebac.max_depth
        elif key == "cache_ttl":
            return self._require_rebac.cache_ttl_seconds
        else:
            raise ValueError(f"Unknown ReBAC option: {key}. Valid options: max_depth, cache_ttl")

    @rpc_expose(description="Register ReBAC namespace schema")
    def register_namespace(self, namespace: dict[str, Any]) -> None:
        """Register a namespace schema for ReBAC.

        Provides public API to register namespace configurations without using internal APIs.

        Args:
            namespace: Namespace configuration dictionary with keys:
                - object_type: Type of objects this namespace applies to
                - config: Schema configuration (relations and permissions)

        Raises:
            RuntimeError: If ReBAC is not available
            ValueError: If namespace configuration is invalid

        Examples:
            >>> # Register file namespace with group inheritance
            >>> nx.register_namespace({
            ...     "object_type": "file",
            ...     "config": {
            ...         "relations": {
            ...             "viewer": {},
            ...             "editor": {}
            ...         },
            ...         "permissions": {
            ...             "read": ["viewer", "editor"],
            ...             "write": ["editor"]
            ...         }
            ...     }
            ... })
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # Validate namespace structure
        if not isinstance(namespace, dict):
            raise ValueError("namespace must be a dictionary")
        if "object_type" not in namespace:
            raise ValueError("namespace must have 'object_type' key")
        if "config" not in namespace:
            raise ValueError("namespace must have 'config' key")

        # Import NamespaceConfig
        import uuid

        from nexus.core.rebac import NamespaceConfig

        # Create NamespaceConfig object
        ns = NamespaceConfig(
            namespace_id=namespace.get("namespace_id", str(uuid.uuid4())),
            object_type=namespace["object_type"],
            config=namespace["config"],
        )

        # Register via manager
        self._require_rebac.create_namespace(ns)

    @rpc_expose(description="Get ReBAC namespace schema")
    def get_namespace(self, object_type: str) -> dict[str, Any] | None:
        """Get namespace schema for an object type.

        Args:
            object_type: Type of objects (e.g., "file", "group")

        Returns:
            Namespace configuration dict or None if not found

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Get file namespace
            >>> ns = nx.get_namespace("file")
            >>> if ns:
            ...     print(f"Relations: {ns['config']['relations'].keys()}")
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        ns = self._require_rebac.get_namespace(object_type)
        if ns is None:
            return None

        return {
            "namespace_id": ns.namespace_id,
            "object_type": ns.object_type,
            "config": ns.config,
            "created_at": ns.created_at.isoformat(),
            "updated_at": ns.updated_at.isoformat(),
        }

    @rpc_expose(description="Create or update ReBAC namespace")
    def namespace_create(self, object_type: str, config: dict[str, Any]) -> None:
        """Create or update a namespace configuration.

        Args:
            object_type: Type of objects this namespace applies to (e.g., "document", "project")
            config: Namespace configuration with "relations" and "permissions" keys

        Raises:
            RuntimeError: If ReBAC is not available
            ValueError: If configuration is invalid

        Examples:
            >>> # Create custom document namespace
            >>> nx.namespace_create("document", {
            ...     "relations": {
            ...         "owner": {},
            ...         "editor": {},
            ...         "viewer": {"union": ["editor", "owner"]}
            ...     },
            ...     "permissions": {
            ...         "read": ["viewer", "editor", "owner"],
            ...         "write": ["editor", "owner"]
            ...     }
            ... })
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # Validate config structure
        if "relations" not in config or "permissions" not in config:
            raise ValueError("Namespace config must have 'relations' and 'permissions' keys")

        # Create namespace object
        import uuid
        from datetime import UTC, datetime

        from nexus.core.rebac import NamespaceConfig

        ns = NamespaceConfig(
            namespace_id=str(uuid.uuid4()),
            object_type=object_type,
            config=config,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        self._require_rebac.create_namespace(ns)

    @rpc_expose(description="List all ReBAC namespaces")
    def namespace_list(self) -> list[dict[str, Any]]:
        """List all registered namespace configurations.

        Returns:
            List of namespace dictionaries with metadata and config

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # List all namespaces
            >>> namespaces = nx.namespace_list()
            >>> for ns in namespaces:
            ...     print(f"{ns['object_type']}: {list(ns['config']['relations'].keys())}")
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # Get all namespaces by querying the database
        conn = self._require_rebac._get_connection()
        try:
            cursor = self._require_rebac._create_cursor(conn)

            cursor.execute(
                self._require_rebac._fix_sql_placeholders(
                    "SELECT namespace_id, object_type, config, created_at, updated_at FROM rebac_namespaces ORDER BY object_type"
                )
            )

            namespaces = []
            for row in cursor.fetchall():
                import json

                namespaces.append(
                    {
                        "namespace_id": row["namespace_id"],
                        "object_type": row["object_type"],
                        "config": json.loads(row["config"]),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
                )

            return namespaces
        finally:
            self._require_rebac._close_connection(conn)

    @rpc_expose(description="Delete ReBAC namespace")
    def namespace_delete(self, object_type: str) -> bool:
        """Delete a namespace configuration.

        Args:
            object_type: Type of objects to remove namespace for

        Returns:
            True if namespace was deleted, False if not found

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Delete custom namespace
            >>> nx.namespace_delete("document")
            True
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        conn = self._require_rebac._get_connection()
        try:
            cursor = self._require_rebac._create_cursor(conn)

            # Check if exists
            cursor.execute(
                self._require_rebac._fix_sql_placeholders(
                    "SELECT namespace_id FROM rebac_namespaces WHERE object_type = ?"
                ),
                (object_type,),
            )

            if cursor.fetchone() is None:
                return False

            # Delete
            cursor.execute(
                self._require_rebac._fix_sql_placeholders(
                    "DELETE FROM rebac_namespaces WHERE object_type = ?"
                ),
                (object_type,),
            )

            conn.commit()

            # Invalidate cache if available
            cache = getattr(self._require_rebac, "_cache", None)
            if cache is not None:
                cache.clear()

            return True
        finally:
            self._require_rebac._close_connection(conn)

    # Consent/Privacy/Sharing/DynamicViewer removed —
    # served via register_service(rebac_service).

    def grant_traverse_on_implicit_dirs(
        self,
        zone_id: str | None = None,
        subject: tuple[str, str] | None = None,
    ) -> list[Any]:
        """Grant TRAVERSE permission on root-level implicit directories.

        This is an optimization for FUSE path resolution. By granting TRAVERSE
        on directories like /zones, /sessions, /skills, we enable O(1) stat()
        checks instead of expensive O(n) descendant access checks.

        Args:
            zone_id: Zone ID for the permissions (default: "root")
            subject: Subject to grant TRAVERSE to (default: ("group", "authenticated"))
                     Use ("group", "authenticated") for all authenticated users.

        Returns:
            List of tuple IDs for created permissions

        Note:
            This should be called during system initialization to set up
            base traverse permissions. TRAVERSE permission allows stat/access
            by name but NOT listing directory contents.

        Examples:
            >>> # Grant traverse to all authenticated users on root directories
            >>> nx.grant_traverse_on_implicit_dirs()
            ['uuid-1', 'uuid-2', 'uuid-3']

            >>> # Grant traverse to a specific user
            >>> nx.grant_traverse_on_implicit_dirs(
            ...     subject=("user", "alice"),
            ...     zone_id="org_acme"
            ... )
        """
        from sqlalchemy.exc import OperationalError

        from nexus.services.permissions.utils.zone import normalize_zone_id

        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # Default subject is authenticated users group
        if subject is None:
            subject = ("group", "authenticated")

        effective_zone_id = normalize_zone_id(zone_id)

        # Root-level implicit directories that need TRAVERSE permission
        implicit_dirs = [
            "/",
            "/zones",
            "/sessions",
            "/skills",
            "/workspace",
            "/shared",
            "/system",
            "/archives",
            "/external",
        ]

        tuple_ids = []
        for dir_path in implicit_dirs:
            try:
                # Check if permission already exists
                existing = self.rebac_list_tuples(
                    subject=subject,
                    relation="traverser-of",
                    object=("file", dir_path),
                )
                if existing:
                    continue

                # Create TRAVERSE permission
                tuple_id = self._require_rebac.rebac_write(
                    subject=subject,
                    relation="traverser-of",
                    object=("file", dir_path),
                    zone_id=effective_zone_id,
                )
                tuple_ids.append(tuple_id)
            except (RuntimeError, ValueError, OperationalError) as e:
                logger.warning(f"Failed to grant TRAVERSE on {dir_path}: {e}")

        return tuple_ids

    def process_tiger_cache_queue(self, batch_size: int = 100) -> int:
        """Process pending Tiger Cache update queue.

        Call this periodically from a background worker to rebuild Tiger Cache
        entries that were queued by rebac_create/rebac_delete operations.

        Args:
            batch_size: Maximum entries to process per call (default: 100)

        Returns:
            Number of entries processed

        Note:
            This should be called periodically (e.g., every 1-5 seconds) from
            a background worker to ensure Tiger Cache stays up-to-date.

        Examples:
            >>> # In a background worker
            >>> import asyncio
            >>> async def tiger_worker(nx):
            ...     while True:
            ...         processed = nx.process_tiger_cache_queue()
            ...         if processed > 0:
            ...             print(f"Processed {processed} Tiger Cache updates")
            ...         await asyncio.sleep(1)
        """
        if not hasattr(self, "_rebac_manager"):
            return 0

        if hasattr(self._rebac_manager, "tiger_process_queue"):
            return self._require_rebac.tiger_process_queue(batch_size=batch_size)

        return 0

    def warm_tiger_cache(
        self,
        subjects: list[tuple[str, str]] | None = None,
        zone_id: str | None = None,
    ) -> int:
        """Warm the Tiger Cache by pre-computing permissions for subjects.

        Call this on startup or after major permission changes to pre-populate
        the Tiger Cache for faster subsequent permission checks.

        Args:
            subjects: List of subjects to warm cache for (default: all subjects with tuples)
            zone_id: Zone ID to scope warming (default: "root")

        Returns:
            Number of cache entries created

        Note:
            This can be slow for large systems. Consider calling during
            off-peak hours or limiting to specific subjects.

        Examples:
            >>> # Warm cache for all subjects
            >>> nx.warm_tiger_cache()
            42

            >>> # Warm cache for specific users
            >>> nx.warm_tiger_cache(subjects=[("user", "alice"), ("user", "bob")])
            8
        """
        from sqlalchemy.exc import OperationalError

        from nexus.services.permissions.utils.zone import normalize_zone_id

        if not hasattr(self, "_rebac_manager"):
            return 0

        effective_zone_id = normalize_zone_id(zone_id)
        entries_created = 0

        # If no subjects provided, get all unique subjects from tuples
        if subjects is None:
            try:
                tuples = self.rebac_list_tuples()
                subjects_set: set[tuple[str, str]] = set()
                for t in tuples:
                    subject_type = t.get("subject_type")
                    subject_id = t.get("subject_id")
                    if subject_type and subject_id:
                        subjects_set.add((subject_type, subject_id))
                subjects = list(subjects_set)
            except (KeyError, TypeError, AttributeError):
                subjects = []

        # Queue updates for each subject
        for subject in subjects:
            if hasattr(self._rebac_manager, "tiger_queue_update"):
                # Queue updates for common permissions
                for permission in ["read", "write", "traverse"]:
                    self._require_rebac.tiger_queue_update(
                        subject=subject,
                        permission=permission,
                        resource_type="file",
                        zone_id=effective_zone_id,
                    )
                    entries_created += 1

        # Process the queue (non-blocking - ignore lock errors)
        # Lock errors are expected during concurrent operations, queue will be processed later
        if hasattr(self._rebac_manager, "tiger_process_queue"):
            try:
                # Use small batch size since each entry can take 10-40 seconds
                self._require_rebac.tiger_process_queue(batch_size=5)
            except (RuntimeError, OperationalError) as e:
                logger.warning(f"[WARM-TIGER] Queue processing failed: {e}")

        return entries_created

    # MCP/Skill/LLM/OAuth facades removed —
    # served via register_service(mcp_service, skill_service, llm_service, oauth_service).

    # MountService async delegation removed —
    # served via register_service(mount_service).

    # Mount/Sync thin delegators (RPC via register_service(mount_service))
    # -------------------------------------------------------------------------

    def add_mount(
        self, mount_point: str, backend_type: str, backend_config: dict[str, Any],
        priority: int = 0, readonly: bool = False, io_profile: str = "balanced",
        context: OperationContext | None = None,
    ) -> str:
        """Add a dynamic backend mount."""
        return self._mount_core_service.add_mount(
            mount_point=mount_point, backend_type=backend_type,
            backend_config=backend_config, priority=priority,
            readonly=readonly, io_profile=io_profile, context=context,
        )

    def remove_mount(self, mount_point: str, context: OperationContext | None = None) -> dict[str, Any]:
        """Remove a backend mount."""
        return self._mount_core_service.remove_mount(mount_point=mount_point, context=context)

    def list_connectors(self, category: str | None = None) -> list[dict[str, Any]]:
        """List available connector types."""
        return self._mount_core_service.list_connectors(category)

    def list_mounts(self, context: OperationContext | None = None) -> list[dict[str, Any]]:
        """List all active backend mounts."""
        return self._mount_core_service.list_mounts(context)

    def get_mount(self, mount_point: str, context: OperationContext | None = None) -> dict[str, Any] | None:
        """Get mount details."""
        return self._mount_core_service.get_mount(mount_point, context)

    def has_mount(self, mount_point: str) -> bool:
        """Check if a mount exists."""
        return self._mount_core_service.has_mount(mount_point)

    def sync_mount(
        self, mount_point: str | None = None, path: str | None = None,
        recursive: bool = True, dry_run: bool = False, sync_content: bool = True,
        include_patterns: list[str] | None = None, exclude_patterns: list[str] | None = None,
        generate_embeddings: bool = False, context: OperationContext | None = None,
        progress_callback: Any = None, full_sync: bool = False,
    ) -> dict[str, Any]:
        """Sync metadata and content from connector backend(s)."""
        from nexus.services.sync_service import SyncContext

        ctx = SyncContext(
            mount_point=mount_point, path=path, recursive=recursive,
            dry_run=dry_run, sync_content=sync_content,
            include_patterns=include_patterns, exclude_patterns=exclude_patterns,
            generate_embeddings=generate_embeddings, context=context,
            progress_callback=progress_callback, full_sync=full_sync,
        )
        return self._sync_service.sync_mount(ctx).to_dict()

    def sync_mount_async(
        self, mount_point: str, path: str | None = None,
        recursive: bool = True, dry_run: bool = False, sync_content: bool = True,
        include_patterns: list[str] | None = None, exclude_patterns: list[str] | None = None,
        generate_embeddings: bool = False, context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Start an async sync job for a mount."""
        if mount_point is None:
            raise ValueError("mount_point is required for async sync")
        user_id = getattr(context, "subject_id", None) if context else None
        params = {
            "path": path, "recursive": recursive, "dry_run": dry_run,
            "sync_content": sync_content, "include_patterns": include_patterns,
            "exclude_patterns": exclude_patterns, "generate_embeddings": generate_embeddings,
        }
        job_id = self._sync_job_service.create_job(mount_point, params, user_id)
        self._sync_job_service.start_job(job_id)
        return {"job_id": job_id, "status": "pending", "mount_point": mount_point}

    def get_sync_job(self, job_id: str) -> dict[str, Any] | None:
        """Get sync job status."""
        return self._sync_job_service.get_job(job_id)

    def cancel_sync_job(self, job_id: str) -> dict[str, Any]:
        """Cancel a running sync job."""
        success = self._sync_job_service.cancel_job(job_id)
        if success:
            return {"success": True, "job_id": job_id, "message": "Cancellation requested"}
        job = self._sync_job_service.get_job(job_id)
        if not job:
            return {"success": False, "job_id": job_id, "message": "Job not found"}
        return {"success": False, "job_id": job_id, "message": f"Cannot cancel job with status: {job['status']}"}

    def list_sync_jobs(
        self, mount_point: str | None = None, status: str | None = None, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List sync jobs with optional filters."""
        return self._sync_job_service.list_jobs(mount_point=mount_point, status=status, limit=limit)

    def save_mount(
        self, mount_point: str, backend_type: str, backend_config: dict[str, Any],
        priority: int = 0, readonly: bool = False, io_profile: str = "balanced",
        owner_user_id: str | None = None, zone_id: str | None = None,
        description: str | None = None, context: OperationContext | None = None,
    ) -> str:
        """Save mount configuration to database."""
        return self._mount_persist_service.save_mount(
            mount_point=mount_point, backend_type=backend_type,
            backend_config=backend_config, priority=priority, readonly=readonly,
            io_profile=io_profile, owner_user_id=owner_user_id,
            zone_id=zone_id, description=description, context=context,
        )

    def list_saved_mounts(
        self, owner_user_id: str | None = None, zone_id: str | None = None,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List saved mount configurations."""
        return self._mount_persist_service.list_saved_mounts(
            owner_user_id=owner_user_id, zone_id=zone_id, context=context,
        )

    def load_mount(self, mount_point: str) -> str:
        """Load saved mount configuration and activate it."""
        return self._mount_persist_service.load_mount(mount_point)

    def delete_saved_mount(self, mount_point: str) -> bool:
        """Delete saved mount configuration."""
        return self._mount_persist_service.delete_saved_mount(mount_point)

    def load_all_saved_mounts(self, auto_sync: bool = False) -> dict[str, Any]:
        """Load all saved mount configurations."""
        return self._mount_persist_service.load_all_mounts(auto_sync)

    def _matches_patterns(
        self, file_path: str, include_patterns: list[str] | None,
        exclude_patterns: list[str] | None,
    ) -> bool:
        """Check if file path matches include/exclude patterns."""
        from nexus.services.sync_service import SyncContext

        ctx = SyncContext(mount_point=None, include_patterns=include_patterns, exclude_patterns=exclude_patterns)
        return self._sync_service._matches_patterns(file_path, ctx)

    def _grant_mount_owner_permission(self, mount_point: str, context: OperationContext | None) -> None:
        """Grant direct_owner permission to mount creator."""
        self._mount_core_service._grant_owner_permission(mount_point, context)

    # -------------------------------------------------------------------------
    # Search thin delegators (RPC via register_service(search_service))
    # -------------------------------------------------------------------------

    def list(
        self, path: str = "/", recursive: bool = True, details: bool = False,
        show_parsed: bool = True, context: Any = None,
        limit: int | None = None, cursor: str | None = None,
    ) -> list[str] | list[dict[str, Any]] | Any:
        """List files in a directory."""
        return self.search_service.list(
            path=path, recursive=recursive, details=details,
            show_parsed=show_parsed, context=context, limit=limit, cursor=cursor,
        )

    def glob(self, pattern: str, path: str = "/", context: Any = None) -> builtins.list[str]:
        """Find files matching a glob pattern."""
        return self.search_service.glob(pattern=pattern, path=path, context=context)

    def glob_batch(
        self, patterns: builtins.list[str], path: str = "/", context: Any = None,
    ) -> dict[str, builtins.list[str]]:
        """Execute multiple glob patterns in a single call."""
        return self.search_service.glob_batch(patterns=patterns, path=path, context=context)

    def grep(
        self, pattern: str, path: str = "/", file_pattern: str | None = None,
        ignore_case: bool = False, max_results: int = 100,
        search_mode: str = "auto", context: Any = None,
    ) -> builtins.list[dict[str, Any]]:
        """Search file contents using regex patterns."""
        return self.search_service.grep(
            pattern=pattern, path=path, file_pattern=file_pattern,
            ignore_case=ignore_case, max_results=max_results,
            search_mode=search_mode, context=context,
        )

    # =========================================================================
    # SearchService Delegation Methods (Semantic Search)
    # =========================================================================

    async def asemantic_search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        filters: dict[str, Any] | None = None,
        search_mode: str = "semantic",
    ) -> builtins.list[dict[str, Any]]:
        """Search documents using natural language queries - delegates to SearchService."""
        return await self.search_service.semantic_search(
            query=query,
            path=path,
            limit=limit,
            filters=filters,
            search_mode=search_mode,
        )

    async def asemantic_search_index(
        self,
        path: str = "/",
        recursive: bool = True,
    ) -> dict[str, int]:
        """Index documents for semantic search - delegates to SearchService."""
        return await self.search_service.semantic_search_index(
            path=path,
            recursive=recursive,
        )

    async def asemantic_search_stats(self) -> dict[str, Any]:
        """Get semantic search indexing statistics - delegates to SearchService."""
        return await self.search_service.semantic_search_stats()

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

    # -------------------------------------------------------------------------
    # TaskQueue thin delegators (RPC via register_service(task_queue_service))
    # Only get_task and cancel_task kept — called by a2a module.
    # -------------------------------------------------------------------------

    def get_task(self, task_id: int, context: OperationContext | None = None) -> dict[str, Any] | None:  # noqa: ARG002
        """Get task status, progress, and result."""
        return self.task_queue_service.get_task(task_id)

    def cancel_task(self, task_id: int, context: OperationContext | None = None) -> dict[str, Any]:  # noqa: ARG002
        """Cancel a pending or running task."""
        return self.task_queue_service.cancel_task(task_id)

    def close(self) -> None:
        """Close the filesystem and release resources."""
        # Stop DeferredPermissionBuffer first to flush pending permissions
        if hasattr(self, "_deferred_permission_buffer") and self._deferred_permission_buffer:
            self._deferred_permission_buffer.stop()

        # Stop Tiger Cache background worker first
        self.stop_tiger_cache_worker()

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
