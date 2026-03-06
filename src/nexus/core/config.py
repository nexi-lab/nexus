"""Configuration dataclasses for NexusFS kernel.

Issue #1287: Extract NexusFS Domain Services from God Object.
Issue #1391: Builder pattern — frozen config dataclasses as SSOT for defaults.
Issue #2034: Slim KernelServices — 3-tier split (Kernel / System / Brick).

These frozen dataclasses group related constructor parameters so that
the kernel receives a single config object instead of 50 keyword args.
Defaults live here (SSOT) — no duplication across NexusFS, factory, connect().

Note: ``CacheConfig`` configures the kernel's **in-memory LRU caches**
(path/list/kv/exists). This is distinct from the ``CacheStore`` pillar
(Dragonfly/ephemeral KV+PubSub) which is a separate storage medium.

Service container hierarchy (matches NEXUS-LEGO-ARCHITECTURE §2):

    KernelServices  — Tier 0: boot-fatal, always present
    SystemServices  — Tier 1: degraded-mode on failure, always started
    BrickServices   — Tier 2: optional, silent on failure, hot-swappable
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import DEFAULT_NATS_URL

if TYPE_CHECKING:
    from nexus.bricks.workflows.protocol import WorkflowProtocol
    from nexus.contracts.protocols.namespace_manager import NamespaceManagerProtocol
    from nexus.contracts.write_observer import WriteObserverProtocol
    from nexus.core.protocols.entity_registry import EntityRegistryProtocol
    from nexus.core.protocols.permission_enforcer import PermissionEnforcerProtocol
    from nexus.core.protocols.rebac_manager import ReBACManagerProtocol
    from nexus.core.protocols.workspace_manager import WorkspaceManagerProtocol

# ---------------------------------------------------------------------------
# Config dataclasses (frozen — immutable, use dataclasses.replace() to copy)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheConfig:
    """In-memory LRU cache and content cache configuration.

    Configures sizes for the kernel's internal path/list/kv/exists caches
    and the optional content cache for faster reads.
    NOT related to the CacheStore pillar (Dragonfly/ephemeral KV+PubSub).
    """

    path_size: int = 512
    list_size: int = 1024
    kv_size: int = 256
    exists_size: int = 1024
    ttl_seconds: int | None = 300
    enable_content_cache: bool = True
    content_cache_size_mb: int = 256


@dataclass(frozen=True)
class PermissionConfig:
    """Permission enforcement configuration.

    Controls ReBAC permission checks, zone isolation,
    Tiger Cache, and deferred permission batching.
    """

    enforce: bool = True
    inherit: bool = True
    allow_admin_bypass: bool = False
    enforce_zone_isolation: bool = True
    audit_strict_mode: bool = True
    enable_tiger_cache: bool = True
    enable_deferred: bool = True
    deferred_flush_interval: float = 0.05


@dataclass(frozen=True)
class DistributedConfig:
    """Distributed coordination configuration.

    Controls event bus, lock manager, and coordination URL for
    multi-node deployments.
    """

    coordination_url: str | None = None
    enable_events: bool = True
    enable_locks: bool = True
    enable_workflows: bool = True
    event_bus_backend: str = "redis"
    nats_url: str = DEFAULT_NATS_URL


@dataclass(frozen=True)
class MemoryConfig:
    """MemGPT 3-tier memory paging configuration (Issue #1258)."""

    enable_paging: bool = True
    main_capacity: int = 100
    recall_max_age_hours: float = 24.0


@dataclass(frozen=True)
class ParseConfig:
    """File parsing configuration.

    Controls auto-parse on write and provider configurations for
    document parsing (unstructured, llamaparse, markitdown).
    """

    auto_parse: bool = True
    providers: tuple[dict[str, Any], ...] | None = None


@dataclass(frozen=True)
class IPCConfig:
    """Inter-Process Communication (IPC) configuration (Issue #2037).

    Controls message delivery limits, concurrency, delivery modes,
    and deduplication for the filesystem-as-IPC subsystem.
    """

    # Message limits
    max_inbox_size: int = 1000
    max_payload_bytes: int = 1_048_576  # 1 MB
    max_cold_concurrency: int = 100
    max_handler_concurrency: int = 50

    # Delivery mode: cold_only | hot_cold | hot_only
    delivery_mode: str = "cold_only"

    # Deduplication TTL (seconds)
    dedup_ttl_seconds: int = 3600

    # Retry configuration
    max_retries: int = 3
    retry_delays: tuple[float, ...] = (1.0, 2.0, 4.0)


# ---------------------------------------------------------------------------
# KernelServices — Tier 0: Storage Pillar validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KernelServices:
    """Tier 0 (KERNEL) — Storage Pillar handles only.

    Per NEXUS-LEGO-ARCHITECTURE §2 and Liedtke's microkernel test, only
    VFS routing and Metastore belong in the kernel.  Both are injected as
    constructor arguments; this container simply carries the router handle.

    All former kernel services (ReBAC, permissions, workspace, write-sync)
    have been moved to ``SystemServices`` (Issue #2193) where they are
    classified as *critical* (BootError on failure) or *degradable*
    (WARNING + None on failure).

    Frozen — all wiring must happen at construction time in factory.py.
    Use ``dataclasses.replace()`` to create modified copies if needed.
    """

    # VFS routing — the only kernel-level mechanism
    router: Any = None


# ---------------------------------------------------------------------------
# SystemServices — Tier 1: degraded-mode on failure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SystemServices:
    """Tier 1 (SYSTEM) — critical + degradable services.

    Contains two severity classes (Issue #2193):

    **Critical** (BootError on failure):
        rebac_manager, audit_store, entity_registry, permission_enforcer,
        write_observer — the "Trusted Computing Base outside the kernel"
        per microkernel terminology (seL4/MINIX 3 pattern).

    **Degradable** (WARNING + None on failure):
        dir_visibility_cache, hierarchy_manager, deferred_permission_buffer,
        workspace_registry, mount_manager, workspace_manager, and all
        remaining system services (agent registry, namespace, observability,
        resiliency, lifecycle management).

    Created by ``nexus.factory._boot_system_services()``.

    Issue #2034: Extracted from the monolithic KernelServices.
    Issue #2193: Absorbed former Tier 0 services per Liedtke's test.
    """

    # =================================================================
    # Former-kernel CRITICAL services (BootError on failure)
    # =================================================================

    # ReBAC permission subsystem — critical (Issue #2133: typed with Protocols)
    rebac_manager: ReBACManagerProtocol | None = None
    audit_store: Any = None
    entity_registry: EntityRegistryProtocol | None = None
    permission_enforcer: PermissionEnforcerProtocol | None = None

    # Write sync — critical
    write_observer: WriteObserverProtocol | None = None

    # =================================================================
    # Former-kernel DEGRADABLE services (WARNING + None on failure)
    # =================================================================

    # ReBAC caching / hierarchy — degradable
    dir_visibility_cache: Any = None
    hierarchy_manager: Any = None
    deferred_permission_buffer: Any = None

    # Workspace subsystem — degradable
    workspace_registry: Any = None
    mount_manager: Any = None
    workspace_manager: WorkspaceManagerProtocol | None = None

    # =================================================================
    # Original system services (all degradable)
    # =================================================================

    # Agent identity (Issue #1502)
    agent_registry: Any = None
    async_agent_registry: Any = None

    # Namespace visibility (Issue #1502)
    namespace_manager: NamespaceManagerProtocol | None = None
    async_namespace_manager: Any = None

    # Workspace branching (Issue #1315)
    context_branch_service: Any = None

    # Brick lifecycle (Issue #1704)
    brick_lifecycle_manager: Any = None

    # Event delivery (Issue #1241)
    delivery_worker: Any = None

    # Query observability (Issue #1301)
    observability_subsystem: Any = None

    # Resiliency policies (Issue #1366)
    resiliency_manager: Any = None

    # Agent eviction under resource pressure (Issue #2170)
    eviction_manager: Any = None

    # Brick reconciler — drift detection and self-healing (Issue #2060)
    brick_reconciler: Any = None

    # Zone lifecycle — ordered zone deprovisioning (Issue #2061)
    zone_lifecycle: Any = None

    # DT_PIPE manager — VFS named-pipe IPC (Issue #809)
    pipe_manager: Any = None

    # Scheduler — task scheduling service (Issue #2195, #2360)
    scheduler_service: Any = None

    # Agent Runtime — process lifecycle + tool dispatch (Issue #2761)
    process_manager: Any = None
    tool_dispatcher: Any = None


# ---------------------------------------------------------------------------
# BrickServices — Tier 2: optional, silent on failure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrickServices:
    """Tier 2 (BRICK) — optional, removable, silent on failure.

    Contains feature bricks that can fail independently without affecting
    the kernel or system services. The system continues without them.

    Created by ``nexus.factory._boot_brick_services()``.

    Issue #2034: Extracted from the monolithic KernelServices.
    """

    # Infrastructure bricks
    event_bus: Any = None
    lock_manager: Any = None
    workflow_engine: WorkflowProtocol | None = None
    rebac_circuit_breaker: Any = None

    # Feature bricks
    wallet_provisioner: Any = None  # PAY brick
    chunked_upload_service: Any = None  # UPLOADS brick
    manifest_resolver: Any = None  # MANIFEST brick
    tool_namespace_middleware: Any = None  # MCP brick
    api_key_creator: Any = None  # AUTH brick (Issue #1519, 3A)
    snapshot_service: Any = None  # SNAPSHOT brick (Issue #1752)
    # --- Cache Brick (Issue #1524) ---
    cache_brick: Any = None  # CacheBrick — owns all cache domain services

    # --- IPC Brick (Issue #1727, LEGO §8) ---
    ipc_storage_driver: Any = None  # KernelVFSAdapter (async bridge to NexusFS)
    ipc_provisioner: Any = None  # AgentProvisioner

    # --- Sandbox Brick (Issue #1307) ---
    agent_event_log: Any = None  # AgentEventLog (sandbox lifecycle audit)

    # --- Delegation & Reputation Bricks (Issue #2131) ---
    delegation_service: Any = None  # DELEGATION brick
    reputation_service: Any = None  # REPUTATION brick

    # --- Version Brick (Issue #2034: moved from KernelServices) ---
    version_service: Any = None  # VersionService (file history, rollback, diff)

    # --- Memory Brick (Issue #2177) ---
    memory_permission: Any = None  # MemoryPermissionProtocol adapter

    # --- Search Brick (Issue #810) ---
    zoekt_pipe_consumer: Any = None  # DT_PIPE consumer for Zoekt index notifications

    # --- Factory-created bricks (Issue #2134: moved from NexusFS flat params) ---
    parse_fn: Any = None  # Callable for parsing files (ParsersBrick)
    content_cache: Any = None  # ContentCache instance
    parser_registry: Any = None  # ParserRegistry (file format detection)
    provider_registry: Any = None  # ProviderRegistry (parsing providers)
    # NOTE: VFSLockManager is kernel-internal (created in NexusFS.__init__),
    # not injected via BrickServices. See write-path-extraction-design.md.

    # --- Governance Brick (Issue #2129) ---
    governance_anomaly_service: Any = None
    governance_collusion_service: Any = None
    governance_graph_service: Any = None
    governance_response_service: Any = None


# ---------------------------------------------------------------------------
# WiredServices — Tier 2b: services needing NexusFS reference
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WiredServices:
    """Tier 2b (WIRED) — services requiring NexusFS reference.

    Created by ``nexus.factory._wired._boot_wired_services()`` and bound
    to NexusFS via ``_bind_wired_services()``.

    Issue #2133: Replaces ``dict[str, Any]`` return type in wiring layer.
    """

    rebac_service: Any = None
    mount_service: Any = None
    gateway: Any = None
    mount_core_service: Any = None
    sync_service: Any = None
    sync_job_service: Any = None
    mount_persist_service: Any = None
    mcp_service: Any = None
    llm_service: Any = None
    oauth_service: Any = None
    search_service: Any = None
    share_link_service: Any = None
    events_service: Any = None
    # Versioning services (Issue #882: session-managed facades)
    time_travel_service: Any = None
    operations_service: Any = None

    # RPC services (Issue #2133: migrated from service_wiring.py)
    workspace_rpc_service: Any = None
    agent_rpc_service: Any = None
    user_provisioning_service: Any = None
    sandbox_rpc_service: Any = None
    metadata_export_service: Any = None
    descendant_checker: Any = None
    memory_provider: Any = None


# ---------------------------------------------------------------------------
# Observability (unchanged from before)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservabilityConfig:
    """Query observability configuration (Issue #1301)."""

    slow_query_threshold_ms: float = 500.0
    enable_query_logging: bool = True
    enable_pool_metrics: bool = True
    log_query_parameters: bool = False  # security: off by default
    max_query_length: int = 1000  # truncate long statements
    max_listener_errors: int = 10  # auto-disable threshold
