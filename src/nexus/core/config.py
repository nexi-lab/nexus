"""Configuration dataclasses for NexusFS kernel.

Issue #1287: Extract NexusFS Domain Services from God Object.

These frozen dataclasses group related constructor parameters so that
the kernel receives a single config object instead of 5-10 keyword args.

Note: ``LRUCacheConfig`` configures the kernel's **in-memory LRU caches**
(path/list/kv/exists). This is distinct from the ``CacheStore`` pillar
(Dragonfly/ephemeral KV+PubSub) which is a separate storage medium.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LRUCacheConfig:
    """In-memory LRU cache configuration for NexusFS kernel.

    Configures sizes for the kernel's internal path/list/kv/exists caches.
    NOT related to the CacheStore pillar (Dragonfly/ephemeral KV+PubSub).
    """

    path_size: int = 512
    list_size: int = 1024
    kv_size: int = 256
    exists_size: int = 1024
    ttl_seconds: int | None = 300
    content_cache_size_mb: int = 256


@dataclass(frozen=True)
class SecurityConfig:
    """Security-related configuration for NexusFS kernel."""

    enforce_permissions: bool = True
    inherit_permissions: bool = True
    allow_admin_bypass: bool = False
    enforce_zone_isolation: bool = True
    audit_strict_mode: bool = True


@dataclass(frozen=True)
class FeatureFlags:
    """Feature flags for NexusFS kernel."""

    enable_workflows: bool = True
    enable_tiger_cache: bool = True
    enable_deferred_permissions: bool = True
    enable_distributed_events: bool = True
    enable_distributed_locks: bool = True
    enable_memory_paging: bool = True


@dataclass(frozen=True)
class ObservabilityConfig:
    """Query observability configuration (Issue #1301)."""

    slow_query_threshold_ms: float = 500.0
    enable_query_logging: bool = True
    enable_pool_metrics: bool = True
    log_query_parameters: bool = False  # security: off by default
    max_query_length: int = 1000  # truncate long statements
    max_listener_errors: int = 10  # auto-disable threshold
