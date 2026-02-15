"""Unified filesystem implementation for Nexus."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from nexus.backends.backend import Backend
from nexus.core.exceptions import InvalidPathError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content

if TYPE_CHECKING:
    from nexus.core.memory_api import Memory
from nexus.core._metadata_generated import FileMetadata, FileMetadataProtocol
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
    CollisionDetail,
    ExportFilter,
    ImportOptions,
    ImportResult,
)
from nexus.core.filesystem import NexusFilesystem
from nexus.core.nexus_fs_core import NexusFSCoreMixin
from nexus.core.nexus_fs_events import NexusFSEventsMixin

# NexusFSLLMMixin removed in Phase B — replaced by LLMSubsystem (Issue #1287)
# NexusFSMCPMixin removed in Phase 1.4 — replaced by MCPService delegation (Issue #1287)
# NexusFSMountsMixin removed in Phase 3 — replaced by service delegation (Issue #1387)
# NexusFSOAuthMixin removed in Phase 1.3 — replaced by OAuthService delegation (Issue #1287)
# NexusFSReBACMixin removed in Phase 3 — replaced by service delegation (Issue #1387)
# NexusFSSearchMixin removed in Phase 1.1 — replaced by SearchService delegation (Issue #1287)
# NexusFSShareLinksMixin removed in Phase 3 — replaced by ShareLinkService delegation (Issue #1387)
# NexusFSSkillsMixin removed in Phase 1.5 — replaced by SkillService delegation (Issue #1287)
# NexusFSTasksMixin removed in Phase 3 — replaced by TaskQueueService delegation (Issue #1387)
# NexusFSVersionsMixin removed in Phase 2.3 - replaced by VersionService
from nexus.core.permissions import OperationContext, Permission
from nexus.core.router import NamespaceConfig, PathRouter
from nexus.core.rpc_decorator import rpc_expose
from nexus.parsers import MarkItDownParser, ParserRegistry
from nexus.parsers.types import ParseResult

# Phase 2: Service imports moved to _wire_services() as lazy imports (Issue #1519)
# NexusFSReBACMixin import removed (Issue #1387)
from nexus.storage.content_cache import ContentCache
from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


class NexusFS(  # type: ignore[misc]
    NexusFSCoreMixin,
    # NexusFSReBACMixin removed — replaced by service delegation (Issue #1387)
    # NexusFSShareLinksMixin removed — replaced by ShareLinkService delegation (Issue #1387)
    # NexusFSVersionsMixin removed - replaced by VersionService (Phase 2.3)
    # NexusFSMountsMixin removed — replaced by service delegation (Issue #1387)
    # NexusFSOAuthMixin removed — replaced by OAuthService delegation (Issue #1287)
    # NexusFSSkillsMixin removed — replaced by SkillService delegation (Issue #1287)
    # NexusFSMCPMixin removed — replaced by MCPService delegation (Issue #1287)
    # NexusFSLLMMixin removed — replaced by LLMSubsystem (Issue #1287)
    NexusFSEventsMixin,  # Issue #1106: Same-box file watching
    # NexusFSTasksMixin removed — replaced by TaskQueueService delegation (Issue #1387)
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
        metadata_store: FileMetadataProtocol,
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
    ):
        """Initialize NexusFS kernel.

        Args:
            backend: Backend instance for file storage (LocalBackend, GCSBackend, etc.)
            metadata_store: FileMetadataProtocol instance (RaftMetadataStore or custom)
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

        # Initialize content cache if enabled and backend supports it
        if cache.enable_content_cache and backend.has_root_path is True:
            content_cache = ContentCache(max_size_mb=cache.content_cache_size_mb)
            backend.content_cache = content_cache

        # Store backend
        self.backend = backend

        # Initialize metadata store (Task #14: Dependency Injection)
        self.metadata: FileMetadataProtocol = metadata_store

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

        # Initialize parser registry with default MarkItDown parser (legacy, for auto_parse)
        self.parser_registry = ParserRegistry()
        self.parser_registry.register(MarkItDownParser())

        # Initialize new provider registry for read(parsed=True) support
        from nexus.parsers.providers import ProviderRegistry
        from nexus.parsers.providers.base import ProviderConfig

        self.provider_registry = ProviderRegistry()

        parse_providers = [dict(p) for p in parsing.providers] if parsing.providers else None
        if parse_providers:
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
            self.provider_registry.auto_discover()

        # Track active parser threads for graceful shutdown
        self._parser_threads: list[threading.Thread] = []
        self._parser_threads_lock = threading.Lock()

        # Create default context (zone_id defaults to "default")
        self._default_context = OperationContext(
            user="anonymous",
            groups=[],
            zone_id="default",
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

        # Agent registry — injected externally (e.g. by FastAPI lifespan) or
        # lazily created via _ensure_agent_registry() when first needed.
        self._agent_registry: Any | None = None

        # Infrastructure services (previously created inline, now injected)
        self._event_bus = svc.event_bus
        self._lock_manager = svc.lock_manager
        self.enable_workflows = distributed.enable_workflows
        self.workflow_engine = svc.workflow_engine

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

        # Issue #913: Track async event tasks to prevent memory leaks
        self._event_tasks: set[asyncio.Task[Any]] = set()

        # Issue #1106: File watcher for same-box event detection (lazy initialized)
        self._file_watcher: Any = None

        # Distributed coordination clients (may be set by factory)
        self._coordination_client: Any = None
        self._event_client: Any = None

        # Issue #1106: Auto-start flag for cache invalidation
        self._cache_invalidation_started: bool = False

        # VFS lock manager — local, in-process path-level locking (Issue #1398)
        from nexus.core.lock_fast import create_vfs_lock_manager

        self._vfs_lock_manager = create_vfs_lock_manager()
        logger.info("VFS lock manager initialized (%s)", type(self._vfs_lock_manager).__name__)

        # Wire self-dependent services (require self reference)
        self._wire_services()

        # Issue #1169: Read Set-Aware Cache for precise invalidation
        # Wraps the metadata cache with read-set-aware invalidation.
        # Falls back to path-based invalidation for entries without read sets.
        self._read_set_cache = None
        metadata_cache = None
        if hasattr(self.metadata, "_cache"):
            metadata_cache = self.metadata._cache

        if metadata_cache is not None and self._cache_config.enable_metadata_cache:
            from nexus.core.read_set import ReadSetRegistry
            from nexus.core.read_set_cache import ReadSetAwareCache

            self._read_set_registry = ReadSetRegistry()
            self._read_set_cache = ReadSetAwareCache(
                base_cache=metadata_cache,
                registry=self._read_set_registry,
            )
            self._read_tracking_enabled = True

        # OPTIMIZATION: Initialize TRAVERSE permissions and Tiger Cache
        self._init_performance_optimizations()

    def _wire_services(self) -> None:
        """Wire services that require a reference to self (NexusFS).

        Called at end of __init__. These services cannot be pre-created in
        factory.py because they need the fully-constructed NexusFS instance.
        """
        # VersionService: injected by factory (Task #45)
        self.version_service = self._services.version_service

        # Lazy-import services to avoid core/ → services/ top-level coupling (#1519)
        from nexus.services.llm_service import LLMService
        from nexus.services.mcp_service import MCPService
        from nexus.services.mount_service import MountService
        from nexus.services.oauth_service import OAuthService
        from nexus.services.rebac_service import ReBACService
        from nexus.services.search_service import SearchService

        # ReBACService: Permission and access control operations
        self.rebac_service = ReBACService(
            rebac_manager=self._rebac_manager,
            enforce_permissions=self._enforce_permissions,
            enable_audit_logging=True,
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

        # SkillService: Skill management
        from nexus.services.skill_service import SkillService as _SkillService

        self.skill_service = _SkillService(gateway=self._gateway)

        # SearchService: Search operations
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
        from nexus.services.events_service import EventsService

        metadata_cache = None
        if hasattr(self.metadata, "_cache"):
            metadata_cache = self.metadata._cache

        self.events_service = EventsService(
            backend=self.backend,
            event_bus=self._event_bus,
            lock_manager=self._lock_manager,
            file_watcher=self._file_watcher,
            zone_id=None,
            metadata_cache=metadata_cache,
        )

    @property
    def _service_extras(self) -> dict[str, Any]:
        """Server layer reads extras via this dict interface."""
        return {k: v for k, v in self._services.server_extras.items() if v is not None}

    @_service_extras.setter
    def _service_extras(self, value: dict[str, Any]) -> None:
        """Server layer sets extras via dict assignment."""
        self._services.server_extras.update(value)

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

    def _init_performance_optimizations(self) -> None:
        """Initialize performance optimizations for permission checks.

        This method:
        1. Syncs tiger_resource_map from existing metadata (Issue #934)
        2. Grants TRAVERSE permission on implicit directories (enables O(1) stat)
        3. Warms the Tiger Cache for faster subsequent permission checks
        4. Starts background worker for Tiger Cache queue processing

        Called automatically during __init__. Can be called manually to refresh.
        """
        import logging
        import os

        logger = logging.getLogger(__name__)

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
        import logging

        logger = logging.getLogger(__name__)

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
        import logging
        import os
        import threading

        logger = logging.getLogger(__name__)

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
            from nexus.services.permissions.entity_registry import EntityRegistry

            # Get or create entity registry (v0.5.0: Pass SessionFactory instead of Session)
            if self._entity_registry is None:
                self._entity_registry = EntityRegistry(self.SessionLocal)

            # Create a session from SessionLocal
            session = self.SessionLocal()

            # Issue #1258: Create MemoryWithPaging if enabled, else standard Memory
            if self._enable_memory_paging:
                from nexus.core.memory_with_paging import MemoryWithPaging

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
                from nexus.core.memory_api import Memory

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

    # Backward compatibility properties for deprecated instance fields
    @property
    def zone_id(self) -> str | None:
        """DEPRECATED: Access via context parameter instead. Returns default zone_id for embedded mode."""
        return self._default_context.zone_id

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
            context: Optional context dict with zone_id, user_id, agent_id

        Returns:
            Memory API instance
        """
        from nexus.core.memory_api import Memory
        from nexus.services.permissions.entity_registry import EntityRegistry

        # Get or create entity registry
        if self._entity_registry is None:
            self._entity_registry = EntityRegistry(self.SessionLocal)

        # Create a session
        session = self.SessionLocal()

        # Parse context properly
        ctx = self._parse_context(context)

        return Memory(
            session=session,
            backend=self.backend,
            zone_id=ctx.zone_id or self._default_context.zone_id,
            user_id=ctx.user or self._default_context.user,
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
        from nexus.core.permissions import OperationContext

        # If already an OperationContext, return as-is
        if isinstance(context, OperationContext):
            return context

        if context is None:
            context = {}

        return OperationContext(
            user=context.get("user_id", "system"),
            groups=context.get("groups", []),
            zone_id=context.get("zone_id"),
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

        # P0-4: Zone boundary security check (Issue #819)
        # Even admins need zone boundary checks (unless they have MANAGE_ZONES capability)
        if ctx.is_admin and self._permission_enforcer:
            from nexus.services.permissions.permissions_enhanced import AdminCapability

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
                f"_check_permission: SKIPPED (admin/system bypass) - path={path}, permission={permission.name}, user={ctx.user}"
            )
            return

        logger.debug(
            f"_check_permission: path={path}, permission={permission.name}, user={ctx.user}, zone={getattr(ctx, 'zone_id', None)}"
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
            subject_id = ctx.subject_id or ctx.user
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
            zone_id=ctx.zone_id or "default",  # P0 SECURITY: Set zone_id
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
                            parent_dir, zone_id=ctx.zone_id or "default"
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
                    f"mkdir: Calling ensure_parent_tuples for {path}, zone_id={ctx.zone_id or 'default'}"
                )
                created_count = self._hierarchy_manager.ensure_parent_tuples(
                    path, zone_id=ctx.zone_id or "default"
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
                    zone_id=ctx.zone_id or "default",
                )
                logger.debug(f"mkdir: Granted direct_owner permission to {ctx.user} for {path}")
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
        from nexus.core.permissions import OperationContext

        if context is not None:
            ctx = (
                context
                if isinstance(context, OperationContext)
                else OperationContext(
                    user=context.user,
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
                user=subject[1],
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
                    user=self._default_context.user,
                    groups=self._default_context.groups,
                    zone_id=zone_id or self._default_context.zone_id,
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
            zone_id=ctx.zone_id,
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
                        route.backend.delete_content(file_meta.etag).unwrap()

            # Batch delete from metadata store
            self.metadata.delete_batch(file_paths)

        # Remove directory in backend (if it still exists)
        # In CAS systems, the directory may no longer exist after deleting its contents
        with contextlib.suppress(NexusFileNotFoundError):
            route.backend.rmdir(route.backend_path, recursive=recursive).unwrap()

        # Also delete the directory's own metadata entry if it exists
        # Directories can have metadata entries (created by mkdir)
        with contextlib.suppress(Exception):
            self.metadata.delete(path)

        # Clean up sparse directory index entries (Issue: rmdir not cleaning directory index)
        # This removes entries from DirectoryEntryModel used by non-recursive list()
        if hasattr(self.metadata, "delete_directory_entries_recursive"):
            with contextlib.suppress(Exception):
                self.metadata.delete_directory_entries_recursive(path)

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
        import logging

        logger = logging.getLogger(__name__)

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
            Permission.TRAVERSE: "traverse",
        }
        rebac_permission = permission_map.get(permission, "read")
        zone_id = context.zone_id or "default"

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
        except Exception:
            # If metadata query fails, return False
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
                    checks, zone_id=context.zone_id or "default"
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
        # OPTIMIZATION: Find common ancestor and query ONCE instead of N queries
        all_checks = []
        path_to_descendants: dict[str, list[str]] = {}

        # Find common ancestor of all paths to minimize DB queries
        if len(paths) > 1:
            # Find the longest common prefix among all paths
            common_prefix = paths[0]
            for path in paths[1:]:
                # Find common prefix between current common_prefix and this path
                min_len = min(len(common_prefix), len(path))
                i = 0
                while i < min_len and common_prefix[i] == path[i]:
                    i += 1
                common_prefix = common_prefix[:i]

            # Trim to last / to get valid directory path
            if "/" in common_prefix:
                common_prefix = common_prefix[: common_prefix.rfind("/") + 1]
            else:
                common_prefix = "/"

            # Query common ancestor ONCE and cache all descendants
            logger.debug(
                f"_has_descendant_access_bulk: Using common ancestor optimization - "
                f"querying '{common_prefix}' once for {len(paths)} directories"
            )
            try:
                all_descendants = self.metadata.list(common_prefix if common_prefix else "/")
                all_paths_set = {meta.path for meta in all_descendants}
                logger.debug(
                    f"_has_descendant_access_bulk: Got {len(all_paths_set)} paths from common ancestor"
                )
            except Exception as e:
                logger.warning(
                    f"_has_descendant_access_bulk: Failed to list common ancestor {common_prefix}: {e}"
                )
                all_paths_set = set()

            # Filter locally for each directory
            for path in paths:
                # Check direct access to the directory itself
                all_checks.append((subject_tuple, rebac_permission, ("file", path)))

                # Filter descendants from cached list
                prefix = path if path.endswith("/") else path + "/"
                if path == "/":
                    descendant_paths = list(all_paths_set)
                else:
                    descendant_paths = [p for p in all_paths_set if p.startswith(prefix)]

                path_to_descendants[path] = descendant_paths

                # Add checks for all descendants
                for desc_path in descendant_paths:
                    all_checks.append((subject_tuple, rebac_permission, ("file", desc_path)))
        else:
            # Single path - just query directly
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
                all_checks, zone_id=context.zone_id or "default"
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
                zone_id=ctx.zone_id,  # v0.6.0: from context
                agent_id=ctx.agent_id,  # v0.6.0: from context
                is_admin=ctx.is_admin,  # v0.6.0: from context
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
                zone_id, agent_id, is_admin = self._get_routing_params(context)
                route = self.router.route(
                    path.rstrip("/"),
                    zone_id=zone_id,
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
            filter: Export filter options (zone_id, path_prefix, after_time, include_deleted)
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
                zone_id="acme-corp"
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

        # Get all files matching prefix (exclude internal system entries)
        from nexus.core.nexus_fs_core import SYSTEM_PATH_PREFIX

        all_files = [
            m
            for m in self.metadata.list_iter(filter.path_prefix)
            if not m.path.startswith(SYSTEM_PATH_PREFIX)
        ]

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

            # Note: include_deleted and zone_id filtering would require
            # database-level support. For now, we skip these filters.
            # TODO: Add deleted_at column support and zone filtering

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
                    if file_meta.custom_metadata:
                        metadata_dict["custom_metadata"] = dict(file_meta.custom_metadata)
                except (AttributeError, TypeError):
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

        parse_ctx = OperationContext(user="system_parser", groups=[], zone_id=None, is_system=True)
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
            # Auto-construct path from agent_id (simple format, no zone in path)
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

        # v0.5.0: Extract user_id, agent_id, and zone_id from context (set by RPC authentication)
        ctx = self._parse_context(context)

        return self._workspace_manager.create_snapshot(
            workspace_path=workspace_path,
            description=description,
            tags=tags,
            created_by=created_by,
            user_id=ctx.user
            or self._default_context.user,  # v0.5.0: Pass user_id for permission check
            agent_id=ctx.agent_id or self._default_context.agent_id,
            zone_id=ctx.zone_id or self._default_context.zone_id,  # v0.5.0: Use context zone_id
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
            context: Operation context with user, permissions, zone info (uses default if None)

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
            zone_id=ctx.zone_id or self._default_context.zone_id,
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
            context: Operation context with user, permissions, zone info (uses default if None)

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
            zone_id=ctx.zone_id or self._default_context.zone_id,
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
            context: Operation context with user, permissions, zone info (uses default if None)

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
            zone_id=ctx.zone_id or self._default_context.zone_id,
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
            zone_id=ctx.zone_id or self._default_context.zone_id,
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
    def update_workspace(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Update an existing workspace configuration.

        Args:
            path: Workspace path to update
            name: Optional new friendly name
            description: Optional new description
            metadata: Optional new metadata

        Returns:
            Updated workspace configuration dict

        Example:
            >>> nx.update_workspace("/my-workspace", name="Updated Name", description="New description")
            {'path': '/my-workspace', 'name': 'Updated Name', 'description': 'New description', ...}
        """
        config = self._workspace_registry.update_workspace(path, name, description, metadata)
        return config.to_dict()

    @rpc_expose()
    def list_workspaces(self, context: Any | None = None) -> list[dict]:
        """List all registered workspaces for the current user.

        Requires authenticated context (raises ValueError if missing).

        Filters workspaces by:
        1. Workspaces created by the user (created_by matches user_id)
        2. OR workspaces in the user's zone-scoped path

        Args:
            context: Required operation context with user_id and zone_id

        Returns:
            List of workspace configuration dicts filtered by current user

        Raises:
            ValueError: If context is None or missing user_id/zone_id

        Example:
            >>> workspaces = nx.list_workspaces(context=ctx)
            >>> for ws in workspaces:
            ...     print(f"{ws['path']}: {ws['name']}")
        """
        # Require authenticated context to prevent leaking all workspaces
        user_id = None
        zone_id = None
        if context is not None:
            user_id = getattr(context, "user_id", None) or getattr(context, "user", None)
            zone_id = getattr(context, "zone_id", None)

        if not user_id or not zone_id:
            raise ValueError(
                "list_workspaces requires authenticated context with user_id and zone_id"
            )

        configs = self._workspace_registry.list_workspaces()

        # Filter workspaces belonging to the current user by:
        # 1. created_by matches user_id (workspaces the user registered at any path)
        # 2. OR path follows zone/user pattern (workspaces in user's scoped directory)
        user_prefix = f"/zone/{zone_id}/user/{user_id}/workspace/"
        configs = [c for c in configs if c.created_by == user_id or c.path.startswith(user_prefix)]

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

    def _extract_zone_id(self, context: dict | Any | None) -> str | None:
        """Extract zone_id from context (dict or OperationContext)."""
        if not context:
            return None
        if isinstance(context, dict):
            return context.get("zone_id")
        return getattr(context, "zone_id", None)

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
                zone_id = self._extract_zone_id(context) or "default"

                # Grant direct_owner to the agent itself
                try:
                    logger.debug(
                        f"register_agent: Granting direct_owner to agent {agent_id} for {agent_dir}"
                    )
                    self._rebac_manager.rebac_write(
                        subject=("agent", agent_id),
                        relation="direct_owner",
                        object=("file", agent_dir),
                        zone_id=zone_id,
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
                        zone_id=zone_id,
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
        context: dict | Any | None,
    ) -> str:
        """Create API key for agent and return the raw key."""
        from nexus.server.auth.database_key import DatabaseAPIKeyAuth

        zone_id = self._extract_zone_id(context)
        session = self.SessionLocal()

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
                zone_id=zone_id,
                expires_at=expires_at,
            )
            session.commit()
            return raw_key
        finally:
            session.close()

    def _ensure_agent_registry(self) -> None:
        """Lazily create AgentRegistry if not already set.

        The registry is normally injected by the FastAPI server lifespan,
        but for tests or standalone usage we auto-create it from the
        available SessionLocal and EntityRegistry.
        """
        if self._agent_registry is not None:
            return

        if self.SessionLocal is None:
            raise RuntimeError(
                "AgentRegistry not initialized and no SessionLocal available "
                "to create one. Provide a record_store when constructing NexusFS."
            )

        from nexus.core.agent_registry import AgentRegistry

        self._agent_registry = AgentRegistry(
            session_factory=self.SessionLocal,
            entity_registry=self._entity_registry,
        )

    @rpc_expose(description="Register an AI agent")
    def register_agent(
        self,
        agent_id: str,
        name: str,
        description: str | None = None,
        generate_api_key: bool = False,
        metadata: dict | None = None,  # v0.5.1: Optional metadata (platform, endpoint_url, etc.)
        capabilities: list[str] | None = None,  # Issue #1210: Agent capabilities for discovery
        context: dict | None = None,
    ) -> dict:
        """Register an AI agent (v0.5.0).

        Agents are persistent identities owned by users. They do NOT have session_id
        or expiry - they live forever until explicitly deleted.

        Agents operate with zero permissions by default (principle of least privilege).
        Permissions must be explicitly granted via ReBAC (rebac_create).

        Args:
            agent_id: Unique agent identifier
            name: Human-readable name
            description: Optional description
            generate_api_key: If True, create API key for agent (not recommended)
            metadata: Optional metadata dict (platform, endpoint_url, agent_id, etc.)
                     Stored in agent's config.yaml for agent configuration
            capabilities: Optional list of capabilities for discovery (e.g. ["search", "analyze"])
            context: Operation context (user_id extracted from here)

        Returns:
            Agent info dict with agent_id, user_id, name, etc.

        Example:
            >>> # Recommended: No API key (uses user's auth + X-Agent-ID)
            >>> agent = nx.register_agent("data_analyst", "Data Analyst")
            >>> # Agent uses owner's credentials + X-Agent-ID header
            >>>
            >>> # With API key (zero permissions by default)
            >>> agent = nx.register_agent("secure_agent", "Secure Agent",
            ...                          generate_api_key=True)
            >>> # Agent starts with 0 permissions, needs explicit ReBAC grants
        """
        import logging

        logger = logging.getLogger(__name__)

        # Extract user_id and zone_id from context
        user_id = self._extract_user_id(context)
        if not user_id:
            raise ValueError("user_id required in context to register agent")

        zone_id = self._extract_zone_id(context) or "default"

        # Derive agent namespace paths
        agent_name_part = agent_id.split(",", 1)[1] if "," in agent_id else agent_id
        agent_dir = f"/zone/{zone_id}/user/{user_id}/agent/{agent_name_part}"

        # Pre-flight: ensure agent does not already exist
        self._check_agent_not_exists(agent_id, user_id, zone_id)

        # Lazily initialise AgentRegistry if not injected (e.g. in tests)
        self._ensure_agent_registry()

        # --- Single registration path (Issue #1588) ---
        # AgentRegistry.register() handles BOTH the DB write AND the EntityRegistry bridge.
        record = self._agent_registry.register(
            agent_id=agent_id,
            owner_id=user_id,
            zone_id=zone_id,
            name=name,
            metadata=metadata,
            capabilities=capabilities,
        )
        agent = record.to_dict()

        # Provision identity, wallet, config, permissions, identity doc, API key
        agent_did = self._provision_agent_identity(agent_id, agent, logger)
        self._provision_agent_wallet(agent_id, zone_id, logger)

        config_path = f"{agent_dir}/config.yaml"
        config_data = self._create_agent_config_data(
            agent_id=agent_id,
            name=name,
            user_id=user_id,
            description=description,
            created_at=agent.get("created_at"),
            metadata=metadata,
        )
        self._create_agent_directory(
            agent_id=agent_id,
            user_id=user_id,
            agent_dir=agent_dir,
            config_path=config_path,
            config_data=config_data,
            context=context,
        )
        agent["config_path"] = config_path

        self._grant_agent_self_permission(agent_id, agent_dir, zone_id, context, logger)

        if agent_did:
            self._write_agent_identity_document(agent_id, agent_did, agent_dir, context, logger)

        if generate_api_key:
            self._provision_agent_api_key(
                agent_id=agent_id,
                user_id=user_id,
                name=name,
                description=description,
                metadata=metadata,
                agent=agent,
                config_path=config_path,
                context=context,
                logger=logger,
            )
        else:
            agent["has_api_key"] = False

        if capabilities:
            agent["capabilities"] = list(capabilities)

        return agent

    # ------------------------------------------------------------------
    # register_agent helper methods (Issue #1588: extracted for clarity)
    # ------------------------------------------------------------------

    def _check_agent_not_exists(
        self,
        agent_id: str,
        user_id: str,
        zone_id: str,
    ) -> None:
        """Raise ValueError if agent config already exists on the filesystem."""
        agent_name_part = agent_id.split(",", 1)[1] if "," in agent_id else agent_id
        agent_dir = f"/zone/{zone_id}/user/{user_id}/agent/{agent_name_part}"
        config_path = f"{agent_dir}/config.yaml"
        try:
            existing_meta = self.metadata.get(config_path)
            if existing_meta:
                raise ValueError(
                    f"Agent already exists at {config_path}. "
                    f"Cannot re-register existing agent. Delete the agent first if you want to recreate it."
                )
        except FileNotFoundError:
            pass  # Config doesn't exist — expected for new agents

    def _provision_agent_identity(
        self,
        agent_id: str,
        agent: dict,
        logger: logging.Logger,
    ) -> str | None:
        """Provision Ed25519 keypair + DID for the agent (Issue #1355).

        Returns the agent DID string if successful, None otherwise.
        Mutates *agent* dict in-place to add ``did`` and ``key_id`` keys.
        """
        if not (hasattr(self, "_key_service") and self._key_service):
            return None
        try:
            key_record = self._key_service.ensure_keypair(agent_id)
            agent_did = key_record.did
            agent["did"] = agent_did
            agent["key_id"] = key_record.key_id
            logger.info(
                "[KYA] Provisioned identity for agent %s (did=%s)",
                agent_id,
                agent_did,
            )
            return agent_did
        except Exception as kya_err:
            logger.warning(
                "[KYA] Failed to provision identity for agent %s: %s",
                agent_id,
                kya_err,
            )
            return None

    def _provision_agent_wallet(
        self,
        agent_id: str,
        zone_id: str,
        logger: logging.Logger,
    ) -> None:
        """Auto-provision a TigerBeetle wallet for the agent (Issue #1210)."""
        if self._wallet_provisioner is None:
            return
        try:
            self._wallet_provisioner(agent_id, zone_id)
            logger.info(f"[WALLET] Provisioned wallet for agent {agent_id}")
        except Exception as wallet_err:
            logger.warning(
                f"[WALLET] Failed to provision wallet for agent {agent_id}: {wallet_err}"
            )

    def _grant_agent_self_permission(
        self,
        agent_id: str,
        agent_dir: str,
        zone_id: str,
        context: dict | None,
        logger: logging.Logger,
    ) -> None:
        """Grant the agent viewer permission on its own config directory."""
        try:
            self.rebac_create(
                subject=("agent", agent_id),
                relation="viewer",
                object=("file", agent_dir),
                zone_id=zone_id,
                context=context,
            )
            logger.info(f"Granted viewer permission to agent {agent_id} on {agent_dir}")
        except Exception as e:
            logger.warning(f"Failed to grant viewer permission to agent: {e}")

    def _write_agent_identity_document(
        self,
        agent_id: str,
        agent_did: str,
        agent_dir: str,
        context: dict | None,
        logger: logging.Logger,
    ) -> None:
        """Write public DID document to the agent's .identity namespace (Issue #1355)."""
        try:
            from nexus.identity.did import create_did_document

            key_record = self._key_service.get_active_keys(agent_id)[0]
            public_key = self._key_service._crypto.public_key_from_bytes(
                key_record.public_key_bytes
            )
            did_doc = create_did_document(agent_did, public_key)
            identity_dir = f"{agent_dir}/.identity"
            ctx = self._parse_context(context)
            self.mkdir(identity_dir, parents=True, exist_ok=True, context=ctx)
            self.write(
                f"{identity_dir}/did.json",
                json.dumps(did_doc, indent=2),
                context=ctx,
            )
            logger.info("[KYA] Wrote DID document to %s/did.json", identity_dir)
        except Exception as did_err:
            logger.warning("[KYA] Failed to write DID document: %s", did_err)

    def _provision_agent_api_key(
        self,
        agent_id: str,
        user_id: str,
        name: str,
        description: str | None,
        metadata: dict | None,
        agent: dict,
        config_path: str,
        context: dict | None,
        logger: logging.Logger,
    ) -> None:
        """Generate an API key for the agent and update its config.yaml."""
        try:
            raw_key = self._create_agent_api_key(
                agent_id=agent_id,
                user_id=user_id,
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
                )
                self._write_agent_config(config_path, updated_config_data, context)
            except Exception as e:
                logger.warning(f"Failed to update config with API key: {e}")
        except Exception as e:
            logger.error(f"Failed to create API key for agent: {e}")
            raise

    @rpc_expose(description="Update agent configuration")
    def update_agent(
        self,
        agent_id: str,
        name: str | None = None,
        description: str | None = None,
        metadata: dict | None = None,
        context: dict | None = None,
    ) -> dict:
        """Update an existing agent's configuration (v0.5.1).

        Updates the agent's config.yaml file and optionally updates entity registry metadata.
        Does NOT regenerate API keys or change permissions.

        Args:
            agent_id: Agent identifier to update
            name: Optional new name
            description: Optional new description
            metadata: Optional metadata to update (platform, endpoint_url, agent_id, etc.)
            context: Operation context (user_id extracted from here)

        Returns:
            Updated agent info dict

        Example:
            >>> # Update agent metadata
            >>> agent = nx.update_agent(
            ...     "alice,DataAnalyst",
            ...     name="Data Analyst Pro",
            ...     description="Enhanced data analysis agent",
            ...     metadata={
            ...         "platform": "langgraph",
            ...         "endpoint_url": "https://agent.example.com",
            ...         "agent_id": "analyst"
            ...     }
            ... )
        """
        import logging

        import yaml

        logger = logging.getLogger(__name__)

        # Extract user_id and zone_id from context
        user_id = self._extract_user_id(context)
        if not user_id:
            raise ValueError("user_id required in context to update agent")

        zone_id = self._extract_zone_id(context) or "default"

        # Extract agent name from agent_id (format: user_id,agent_name)
        agent_name_part = agent_id.split(",", 1)[1] if "," in agent_id else agent_id
        agent_dir = f"/zone/{zone_id}/user/{user_id}/agent/{agent_name_part}"
        config_path = f"{agent_dir}/config.yaml"

        # Check if agent config exists
        try:
            existing_meta = self.metadata.get(config_path)
            if not existing_meta:
                raise ValueError(f"Agent not found at {config_path}")
        except FileNotFoundError as e:
            raise ValueError(f"Agent not found: {agent_id}") from e

        # Read existing config
        ctx = self._parse_context(context)
        existing_content = self.read(config_path, context=ctx)
        if isinstance(existing_content, dict):
            existing_config = existing_content
        else:
            existing_config = yaml.safe_load(existing_content.decode("utf-8"))

        # Update fields
        if name is not None:
            existing_config["name"] = name
        if description is not None:
            existing_config["description"] = description

        # Update metadata section
        if metadata is not None:
            if "metadata" not in existing_config:
                existing_config["metadata"] = {}
            existing_config["metadata"].update(metadata)

        # Write updated config back
        updated_yaml = yaml.dump(existing_config, default_flow_style=False, sort_keys=False)
        self.write(config_path, updated_yaml.encode("utf-8"), context=ctx)

        # Optionally update entity registry if name/description changed
        if self._entity_registry and (name is not None or description is not None):
            entity = self._entity_registry.get_entity("agent", agent_id)
            if entity and entity.entity_metadata:
                import json

                try:
                    entity_meta = json.loads(entity.entity_metadata)
                    if name is not None:
                        entity_meta["name"] = name
                    if description is not None:
                        entity_meta["description"] = description

                    # Update entity metadata
                    from sqlalchemy import update

                    from nexus.storage.models import EntityRegistryModel

                    with self._entity_registry._get_session() as session:
                        stmt = (
                            update(EntityRegistryModel)
                            .where(
                                EntityRegistryModel.entity_type == "agent",
                                EntityRegistryModel.entity_id == agent_id,
                            )
                            .values(entity_metadata=json.dumps(entity_meta))
                        )
                        session.execute(stmt)
                        session.commit()
                        logger.info(f"Updated entity registry metadata for agent {agent_id}")
                except Exception as e:
                    logger.warning(f"Failed to update entity registry: {e}")

        return {
            "agent_id": agent_id,
            "user_id": user_id,
            "name": existing_config.get("name"),
            "description": existing_config.get("description"),
            "metadata": existing_config.get("metadata", {}),
            "config_path": config_path,
        }

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
            from nexus.services.permissions.entity_registry import EntityRegistry

            self._entity_registry = EntityRegistry(self.SessionLocal)

        entities = self._entity_registry.get_entities_by_type("agent")
        result = []

        # Query API keys for all agents in one go for efficiency
        from sqlalchemy import select

        from nexus.storage.models import APIKeyModel

        session = self.SessionLocal()
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
                        # Try to read from config.yaml (use default zone for now)
                        config_path = f"/zone/default/user/{user_id}/agent/{agent_name}/config.yaml"
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
            from nexus.services.permissions.entity_registry import EntityRegistry

            self._entity_registry = EntityRegistry(self.SessionLocal)

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

        session = self.SessionLocal()
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
                        # Get zone_id from context
                        ctx = self._parse_context(_context)
                        zone_id = self._extract_zone_id(_context) or "default"
                        config_path = (
                            f"/zone/{zone_id}/user/{user_id}/agent/{agent_name}/config.yaml"
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
                        zone_id = self._extract_zone_id(_context) or "default"
                        config_path = (
                            f"/zone/{zone_id}/user/{user_id}/agent/{agent_name}/config.yaml"
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
        import logging

        logger = logging.getLogger(__name__)

        try:
            # Agent ID format: user_id,agent_name
            if "," in agent_id:
                user_id, agent_name_part = agent_id.split(",", 1)
                # Get zone_id from context or use default
                zone_id = self._extract_zone_id(_context) or "default"
                # Use new namespace convention: /zone/{zone_id}/user/{user_id}/agent/{agent_id}
                agent_dir = f"/zone/{zone_id}/user/{user_id}/agent/{agent_name_part}"

                # Delete agent directory and config
                try:
                    ctx = self._parse_context(_context)
                    if self.exists(agent_dir, context=ctx):
                        # Use admin override for cleanup during agent deletion
                        self.rmdir(agent_dir, recursive=True, context=ctx, is_admin=True)
                except Exception as e:
                    logger.warning(f"Failed to delete agent directory {agent_dir}: {e}")

                # Delete ALL API keys associated with this agent
                session = self.SessionLocal()
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
                        logger.info(f"Revoked {rowcount} API key(s) for agent {agent_id}")
                except Exception as e:
                    logger.warning(f"Failed to revoke API keys for agent {agent_id}: {e}")
                    session.rollback()
                finally:
                    session.close()

                # Delete ALL ReBAC permissions for this agent
                if self._rebac_manager:
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
            logger.warning(f"Failed to cleanup agent resources: {e}")

        # Issue #1210: Wallet cleanup warning on agent deletion
        if self._wallet_provisioner is not None:
            zone_id_for_wallet = self._extract_zone_id(_context) or "default"
            try:
                # Check if wallet provisioner supports cleanup (duck-typed)
                cleanup_fn = getattr(self._wallet_provisioner, "cleanup", None)
                if cleanup_fn is not None:
                    cleanup_fn(agent_id, zone_id_for_wallet)
                    logger.info(f"[WALLET] Cleaned up wallet for agent {agent_id}")
                else:
                    logger.debug(
                        f"[WALLET] No cleanup handler for agent {agent_id} wallet "
                        f"(TigerBeetle accounts are immutable)"
                    )
            except Exception as wallet_err:
                logger.warning(
                    f"[WALLET] Failed to cleanup wallet for agent {agent_id}: {wallet_err}"
                )

        # Lazily initialise AgentRegistry if not injected (e.g. in tests)
        self._ensure_agent_registry()

        # Single unregister path (Issue #1588): AgentRegistry.unregister() handles
        # BOTH the agent_records DB delete AND the EntityRegistry bridge delete.
        deleted = self._agent_registry.unregister(agent_id)

        return deleted

    # ===== Agent Lifecycle API (Issue #1240) =====

    @rpc_expose(description="Transition agent lifecycle state")
    def agent_transition(
        self,
        agent_id: str,
        target_state: str,
        expected_generation: int | None = None,
        context: dict | None = None,
    ) -> dict:
        """Transition an agent's lifecycle state with optimistic locking.

        Args:
            agent_id: Agent identifier
            target_state: Target state ("CONNECTED", "IDLE", "SUSPENDED")
            expected_generation: Expected generation for optimistic locking
            context: Operation context

        Returns:
            Dict with agent_id, state, generation

        Raises:
            ValueError: If AgentRegistry not available or invalid state
            InvalidTransitionError: If transition is not allowed
            StaleAgentError: If expected_generation doesn't match
        """
        if not hasattr(self, "_agent_registry") or not self._agent_registry:
            raise ValueError("AgentRegistry not available")

        from nexus.core.agent_record import AgentState

        try:
            target = AgentState(target_state)
        except ValueError as err:
            raise ValueError(
                f"Invalid target state '{target_state}'. Valid states: CONNECTED, IDLE, SUSPENDED"
            ) from err

        record = self._agent_registry.transition(
            agent_id=agent_id,
            target_state=target,
            expected_generation=expected_generation,
        )
        return {
            "agent_id": record.agent_id,
            "state": record.state.value,
            "generation": record.generation,
        }

    @rpc_expose(description="Record agent heartbeat")
    def agent_heartbeat(
        self,
        agent_id: str,
        context: dict | None = None,
    ) -> dict:
        """Record a heartbeat for an active agent.

        Args:
            agent_id: Agent identifier
            context: Operation context

        Returns:
            Dict with ok=True
        """
        if not hasattr(self, "_agent_registry") or not self._agent_registry:
            raise ValueError("AgentRegistry not available")

        self._agent_registry.heartbeat(agent_id)
        return {"ok": True}

    @rpc_expose(description="List agents in a zone")
    def agent_list_by_zone(
        self,
        zone_id: str,
        state: str | None = None,
        context: dict | None = None,
    ) -> list[dict]:
        """List agents in a zone, optionally filtered by state.

        Args:
            zone_id: Zone identifier
            state: Optional state filter ("UNKNOWN", "CONNECTED", "IDLE", "SUSPENDED")
            context: Operation context

        Returns:
            List of agent record dicts
        """
        if not hasattr(self, "_agent_registry") or not self._agent_registry:
            raise ValueError("AgentRegistry not available")

        state_enum = None
        if state:
            from nexus.core.agent_record import AgentState

            try:
                state_enum = AgentState(state)
            except ValueError as err:
                raise ValueError(f"Invalid state filter '{state}'") from err

        records = self._agent_registry.list_by_zone(zone_id, state=state_enum)
        return [
            {
                "agent_id": r.agent_id,
                "owner_id": r.owner_id,
                "zone_id": r.zone_id,
                "name": r.name,
                "state": r.state.value,
                "generation": r.generation,
                "last_heartbeat": r.last_heartbeat.isoformat() if r.last_heartbeat else None,
                "created_at": r.created_at.isoformat(),
                "updated_at": r.updated_at.isoformat(),
            }
            for r in records
        ]

    # ===== User Provisioning API (Issue #820) =====

    @rpc_expose(description="Provision a new user account with all resources")
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
        """Provision a new user with all default resources (Issue #820).

        Creates:
        - User record (UserModel) in database
        - Zone record (ZoneModel) if it doesn't exist
        - All user directories under /zone/{zone_id}/user/{user_id}/
        - Default workspace
        - Default agents (ImpersonatedUser, UntrustedAgent)
        - Default skills (all from data/skills/)
        - API key (if create_api_key=True)
        - ReBAC permissions (user as zone owner)
        - Entity registry entries

        Args:
            user_id: Unique user identifier
            email: User email address
            display_name: Optional display name
            zone_id: Zone ID (extracted from email if not provided)
            zone_name: Optional custom zone name (default: "{zone_id} Organization")
            create_api_key: Whether to create API key for user
            api_key_name: Optional custom name for API key (default: "Primary key for {email}")
            api_key_expires_at: Optional expiry datetime for API key (default: None = no expiry)
            create_agents: Whether to create default agents
            import_skills: Whether to import default skills
            context: Operation context

        Returns:
            {
                "user_id": str,
                "zone_id": str,
                "api_key": str | None,
                "key_id": str | None,
                "workspace_path": str,
                "agent_paths": list[str],
                "skill_paths": list[str],
            }

        Example:
            >>> result = nx.provision_user(
            ...     user_id="alice",
            ...     email="alice@example.com",
            ...     display_name="Alice Smith"
            ... )
            >>> print(result["workspace_path"])
            /zone/alice/user/alice/workspace/ws_personal_abc123
        """
        import logging
        from datetime import UTC, datetime

        logger = logging.getLogger(__name__)

        # Input validation
        if not user_id:
            raise ValueError("user_id is required")
        if not email or "@" not in email:
            raise ValueError("Valid email required")

        # Extract zone_id from email if not provided
        if not zone_id:
            zone_id = email.split("@")[0]
            if not zone_id:
                raise ValueError("Could not extract zone_id from email")

        logger.info(f"Provisioning user {user_id} (email={email}, zone={zone_id})")

        # Use admin context for provisioning
        admin_context = context or OperationContext(
            user=user_id,
            groups=[],
            zone_id=zone_id,
            is_admin=True,
        )

        # Track created resources
        created_resources: dict[str, Any] = {
            "user": False,
            "zone": False,
            "directories": [],
            "workspace": None,
            "agents": [],
            "skills": [],
        }

        # Initialize entity registry
        if not self._entity_registry:
            from nexus.services.permissions.entity_registry import EntityRegistry

            self._entity_registry = EntityRegistry(self.SessionLocal)

        session = self.SessionLocal()
        api_key = None
        key_id = None

        try:
            # 1. Create/update ZoneModel (idempotent)
            from nexus.storage.models import UserModel, ZoneModel

            zone = session.query(ZoneModel).filter_by(zone_id=zone_id).first()
            if not zone:
                zone = ZoneModel(
                    zone_id=zone_id,
                    name=zone_name or f"{zone_id} Organization",
                    is_active=1,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
                session.add(zone)
                session.commit()
                logger.info(f"Created zone: {zone_id}")
                created_resources["zone"] = True
            else:
                logger.debug(f"Zone already exists: {zone_id}")

            # 2. Register zone in entity registry (idempotent)
            if not self._entity_registry.get_entity("zone", zone_id):
                self._entity_registry.register_entity("zone", zone_id)
                logger.info(f"Registered zone in entity registry: {zone_id}")

            # 3. Create/update UserModel (idempotent)
            user = session.query(UserModel).filter_by(user_id=user_id).first()
            if user:
                logger.debug(f"User already exists: {user_id}")
                # Reactivate if soft-deleted
                if not user.is_active:
                    user.is_active = 1
                    user.deleted_at = None
                    session.commit()
                    logger.info(f"Reactivated soft-deleted user: {user_id}")
            else:
                user = UserModel(
                    user_id=user_id,
                    email=email,
                    username=user_id,
                    display_name=display_name or user_id,
                    zone_id=zone_id,
                    primary_auth_method="api_key",
                    is_active=1,
                    is_global_admin=0,
                    email_verified=1,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
                session.add(user)
                session.commit()
                logger.info(f"Created user: {user_id}")
                created_resources["user"] = True

            # 4. Register user in entity registry (idempotent)
            if not self._entity_registry.get_entity("user", user_id):
                self._entity_registry.register_entity(
                    "user", user_id, parent_type="zone", parent_id=zone_id
                )
                logger.info(f"Registered user in entity registry: {user_id}")

            admin_context.user_id = user_id
            # 5. Create API key (if requested and doesn't exist)
            if create_api_key:
                from sqlalchemy import select

                from nexus.server.auth.database_key import DatabaseAPIKeyAuth
                from nexus.storage.models import APIKeyModel

                # Lock the user row to prevent race conditions during concurrent provisioning
                # This ensures only one thread can check and create API keys at a time
                user_row = session.execute(
                    select(UserModel).where(UserModel.user_id == user_id).with_for_update()
                ).scalar_one_or_none()

                if not user_row:
                    raise ValueError(f"User not found: {user_id}")

                # Check if user already has an API key
                existing_key_stmt = (
                    select(APIKeyModel)
                    .where(
                        APIKeyModel.user_id == user_id,
                        APIKeyModel.subject_type == "user",
                        APIKeyModel.revoked == 0,
                    )
                    .limit(1)
                )
                existing_key = session.scalar(existing_key_stmt)

                if not existing_key:
                    # Use custom key name if provided, otherwise default
                    key_name = api_key_name or f"Primary key for {email}"

                    key_id, api_key = DatabaseAPIKeyAuth.create_key(
                        session,
                        user_id=user_id,
                        name=key_name,
                        zone_id=zone_id,
                        is_admin=False,
                        expires_at=api_key_expires_at,  # Use provided expiry or None
                    )
                    session.commit()
                    logger.info(f"Created API key for user: {user_id}")
                else:
                    logger.debug(f"User already has an API key: {user_id}")

        except Exception as e:
            logger.error(f"Database operation failed during provisioning: {e}")
            session.rollback()
            raise
        finally:
            session.close()

        # 6. Create user directories
        try:
            dir_paths = self._create_user_directories(user_id, zone_id, admin_context)
            created_resources["directories"] = dir_paths
            logger.info(f"Created {len(dir_paths)} directories for user {user_id}")
        except Exception as e:
            logger.error(f"Failed to create user directories: {e}")
            # Continue - directories might already exist

        # 7. Create default workspace
        workspace_path = None
        try:
            import uuid

            # Generate workspace ID: ws_personal_{12-char-uuid}
            uuid_suffix = str(uuid.uuid4()).replace("-", "")[:12]
            workspace_id = f"ws_personal_{uuid_suffix}"
            workspace_path = f"/zone/{zone_id}/user/{user_id}/workspace/{workspace_id}"

            if not self.exists(workspace_path, context=admin_context):
                self.mkdir(workspace_path, parents=True, exist_ok=True, context=admin_context)
                self.register_workspace(
                    workspace_path,
                    name="Personal Workspace",
                    description="Default personal workspace",
                    context=admin_context,
                )
                logger.info(f"Created workspace: {workspace_path}")
                created_resources["workspace"] = workspace_path
            else:
                logger.debug(f"Workspace already exists: {workspace_path}")
                created_resources["workspace"] = workspace_path
        except Exception as e:
            logger.error(f"Failed to create workspace: {e}")

        # 8. Create agents (if requested)
        agent_paths = []
        if create_agents:
            try:
                from nexus.core.agent_provisioning import create_standard_agents

                agent_results = create_standard_agents(self, user_id, admin_context)

                for agent_name, agent_result in agent_results.items():
                    if agent_result and "config_path" in agent_result:
                        agent_paths.append(agent_result["config_path"])
                        created_resources["agents"].append(agent_name)

                logger.info(f"Created {len(agent_paths)} agents for user {user_id}")
            except Exception as e:
                logger.error(f"Failed to create agents: {e}")

        # 9. Import skills (if requested) - ASYNC for fast registration
        skill_paths: list[str] = []
        if import_skills:
            # Launch skill import as background thread to avoid blocking registration
            # Skills will appear in user's workspace as they complete
            def _import_skills_async() -> None:
                try:
                    logger.info(f"[ASYNC] Starting background skill import for user {user_id}")
                    imported_paths = self._import_user_skills(zone_id, user_id, admin_context)
                    logger.info(
                        f"[ASYNC] Background skill import completed for {user_id}: "
                        f"{len(imported_paths)} skills imported"
                    )

                    # Grant SkillBuilder permissions after skills are imported
                    if create_agents:
                        try:
                            from nexus.core.agent_provisioning import (
                                grant_skill_builder_permissions,
                            )

                            granted = grant_skill_builder_permissions(self, user_id, zone_id)
                            logger.info(
                                f"[ASYNC] Granted {granted} permissions to SkillBuilder agent for user {user_id}"
                            )
                        except Exception as e:
                            logger.error(f"[ASYNC] Failed to grant SkillBuilder permissions: {e}")
                except Exception as e:
                    logger.error(f"[ASYNC] Failed to import skills in background: {e}")

            # Start background import thread
            import threading

            skill_import_thread = threading.Thread(
                target=_import_skills_async,
                name=f"skill-import-{user_id[:8]}",
                daemon=True,  # Don't block process exit
            )
            skill_import_thread.start()
            logger.info(f"Skill import started in background for user {user_id}")
            created_resources["skills"] = "importing"  # Placeholder to indicate async import

        # 10. Grant ReBAC permissions (zone owner)
        try:
            self.rebac_create(
                subject=("user", user_id),
                relation="member",
                object=("group", f"zone_owners:{zone_id}"),
                zone_id=zone_id,
                context=admin_context,
            )
            logger.info(f"Granted zone owner permissions to user {user_id}")
        except Exception as e:
            if "already exists" in str(e).lower():
                logger.debug(f"User already has zone owner permissions: {user_id}")
            else:
                logger.warning(f"Failed to grant zone owner permissions: {e}")

        logger.info(f"Successfully provisioned user {user_id}")

        return {
            "user_id": user_id,
            "zone_id": zone_id,
            "api_key": api_key,
            "key_id": key_id,
            "workspace_path": workspace_path,
            "agent_paths": agent_paths,
            "skill_paths": skill_paths,
            "created_resources": created_resources,  # For debugging
        }

    @rpc_expose(description="Deprovision a user and remove all their resources")
    def deprovision_user(
        self,
        user_id: str,
        zone_id: str | None = None,
        delete_user_record: bool = False,
        force: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Deprovision a user and remove all their resources.

        Removes:
        - All user directories (workspace, memory, skill, agent, connector, resource)
        - All API keys for the user
        - All OAuth-specific records (OAuth API keys, OAuth account linkages)
        - All ReBAC permissions where user is subject
        - Entity registry entries for user and their agents
        - Optionally: UserModel record (soft delete)

        Safety checks:
        - Prevents deprovisioning global admin users (unless force=True)
        - Idempotent: safe to call multiple times
        - Handles missing resources gracefully

        Args:
            user_id: User ID to deprovision
            zone_id: Zone ID (looked up from user if not provided)
            delete_user_record: If True, soft-deletes UserModel record
            force: Bypass safety checks (e.g., allow deprovisioning admin users)
            context: Operation context

        Returns:
            {
                "user_id": str,
                "zone_id": str,
                "deleted_directories": list[str],
                "deleted_api_keys": int,
                "deleted_oauth_api_keys": int,
                "deleted_oauth_accounts": int,
                "deleted_permissions": int,
                "deleted_entities": int,
                "user_record_deleted": bool,
            }

        Example:
            >>> result = nx.deprovision_user(
            ...     user_id="alice",
            ...     zone_id="example",
            ...     delete_user_record=True
            ... )
            >>> print(result["deleted_directories"])
            ['/zone/example/user/alice/workspace', ...]
        """
        import logging
        from datetime import UTC, datetime

        logger = logging.getLogger(__name__)

        # Input validation
        if not user_id:
            raise ValueError("user_id is required")

        logger.info(f"Deprovisioning user {user_id}")

        # Use admin context for deprovisioning
        admin_context = context or OperationContext(
            user="system",
            groups=[],
            zone_id=zone_id or "system",
            is_admin=True,
        )

        # Track deleted resources
        result: dict[str, Any] = {
            "user_id": user_id,
            "zone_id": None,
            "deleted_directories": [],
            "deleted_api_keys": 0,
            "deleted_oauth_api_keys": 0,
            "deleted_oauth_accounts": 0,
            "deleted_permissions": 0,
            "deleted_entities": 0,
            "user_record_deleted": False,
        }

        # Look up user in database
        session = self.SessionLocal()
        try:
            from nexus.storage.models import UserModel

            user = session.query(UserModel).filter_by(user_id=user_id).first()

            if not user:
                logger.warning(f"User not found in database: {user_id}")
                # Continue with cleanup even if user doesn't exist
            else:
                # Get zone_id from user if not provided
                if not zone_id:
                    zone_id = user.zone_id
                result["zone_id"] = zone_id

                # Safety check: prevent deprovisioning global admin
                if user.is_global_admin and not force:
                    raise ValueError(
                        f"Cannot deprovision global admin user {user_id}. "
                        "Use force=True to override."
                    )

                logger.info(
                    f"Found user {user_id} (email={user.email}, zone={zone_id}, "
                    f"is_admin={user.is_global_admin})"
                )

            # Update context with proper zone_id
            if zone_id:
                admin_context = OperationContext(
                    user="system",
                    groups=[],
                    zone_id=zone_id,
                    is_admin=True,
                )

            # 1. Delete user directories
            if zone_id:
                user_base_path = f"/zone/{zone_id}/user/{user_id}"
                logger.info(f"Deleting user directories under {user_base_path}")

                ALL_RESOURCE_TYPES = [
                    "workspace",
                    "memory",
                    "skill",
                    "agent",
                    "connector",
                    "resource",
                ]

                for resource_type in ALL_RESOURCE_TYPES:
                    dir_path = f"{user_base_path}/{resource_type}"
                    try:
                        # Try to delete even if exists() returns False
                        # (subdirectories may exist even if parent has no metadata)
                        was_deleted = self._delete_directory_recursive(dir_path, admin_context)
                        if was_deleted:
                            result["deleted_directories"].append(dir_path)
                            logger.info(f"Deleted directory: {dir_path}")
                    except Exception as e:
                        logger.warning(f"Failed to delete directory {dir_path}: {e}")

            # 2. Delete API keys (both user and agent keys)
            try:
                from nexus.storage.models import APIKeyModel

                # Delete ALL API keys for this user (subject_type="user" and "agent")
                # Agent keys have subject_type="agent" and belong to user's agents
                deleted_keys = (
                    session.query(APIKeyModel)
                    .filter_by(user_id=user_id)  # Remove subject_type filter to delete all keys
                    .delete()
                )
                session.commit()
                result["deleted_api_keys"] = deleted_keys
                logger.info(f"Deleted {deleted_keys} API keys for user {user_id}")
            except Exception as e:
                logger.warning(f"Failed to delete API keys: {e}")
                session.rollback()

            # 3. Delete OAuth-specific records (for OAuth authenticated users)
            try:
                from sqlalchemy import inspect

                from nexus.storage.models import OAuthAPIKeyModel, UserOAuthAccountModel

                # Check if OAuth tables exist (they may not in test environments)
                has_oauth_tables = False
                if session.bind is not None:
                    inspector = inspect(session.bind)
                    table_names = inspector.get_table_names()
                    has_oauth_tables = (
                        "oauth_api_keys" in table_names and "user_oauth_accounts" in table_names
                    )

                if has_oauth_tables:
                    # Delete OAuth API keys (encrypted keys for OAuth users)
                    deleted_oauth_keys = (
                        session.query(OAuthAPIKeyModel).filter_by(user_id=user_id).delete()
                    )
                    result["deleted_oauth_api_keys"] = deleted_oauth_keys
                    logger.info(f"Deleted {deleted_oauth_keys} OAuth API keys for user {user_id}")

                    # Delete OAuth account linkages (Google, GitHub, etc.)
                    deleted_oauth_accounts = (
                        session.query(UserOAuthAccountModel).filter_by(user_id=user_id).delete()
                    )
                    session.commit()
                    result["deleted_oauth_accounts"] = deleted_oauth_accounts
                    logger.info(
                        f"Deleted {deleted_oauth_accounts} OAuth accounts for user {user_id}"
                    )
                else:
                    logger.debug("OAuth tables not present in database, skipping OAuth cleanup")
            except Exception as e:
                logger.warning(f"Failed to delete OAuth records: {e}")
                session.rollback()

            # 4. Delete ReBAC permissions
            try:
                # Query all permissions where user is subject
                if hasattr(self, "rebac_manager") and self.rebac_manager:
                    tuples = self.rebac_manager.query_tuples_by_subject(("user", user_id))
                    deleted_count = 0
                    for tuple_info in tuples:
                        tuple_id = tuple_info.get("tuple_id")
                        if tuple_id:
                            try:
                                self.rebac_delete(tuple_id)
                                deleted_count += 1
                            except Exception:
                                pass  # Continue with other tuples
                    result["deleted_permissions"] = deleted_count
                    logger.info(f"Deleted {deleted_count} ReBAC permissions for user {user_id}")
                else:
                    logger.debug("ReBAC manager not available")
            except Exception as e:
                logger.warning(f"Failed to delete ReBAC permissions: {e}")

            # 5. Delete entity registry entries
            try:
                if self._entity_registry:
                    # Delete user entity (cascade will delete children)
                    user_entity = self._entity_registry.get_entity("user", user_id)
                    if user_entity:
                        # Delete with cascade to remove all child entities (agents, etc.)
                        deleted = self._entity_registry.delete_entity("user", user_id, cascade=True)
                        if deleted:
                            result["deleted_entities"] = 1  # At least the user entity
                            logger.info(
                                f"Deleted user entity and children from registry: {user_id}"
                            )
                        else:
                            logger.warning(f"Failed to delete user entity: {user_id}")
                    else:
                        logger.debug(f"User not found in entity registry: {user_id}")
            except Exception as e:
                logger.warning(f"Failed to delete entity registry entries: {e}")

            # 6. Soft-delete user record (if requested)
            if delete_user_record and user:
                try:
                    user.is_active = 0
                    user.deleted_at = datetime.now(UTC)
                    session.commit()
                    result["user_record_deleted"] = True
                    logger.info(f"Soft-deleted user record: {user_id}")
                except Exception as e:
                    logger.warning(f"Failed to soft-delete user record: {e}")
                    session.rollback()

        except Exception as e:
            logger.error(f"Error during user deprovisioning: {e}")
            session.rollback()
            raise
        finally:
            session.close()

        logger.info(
            f"Successfully deprovisioned user {user_id}: "
            f"dirs={len(result['deleted_directories'])}, "
            f"keys={result['deleted_api_keys']}, "
            f"perms={result['deleted_permissions']}, "
            f"entities={result['deleted_entities']}"
        )

        return result

    def _delete_directory_recursive(self, dir_path: str, context: OperationContext) -> bool:
        """Recursively delete a directory and all its contents.

        Strategy:
        1. Try physical deletion first (for LocalBackend) - fastest and most reliable
        2. Fall back to virtual filesystem deletion if physical deletion fails

        Args:
            dir_path: Directory path to delete
            context: Operation context

        Returns:
            True if directory was deleted (or had content deleted), False otherwise
        """
        import logging
        import os
        import shutil

        logger = logging.getLogger(__name__)

        directory_removed = False
        had_content = False  # Track if directory had any content

        # Approach 1: Physical deletion (for LocalBackend) - Try first for efficiency
        if hasattr(self, "backend") and self.backend.has_root_path is True:
            try:
                # Convert virtual path to physical path
                # LocalBackend stores directories under "dirs" subdirectory
                physical_path = self.backend.root_path / "dirs" / dir_path.lstrip("/")
                if physical_path.exists() and physical_path.is_dir():
                    # Check if directory actually has content (not just an empty stub)
                    from contextlib import suppress

                    with suppress(OSError):
                        # Use os.listdir to check if directory has any files/subdirs
                        dir_contents = os.listdir(physical_path)
                        if dir_contents:
                            had_content = True  # Directory has actual content

                    try:
                        # shutil.rmtree() removes entire directory tree in one go
                        shutil.rmtree(physical_path)
                        directory_removed = True
                        logger.info(f"Deleted physical directory: {dir_path}")
                    except OSError as e:
                        logger.debug(f"shutil.rmtree failed for {physical_path}: {e}")
                        # Try os.rmdir() for empty directories
                        try:
                            os.rmdir(physical_path)
                            directory_removed = True
                            logger.info(f"Deleted empty physical directory: {dir_path}")
                        except OSError as e2:
                            logger.debug(f"os.rmdir failed for {physical_path}: {e2}")
            except Exception as e:
                logger.debug(f"Physical deletion failed for {dir_path}: {e}")

        # If physical deletion worked, still need to clean up metadata and permissions
        if directory_removed:
            # Clean up metadata for the directory and all children
            if hasattr(self, "metadata"):
                try:
                    session = self.SessionLocal()
                    try:
                        from nexus.storage.models import FilePathModel

                        # Delete file paths for directory and all children (paths starting with dir_path)
                        deleted_count = (
                            session.query(FilePathModel)
                            .filter(FilePathModel.virtual_path.like(f"{dir_path}%"))
                            .delete(synchronize_session=False)
                        )
                        session.commit()
                        logger.debug(f"Deleted {deleted_count} file path entries for {dir_path}")
                    finally:
                        session.close()
                except Exception as e:
                    logger.warning(f"Failed to clean up file paths for {dir_path}: {e}")

            # Clean up ReBAC permission tuples for directory and all children
            if hasattr(self, "rebac_manager") and self.rebac_manager:
                try:
                    # Query tuples where resource path starts with dir_path
                    from nexus.storage.models import ReBACTupleModel

                    session = self.SessionLocal()
                    try:
                        deleted_tuples = (
                            session.query(ReBACTupleModel)
                            .filter(
                                ReBACTupleModel.object_type == "file",
                                ReBACTupleModel.object_id.like(f"{dir_path}%"),
                            )
                            .delete(synchronize_session=False)
                        )
                        session.commit()
                        logger.debug(f"Deleted {deleted_tuples} ReBAC tuples for {dir_path}")
                    finally:
                        session.close()
                except Exception as e:
                    logger.warning(f"Failed to clean up ReBAC tuples for {dir_path}: {e}")

            # Invalidate all caches
            try:
                parent_path = "/".join(dir_path.rstrip("/").split("/")[:-1])
                if parent_path and hasattr(self, "_list_cache"):
                    self._list_cache.pop(parent_path, None)
                    # Also clear the deleted directory itself
                    self._list_cache.pop(dir_path, None)
                if hasattr(self, "_exists_cache"):
                    self._exists_cache.pop(dir_path, None)
                    # Clear cache for parent too
                    if parent_path:
                        self._exists_cache.pop(parent_path, None)
            except Exception:
                pass

            # Clear tiger cache entries for the directory and children
            if hasattr(self, "rebac_manager") and hasattr(self.rebac_manager, "_tiger_cache"):
                try:
                    tiger_cache = self.rebac_manager._tiger_cache
                    if hasattr(tiger_cache, "invalidate_all"):
                        tiger_cache.invalidate_all()
                        logger.debug("Invalidated tiger cache")
                except Exception as e:
                    logger.debug(f"Failed to invalidate tiger cache: {e}")

            return True  # Successfully deleted

        # Approach 2: Virtual filesystem deletion (fallback)
        # First check if directory actually exists before attempting deletion
        from contextlib import suppress

        directory_exists = False
        with suppress(Exception):
            directory_exists = self.exists(dir_path, context=context)

        if not directory_exists:
            # Directory doesn't exist, nothing to delete
            logger.debug(f"Directory does not exist: {dir_path}")
            return False

        try:
            # List immediate children
            result = self.list(dir_path, recursive=False, context=context)

            # Handle different return formats
            if isinstance(result, dict) and "files" in result:
                children = result["files"]
            elif isinstance(result, list):
                children = result
            else:
                children = []

            # Delete each child
            if children:
                had_content = True  # Directory has children

            for item in children:
                child_path: str | None = None
                is_dir = False

                if isinstance(item, str):
                    child_path = item
                    # Determine if directory by trying to list
                    try:
                        self.list(child_path, recursive=False, context=context)
                        is_dir = True
                    except Exception:
                        pass
                elif isinstance(item, dict):
                    child_path = item.get("path")
                    if not child_path:
                        continue
                    is_dir = item.get("type", "") == "directory"

                if not child_path or child_path == dir_path:
                    continue

                # Recursively delete subdirectory or delete file
                try:
                    if is_dir:
                        self._delete_directory_recursive(child_path, context)
                    else:
                        self.delete(child_path, context=context)
                except Exception as e:
                    logger.warning(f"Failed to delete {child_path}: {e}")

            # Try to remove the directory itself using virtual filesystem methods
            for method_name, method_func in [
                ("rmdir", lambda: self.rmdir(dir_path, context=context)),
                ("delete", lambda: self.delete(dir_path, context=context)),
            ]:
                try:
                    method_func()
                    directory_removed = True
                    logger.info(f"Deleted directory with {method_name}: {dir_path}")
                    break
                except Exception as e:
                    logger.debug(f"{method_name} failed for {dir_path}: {e}")

        except Exception as e:
            logger.error(f"Virtual filesystem deletion failed for {dir_path}: {e}")

        if not directory_removed:
            logger.warning(f"Could not remove directory {dir_path}")

        # Return True if we had content (even if deletion failed) or successfully removed
        return had_content or directory_removed

    def _create_user_directories(
        self, user_id: str, zone_id: str, context: OperationContext
    ) -> list[str]:
        """Create all user directories with proper permissions.

        Args:
            user_id: User ID
            zone_id: Zone ID
            context: Operation context

        Returns:
            List of created directory paths
        """
        import logging

        logger = logging.getLogger(__name__)

        ALL_RESOURCE_TYPES = ["workspace", "memory", "skill", "agent", "connector", "resource"]
        created_paths = []

        for resource_type in ALL_RESOURCE_TYPES:
            folder_path = f"/zone/{zone_id}/user/{user_id}/{resource_type}"

            try:
                # Create directory (idempotent)
                self.mkdir(folder_path, parents=True, exist_ok=True, context=context)

                # Grant user ownership
                try:
                    self.rebac_create(
                        subject=("user", user_id),
                        relation="direct_owner",
                        object=("file", folder_path),
                        zone_id=zone_id,
                        context=context,
                    )
                except Exception as e:
                    if "already exists" in str(e).lower():
                        logger.debug(f"Permission already exists for {folder_path}")
                    else:
                        raise

                created_paths.append(folder_path)
            except Exception as e:
                logger.warning(f"Failed to create directory {folder_path}: {e}")

        return created_paths

    def _import_user_skills(
        self, _zone_id: str, _user_id: str, context: OperationContext
    ) -> list[str]:
        """Import all default skills from data/skills/ directory.

        Args:
            _zone_id: Zone ID (unused, for future use)
            _user_id: User ID (unused, for future use)
            context: Operation context

        Returns:
            List of imported skill paths
        """
        import base64
        import logging
        import os
        from pathlib import Path

        logger = logging.getLogger(__name__)

        # Find skills directory
        possible_dirs = []

        # 1. Try NEXUS_DATA_DIR environment variable (for Docker/production)
        if os.environ.get("NEXUS_DATA_DIR"):
            data_dir = Path(os.environ["NEXUS_DATA_DIR"])
            possible_dirs.append(data_dir / "skills")

        # 2. Try backend data directory if available
        if self.backend.has_data_dir is True and self.backend.data_dir:
            backend_data_dir = Path(self.backend.data_dir)
            possible_dirs.append(backend_data_dir / "skills")

        # 3. Fall back to relative path from module location (for development)
        possible_dirs.append(Path(__file__).parent.parent.parent.parent / "data" / "skills")

        skills_dir = None
        for dir_path in possible_dirs:
            if dir_path.exists() and dir_path.is_dir():
                skills_dir = dir_path
                break

        if not skills_dir:
            logger.warning(
                f"Skills directory not found in any of: {[str(d) for d in possible_dirs]}"
            )
            return []

        skill_files = list(skills_dir.glob("*.skill"))
        skill_paths = []

        for skill_file in skill_files:
            try:
                with open(skill_file, "rb") as f:
                    zip_bytes = f.read()

                zip_base64 = base64.b64encode(zip_bytes).decode("utf-8")

                result = self.skills_import(
                    zip_data=zip_base64,
                    tier="personal",  # User's personal skills
                    allow_overwrite=False,  # Allow overwrite during provisioning
                    context=context,
                )

                skill_paths.extend(result.get("skill_paths", []))
                logger.debug(f"Imported skill: {skill_file.name}")
            except Exception as e:
                logger.warning(f"Failed to import skill {skill_file.name}: {e}")

        return skill_paths

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
        from nexus.services.ace.trajectory import TrajectoryManager

        session = self.SessionLocal()
        try:
            ctx = self._parse_context(context)
            traj_mgr = TrajectoryManager(
                session,
                self.backend,
                ctx.user or "system",
                ctx.agent_id or self._default_context.agent_id,
                ctx.zone_id or self._default_context.zone_id,
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
            scope: Scope level ('agent', 'user', 'zone', 'global')
            context: Operation context

        Returns:
            Dict with playbook_id
        """
        from nexus.services.ace.playbook import PlaybookManager

        session = self.SessionLocal()
        try:
            ctx = self._parse_context(context)
            playbook_mgr = PlaybookManager(
                session,
                self.backend,
                ctx.user or "system",
                ctx.agent_id or self._default_context.agent_id,
                ctx.zone_id or self._default_context.zone_id,
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
        from nexus.services.ace.playbook import PlaybookManager

        session = self.SessionLocal()
        try:
            ctx = self._parse_context(context)
            playbook_mgr = PlaybookManager(
                session,
                self.backend,
                ctx.user or "system",
                ctx.agent_id or self._default_context.agent_id,
                ctx.zone_id or self._default_context.zone_id,
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
        from nexus.services.ace.playbook import PlaybookManager

        session = self.SessionLocal()
        try:
            ctx = self._parse_context(context)
            playbook_mgr = PlaybookManager(
                session,
                self.backend,
                ctx.user or "system",
                ctx.agent_id or self._default_context.agent_id,
                ctx.zone_id or self._default_context.zone_id,
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

            from nexus.sandbox.sandbox_manager import SandboxManager

            # Initialize sandbox manager with E2B credentials and config for Docker provider
            # Pass config if available (needed for Docker provider initialization)
            config = getattr(self, "_config", None)
            self._sandbox_manager = SandboxManager(
                session_factory=self.SessionLocal,
                e2b_api_key=os.getenv("E2B_API_KEY"),
                e2b_team_id=os.getenv("E2B_TEAM_ID"),
                e2b_template_id=os.getenv("E2B_TEMPLATE_ID"),
                config=config,  # Pass config for Docker provider
            )

            # Attach smart router if providers are available (Issue #1317)
            if self._sandbox_manager.providers:
                from nexus.sandbox.sandbox_router import SandboxRouter

                self._sandbox_manager._router = SandboxRouter(
                    available_providers=self._sandbox_manager.providers,
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
        from nexus.core.sync_bridge import run_sync

        return run_sync(coro)

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
            context: Operation context with user/agent/zone info

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
            zone_id=ctx.zone_id or self._default_context.zone_id or "default",
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
        as_script: bool = False,
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
            as_script: If True, run as standalone script (stateless).
                      If False (default), use Jupyter kernel for Python (stateful).

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

        import dataclasses

        execution_result = await self._sandbox_manager.run_code(
            sandbox_id, language, code, timeout, as_script=as_script
        )
        result = dataclasses.asdict(execution_result)
        # Convert ValidationResult Pydantic models to dicts for serialization
        if result.get("validations"):
            result["validations"] = [
                v.model_dump() if hasattr(v, "model_dump") else v for v in result["validations"]
            ]
        return result

    @rpc_expose(description="Validate code in sandbox")
    async def sandbox_validate(  # type: ignore[override]
        self,
        sandbox_id: str,
        workspace_path: str = "/workspace",
        context: dict | None = None,  # noqa: ARG002
    ) -> dict:
        """Run validation pipeline in sandbox.

        Detects project type and runs applicable linters (ruff, mypy, eslint, etc.),
        returning structured validation results.

        Args:
            sandbox_id: Sandbox ID
            workspace_path: Workspace root path in sandbox
            context: Operation context

        Returns:
            Dict with validations list
        """
        self._ensure_sandbox_manager()
        assert self._sandbox_manager is not None

        results = await self._sandbox_manager.validate(sandbox_id, workspace_path)
        return {"validations": results}

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
        zone_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> dict:
        """List user's sandboxes.

        Args:
            context: Operation context
            verify_status: If True, verify status with provider (slower but accurate)
            user_id: Filter by user_id (admin only)
            zone_id: Filter by zone_id (admin only)
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
        filter_zone_id = zone_id if (zone_id is not None and ctx.is_admin) else ctx.zone_id
        filter_agent_id = agent_id if agent_id is not None else ctx.agent_id

        sandboxes = await self._sandbox_manager.list_sandboxes(
            user_id=filter_user_id,
            zone_id=filter_zone_id,
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
            context: Operation context with user/agent/zone info

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
            zone_id=ctx.zone_id or self._default_context.zone_id or "default",
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
                "NEXUS_URL", "http://localhost:2026"
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

    def clear_dir_visibility_cache(self) -> None:
        """Clear the directory visibility cache.

        Use this to force fresh visibility computations, for example
        after bulk permission changes or for testing purposes.
        """
        if hasattr(self, "_dir_visibility_cache") and self._dir_visibility_cache is not None:
            self._dir_visibility_cache.clear()

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

    # -------------------------------------------------------------------------
    # ReBACService Delegation Methods (12 core methods)
    # -------------------------------------------------------------------------

    async def arebac_create(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: Any = None,
        zone_id: str | None = None,
        context: Any = None,
        column_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a ReBAC relationship tuple - delegates to ReBACService.

        Async version of rebac_create() using the service layer.

        Args:
            subject: Subject tuple (type, id) e.g., ("user", "alice")
            relation: Relation name e.g., "owner", "can-read", "member"
            object: Object tuple (type, id) e.g., ("file", "/doc.txt")
            expires_at: Optional expiration datetime
            zone_id: Zone ID for multi-zone isolation
            context: Operation context for permission checks
            column_config: Optional column-level permissions for dynamic_viewer relation

        Returns:
            Tuple ID (UUID string)
        """
        return await self.rebac_service.rebac_create(
            subject=subject,
            relation=relation,
            object=object,
            expires_at=expires_at,
            zone_id=zone_id,
            context=context,
            column_config=column_config,
        )

    async def arebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: Any = None,
        zone_id: str | None = None,
    ) -> bool:
        """Check if subject has permission on object - delegates to ReBACService.

        Async version of rebac_check() using the service layer.

        Args:
            subject: Subject tuple e.g., ("user", "alice")
            permission: Permission to check e.g., "read", "write", "owner"
            object: Object tuple e.g., ("file", "/doc.txt")
            context: Optional ABAC context for condition evaluation
            zone_id: Zone ID for multi-zone isolation

        Returns:
            True if permission granted, False otherwise
        """
        return await self.rebac_service.rebac_check(
            subject=subject,
            permission=permission,
            object=object,
            context=context,
            zone_id=zone_id,
        )

    async def arebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
        limit: int = 100,
    ) -> list[tuple[str, str]]:
        """Find all subjects with permission on object - delegates to ReBACService.

        Async version of rebac_expand() using the service layer.

        Args:
            permission: Permission to check e.g., "read", "write", "owner"
            object: Object tuple e.g., ("file", "/doc.txt")
            zone_id: Zone ID for multi-zone isolation
            limit: Maximum results

        Returns:
            List of subject tuples with the permission
        """
        return await self.rebac_service.rebac_expand(
            permission=permission,
            object=object,
            _zone_id=zone_id,
            _limit=limit,
        )

    async def arebac_explain(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        """Explain why subject has/doesn't have permission - delegates to ReBACService.

        Async version of rebac_explain() using the service layer.

        Args:
            subject: Subject tuple e.g., ("user", "alice")
            permission: Permission to explain e.g., "read", "write"
            object: Object tuple e.g., ("file", "/doc.txt")
            zone_id: Zone ID for multi-zone isolation
            context: Operation context

        Returns:
            Explanation dictionary with result, reason, and paths
        """
        return await self.rebac_service.rebac_explain(
            subject=subject,
            permission=permission,
            object=object,
            zone_id=zone_id,
            context=context,
        )

    async def arebac_check_batch(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str | None = None,
    ) -> list[bool]:
        """Check multiple permissions in batch - delegates to ReBACService.

        Async version of rebac_check_batch() using the service layer.

        Args:
            checks: List of (subject, permission, object) tuples
            zone_id: Zone ID

        Returns:
            List of boolean results (same order as input)
        """
        return await self.rebac_service.rebac_check_batch(
            checks=checks,
            _zone_id=zone_id,
        )

    async def arebac_delete(self, tuple_id: str) -> bool:
        """Delete a relationship tuple by ID - delegates to ReBACService.

        Async version of rebac_delete() using the service layer.

        Args:
            tuple_id: UUID of tuple to delete

        Returns:
            True if deleted, False if not found
        """
        return await self.rebac_service.rebac_delete(tuple_id=tuple_id)

    async def arebac_list_tuples(
        self,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
        relation_in: list[str] | None = None,
        zone_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List relationship tuples with filters - delegates to ReBACService.

        Async version of rebac_list_tuples() using the service layer.

        Args:
            subject: Filter by subject (optional)
            relation: Filter by relation (optional)
            object: Filter by object (optional)
            relation_in: Filter by multiple relations (optional)
            zone_id: Zone ID for multi-zone isolation
            limit: Maximum results
            offset: Pagination offset

        Returns:
            List of tuple dictionaries
        """
        return await self.rebac_service.rebac_list_tuples(
            subject=subject,
            relation=relation,
            object=object,
            relation_in=relation_in,
            _zone_id=zone_id,
            _limit=limit,
            _offset=offset,
        )

    async def aget_namespace(self, object_type: str) -> dict[str, Any] | None:
        """Get namespace schema for object type - delegates to ReBACService.

        Async version of get_namespace() using the service layer.

        Args:
            object_type: Type of object (e.g., "file", "folder")

        Returns:
            Namespace configuration dict or None if not found
        """
        return await self.rebac_service.get_namespace(object_type=object_type)

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
            context: Operation context (OperationContext, EnhancedOperationContext, or dict)

        Returns:
            Subject tuple (type, id) or None if not found

        Examples:
            >>> context = {"subject": ("user", "alice")}
            >>> self._get_subject_from_context(context)
            ('user', 'alice')

            >>> context = OperationContext(user="alice", groups=[])
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
            subject_id = context.get("subject_id") or context.get("user")
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
            subject_id = getattr(context, "subject_id", None) or getattr(context, "user", None)
            if subject_id:
                return (subject_type, subject_id)

        # Last resort: use user field
        if hasattr(context, "user") and context.user:
            return ("user", context.user)

        return None

    def _check_share_permission(
        self,
        resource: tuple[str, str],
        context: Any,
        required_permission: str = "execute",
    ) -> None:
        """Check if caller has permission to share/manage a resource.

        This helper centralizes the permission check logic used by rebac_create,
        share_with_user, and share_with_group to prevent code duplication.

        Args:
            resource: Resource tuple (object_type, object_id)
            context: Operation context (OperationContext, EnhancedOperationContext, or dict)
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
                user=context.get("user", "unknown"),
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
                subject=self._get_subject_from_context(context) or ("user", op_context.user),
                permission="owner",  # Only owners can manage permissions
                object=resource,
                context=context,
            )
            if not has_permission:
                raise PermissionError(
                    f"Access denied: User '{op_context.user}' does not have owner "
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
                if zone_id and op_context.user:
                    from nexus.server.auth.user_helpers import is_zone_admin

                    if is_zone_admin(self._rebac_manager, op_context.user, zone_id):
                        # Zone admin can share resources in their zone
                        return

                # Neither owner nor zone admin - deny
                perm_name = required_permission.upper()
                raise PermissionError(
                    f"Access denied: User '{op_context.user}' does not have {perm_name} "
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
        context: Any = None,  # Accept OperationContext, EnhancedOperationContext, or dict
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
            # Handle both dict and OperationContext/EnhancedOperationContext
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
        context: Any = None,  # Accept OperationContext, EnhancedOperationContext, or dict
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
            zone_id: Optional zone ID for multi-zone isolation (defaults to "default")

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
            # Handle both dict and OperationContext/EnhancedOperationContext
            if isinstance(context, dict):
                effective_zone_id = context.get("zone")
            elif hasattr(context, "zone_id"):
                effective_zone_id = context.zone_id
        # BUGFIX: Don't default to "default" - let ReBAC manager handle None
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
        context: Any = None,  # Accept OperationContext, EnhancedOperationContext, or dict
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
            # Handle both dict and OperationContext/EnhancedOperationContext
            if isinstance(context, dict):
                effective_zone_id = context.get("zone")
            elif hasattr(context, "zone_id"):
                effective_zone_id = context.zone_id

        # Get explanation
        return self._require_rebac.rebac_explain(
            subject=subject, permission=permission, object=object, zone_id=effective_zone_id
        )

    @rpc_expose(description="Batch ReBAC permission checks")
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

    # =========================================================================
    # Consent & Privacy Controls (Advanced Feature)
    # =========================================================================

    @rpc_expose(description="Expand ReBAC permissions with privacy filtering")
    def rebac_expand_with_privacy(
        self,
        permission: str,
        object: tuple[str, str],
        respect_consent: bool = True,
        requester: tuple[str, str] | None = None,
    ) -> list[tuple[str, str]]:
        """Find subjects with permission, optionally filtering by consent.

        This enables privacy-aware queries where subjects who haven't granted
        consent are filtered from results.

        Args:
            permission: Permission to check
            object: Object to expand on
            respect_consent: Filter results by consent/public_discoverable
            requester: Who is requesting (for consent checks)

        Returns:
            List of subjects, potentially filtered by privacy

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Standard expand (no privacy filtering)
            >>> viewers = nx.rebac_expand_with_privacy(
            ...     "view",
            ...     ("file", "/doc.txt"),
            ...     respect_consent=False
            ... )
            >>> # Returns all viewers

            >>> # Privacy-aware expand
            >>> viewers = nx.rebac_expand_with_privacy(
            ...     "view",
            ...     ("file", "/doc.txt"),
            ...     respect_consent=True,
            ...     requester=("user", "charlie")
            ... )
            >>> # Returns only users charlie can discover
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # Get all subjects with permission
        all_subjects = self.rebac_expand(permission, object)

        if not respect_consent or not requester:
            return all_subjects

        # Filter by consent - only return subjects requester can discover
        filtered = []
        for subject in all_subjects:
            # Check if requester can discover this subject
            can_discover = self._require_rebac.rebac_check(
                subject=requester, permission="discover", object=subject
            )
            if can_discover:
                filtered.append(subject)

        return filtered

    @rpc_expose(description="Grant consent for discovery")
    def grant_consent(
        self,
        from_subject: tuple[str, str],
        to_subject: tuple[str, str],
        expires_at: datetime | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Grant consent for one subject to discover another.

        Args:
            from_subject: Who is granting consent (e.g., profile, resource)
            to_subject: Who can now discover
            expires_at: Optional expiration
            zone_id: Optional zone ID

        Returns:
            Tuple ID

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Alice grants Bob consent to see her profile
            >>> from datetime import timedelta, UTC
            >>> nx.grant_consent(
            ...     from_subject=("profile", "alice"),
            ...     to_subject=("user", "bob"),
            ...     expires_at=datetime.now(UTC) + timedelta(days=30)
            ... )
            'uuid-string'

            >>> # Grant permanent consent
            >>> nx.grant_consent(
            ...     from_subject=("file", "/doc.txt"),
            ...     to_subject=("user", "charlie")
            ... )
            'uuid-string'
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        return self.rebac_create(
            subject=to_subject,
            relation="consent_granted",
            object=from_subject,
            expires_at=expires_at,
            zone_id=zone_id,
        )

    @rpc_expose(description="Revoke consent")
    def revoke_consent(self, from_subject: tuple[str, str], to_subject: tuple[str, str]) -> bool:
        """Revoke previously granted consent.

        Args:
            from_subject: Who is revoking
            to_subject: Who loses discovery access

        Returns:
            True if consent was revoked, False if no consent existed

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Revoke Bob's consent to see Alice's profile
            >>> nx.revoke_consent(
            ...     from_subject=("profile", "alice"),
            ...     to_subject=("user", "bob")
            ... )
            True
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # Find the consent tuple
        tuples = self.rebac_list_tuples(
            subject=to_subject, relation="consent_granted", object=from_subject
        )

        if tuples:
            return self.rebac_delete(tuples[0]["tuple_id"])
        return False

    @rpc_expose(description="Make resource publicly discoverable")
    def make_public(self, resource: tuple[str, str], zone_id: str | None = None) -> dict[str, Any]:
        """Make a resource publicly discoverable.

        Args:
            resource: Resource to make public
            zone_id: Optional zone ID

        Returns:
            Tuple ID

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Make alice's profile public
            >>> nx.make_public(("profile", "alice"))
            'uuid-string'

            >>> # Make file publicly discoverable
            >>> nx.make_public(("file", "/public/doc.txt"))
            'uuid-string'
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        return self.rebac_create(
            subject=("*", "*"),  # Wildcard = public
            relation="public_discoverable",
            object=resource,
            zone_id=zone_id,
        )

    @rpc_expose(description="Make resource private")
    def make_private(self, resource: tuple[str, str]) -> bool:
        """Remove public discoverability from a resource.

        Args:
            resource: Resource to make private

        Returns:
            True if made private, False if wasn't public

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Make alice's profile private
            >>> nx.make_private(("profile", "alice"))
            True
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # Find public tuple
        tuples = self.rebac_list_tuples(
            subject=("*", "*"), relation="public_discoverable", object=resource
        )

        if tuples:
            return self.rebac_delete(tuples[0]["tuple_id"])
        return False

    # =========================================================================
    # Cross-Zone Sharing APIs
    # =========================================================================

    @rpc_expose(description="Share a resource with a specific user (same or different zone)")
    def share_with_user(
        self,
        resource: tuple[str, str],
        user_id: str,
        relation: str = "viewer",
        zone_id: str | None = None,
        user_zone_id: str | None = None,
        expires_at: datetime | None = None,
        context: Any = None,  # Accept OperationContext, EnhancedOperationContext, or dict
    ) -> dict[str, Any]:
        """Share a resource with a specific user, regardless of zone.

        This enables cross-zone sharing - users from different organizations
        can be granted access to specific resources.

        Args:
            resource: Resource to share (e.g., ("file", "/path/to/doc.txt"))
            user_id: User to share with (e.g., "bob@partner-company.com")
            relation: Permission level - "viewer" (read) or "editor" (read/write)
            zone_id: Resource owner's zone ID (defaults to current zone)
            user_zone_id: Recipient user's zone ID (for cross-zone shares)
            expires_at: Optional expiration datetime for the share
            context: Operation context (automatically provided by RPC server)

        Returns:
            Share ID (tuple_id) that can be used to revoke the share

        Raises:
            RuntimeError: If ReBAC is not available
            ValueError: If relation is not "viewer", "editor", or "owner"
            PermissionError: If caller does not have execute permission (owner) on the resource

        Examples:
            >>> # Share file with user in same zone
            >>> share_id = nx.share_with_user(
            ...     resource=("file", "/project/doc.txt"),
            ...     user_id="alice@mycompany.com",
            ...     relation="editor"
            ... )

            >>> # Share file with user in different zone
            >>> share_id = nx.share_with_user(
            ...     resource=("file", "/project/doc.txt"),
            ...     user_id="bob@partner.com",
            ...     user_zone_id="partner-zone",
            ...     relation="viewer",
            ...     expires_at=datetime(2024, 12, 31)
            ... )
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # SECURITY: Check execute permission before allowing permission management
        # Only owners (those with execute permission) can grant/manage permissions on resources
        # Now applies to ALL resource types, not just files
        self._check_share_permission(resource=resource, context=context)

        # Map user-facing relation to internal tuple relation
        # These shared-* relations are included in the viewer/editor/owner unions
        # for proper permission inheritance
        relation_map = {
            "viewer": "shared-viewer",
            "editor": "shared-editor",
            "owner": "shared-owner",
        }
        if relation not in relation_map:
            raise ValueError(f"relation must be 'viewer', 'editor', or 'owner', got '{relation}'")

        tuple_relation = relation_map[relation]

        # Parse expires_at if it's a string (from RPC)
        expires_dt = None
        if expires_at is not None:
            if isinstance(expires_at, str):
                from datetime import datetime as dt

                expires_dt = dt.fromisoformat(expires_at.replace("Z", "+00:00"))
            else:
                expires_dt = expires_at

        # Use shared-* relations which are allowed to cross zone boundaries
        # Call underlying manager directly to support cross-zone parameters
        result = self._require_rebac.rebac_write(
            subject=("user", user_id),
            relation=tuple_relation,
            object=resource,
            zone_id=zone_id,
            subject_zone_id=user_zone_id,
            expires_at=expires_dt,
        )
        return {
            "tuple_id": result.tuple_id,
            "revision": result.revision,
            "consistency_token": result.consistency_token,
        }

    @rpc_expose(description="Share a resource with a group (all members get access)")
    def share_with_group(
        self,
        resource: tuple[str, str],
        group_id: str,
        relation: str = "viewer",
        zone_id: str | None = None,
        group_zone_id: str | None = None,
        expires_at: datetime | None = None,
        context: Any = None,  # Accept OperationContext, EnhancedOperationContext, or dict
    ) -> dict[str, Any]:
        """Share a resource with a group (all members get access).

        Uses userset-as-subject pattern: ("group", group_id, "member")
        All members of the group will have the specified permission level.

        This enables cross-zone sharing - groups from different organizations
        can be granted access to specific resources.

        Args:
            resource: Resource to share (e.g., ("file", "/path/to/doc.txt"))
            group_id: Group to share with (e.g., "developers")
            relation: Permission level - "viewer" (read), "editor" (read/write), or "owner"
            zone_id: Resource owner's zone ID (defaults to current zone)
            group_zone_id: Recipient group's zone ID (for cross-zone shares)
            expires_at: Optional expiration datetime for the share
            context: Operation context (automatically provided by RPC server)

        Returns:
            Share ID (tuple_id) that can be used to revoke the share

        Raises:
            RuntimeError: If ReBAC is not available
            ValueError: If relation is not "viewer", "editor", or "owner"
            PermissionError: If caller does not have execute permission (owner) on the resource

        Examples:
            >>> # Share file with group in same zone
            >>> share_id = nx.share_with_group(
            ...     resource=("file", "/project/doc.txt"),
            ...     group_id="developers",
            ...     relation="editor"
            ... )

            >>> # Share file with group in different zone
            >>> share_id = nx.share_with_group(
            ...     resource=("file", "/project/doc.txt"),
            ...     group_id="partner-team",
            ...     group_zone_id="partner-zone",
            ...     relation="viewer",
            ...     expires_at=datetime(2024, 12, 31)
            ... )
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # SECURITY: Check execute permission before allowing permission management
        # Only owners (those with execute permission) can grant/manage permissions on resources
        # Now applies to ALL resource types, not just files
        self._check_share_permission(resource=resource, context=context)

        # Map user-facing relation to internal tuple relation
        # These shared-* relations are included in the viewer/editor/owner unions
        # for proper permission inheritance
        relation_map = {
            "viewer": "shared-viewer",
            "editor": "shared-editor",
            "owner": "shared-owner",
        }
        if relation not in relation_map:
            raise ValueError(f"relation must be 'viewer', 'editor', or 'owner', got '{relation}'")

        tuple_relation = relation_map[relation]

        # Parse expires_at if it's a string (from RPC)
        expires_dt = None
        if expires_at is not None:
            if isinstance(expires_at, str):
                from datetime import datetime as dt

                expires_dt = dt.fromisoformat(expires_at.replace("Z", "+00:00"))
            else:
                expires_dt = expires_at

        # Use userset-as-subject pattern: ("group", group_id, "member")
        # This allows all members of the group to have the specified permission
        # Use shared-* relations which are allowed to cross zone boundaries
        # Call underlying manager directly to support cross-zone parameters
        result = self._require_rebac.rebac_write(
            subject=("group", group_id, "member"),  # Userset-as-subject pattern
            relation=tuple_relation,
            object=resource,
            zone_id=zone_id,
            subject_zone_id=group_zone_id,
            expires_at=expires_dt,
        )
        return {
            "tuple_id": result.tuple_id,
            "revision": result.revision,
            "consistency_token": result.consistency_token,
        }

    @rpc_expose(description="Revoke a share by resource and user")
    def revoke_share(
        self,
        resource: tuple[str, str],
        user_id: str,
    ) -> bool:
        """Revoke a share for a specific user on a resource.

        Args:
            resource: Resource to unshare (e.g., ("file", "/path/to/doc.txt"))
            user_id: User to revoke access from

        Returns:
            True if share was revoked, False if no share existed

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> nx.revoke_share(
            ...     resource=("file", "/project/doc.txt"),
            ...     user_id="bob@partner.com"
            ... )
            True
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # Find the share tuple - use single query with relation_in (N+1 FIX)
        tuples = self.rebac_list_tuples(
            subject=("user", user_id),
            relation_in=["shared-viewer", "shared-editor", "shared-owner"],
            object=resource,
        )
        if tuples:
            return self.rebac_delete(tuples[0]["tuple_id"])
        return False

    @rpc_expose(description="Revoke a share by share ID")
    def revoke_share_by_id(self, share_id: str) -> bool:
        """Revoke a share using its ID.

        Args:
            share_id: The share ID returned by share_with_user()

        Returns:
            True if share was revoked, False if share didn't exist

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> share_id = nx.share_with_user(resource, user_id)
            >>> nx.revoke_share_by_id(share_id)
            True
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        return self.rebac_delete(share_id)

    @rpc_expose(description="List shares I've created (outgoing)")
    def list_outgoing_shares(
        self,
        resource: tuple[str, str] | None = None,
        zone_id: str | None = None,  # noqa: ARG002 - Reserved for future zone filtering
        limit: int = 100,
        offset: int = 0,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List shares created by the current zone (resources shared with others).

        Uses iterator caching for efficient pagination (Issue #735).

        Args:
            resource: Filter by specific resource (optional)
            zone_id: Zone ID to list shares for (defaults to current zone)
            limit: Maximum number of results
            offset: Number of results to skip
            cursor: Pagination cursor from previous request

        Returns:
            Dictionary with keys:
            - items: List of share info dictionaries
            - next_cursor: Cursor for next page (None if no more)
            - total_count: Total number of shares
            - has_more: Boolean indicating if more pages exist

            Each share info dict has keys:
            - share_id: Unique share identifier
            - resource_type: Type of shared resource
            - resource_id: ID of shared resource
            - recipient_id: User the resource is shared with
            - permission_level: "viewer", "editor", or "owner"
            - created_at: When the share was created
            - expires_at: When the share expires (if set)

        Examples:
            >>> # List all outgoing shares
            >>> result = nx.list_outgoing_shares()
            >>> for share in result["items"]:
            ...     print(f"{share['resource_id']} -> {share['recipient_id']}")

            >>> # Paginated iteration with cursor
            >>> result = nx.list_outgoing_shares(limit=50)
            >>> while result["has_more"]:
            ...     result = nx.list_outgoing_shares(limit=50, cursor=result["next_cursor"])
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        from nexus.services.permissions.rebac_iterator_cache import CursorExpiredError

        # Map relation back to permission level
        relation_to_level = {
            "shared-viewer": "viewer",
            "shared-editor": "editor",
            "shared-owner": "owner",
        }

        def _transform_tuples(tuples: list[dict[str, Any]]) -> list[dict[str, Any]]:
            """Transform raw tuples to share info format."""
            return [
                {
                    "share_id": t.get("tuple_id"),
                    "resource_type": t.get("object_type"),
                    "resource_id": t.get("object_id"),
                    "recipient_id": t.get("subject_id"),
                    "permission_level": relation_to_level.get(t.get("relation") or "", "viewer"),
                    "created_at": t.get("created_at"),
                    "expires_at": t.get("expires_at"),
                }
                for t in tuples
            ]

        def _compute_shares() -> list[dict[str, Any]]:
            """Compute all shares (called on cache miss)."""
            all_tuples = self.rebac_list_tuples(
                relation_in=["shared-viewer", "shared-editor", "shared-owner"],
                object=resource,
            )
            return _transform_tuples(all_tuples)

        # Get current zone ID for cache isolation
        current_zone = getattr(self, "_current_zone_id", "default")

        # Try to use cursor-based pagination
        if cursor:
            try:
                items, next_cursor, total = self._require_rebac._iterator_cache.get_page(
                    cursor_id=cursor,
                    offset=offset,
                    limit=limit,
                )
                return {
                    "items": items,
                    "next_cursor": next_cursor,
                    "total_count": total,
                    "has_more": next_cursor is not None,
                }
            except CursorExpiredError:
                # Fall through to recompute
                pass

        # Compute query hash for cache key
        resource_str = f"{resource[0]}:{resource[1]}" if resource else "all"
        query_hash = f"outgoing:{current_zone}:{resource_str}"

        # Get or create cached results
        cursor_id, all_results, total = self._require_rebac._iterator_cache.get_or_create(
            query_hash=query_hash,
            zone_id=current_zone,
            compute_fn=_compute_shares,
        )

        # Get requested page
        items = all_results[offset : offset + limit]
        has_more = offset + limit < total
        next_cursor = cursor_id if has_more else None

        return {
            "items": items,
            "next_cursor": next_cursor,
            "total_count": total,
            "has_more": has_more,
        }

    @rpc_expose(description="List shares I've received (incoming)")
    def list_incoming_shares(
        self,
        user_id: str,
        limit: int = 100,
        offset: int = 0,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List shares received by a user (resources shared with me).

        This includes cross-zone shares from other organizations.
        Uses iterator caching for efficient pagination (Issue #735).

        Args:
            user_id: User ID to list incoming shares for
            limit: Maximum number of results
            offset: Number of results to skip
            cursor: Pagination cursor from previous request

        Returns:
            Dictionary with keys:
            - items: List of share info dictionaries
            - next_cursor: Cursor for next page (None if no more)
            - total_count: Total number of shares
            - has_more: Boolean indicating if more pages exist

            Each share info dict has keys:
            - share_id: Unique share identifier
            - resource_type: Type of shared resource
            - resource_id: ID of shared resource
            - owner_zone_id: Zone that owns the resource
            - permission_level: "viewer", "editor", or "owner"
            - created_at: When the share was created
            - expires_at: When the share expires (if set)

        Examples:
            >>> # List all resources shared with me
            >>> result = nx.list_incoming_shares(user_id="alice@mycompany.com")
            >>> for share in result["items"]:
            ...     print(f"{share['resource_id']} from {share['owner_zone_id']}")

            >>> # Paginated iteration with cursor
            >>> result = nx.list_incoming_shares(user_id="alice@mycompany.com", limit=50)
            >>> while result["has_more"]:
            ...     result = nx.list_incoming_shares(
            ...         user_id="alice@mycompany.com", limit=50, cursor=result["next_cursor"]
            ...     )
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        from nexus.services.permissions.rebac_iterator_cache import CursorExpiredError

        # Map relation back to permission level
        relation_to_level = {
            "shared-viewer": "viewer",
            "shared-editor": "editor",
            "shared-owner": "owner",
        }

        def _transform_tuples(tuples: list[dict[str, Any]]) -> list[dict[str, Any]]:
            """Transform raw tuples to share info format."""
            return [
                {
                    "share_id": t.get("tuple_id"),
                    "resource_type": t.get("object_type"),
                    "resource_id": t.get("object_id"),
                    "owner_zone_id": t.get("zone_id"),
                    "permission_level": relation_to_level.get(t.get("relation") or "", "viewer"),
                    "created_at": t.get("created_at"),
                    "expires_at": t.get("expires_at"),
                }
                for t in tuples
            ]

        def _compute_shares() -> list[dict[str, Any]]:
            """Compute all shares (called on cache miss)."""
            all_tuples = self.rebac_list_tuples(
                subject=("user", user_id),
                relation_in=["shared-viewer", "shared-editor", "shared-owner"],
            )
            return _transform_tuples(all_tuples)

        # Get current zone ID for cache isolation
        current_zone = getattr(self, "_current_zone_id", "default")

        # Try to use cursor-based pagination
        if cursor:
            try:
                items, next_cursor, total = self._require_rebac._iterator_cache.get_page(
                    cursor_id=cursor,
                    offset=offset,
                    limit=limit,
                )
                return {
                    "items": items,
                    "next_cursor": next_cursor,
                    "total_count": total,
                    "has_more": next_cursor is not None,
                }
            except CursorExpiredError:
                # Fall through to recompute
                pass

        # Compute query hash for cache key
        query_hash = f"incoming:{current_zone}:{user_id}"

        # Get or create cached results
        cursor_id, all_results, total = self._require_rebac._iterator_cache.get_or_create(
            query_hash=query_hash,
            zone_id=current_zone,
            compute_fn=_compute_shares,
        )

        # Get requested page
        items = all_results[offset : offset + limit]
        has_more = offset + limit < total
        next_cursor = cursor_id if has_more else None

        return {
            "items": items,
            "next_cursor": next_cursor,
            "total_count": total,
            "has_more": has_more,
        }

    # =========================================================================
    # Dynamic Viewer - Column-level Permissions for Data Files
    # =========================================================================

    @rpc_expose(description="Get dynamic viewer configuration for a file")
    def get_dynamic_viewer_config(
        self,
        subject: tuple[str, str],
        file_path: str,
    ) -> dict[str, Any] | None:
        """Get the dynamic_viewer configuration for a subject and file.

        Args:
            subject: (subject_type, subject_id) tuple (e.g., ('agent', 'alice'))
            file_path: Path to the file

        Returns:
            Dictionary with column_config if dynamic_viewer relation exists, None otherwise

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Get alice's dynamic viewer config for users.csv
            >>> config = nx.get_dynamic_viewer_config(
            ...     subject=("agent", "alice"),
            ...     file_path="/data/users.csv"
            ... )
            >>> if config:
            ...     print(config["mode"])  # "whitelist" or "blacklist"
            ...     print(config["visible_columns"])  # ["name", "email"]
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # Find dynamic_viewer tuples for this subject and file
        tuples = self.rebac_list_tuples(
            subject=subject, relation="dynamic_viewer", object=("file", file_path)
        )

        if not tuples:
            return None

        # Get the most recent tuple (in case there are multiple)
        tuple_data = tuples[0]

        # Parse conditions from the tuple
        import json

        conn = self._require_rebac._get_connection()
        try:
            cursor = self._require_rebac._create_cursor(conn)
            cursor.execute(
                self._require_rebac._fix_sql_placeholders(
                    "SELECT conditions FROM rebac_tuples WHERE tuple_id = ?"
                ),
                (tuple_data["tuple_id"],),
            )
            row = cursor.fetchone()
            if row and row["conditions"]:
                conditions = json.loads(row["conditions"])
                if conditions.get("type") == "dynamic_viewer":
                    column_config = conditions.get("column_config")
                    return column_config if column_config is not None else None
        finally:
            self._require_rebac._close_connection(conn)

        return None

    @rpc_expose(description="Apply dynamic viewer filter to CSV data")
    def apply_dynamic_viewer_filter(
        self,
        data: str,
        column_config: dict[str, Any],
        file_format: str = "csv",
    ) -> dict[str, Any]:
        """Apply column-level filtering and aggregations to CSV data.

        Args:
            data: Raw data content (CSV string)
            column_config: Column configuration dict with hidden_columns, aggregations, visible_columns
            file_format: Format of the data (currently only "csv" is supported)

        Returns:
            Dictionary with:
                - filtered_data: Filtered data as CSV string (visible columns + aggregated columns)
                - aggregations: Dictionary of computed aggregations
                - columns_shown: List of column names included in filtered data
                - aggregated_columns: List of aggregated column names with operation prefix

        Raises:
            ValueError: If file_format is not supported
            RuntimeError: If data parsing fails

        Examples:
            >>> # Apply filter to CSV data
            >>> result = nx.apply_dynamic_viewer_filter(
            ...     data="name,email,age,password\\nalice,a@ex.com,30,secret\\nbob,b@ex.com,25,pwd\\n",
            ...     column_config={
            ...         "hidden_columns": ["password"],
            ...         "aggregations": {"age": "mean"},
            ...         "visible_columns": ["name", "email"]
            ...     }
            ... )
            >>> print(result["filtered_data"])  # name,email,mean(age) with values
            >>> print(result["aggregations"])    # {"age": {"mean": 27.5}}
        """
        if file_format != "csv":
            raise ValueError(f"Unsupported file format: {file_format}. Only 'csv' is supported.")

        try:
            import io

            import pandas as pd
        except ImportError as e:
            raise RuntimeError(
                "pandas is required for dynamic viewer filtering. Install with: pip install pandas"
            ) from e

        # Parse CSV data
        try:
            df = pd.read_csv(io.StringIO(data))
        except (ValueError, pd.errors.ParserError) as e:
            raise RuntimeError(f"Failed to parse CSV data: {e}") from e

        # Get configuration
        hidden_columns = column_config.get("hidden_columns", [])
        aggregations = column_config.get("aggregations", {})
        visible_columns = column_config.get("visible_columns", [])

        # Auto-calculate visible_columns if empty
        # visible_columns = all columns - hidden_columns - aggregation columns
        if not visible_columns:
            all_cols = set(df.columns)
            hidden_set = set(hidden_columns)
            agg_set = set(aggregations.keys())
            visible_columns = list(all_cols - hidden_set - agg_set)

        # Build result dataframe in original column order
        # Iterate through original columns and add visible/aggregated columns in order
        result_columns = []  # List of (column_name, series) tuples
        aggregation_results: dict[str, dict[str, float | int | str]] = {}
        aggregated_column_names = []
        columns_shown = []

        for col in df.columns:
            if col in hidden_columns:
                # Skip hidden columns
                continue
            elif col in aggregations:
                # Add aggregated column at original position
                operation = aggregations[col]
                try:
                    # Compute aggregation
                    if operation == "mean":
                        agg_value = float(df[col].mean())
                    elif operation == "sum":
                        agg_value = float(df[col].sum())
                    elif operation == "count":
                        agg_value = int(df[col].count())
                    elif operation == "min":
                        agg_value = float(df[col].min())
                    elif operation == "max":
                        agg_value = float(df[col].max())
                    elif operation == "std":
                        agg_value = float(df[col].std())
                    elif operation == "median":
                        agg_value = float(df[col].median())
                    else:
                        # Unknown operation, skip
                        continue

                    # Store aggregation result
                    if col not in aggregation_results:
                        aggregation_results[col] = {}
                    aggregation_results[col][operation] = agg_value

                    # Add aggregated column with formatted name
                    agg_col_name = f"{operation}({col})"
                    aggregated_column_names.append(agg_col_name)

                    # Create series with aggregated value repeated for all rows
                    agg_series = pd.Series([agg_value] * len(df), name=agg_col_name)
                    result_columns.append((agg_col_name, agg_series))

                except (ValueError, TypeError, KeyError) as e:
                    # If aggregation fails, store error message
                    if col not in aggregation_results:
                        aggregation_results[col] = {}
                    aggregation_results[col][operation] = f"error: {str(e)}"
            elif col in visible_columns:
                # Add visible column at original position
                result_columns.append((col, df[col]))
                columns_shown.append(col)

        # Build result dataframe from ordered columns
        result_df = pd.DataFrame(dict(result_columns)) if result_columns else pd.DataFrame()

        # Convert result dataframe to CSV string
        filtered_data = result_df.to_csv(index=False)

        return {
            "filtered_data": filtered_data,
            "aggregations": aggregation_results,
            "columns_shown": columns_shown,
            "aggregated_columns": aggregated_column_names,
        }

    @rpc_expose(description="Read file with dynamic viewer permissions applied")
    def read_with_dynamic_viewer(
        self,
        file_path: str,
        subject: tuple[str, str],
        context: Any = None,
    ) -> dict[str, Any]:
        """Read a CSV file with dynamic_viewer permissions applied.

        This method checks if the subject has dynamic_viewer permissions on the file,
        and if so, applies the column-level filtering before returning the data.
        Only supports CSV files.

        Args:
            file_path: Path to the CSV file to read
            subject: (subject_type, subject_id) tuple
            context: Operation context (automatically provided by RPC server)

        Returns:
            Dictionary with:
                - content: Filtered file content (or full content if not dynamic viewer)
                - is_filtered: Boolean indicating if dynamic filtering was applied
                - config: The column config used (if filtered)
                - aggregations: Computed aggregations (if any)
                - columns_shown: List of visible columns (if filtered)
                - aggregated_columns: List of aggregated column names with operation prefix

        Raises:
            PermissionError: If subject has no read permission on file
            ValueError: If file is not a CSV file for dynamic_viewer
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Read CSV file with dynamic viewer permissions
            >>> result = nx.read_with_dynamic_viewer(
            ...     file_path="/data/users.csv",
            ...     subject=("agent", "alice")
            ... )
            >>> if result["is_filtered"]:
            ...     print("Filtered data:", result["content"])
            ...     print("Aggregations:", result["aggregations"])
            ...     print("Columns:", result["columns_shown"])
            ...     print("Aggregated:", result["aggregated_columns"])
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in standalone mode."
            )

        # Check if this is a CSV file
        if not file_path.lower().endswith(".csv"):
            raise ValueError(
                f"read_with_dynamic_viewer only supports CSV files. "
                f"File '{file_path}' does not have .csv extension."
            )

        # Check if subject has read permission (either viewer or dynamic_viewer)
        has_read = self.rebac_check(
            subject=subject, permission="read", object=("file", file_path), context=context
        )

        if not has_read:
            raise PermissionError(f"Subject {subject} does not have read permission on {file_path}")

        # Get dynamic viewer config
        column_config = self.get_dynamic_viewer_config(subject=subject, file_path=file_path)

        # Read the file content WITHOUT dynamic_viewer filtering
        # We need the raw content to apply filtering here
        if (
            hasattr(self, "metadata")
            and hasattr(self, "router")
            and hasattr(self, "_get_routing_params")
        ):
            # NexusFS instance - read directly from backend to bypass filtering
            zone_id, agent_id, is_admin = self._get_routing_params(context)
            route = self.router.route(
                file_path,
                zone_id=zone_id,
                agent_id=agent_id,
                is_admin=is_admin,
                check_write=False,
            )
            meta = self.metadata.get(file_path)
            if meta is None or meta.etag is None:
                raise RuntimeError(f"File not found: {file_path}")

            # Read raw content from backend
            content_bytes = route.backend.read_content(meta.etag, context=context).unwrap()
            content = (
                content_bytes.decode("utf-8") if isinstance(content_bytes, bytes) else content_bytes
            )
        else:
            # Fallback: read from filesystem
            with open(file_path, encoding="utf-8") as f:
                content = f.read()

        # If no dynamic viewer config, return full content
        if not column_config:
            return {
                "content": content.encode("utf-8") if isinstance(content, str) else content,
                "is_filtered": False,
                "config": None,
                "aggregations": {},
                "columns_shown": [],
                "aggregated_columns": [],
            }

        # Apply dynamic viewer filtering to raw content
        result = self.apply_dynamic_viewer_filter(
            data=content,  # Raw unfiltered content
            column_config=column_config,
            file_format="csv",
        )

        return {
            "content": result["filtered_data"].encode("utf-8")
            if isinstance(result["filtered_data"], str)
            else result["filtered_data"],
            "is_filtered": True,
            "config": column_config,
            "aggregations": result["aggregations"],
            "columns_shown": result["columns_shown"],
            "aggregated_columns": result["aggregated_columns"],
        }

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
            zone_id: Zone ID for the permissions (default: "default")
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
            zone_id: Zone ID to scope warming (default: "default")

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

    # -------------------------------------------------------------------------
    # MCPService Delegation Methods (5 methods)
    # Issue #1287 Phase 1.4: NexusFSMCPMixin removed, replaced by MCPService delegation
    # -------------------------------------------------------------------------

    @rpc_expose(description="List MCP server mounts")
    async def mcp_list_mounts(
        self,
        tier: str | None = None,
        include_unmounted: bool = True,
        _context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List all MCP mounts - delegates to MCPService."""
        return await self.mcp_service.mcp_list_mounts(
            tier=tier,
            include_unmounted=include_unmounted,
            context=_context,
        )

    # Backward-compat alias
    amcp_list_mounts = mcp_list_mounts

    @rpc_expose(description="List tools from MCP mount")
    async def mcp_list_tools(
        self,
        name: str,
        _context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List MCP tools from specific mount - delegates to MCPService."""
        return await self.mcp_service.mcp_list_tools(
            name=name,
            context=_context,
        )

    # Backward-compat alias
    amcp_list_tools = mcp_list_tools

    @rpc_expose(description="Mount MCP server")
    async def mcp_mount(
        self,
        name: str,
        transport: str | None = None,
        command: str | None = None,
        url: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        description: str | None = None,
        tier: str = "system",
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Mount an MCP server - delegates to MCPService."""
        return await self.mcp_service.mcp_mount(
            name=name,
            transport=transport,
            command=command,
            url=url,
            args=args,
            env=env,
            headers=headers,
            description=description,
            tier=tier,
            context=_context,
        )

    # Backward-compat alias
    amcp_mount = mcp_mount

    @rpc_expose(description="Unmount MCP server")
    async def mcp_unmount(
        self,
        name: str,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Unmount an MCP server - delegates to MCPService."""
        return await self.mcp_service.mcp_unmount(name=name, _context=_context)

    # Backward-compat alias
    amcp_unmount = mcp_unmount

    @rpc_expose(description="Sync tools from MCP server")
    async def mcp_sync(
        self,
        name: str,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Sync/refresh tools from MCP mount - delegates to MCPService."""
        return await self.mcp_service.mcp_sync(
            name=name,
            context=_context,
        )

    # Backward-compat alias
    amcp_sync = mcp_sync

    # -------------------------------------------------------------------------
    # SkillService Delegation Methods (10 methods)
    # Issue #1287 Phase 1.5: NexusFSSkillsMixin removed, replaced by SkillService delegation
    # -------------------------------------------------------------------------

    @rpc_expose(description="Share a skill with users, groups, or make public")
    def skills_share(
        self,
        skill_path: str,
        share_with: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Grant read permission on a skill - delegates to SkillService."""
        tuple_id = self.skill_service.share(skill_path, share_with, context)
        return {
            "success": True,
            "tuple_id": tuple_id,
            "skill_path": skill_path,
            "share_with": share_with,
        }

    @rpc_expose(description="Revoke sharing permission on a skill")
    def skills_unshare(
        self,
        skill_path: str,
        unshare_from: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Revoke read permission on a skill - delegates to SkillService."""
        success = self.skill_service.unshare(skill_path, unshare_from, context)
        return {
            "success": success,
            "skill_path": skill_path,
            "unshare_from": unshare_from,
        }

    @rpc_expose(description="Discover skills the user has permission to see")
    def skills_discover(
        self,
        filter: str = "all",
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """List skills the user can see - delegates to SkillService."""
        skills = self.skill_service.discover(context, filter)
        return {
            "skills": [s.to_dict() for s in skills],
            "count": len(skills),
        }

    @rpc_expose(description="Subscribe to a skill (add to user's library)")
    def skills_subscribe(
        self,
        skill_path: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Subscribe to a skill - delegates to SkillService."""
        newly_subscribed = self.skill_service.subscribe(skill_path, context)
        return {
            "success": True,
            "skill_path": skill_path,
            "already_subscribed": not newly_subscribed,
        }

    @rpc_expose(description="Unsubscribe from a skill (remove from user's library)")
    def skills_unsubscribe(
        self,
        skill_path: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Unsubscribe from a skill - delegates to SkillService."""
        was_subscribed = self.skill_service.unsubscribe(skill_path, context)
        return {
            "success": True,
            "skill_path": skill_path,
            "was_subscribed": was_subscribed,
        }

    @rpc_expose(description="Get skill metadata for system prompt injection")
    def skills_get_prompt_context(
        self,
        max_skills: int = 50,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Get prompt context - delegates to SkillService."""
        prompt_context = self.skill_service.get_prompt_context(context, max_skills)
        return prompt_context.to_dict()

    @rpc_expose(description="Load full skill content on-demand")
    def skills_load(
        self,
        skill_path: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Load skill content - delegates to SkillService."""
        content = self.skill_service.load(skill_path, context)
        return content.to_dict()

    @rpc_expose(description="Export a skill as a .skill (ZIP) package")
    def skills_export(
        self,
        skill_path: str | None = None,
        skill_name: str | None = None,
        output_path: str | None = None,
        format: str = "generic",
        include_dependencies: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Export a skill - delegates to SkillService."""
        return self.skill_service.export(
            skill_path=skill_path,
            skill_name=skill_name,
            output_path=output_path,
            format=format,
            include_dependencies=include_dependencies,
            context=context,
        )

    @rpc_expose(description="Import a skill from a .skill (ZIP) package")
    def skills_import(
        self,
        source_path: str | None = None,
        zip_bytes: bytes | str | None = None,
        zip_data: str | None = None,
        target_path: str | None = None,
        allow_overwrite: bool = False,
        context: OperationContext | None = None,
        tier: str | None = None,
    ) -> dict[str, Any]:
        """Import a skill - delegates to SkillService."""
        return self.skill_service.import_skill(
            source_path=source_path,
            zip_bytes=zip_bytes,
            zip_data=zip_data,
            target_path=target_path,
            allow_overwrite=allow_overwrite,
            context=context,
            tier=tier,
        )

    @rpc_expose(description="Validate a .skill (ZIP) package")
    def skills_validate_zip(
        self,
        source_path: str | None = None,
        zip_bytes: bytes | str | None = None,
        zip_data: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Validate a skill package - delegates to SkillService."""
        return self.skill_service.validate_zip(
            source_path=source_path,
            zip_bytes=zip_bytes,
            zip_data=zip_data,
            context=context,
        )

    # -------------------------------------------------------------------------
    # LLMService Delegation Methods (4 methods)
    # Issue #1287 Phase B: NexusFSLLMMixin removed, replaced by LLMService delegation
    # -------------------------------------------------------------------------

    @rpc_expose(description="Read document with LLM and return answer")
    async def llm_read(
        self,
        path: str,
        prompt: str,
        model: str = "claude-sonnet-4",
        max_tokens: int = 1000,
        api_key: str | None = None,
        use_search: bool = True,
        search_mode: str = "semantic",
        provider: Any = None,
    ) -> str:
        """Read document with LLM and return answer - delegates to LLMService."""
        return await self.llm_service.llm_read(
            path=path,
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            api_key=api_key,
            use_search=use_search,
            search_mode=search_mode,
            provider=provider,
        )

    # Backward-compat alias
    allm_read = llm_read

    @rpc_expose(description="Read document with LLM and return detailed result")
    async def llm_read_detailed(
        self,
        path: str,
        prompt: str,
        model: str = "claude-sonnet-4",
        max_tokens: int = 1000,
        api_key: str | None = None,
        use_search: bool = True,
        search_mode: str = "semantic",
        provider: Any = None,
    ) -> Any:
        """Read document with LLM with detailed metadata - delegates to LLMService."""
        return await self.llm_service.llm_read_detailed(
            path=path,
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            api_key=api_key,
            use_search=use_search,
            search_mode=search_mode,
            provider=provider,
        )

    # Backward-compat alias
    allm_read_detailed = llm_read_detailed

    @rpc_expose(description="Stream document reading response")
    async def llm_read_stream(
        self,
        path: str,
        prompt: str,
        model: str = "claude-sonnet-4",
        max_tokens: int = 1000,
        api_key: str | None = None,
        use_search: bool = True,
        search_mode: str = "semantic",
        provider: Any = None,
    ) -> Any:
        """Stream LLM response - delegates to LLMService."""
        return self.llm_service.llm_read_stream(
            path=path,
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            api_key=api_key,
            use_search=use_search,
            search_mode=search_mode,
            provider=provider,
        )

    # Backward-compat alias
    allm_read_stream = llm_read_stream

    @rpc_expose(description="Create an LLM document reader for advanced usage")
    def create_llm_reader(
        self,
        provider: Any = None,
        model: str | None = None,
        api_key: str | None = None,
        system_prompt: str | None = None,
        max_context_tokens: int = 3000,
    ) -> Any:
        """Create an LLM document reader - delegates to LLMService."""
        return self.llm_service.create_llm_reader(
            provider=provider,
            model=model,
            api_key=api_key,
            system_prompt=system_prompt,
            max_context_tokens=max_context_tokens,
        )

    # Backward-compat alias
    acreate_llm_reader = create_llm_reader

    # -------------------------------------------------------------------------
    # OAuthService Delegation Methods (7 methods)
    # Issue #1287 Phase 1.3: NexusFSOAuthMixin removed, replaced by OAuthService delegation
    # -------------------------------------------------------------------------

    @rpc_expose(description="List available OAuth providers")
    async def oauth_list_providers(
        self,
        _context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List available OAuth providers - delegates to OAuthService."""
        return await self.oauth_service.oauth_list_providers(context=_context)

    # Backward-compat alias
    aoauth_list_providers = oauth_list_providers

    @rpc_expose(description="Get OAuth authorization URL")
    async def oauth_get_auth_url(
        self,
        provider: str,
        redirect_uri: str = "http://localhost:3000/oauth/callback",
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get OAuth authorization URL - delegates to OAuthService."""
        return await self.oauth_service.oauth_get_auth_url(
            provider=provider,
            redirect_uri=redirect_uri,
            scopes=scopes,
        )

    # Backward-compat alias
    aoauth_get_auth_url = oauth_get_auth_url

    @rpc_expose(description="Exchange OAuth authorization code for tokens")
    async def oauth_exchange_code(
        self,
        provider: str,
        code: str,
        user_email: str | None = None,
        state: str | None = None,
        redirect_uri: str | None = None,
        code_verifier: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Exchange OAuth code for tokens - delegates to OAuthService."""
        return await self.oauth_service.oauth_exchange_code(
            provider=provider,
            code=code,
            user_email=user_email,
            state=state,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
            context=context,
        )

    # Backward-compat alias
    aoauth_exchange_code = oauth_exchange_code

    @rpc_expose(description="List OAuth credentials")
    async def oauth_list_credentials(
        self,
        provider: str | None = None,
        include_revoked: bool = False,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List OAuth credentials - delegates to OAuthService."""
        return await self.oauth_service.oauth_list_credentials(
            provider=provider,
            include_revoked=include_revoked,
            context=context,
        )

    # Backward-compat alias
    aoauth_list_credentials = oauth_list_credentials

    @rpc_expose(description="Revoke OAuth credential")
    async def oauth_revoke_credential(
        self,
        provider: str,
        user_email: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Revoke OAuth credential - delegates to OAuthService."""
        return await self.oauth_service.oauth_revoke_credential(
            provider=provider,
            user_email=user_email,
            context=context,
        )

    # Backward-compat alias
    aoauth_revoke_credential = oauth_revoke_credential

    @rpc_expose(description="Test OAuth credential validity")
    async def oauth_test_credential(
        self,
        provider: str,
        user_email: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Test OAuth credential - delegates to OAuthService."""
        return await self.oauth_service.oauth_test_credential(
            provider=provider,
            user_email=user_email,
            context=context,
        )

    # Backward-compat alias
    aoauth_test_credential = oauth_test_credential

    @rpc_expose(description="Connect to MCP provider via Klavis OAuth")
    async def mcp_connect(
        self,
        provider: str,
        redirect_url: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Connect to MCP provider via Klavis - delegates to OAuthService."""
        return await self.oauth_service.mcp_connect(
            provider=provider,
            redirect_url=redirect_url,
            context=context,
        )

    # Backward-compat alias
    amcp_connect = mcp_connect

    # =========================================================================
    # MountService Delegation Methods
    # =========================================================================

    async def aadd_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        readonly: bool = False,
        context: OperationContext | None = None,
    ) -> str:
        """Add a dynamic backend mount - delegates to MountService."""
        return await self.mount_service.add_mount(
            mount_point=mount_point,
            backend_type=backend_type,
            backend_config=backend_config,
            priority=priority,
            readonly=readonly,
            context=context,
        )

    async def aremove_mount(
        self,
        mount_point: str,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Remove a backend mount - delegates to MountService."""
        return await self.mount_service.remove_mount(
            mount_point=mount_point,
            _context=_context,
        )

    async def alist_connectors(
        self,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """List available connector types - delegates to MountService."""
        return await self.mount_service.list_connectors(category=category)

    async def alist_mounts(
        self,
        _context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List all backend mounts - delegates to MountService."""
        return await self.mount_service.list_mounts(_context=_context)

    async def aget_mount(
        self,
        mount_point: str,
    ) -> dict[str, Any] | None:
        """Get mount details - delegates to MountService."""
        return await self.mount_service.get_mount(mount_point=mount_point)

    async def ahas_mount(
        self,
        mount_point: str,
    ) -> bool:
        """Check if mount exists - delegates to MountService."""
        return await self.mount_service.has_mount(mount_point=mount_point)

    async def asave_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        readonly: bool = False,
        owner_user_id: str | None = None,
        zone_id: str | None = None,
        description: str | None = None,
        context: OperationContext | None = None,
    ) -> str:
        """Save mount configuration to database - delegates to MountService."""
        return await self.mount_service.save_mount(
            mount_point=mount_point,
            backend_type=backend_type,
            backend_config=backend_config,
            priority=priority,
            readonly=readonly,
            owner_user_id=owner_user_id,
            zone_id=zone_id,
            description=description,
            context=context,
        )

    async def alist_saved_mounts(
        self,
        owner_user_id: str | None = None,
        zone_id: str | None = None,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List saved mount configurations - delegates to MountService."""
        return await self.mount_service.list_saved_mounts(
            owner_user_id=owner_user_id,
            zone_id=zone_id,
            context=context,
        )

    async def aload_mount(
        self,
        mount_point: str,
    ) -> str:
        """Load and activate saved mount - delegates to MountService."""
        return await self.mount_service.load_mount(mount_point=mount_point)

    async def adelete_saved_mount(
        self,
        mount_point: str,
    ) -> bool:
        """Delete saved mount configuration - delegates to MountService."""
        return await self.mount_service.delete_saved_mount(mount_point=mount_point)

    async def async_mount(
        self,
        mount_point: str | None = None,
        path: str | None = None,
        recursive: bool = True,
        dry_run: bool = False,
        sync_content: bool = True,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        generate_embeddings: bool = False,
        context: OperationContext | None = None,
        progress_callback: Any = None,
    ) -> dict[str, Any]:
        """Sync metadata from connector backend - delegates to MountService."""
        return await self.mount_service.sync_mount(
            mount_point=mount_point,
            path=path,
            recursive=recursive,
            dry_run=dry_run,
            sync_content=sync_content,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            generate_embeddings=generate_embeddings,
            context=context,
            progress_callback=progress_callback,
        )

    async def async_mount_async(
        self,
        mount_point: str,
        path: str | None = None,
        recursive: bool = True,
        dry_run: bool = False,
        sync_content: bool = True,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        generate_embeddings: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Start async sync job for a mount - delegates to MountService."""
        return await self.mount_service.sync_mount_async(
            mount_point=mount_point,
            path=path,
            recursive=recursive,
            dry_run=dry_run,
            sync_content=sync_content,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            generate_embeddings=generate_embeddings,
            context=context,
        )

    async def aget_sync_job(
        self,
        job_id: str,
    ) -> dict[str, Any] | None:
        """Get sync job status and progress - delegates to MountService."""
        return await self.mount_service.get_sync_job(job_id=job_id)

    async def acancel_sync_job(
        self,
        job_id: str,
    ) -> dict[str, Any]:
        """Cancel a running sync job - delegates to MountService."""
        return await self.mount_service.cancel_sync_job(job_id=job_id)

    async def alist_sync_jobs(
        self,
        mount_point: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List sync jobs - delegates to MountService."""
        return await self.mount_service.list_sync_jobs(
            mount_point=mount_point,
            status=status,
            limit=limit,
        )

    # -------------------------------------------------------------------------
    # MountService Sync Delegation (replaces NexusFSMountsMixin, Issue #1387)
    # These @rpc_expose methods are discovered by the FastAPI server.
    # -------------------------------------------------------------------------

    @cached_property
    def _mount_core_service(self) -> Any:
        """Get or create MountCoreService."""
        from nexus.services.mount_core_service import MountCoreService

        return MountCoreService(self._gateway)

    @cached_property
    def _sync_service(self) -> Any:
        """Get or create SyncService."""
        from nexus.services.sync_service import SyncService

        return SyncService(self._gateway)

    @cached_property
    def _sync_job_service(self) -> Any:
        """Get or create SyncJobService."""
        from nexus.services.sync_job_service import SyncJobService

        return SyncJobService(self._gateway, self._sync_service)

    @cached_property
    def _mount_persist_service(self) -> Any:
        """Get or create MountPersistService."""
        from nexus.services.mount_persist_service import MountPersistService

        return MountPersistService(
            mount_manager=getattr(self, "mount_manager", None),
            mount_service=self._mount_core_service,
            sync_service=self._sync_service,
        )

    @rpc_expose(description="Add dynamic backend mount")
    def add_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        readonly: bool = False,
        context: OperationContext | None = None,
    ) -> str:
        """Add a dynamic backend mount to the filesystem."""
        return self._mount_core_service.add_mount(
            mount_point=mount_point,
            backend_type=backend_type,
            backend_config=backend_config,
            priority=priority,
            readonly=readonly,
            context=context,
        )

    @rpc_expose(description="Remove backend mount")
    def remove_mount(
        self,
        mount_point: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Remove a backend mount from the filesystem."""
        return self._mount_core_service.remove_mount(
            mount_point=mount_point,
            context=context,
        )

    @rpc_expose(description="Delete connector completely (bundled operation)")
    def delete_connector(
        self,
        mount_point: str,
        revoke_oauth: bool = False,
        provider: str | None = None,
        user_email: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Delete a connector completely with bundled operations.

        Combines: deactivate, delete config, optional OAuth revocation, directory cleanup.
        """
        import logging

        _logger = logging.getLogger(__name__)
        result: dict[str, Any] = {
            "removed": False,
            "directory_deleted": False,
            "config_deleted": False,
            "oauth_revoked": False,
            "errors": [],
            "warnings": [],
        }

        # Step 1: Try to deactivate connector if active (non-fatal)
        try:
            remove_result = self._mount_core_service.remove_mount(mount_point, context)
            result["removed"] = remove_result.get("removed", False)
            result["directory_deleted"] = remove_result.get("removed", False)
            if remove_result.get("errors"):
                result["warnings"].extend(remove_result["errors"])
        except PermissionError:
            raise
        except Exception as e:
            result["warnings"].append(f"Failed to deactivate connector (continuing): {e}")

        # Step 2: Delete saved configuration (FATAL - must succeed)
        try:
            config_deleted = self._mount_persist_service.delete_saved_mount(mount_point)
            result["config_deleted"] = config_deleted
        except Exception as e:
            error_msg = f"Failed to delete connector configuration: {e}"
            result["errors"].append(error_msg)
            raise RuntimeError(error_msg) from e

        # Step 3: Optionally revoke OAuth credentials
        if revoke_oauth:
            if not provider or not user_email:
                result["warnings"].append(
                    "OAuth revocation requested but provider or user_email not provided"
                )
            else:
                try:
                    from nexus.core.context_utils import get_zone_id
                    from nexus.core.sync_bridge import run_sync

                    zone_id = get_zone_id(context)
                    token_manager = self._get_token_manager()  # type: ignore[attr-defined]
                    revoked = run_sync(
                        token_manager.revoke_credential(
                            provider=provider,
                            user_email=user_email,
                            zone_id=zone_id,
                        )
                    )
                    result["oauth_revoked"] = revoked
                except Exception as e:
                    result["warnings"].append(
                        f"Failed to revoke OAuth credentials (non-fatal): {e}"
                    )

        # Step 4: Delete mount point directory
        try:
            self.rmdir(mount_point, recursive=True, context=context)  # type: ignore[attr-defined]
            result["directory_deleted"] = True
            _logger.info(f"Deleted mount point directory: {mount_point}")
        except Exception as e:
            result["warnings"].append(f"Failed to delete mount point directory (non-fatal): {e}")
            _logger.warning(f"Failed to delete mount point directory {mount_point}: {e}")

        return result

    @rpc_expose(description="List available connector types")
    def list_connectors(self, category: str | None = None) -> list[dict[str, Any]]:
        """List all available connector types."""
        return self._mount_core_service.list_connectors(category)

    @rpc_expose(description="List all active mounts")
    def list_mounts(self, context: OperationContext | None = None) -> list[dict[str, Any]]:
        """List all active backend mounts."""
        return self._mount_core_service.list_mounts(context)

    @rpc_expose(description="Get mount details")
    def get_mount(
        self,
        mount_point: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any] | None:
        """Get details about a specific mount."""
        return self._mount_core_service.get_mount(mount_point, context)

    @rpc_expose(description="Check if mount exists")
    def has_mount(self, mount_point: str) -> bool:
        """Check if a mount exists."""
        return self._mount_core_service.has_mount(mount_point)

    @rpc_expose(description="Sync metadata from connector backend")
    def sync_mount(
        self,
        mount_point: str | None = None,
        path: str | None = None,
        recursive: bool = True,
        dry_run: bool = False,
        sync_content: bool = True,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        generate_embeddings: bool = False,
        context: OperationContext | None = None,
        progress_callback: Any = None,
        full_sync: bool = False,
    ) -> dict[str, Any]:
        """Sync metadata and content from connector backend(s)."""
        from nexus.services.sync_service import SyncContext

        ctx = SyncContext(
            mount_point=mount_point,
            path=path,
            recursive=recursive,
            dry_run=dry_run,
            sync_content=sync_content,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            generate_embeddings=generate_embeddings,
            context=context,
            progress_callback=progress_callback,
            full_sync=full_sync,
        )

        result = self._sync_service.sync_mount(ctx)
        return result.to_dict()

    @rpc_expose(description="Start async sync job for a mount")
    def sync_mount_async(
        self,
        mount_point: str,
        path: str | None = None,
        recursive: bool = True,
        dry_run: bool = False,
        sync_content: bool = True,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        generate_embeddings: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Start an async sync job for a mount."""
        if mount_point is None:
            raise ValueError("mount_point is required for async sync")

        user_id = None
        if context:
            user_id = getattr(context, "subject_id", None)

        params = {
            "path": path,
            "recursive": recursive,
            "dry_run": dry_run,
            "sync_content": sync_content,
            "include_patterns": include_patterns,
            "exclude_patterns": exclude_patterns,
            "generate_embeddings": generate_embeddings,
        }

        job_id = self._sync_job_service.create_job(mount_point, params, user_id)
        self._sync_job_service.start_job(job_id)

        return {
            "job_id": job_id,
            "status": "pending",
            "mount_point": mount_point,
        }

    @rpc_expose(description="Get sync job status and progress")
    def get_sync_job(self, job_id: str) -> dict[str, Any] | None:
        """Get sync job status."""
        return self._sync_job_service.get_job(job_id)

    @rpc_expose(description="Cancel a running sync job")
    def cancel_sync_job(self, job_id: str) -> dict[str, Any]:
        """Cancel a running sync job."""
        success = self._sync_job_service.cancel_job(job_id)

        if success:
            return {"success": True, "job_id": job_id, "message": "Cancellation requested"}

        job = self._sync_job_service.get_job(job_id)
        if not job:
            return {"success": False, "job_id": job_id, "message": "Job not found"}
        return {
            "success": False,
            "job_id": job_id,
            "message": f"Cannot cancel job with status: {job['status']}",
        }

    @rpc_expose(description="List sync jobs")
    def list_sync_jobs(
        self,
        mount_point: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List sync jobs with optional filters."""
        return self._sync_job_service.list_jobs(
            mount_point=mount_point,
            status=status,
            limit=limit,
        )

    @rpc_expose(description="Save mount configuration to database")
    def save_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        readonly: bool = False,
        owner_user_id: str | None = None,
        zone_id: str | None = None,
        description: str | None = None,
        context: OperationContext | None = None,
    ) -> str:
        """Save mount configuration to database."""
        return self._mount_persist_service.save_mount(
            mount_point=mount_point,
            backend_type=backend_type,
            backend_config=backend_config,
            priority=priority,
            readonly=readonly,
            owner_user_id=owner_user_id,
            zone_id=zone_id,
            description=description,
            context=context,
        )

    @rpc_expose(description="List saved mount configurations")
    def list_saved_mounts(
        self,
        owner_user_id: str | None = None,
        zone_id: str | None = None,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List saved mount configurations."""
        return self._mount_persist_service.list_saved_mounts(
            owner_user_id=owner_user_id,
            zone_id=zone_id,
            context=context,
        )

    @rpc_expose(description="Load and activate saved mount")
    def load_mount(self, mount_point: str) -> str:
        """Load saved mount configuration and activate it."""
        return self._mount_persist_service.load_mount(mount_point)

    @rpc_expose(description="Delete saved mount configuration")
    def delete_saved_mount(self, mount_point: str) -> bool:
        """Delete saved mount configuration."""
        return self._mount_persist_service.delete_saved_mount(mount_point)

    def load_all_saved_mounts(self, auto_sync: bool = False) -> dict[str, Any]:
        """Load all saved mount configurations."""
        return self._mount_persist_service.load_all_mounts(auto_sync)

    def _matches_patterns(
        self,
        file_path: str,
        include_patterns: list[str] | None,
        exclude_patterns: list[str] | None,
    ) -> bool:
        """Check if file path matches include/exclude patterns (backward compat)."""
        from nexus.services.sync_service import SyncContext

        ctx = SyncContext(
            mount_point=None,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )
        return self._sync_service._matches_patterns(file_path, ctx)

    def _grant_mount_owner_permission(
        self,
        mount_point: str,
        context: OperationContext | None,
    ) -> None:
        """Grant direct_owner permission to mount creator (backward compat)."""
        self._mount_core_service._grant_owner_permission(mount_point, context)

    # =========================================================================
    # SearchService Delegation Methods (list / glob / grep)
    # =========================================================================

    @rpc_expose(description="List files in directory")
    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        prefix: str | None = None,
        show_parsed: bool = True,
        context: Any = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> list[str] | list[dict[str, Any]] | Any:
        """List files in a directory - delegates to SearchService."""
        return self.search_service.list(
            path=path,
            recursive=recursive,
            details=details,
            prefix=prefix,
            show_parsed=show_parsed,
            context=context,
            limit=limit,
            cursor=cursor,
        )

    @rpc_expose(description="Find files by glob pattern")
    def glob(self, pattern: str, path: str = "/", context: Any = None) -> builtins.list[str]:
        """Find files matching a glob pattern - delegates to SearchService."""
        return self.search_service.glob(pattern=pattern, path=path, context=context)

    @rpc_expose(description="Execute multiple glob patterns in single call")
    def glob_batch(
        self, patterns: builtins.list[str], path: str = "/", context: Any = None
    ) -> dict[str, builtins.list[str]]:
        """Execute multiple glob patterns in a single call - delegates to SearchService."""
        return self.search_service.glob_batch(patterns=patterns, path=path, context=context)

    @rpc_expose(description="Search file contents")
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
        """Search file contents using regex patterns - delegates to SearchService."""
        return self.search_service.grep(
            pattern=pattern,
            path=path,
            file_pattern=file_pattern,
            ignore_case=ignore_case,
            max_results=max_results,
            search_mode=search_mode,
            context=context,
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

    # Non-prefixed aliases (backward compat — mixin used these names)
    semantic_search = asemantic_search
    semantic_search_index = asemantic_search_index
    semantic_search_stats = asemantic_search_stats
    initialize_semantic_search = ainitialize_semantic_search

    # =========================================================================
    # ShareLinkService Delegation Methods (6 methods)
    # Replaces NexusFSShareLinksMixin (Issue #1387)
    # =========================================================================

    @rpc_expose(description="Create a share link for a file or directory")
    async def create_share_link(
        self,
        path: str,
        permission_level: str = "viewer",
        expires_in_hours: int | None = None,
        max_access_count: int | None = None,
        password: str | None = None,
        context: OperationContext | None = None,
    ) -> Any:
        """Create a shareable link for a file or directory."""
        return await self.share_link_service.create_share_link(
            path=path,
            permission_level=permission_level,
            expires_in_hours=expires_in_hours,
            max_access_count=max_access_count,
            password=password,
            context=context,
        )

    @rpc_expose(description="Get details of a share link")
    async def get_share_link(
        self,
        link_id: str,
        context: OperationContext | None = None,
    ) -> Any:
        """Get details of a share link."""
        return await self.share_link_service.get_share_link(
            link_id=link_id,
            context=context,
        )

    @rpc_expose(description="List share links created by the current user")
    async def list_share_links(
        self,
        path: str | None = None,
        include_revoked: bool = False,
        include_expired: bool = False,
        context: OperationContext | None = None,
    ) -> Any:
        """List share links created by the current user."""
        return await self.share_link_service.list_share_links(
            path=path,
            include_revoked=include_revoked,
            include_expired=include_expired,
            context=context,
        )

    @rpc_expose(description="Revoke a share link")
    async def revoke_share_link(
        self,
        link_id: str,
        context: OperationContext | None = None,
    ) -> Any:
        """Revoke a share link, immediately disabling access."""
        return await self.share_link_service.revoke_share_link(
            link_id=link_id,
            context=context,
        )

    @rpc_expose(description="Access a shared resource via share link")
    async def access_share_link(
        self,
        link_id: str,
        password: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        context: OperationContext | None = None,
    ) -> Any:
        """Validate and access a shared resource via share link."""
        return await self.share_link_service.access_share_link(
            link_id=link_id,
            password=password,
            ip_address=ip_address,
            user_agent=user_agent,
            context=context,
        )

    @rpc_expose(description="Get access logs for a share link")
    async def get_share_link_access_logs(
        self,
        link_id: str,
        limit: int = 100,
        context: OperationContext | None = None,
    ) -> Any:
        """Get access logs for a share link."""
        return await self.share_link_service.get_share_link_access_logs(
            link_id=link_id,
            limit=limit,
            context=context,
        )

    # =========================================================================
    # TaskQueueService Delegation Methods (5 methods)
    # Replaces NexusFSTasksMixin (Issue #1387)
    # =========================================================================

    @cached_property
    def task_queue_service(self) -> Any:
        """Get or create TaskQueueService."""
        from nexus.services.task_queue_service import TaskQueueService

        db_path = self._resolve_tasks_db_path()
        return TaskQueueService(db_path=db_path)

    def _resolve_tasks_db_path(self) -> str:
        """Resolve the path for the tasks fjall database.

        Priority:
        1. NEXUS_TASKS_DB_PATH environment variable
        2. NEXUS_DATA_DIR/tasks-db
        3. backend.root_path/../tasks-db (alongside backend storage)
        4. .nexus-data/tasks-db (fallback)
        """
        import os

        env_path = os.environ.get("NEXUS_TASKS_DB_PATH")
        if env_path:
            return env_path

        data_dir = os.environ.get("NEXUS_DATA_DIR")
        if data_dir:
            return os.path.join(data_dir, "tasks-db")

        backend = getattr(self, "backend", None)
        if backend is not None:
            root_path = getattr(backend, "root_path", None)
            if root_path is not None:
                return os.path.join(str(root_path), "tasks-db")

        return os.path.join(".nexus-data", "tasks-db")

    @rpc_expose(description="Submit a task to the durable task queue")
    def submit_task(
        self,
        task_type: str,
        params_json: str = "{}",
        priority: int = 2,
        max_retries: int = 3,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Submit a task to the durable task queue."""
        return self.task_queue_service.submit_task(
            task_type=task_type,
            params_json=params_json,
            priority=priority,
            max_retries=max_retries,
        )

    @rpc_expose(description="Get task status and result")
    def get_task(
        self,
        task_id: int,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        """Get task status, progress, and result."""
        return self.task_queue_service.get_task(task_id)

    @rpc_expose(description="Cancel a pending or running task")
    def cancel_task(
        self,
        task_id: int,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Cancel a pending or running task."""
        return self.task_queue_service.cancel_task(task_id)

    @rpc_expose(description="List tasks with optional filters")
    def list_queue_tasks(
        self,
        task_type: str | None = None,
        status: int | None = None,
        limit: int = 50,
        offset: int = 0,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> list[dict[str, Any]]:  # type: ignore[valid-type]
        """List tasks with optional filters."""
        return self.task_queue_service.list_tasks(
            task_type=task_type,
            status=status,
            limit=limit,
            offset=offset,
        )

    @rpc_expose(description="Get task queue statistics")
    def get_task_stats(
        self,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Get task queue statistics."""
        return self.task_queue_service.get_task_stats()

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
            import contextlib

            with contextlib.suppress(Exception):
                self._memory_api.session.close()

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
            import contextlib

            for mount in self.router.list_mounts():
                with contextlib.suppress(Exception):
                    if hasattr(mount.backend, "token_manager"):
                        mount.backend.token_manager.close()
