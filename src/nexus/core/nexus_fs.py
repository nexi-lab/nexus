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
        from nexus.services.tiger_cache_manager import TigerCacheManager

        self._tiger_cache_manager = TigerCacheManager(
            rebac_manager=self._rebac_manager,
            metadata_store=self.metadata,
            default_zone_id=self._default_context.zone_id or "root",
            process_queue_fn=getattr(self, "process_tiger_cache_queue", None),
            warm_cache_fn=getattr(self, "warm_tiger_cache", None),
        )
        self._tiger_cache_manager.initialize()

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

        # ReBACService: Permission and access control operations
        # Must be created before AgentRPCService and UserProvisioningService
        # which depend on rebac_service sync methods for DI.
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
            rebac_create_fn=self.rebac_service.rebac_create_sync,
            rebac_list_tuples_fn=self.rebac_service.rebac_list_tuples_sync,
            rebac_delete_fn=self.rebac_service.rebac_delete_sync,
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
            rebac_create_fn=self.rebac_service.rebac_create_sync,
            rebac_delete_fn=self.rebac_service.rebac_delete_sync,
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

        # DescendantAccessChecker: extracted from NexusFS (Issue #2033)
        from nexus.services.descendant_access import DescendantAccessChecker

        self._descendant_checker = DescendantAccessChecker(
            rebac_manager=self._rebac_manager,
            rebac_service=self.rebac_service,
            dir_visibility_cache=self._dir_visibility_cache,
            permission_enforcer=self._permission_enforcer,
            metadata_store=self.metadata,
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

    # =========================================================================
    # Service Forwarding (Issue #2033: Facade Removal)
    # =========================================================================
    # Methods below were removed from NexusFS and forwarded to services.
    # Callers continue using nx.method_name() — __getattr__ routes to
    # the correct service transparently.

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

    # =========================================================================
    # Thin forwarders for abstract base class methods (kernel operations)
    # =========================================================================
    # list/glob/grep are @abstractmethod on NexusFilesystem so they need
    # real method definitions. __getattr__ alone doesn't satisfy ABCMeta.

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

    async def aget_version(self, path: str, version: int, context: OperationContext | None = None) -> bytes:
        """Delegate to VersionService."""
        return await self.version_service.get_version(path, version, context)

    @rpc_expose(description="Get specific file version")
    def get_version(self, path: str, version: int, context: OperationContext | None = None) -> bytes:
        """Get a specific version of a file. Delegates to VersionService."""
        return cast(bytes, NexusFS._run_async(self.aget_version(path, version, context)))

    async def alist_versions(self, path: str, context: OperationContext | None = None) -> list[dict[str, Any]]:
        """Delegate to VersionService."""
        return await self.version_service.list_versions(path, context)

    @rpc_expose(description="List file versions")
    def list_versions(self, path: str, context: OperationContext | None = None) -> list[dict[str, Any]]:
        """List all versions of a file. Delegates to VersionService."""
        return cast(list[dict[str, Any]], NexusFS._run_async(self.alist_versions(path, context)))

    async def arollback(self, path: str, version: int, context: OperationContext | None = None) -> None:
        """Delegate to VersionService."""
        return await self.version_service.rollback(path, version, context)

    @rpc_expose(description="Rollback file to previous version")
    def rollback(self, path: str, version: int, context: OperationContext | None = None) -> None:
        """Rollback file to a previous version. Delegates to VersionService."""
        cast(None, NexusFS._run_async(self.arollback(path, version, context)))

    async def adiff_versions(self, path: str, v1: int, v2: int, mode: str = "metadata", context: OperationContext | None = None) -> dict[str, Any] | str:
        """Delegate to VersionService."""
        return await self.version_service.diff_versions(path, v1, v2, mode, context)

    @rpc_expose(description="Compare file versions")
    def diff_versions(self, path: str, v1: int, v2: int, mode: str = "metadata", context: OperationContext | None = None) -> dict[str, Any] | str:
        """Compare two versions of a file. Delegates to VersionService."""
        return cast(dict[str, Any] | str, NexusFS._run_async(self.adiff_versions(path, v1, v2, mode, context)))

    # Async ReBAC methods (arebac_*) removed — served via register_service(rebac_service).

    # -------------------------------------------------------------------------
    # Context Extraction (shared with nexus_fs_core.py)
    # -------------------------------------------------------------------------

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

    # MCP/Skill/LLM/OAuth facades removed —
    # served via register_service(mcp_service, skill_service, llm_service, oauth_service).

    # MountService async delegation removed —
    # served via register_service(mount_service).

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
    def cancel_sync_job(self, job_id: str) -> dict[str, Any]:
        """Cancel a running sync job."""
        success = self._sync_job_service.cancel_job(job_id)
        if success:
            return {"success": True, "job_id": job_id, "message": "Cancellation requested"}
        job = self._sync_job_service.get_job(job_id)
        if not job:
            return {"success": False, "job_id": job_id, "message": "Job not found"}
        return {"success": False, "job_id": job_id, "message": f"Cannot cancel job with status: {job['status']}"}
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
