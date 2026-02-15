"""Configuration dataclasses for NexusFS kernel.

Issue #1287: Extract NexusFS Domain Services from God Object.
Issue #1391: Builder pattern — frozen config dataclasses as SSOT for defaults.

These frozen dataclasses group related constructor parameters so that
the kernel receives a single config object instead of 50 keyword args.
Defaults live here (SSOT) — no duplication across NexusFS, factory, connect().

Note: ``CacheConfig`` configures the kernel's **in-memory LRU caches**
(path/list/kv/exists). This is distinct from the ``CacheStore`` pillar
(Dragonfly/ephemeral KV+PubSub) which is a separate storage medium.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    nats_url: str = "nats://localhost:4222"


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
# KernelServices — mutable container for injected service dependencies
# ---------------------------------------------------------------------------


@dataclass
class KernelServices:
    """Injected service dependencies for NexusFS kernel.

    All default to None = service not available. Created by
    ``nexus.factory.create_nexus_services()`` and bundled here
    for clean injection into the kernel constructor.

    NOT frozen — services are wired post-construction by factory.
    """

    # Permission services
    router: Any = None
    rebac_manager: Any = None
    dir_visibility_cache: Any = None
    audit_store: Any = None
    entity_registry: Any = None
    permission_enforcer: Any = None
    hierarchy_manager: Any = None
    deferred_permission_buffer: Any = None

    # Workspace services
    workspace_registry: Any = None
    mount_manager: Any = None
    workspace_manager: Any = None

    # Sync/versioning
    write_observer: Any = None
    version_service: Any = None
    overlay_resolver: Any = None
    wallet_provisioner: Any = None

    # Infrastructure (moved from _service_extras dict)
    event_bus: Any = None
    lock_manager: Any = None
    workflow_engine: Any = None

    # Server-layer extras (moved from _service_extras dict)
    observability_subsystem: Any = None
    chunked_upload_service: Any = None
    manifest_resolver: Any = None
    manifest_metrics: Any = None
    rebac_circuit_breaker: Any = None
    tool_namespace_middleware: Any = None
    resiliency_manager: Any = None
    delivery_worker: Any = None


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


# ---------------------------------------------------------------------------
# Backward-compatibility aliases (Issue #1391)
# Old names still importable; will be removed in a future release.
# ---------------------------------------------------------------------------

LRUCacheConfig = CacheConfig
"""Deprecated: Use ``CacheConfig`` instead."""

SecurityConfig = PermissionConfig
"""Deprecated: Use ``PermissionConfig`` instead.

Note: Field names changed:
- ``enforce_permissions`` → ``enforce``
- ``inherit_permissions`` → ``inherit``
"""


class FeatureFlags:
    """Deprecated: Fields distributed into PermissionConfig, DistributedConfig, MemoryConfig."""

    def __init__(self, **_kw: Any) -> None:  # noqa: ANN401
        pass
