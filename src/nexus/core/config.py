"""Configuration dataclasses for NexusFS kernel.

Issue #1287: Extract NexusFS Domain Services from God Object.
Issue #1391: Builder pattern — frozen config dataclasses as SSOT for defaults.
Issue #2034: Unified services dict (formerly 3-tier split).

These frozen dataclasses group related constructor parameters so that
the kernel receives a single config object instead of 50 keyword args.
Defaults live here (SSOT) — no duplication across NexusFS, factory, connect().

Note: ``CacheConfig`` configures the kernel's **in-memory LRU caches**
(path/list/kv/exists). This is distinct from the ``CacheStore`` pillar
(Dragonfly/ephemeral KV+PubSub) which is a separate storage medium.

Service container hierarchy (matches NEXUS-LEGO-ARCHITECTURE §2):

    ServiceRegistry — Tier 1+2: all services accessed via nx.service("name")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nexus.contracts.constants import DEFAULT_NATS_URL

# ---------------------------------------------------------------------------
# Config dataclasses (frozen — immutable, use dataclasses.replace() to copy)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheConfig:
    """In-memory LRU cache configuration.

    Configures sizes for the kernel's internal path/list/kv/exists caches.
    NOT related to the CacheStore pillar (Dragonfly/ephemeral KV+PubSub).
    """

    path_size: int = 512
    list_size: int = 1024
    kv_size: int = 256
    exists_size: int = 1024
    ttl_seconds: int | None = 300


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
    document parsing (unstructured, llamaparse, pdf-inspector).
    """

    auto_parse: bool = True
    providers: tuple[dict[str, Any], ...] | None = None


@dataclass(frozen=True)
class TieringConfig:
    """Volume-level cold tiering configuration (Issue #3406).

    Controls automatic upload of sealed CAS volumes to cloud storage
    (S3/GCS) and range-read retrieval for tiered content.
    """

    enabled: bool = False
    quiet_period_seconds: float = 3600.0  # 1 hour
    min_volume_size_bytes: int = 100 * 1024 * 1024  # 100 MB
    cloud_backend: str = "gcs"  # "gcs" or "s3"
    cloud_bucket: str = ""
    upload_rate_limit_bytes: int = 25 * 1024 * 1024  # 25 MB/s
    sweep_interval_seconds: float = 60.0  # how often to check for tierable volumes
    # LRU local cache for recently-accessed tiered volumes
    local_cache_size_bytes: int = 10 * 1024 * 1024 * 1024  # 10 GB
    # Burst re-download: reads within window triggers full volume cache
    burst_read_threshold: int = 5  # reads within burst_window to trigger
    burst_read_window_seconds: float = 60.0  # time window for burst detection


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
