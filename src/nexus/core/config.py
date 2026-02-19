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

from nexus.constants import DEFAULT_NATS_URL

if TYPE_CHECKING:
    from nexus.bricks.workflows.protocol import WorkflowProtocol
    from nexus.contracts.write_observer import WriteObserverProtocol
    from nexus.core.cache_invalidation import CacheInvalidationObserver
    from nexus.services.protocols.namespace_manager import NamespaceManagerProtocol

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

    enable_metadata_cache: bool = True
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

    Controls ReBAC permission checks, zone isolation, audit logging,
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


# ---------------------------------------------------------------------------
# KernelServices — Tier 0: boot-fatal kernel mechanisms
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KernelServices:
    """Tier 0 (KERNEL) — mandatory services that are fatal on failure.

    Contains only kernel-level mechanisms: VFS routing, permissions,
    workspace management, sync/versioning, and cache invalidation.

    Created by ``nexus.factory._boot_kernel_services()`` and injected
    into the NexusFS kernel constructor.

    Frozen — all wiring must happen at construction time in factory.py.
    Use ``dataclasses.replace()`` to create modified copies if needed.

    Issue #2034: Slimmed from ~40 fields to 15 kernel-only fields.
    System and brick services moved to SystemServices / BrickServices.
    """

    # VFS routing
    router: Any = None

    # ReBAC permission subsystem
    rebac_manager: Any = None
    dir_visibility_cache: Any = None
    audit_store: Any = None
    entity_registry: Any = None
    permission_enforcer: Any = None
    hierarchy_manager: Any = None
    deferred_permission_buffer: Any = None

    # Workspace subsystem
    workspace_registry: Any = None
    mount_manager: Any = None
    workspace_manager: Any = None

    # Sync / versioning
    write_observer: WriteObserverProtocol | None = None
    version_service: Any = None
    overlay_resolver: Any = None

    # Cache invalidation (Issue #1169 / #1519)
    cache_observer: CacheInvalidationObserver | None = None


# ---------------------------------------------------------------------------
# SystemServices — Tier 1: degraded-mode on failure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SystemServices:
    """Tier 1 (SYSTEM) — critical, always-started, degraded-mode on failure.

    Contains services that the kernel needs for agent identity, namespace
    visibility, event delivery, observability, and lifecycle management.
    System fails gracefully if these are unavailable (logs WARNING, sets None).

    Created by ``nexus.factory._boot_system_services()``.

    Issue #2034: Extracted from the monolithic KernelServices.
    """

    # Agent identity (Issue #1502)
    agent_registry: Any = None
    async_agent_registry: Any = None

    # Namespace visibility (Issue #1502)
    namespace_manager: NamespaceManagerProtocol | None = None
    async_namespace_manager: Any = None

    # Workspace branching (Issue #1315)
    context_branch_service: Any = None

    # Hook engine chain (Issue #1257)
    scoped_hook_engine: Any = None

    # Brick lifecycle (Issue #1704)
    brick_lifecycle_manager: Any = None

    # Event delivery (Issue #1241)
    delivery_worker: Any = None

    # Query observability (Issue #1301)
    observability_subsystem: Any = None

    # Resiliency policies (Issue #1366)
    resiliency_manager: Any = None


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
    task_queue_service: Any = None  # TASK_QUEUE brick (Issue #655)

    # --- IPC Brick (Issue #1727, LEGO §8) ---
    ipc_storage_driver: Any = None  # IPCStorageDriver (RecordStore or VFS)
    ipc_vfs_driver: Any = None  # IPCVFSDriver (Backend mounted at /agents)
    ipc_provisioner: Any = None  # AgentProvisioner

    # --- Skills Brick (Issue #2035) ---
    skill_service: Any = None  # SkillService (protocol-based)
    skill_package_service: Any = None  # SkillPackageService


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
